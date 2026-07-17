# SP-4 — Document Intake

- **Status:** Implemented 2026-07-17 on `feature/sp-1-project-setup`. All §7 done criteria verified (unit + testcontainers + static gates; full suite 151 passed, 3 e2e skipped; ruff / mypy --strict / lint-imports 1 kept 0 broken).
- **Depends on:** SP-2 (document/stage_run registry, ArtifactStore), SP-3 (harness — unrelated but in the tree).
- **Roadmap:** phase 1 "Document Intake — upload · checksum · dedup · versioning · registry". Populates the `document.checksum` column SP-2 left nullable.

## 1. Purpose

Intake is the front door: a PDF is uploaded, content-addressed by checksum, deduplicated, versioned under a
logical document key, and registered — so every downstream stage (OCR at SP-5 onward) operates on a
registered, immutable source. This is buildable with no external service (checksum + object storage +
Postgres); Azure Document Intelligence (SP-5) later consumes the stored PDF.

Thin vertical slice: upload → checksum → dedup → version → register → fetch. No OCR, no async pipeline
kickoff yet (SP-5 wires OCR as the first real stage over an intake'd document).

## 2. Scope decisions (made autonomously; documented for review)

1. **Content-address the PDF by sha256.** Store raw bytes at `documents/{checksum}.pdf`. Identical bytes
   uploaded twice ⇒ same key ⇒ dedup falls out of the address, mirroring SP-2's artifact story.
2. **Dedup returns the existing document (idempotent upload).** Re-uploading the same bytes does NOT create
   a second `document` row or re-store the blob; it returns the existing record. Matched by `checksum`.
3. **Versioning is per logical `doc_key`.** A `doc_key` (caller-supplied; defaults to the uploaded filename)
   identifies a logical document across revisions. Uploading *different* bytes under the same `doc_key`
   creates `version = max(version for doc_key) + 1`. Same bytes under any doc_key ⇒ dedup wins (no new
   version). So `version` tracks content revisions of a logical document; `checksum` identifies exact bytes.
4. **`document` extends SP-2's table, it is not replaced.** SP-2 shipped `document(id, created_at, checksum?)`.
   SP-4 adds `filename`, `byte_size`, `doc_key`, `version`, `content_type`, and makes `checksum` NOT NULL for
   intake'd documents. The `/ping-job` walking skeleton created checksum-less documents; SP-4 keeps a
   nullable-checksum path working (ping is synthetic) OR migrates ping to a dedicated flow — decided in §3.4.
5. **Upload is multipart to the API.** `POST /documents` with a file part; the API streams to the object
   store. A size cap (`FDI_MAX_UPLOAD_MB`, default 50) rejects oversized files with 413. (The sibling
   `.env` exposes `MAX_UPLOAD_MB` — mirror it as `FDI_MAX_UPLOAD_MB`.)
6. **Determinism.** Checksum + content-addressed key are deterministic; ids, timestamps, and `version` live
   in Postgres. Re-intake of identical bytes is a no-op returning the same row.

## 3. Architecture

### 3.1 Layering

- **`fdi.core`** — `sha256_hex(data: bytes) -> str` (pure) if not already present; keep pure hashing here.
- **`fdi.platform.storage`** — `ArtifactStore.put_bytes(key, data, content_type)` + `get_bytes(key)` for raw
  (non-JSON) blobs; `document_key(checksum) -> "documents/{checksum}.pdf"` helper (may live in pipeline).
- **`fdi.platform.db`** — extend the `Document` model; add `DocumentRepository` methods: `get_by_checksum`,
  `next_version(doc_key)`, `create(...)`, `list_by_doc_key`, `get(id)`.
- **`fdi.pipeline.intake`** — `intake_document(*, pdf, filename, doc_key, content_type, session_factory, store) -> DocumentView`:
  checksum → dedup (return existing) → store bytes → create row with next version → return a view.
- **`fdi.api`** — `POST /documents` (multipart), `GET /documents/{id}`, `GET /documents?doc_key=` (versions).
- **`fdi.core` / schemas** — a `DocumentView` value/response (id, checksum, filename, byte_size, doc_key,
  version, content_type, created_at).

### 3.2 Registry (Postgres) — migration 0005

Extend `document`:
```
document(
  id, created_at,                      -- from SP-2
  checksum,                            -- SP-2 nullable; intake writes it
  filename        TEXT     NULL,       -- original upload name
  byte_size       INTEGER  NULL,
  content_type    STRING(128) NULL,    -- e.g. application/pdf
  doc_key         STRING(256) NULL,    -- logical identity for versioning
  version         INTEGER  NULL        -- 1-based per doc_key
)
CHECK: none new (values are open). A partial UNIQUE(checksum) is desirable for dedup integrity but
       Postgres UNIQUE treats NULLs as distinct, which keeps the synthetic checksum-less ping docs legal;
       add UNIQUE(checksum) (nullable) so two intake'd rows can never share bytes. Confirm ping's
       checksum-less rows do not violate it (NULLs are exempt).
```
All new columns NULLABLE so SP-2's ping-created rows and this migration coexist without a backfill.

### 3.3 Intake flow

```
intake_document(pdf, filename, doc_key, content_type, session_factory, store):
    checksum = sha256_hex(pdf)
    async with session_factory() as s:
        existing = await DocumentRepository(s).get_by_checksum(checksum)
        if existing: return view(existing)            # dedup: idempotent
    await store.put_bytes(document_key(checksum), pdf, content_type)   # content-addressed
    async with session_factory() as s:
        version = await DocumentRepository(s).next_version(doc_key)     # max+1, or 1
        doc = await DocumentRepository(s).create(id=uuid4, checksum=..., filename=..., byte_size=len(pdf),
                                                 content_type=..., doc_key=doc_key, version=version)
        await s.commit(); return view(doc)
```
Race note: two concurrent identical uploads could both miss `get_by_checksum` and race on the
`UNIQUE(checksum)` insert — the loser catches `IntegrityError` and re-reads the winner's row (documented,
handled in the repo/service). This is why UNIQUE(checksum) is load-bearing, not decorative.

### 3.4 Relationship to the SP-2 ping walking skeleton

`/ping-job` still creates a synthetic checksum-less `document` and runs the ping stage — unchanged. Intake
is a *separate* entry (`POST /documents`) producing a real, checksummed, versioned document. SP-5 will run
the OCR stage over an intake'd document (dispatch `run_stage(doc_id, "ocr")`). No teardown of ping here.

## 4. Error handling

- Oversized upload → 413 (`FDI_MAX_UPLOAD_MB`). Empty/again non-PDF content-type → 415/400 (accept
  `application/pdf`; a lenient path may accept any bytes but record the declared content_type — keep strict:
  require `application/pdf`, 415 otherwise).
- Unknown document id → 404.
- Concurrent identical upload race → resolved to the existing row (no 500), per §3.3.
- Storage failure after checksum but before the row commit → the blob may exist with no row; the next
  identical upload re-stores (idempotent put) and creates the row. No partial row is committed.

## 5. Testing

- **Unit** (no Docker): `sha256_hex` determinism; `document_key`; intake dedup logic with a fake store+repo
  (same bytes twice ⇒ one store write, one row); version increment per doc_key (pure-ish with a fake repo).
- **Integration** (testcontainers): `put_bytes`/`get_bytes` round-trip; `DocumentRepository` get_by_checksum /
  next_version / list_by_doc_key against real Postgres; the `UNIQUE(checksum)` constraint bites on a
  duplicate insert (IntegrityError); `intake_document` end to end (store + row + dedup returns same id;
  different bytes same doc_key ⇒ version 2).
- **API** (integration): `POST /documents` multipart → 201 with a DocumentView; re-upload same bytes ⇒ same id
  (dedup); oversized ⇒ 413; `GET /documents/{id}` ⇒ 200; `GET /documents?doc_key=` ⇒ versions listed.

## 6. Explicitly deferred (YAGNI)

- OCR / any pipeline stage over the document → SP-5 (Azure DI).
- Presigned direct-to-storage upload (bypassing the API) → when corpus-scale ingestion arrives.
- Rich document metadata (company, fiscal year, language) extraction → later layers infer it; intake stores
  only what the upload carries.
- Virus scanning / PDF validation beyond content-type → out of scope for the dev slice.

## 7. Done criteria

1. `POST /documents` (multipart PDF) stores the bytes content-addressed at `documents/{checksum}.pdf` and
   returns a DocumentView with checksum, filename, byte_size, doc_key, version=1.
2. Re-uploading identical bytes returns the SAME document id and does not create a second row (dedup).
3. Different bytes under the same `doc_key` create version 2; `GET /documents?doc_key=` lists both.
4. `UNIQUE(checksum)` is enforced (a duplicate insert raises IntegrityError; the service resolves the race
   to the existing row).
5. Oversized upload → 413; unknown id → 404.
6. `document.checksum`, `filename`, `byte_size`, `doc_key`, `version`, `content_type` are populated for
   intake'd documents; SP-2's synthetic ping documents still work (nullable columns).
7. All four static gates green; `core` stays pure (hashing only); full suite passes.
