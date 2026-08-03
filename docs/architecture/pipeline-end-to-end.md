# The FDI Pipeline, End to End — Annual Report PDF → Extracted Facts

**Written:** 2026-08-03 · **Branch:** `fix/document-level-basis-scoping`
**Scope:** what actually *runs* today, step by step, from a PDF upload to a
field-extraction record. Everything below was read out of the code, not the
specs; where a spec describes something not yet wired, it is called out in
[§12](#12-what-is-live-vs-what-is-designed-but-not-wired).

---

## 0. The two halves, in one picture

The system is two distinct machines joined by an object store and a Postgres
registry:

```
                          ┌─────────────────────────────────────────────┐
  PDF ──POST /documents──▶│ INTAKE  checksum · dedup · version · store   │
                          └───────────────────┬─────────────────────────┘
                                              │ POST /documents/{id}/ocr
                                              ▼
   ╔══════════════════════ ASYNC INGESTION DAG (Celery, 17 stages) ═════════════╗
   ║  ocr → normalize → ast → {graph → provenance → basis} → chunks            ║
   ║        structure ↗            semantic ↗                → embedding→index ║
   ║  (+ side branches: linking, ontology, quality, knowledge, graph_load)     ║
   ╚═══════════════════════════════════════════╤═══════════════════════════════╝
                                               │ artifacts in MinIO,
                                               │ run rows in Postgres
                                               ▼
                          ┌─────────────────────────────────────────────┐
   answer ◀─POST /extract/document─┤ SYNCHRONOUS EXTRACTION              │
                          │ schema → units → retrieval → LLM → records  │
                          └─────────────────────────────────────────────┘
```

**Half 1 (ingestion)** is a content-addressed, cached, fan-out DAG driven by
Celery. It is *document-shaped*: it knows nothing about the field schema.

**Half 2 (extraction)** is a single synchronous HTTP request. It is
*schema-shaped*: it reads the field-extraction schema, groups fields into
Retrieval Units, and runs one retrieval + one LLM call per unit.

The seam between them is deliberate. Ingestion is expensive and cached
forever; extraction is cheap to re-run against a new schema.

---

## 1. Topology and layering

### 1.1 Runtime services (`docker/compose.yml`, project name `fdi`)

| Service | Image | Role |
|---|---|---|
| `postgres` | postgres:18.4 | Document registry, `stage_run` rows, usage events |
| `minio` | MinIO | Artifact store — source PDFs, every stage artifact, extraction records, the editable schema |
| `rabbitmq` | 4.3.2 | Celery broker |
| `redis` | 8.8.0 | Celery result backend |
| `milvus` (+`etcd`) | v2.6.20 | Vector + BM25 index over chunks |
| `neo4j` | 5.26.28 LTS | Graph projection (optional; `graph_load` skips cleanly if absent) |
| `api` | backend image | FastAPI, port 8000 |
| `worker` | backend image | Celery prefork worker |
| `migrate` | backend image | `alembic upgrade head`, runs to completion first |
| `frontend` | Next.js | profile `ui`, port 3000 |

External (not in Compose): **Azure Document Intelligence** (OCR) and **Azure
OpenAI** (`gpt-4.1` deployment, used for both section classification and field
extraction). Embeddings are **local** — BGE-small via fastembed, 384-dim, CPU.

### 1.2 Package layering (enforced by import-linter)

```
fdi.core      pure algorithms — no I/O, no config, no clock/RNG, never raises
fdi.pipeline  orchestration — may import core + platform
fdi.platform  I/O adapters — Azure, MinIO, Postgres, Milvus, Neo4j, fastembed
fdi.api       FastAPI routes      ─┐ siblings; neither may import the other
fdi.workers   Celery tasks        ─┘ (one exemption: api/app.py → workers.tasks)
```

Two conventions run through the whole codebase and explain a lot of the code
you will read:

* **`fdi.core` functions are total.** They never raise on any input shape.
  Malformed input degrades field-by-field (`page → None`, `basis → "unknown"`).
* **"Decision 10 — never raise."** Every I/O step in extraction is individually
  guarded, so one unit's failure degrades *that unit's* fields to well-formed
  null records instead of failing the document.

---

## 2. The stage framework (before any actual stage)

Every ingestion step implements one three-line protocol
([`pipeline/stage.py`](../../backend/src/fdi/pipeline/stage.py)):

```python
class Stage(Protocol):
    name: str
    version: int
    deps: tuple[str, ...]
    async def run(self, ctx: StageContext) -> object: ...
```

A stage is a **near-pure async transform**. It may read upstream artifacts
(`ctx.read("ocr")`), read the source PDF (`ctx.read_source()`), and use injected
services (`ctx.services["ocr"]`). It owns no persistence, no hashing, no
scheduling — all of that is
[`pipeline/run.py::run_stage`](../../backend/src/fdi/pipeline/run.py).

### 2.1 What `run_stage` does, in order

1. **Compute `input_hash`.**
   * Root stage (`deps == ()`): `sha256(document.checksum)`.
   * Derived stage: `sha256("\n".join(dep output_hashes))`.
   * *Multi-dep race guard:* a stage with N deps gets dispatched once per
     finishing predecessor. If not every dep is recorded yet, it returns `[]`
     and does nothing — the dep that finishes **last** re-dispatches it and
     that call proceeds. This is by design, not a bug.
2. **Derive the artifact key:**
   `artifacts/{doc_id}/{stage}/v{version}/{input_hash}.json`
3. **Record a `stage_run` row** (status `running`).
4. **Cache check.** If that key already exists in MinIO → record `finish_run`
   and return successors **without executing the body**. This is why re-running
   ingestion is nearly free, and why an LLM/embedding stage never re-bills on a
   second run with identical inputs.
5. **Cache miss** → `stage.run(ctx)` → `validate_finite(body)` (rejects NaN/Inf,
   which are not valid JSON) → `store.put_json(key, body)`.
6. **Usage accounting.** OCR records one `ocr_usage_event` with the page count;
   any LLM calls the stage reported to `ctx.services["usage"]` become
   `llm_usage_event` rows. Flushed on **both** the success and failure paths —
   a stage that made 3 calls before erroring on the 4th still billed for 3.
7. **Return `resolver.successors(name)`**; the Celery task enqueues each.

### 2.2 Versioning is the re-run mechanism

Because the key embeds `v{version}`, **bumping `Stage.version` invalidates the
cache for every document that ever ran** — that is how the codebase does
backfills. `IndexStage` v2→v3 is the canonical example: it re-indexed every
document to add `text`/`doc_id` to Milvus metadata *without* re-embedding
anything, because `embedding` artifacts stayed cached.

### 2.3 Staleness and replay

* `GET /docs/{doc_id}/stale` — `DagResolver.stale()` compares recorded run facts
  against current stage versions and expected input hashes. Pure; reads no blobs.
* `POST /docs/{doc_id}/reprocess?from=<stage>` — dispatches exactly that stage;
  successors chain themselves.

### 2.4 Celery discipline (`workers/tasks.py`)

Five rules, each closing a measured failure:

1. The task is `def`, never `async def` (Celery reports SUCCESS on an un-awaited
   coroutine).
2. The correlation ID is bound in the **sync wrapper**, before `asyncio.run()`
   (contextvars are copy-in / no-copy-out).
3. The bind is **unconditional**, the clear is in `finally` — prefork reuses a
   process for up to 50 tasks and resets nothing, so a job without an ID
   inherits the previous job's.
4. The DB engine is created **inside** the coroutine, disposed in `finally` —
   a pool built at import is inherited across `fork()` and corrupts silently.
5. The failure handler never masks the cause.

---

## 3. Step 1 — Intake (`POST /documents`)

**Route:** [`api/routes.py:347`](../../backend/src/fdi/api/routes.py#L347) →
[`pipeline/intake.py`](../../backend/src/fdi/pipeline/intake.py)

```
multipart file + optional doc_key
  ├─ reject non-application/pdf → 415
  ├─ reject > FDI_MAX_UPLOAD_MB (100 MB) → 413
  ├─ checksum = sha256(bytes)
  ├─ dedup: existing row with same checksum? → return it, no re-store
  ├─ store bytes at  documents/{checksum}.pdf   (content-addressed)
  ├─ version = next_version(doc_key)
  └─ INSERT document row → 201 DocumentView
```

**`doc_key` vs `doc_id`.** `doc_id` is a UUID for one uploaded file.
`doc_key` is the stable business identity (e.g. `"TCS-FY2025-26"`), and
`version` increments per re-upload under the same key. Extraction addresses
documents by `doc_key` and always takes the **latest** version.

**Race handling:** two concurrent identical uploads can both miss the dedup
read and collide on `UNIQUE(checksum)`. The loser catches `IntegrityError`,
re-reads, and returns the winner's row rather than failing the request.

**Intake does not start the pipeline.** You must explicitly
`POST /documents/{doc_id}/ocr`. Everything downstream chains itself from there.

---

## 4. Step 2 — `ocr` (root stage, v5)

**Service:** Azure Document Intelligence, model `prebuilt-layout`, sync SDK call
run via `asyncio.to_thread`, polling timeout 300 s.
**Input:** the source PDF bytes. **Output:** `normalize_ocr(raw AnalyzeResult)`.

The artifact is a *stable subset* of Azure's `AnalyzeResult` — schema_version 5:

| Key | Contents | Who consumes it |
|---|---|---|
| `pages` | page list (also the OCR billing unit) | costing |
| `paragraphs` | `content`, `role`, `bounding_regions`, `spans{offset,length}` | `normalize`, `structure`, `provenance`, note index |
| `tables` | `cells[{row_index, column_index, content, elements, bounding_regions}]`, `caption`, `footnotes`, `spans` | `structure`, `statements`, note index |
| `sections` | Azure's own `elements` hierarchy | `ast.attach_table_refs` |
| `figures` | caption + regions | `linking` |
| `selection_marks`, low-confidence words | | diagnostics |

**Why spans matter:** `spans{offset,length}` is a character offset into the
document's flattened text. It is the *only* way to separate two notes printed on
the same page (TCS p301 holds note 13(a) at offset 897,095 and 13(b) at 897,486).
Page granularity is provably too coarse.

**Version rule:** bump `OcrStage.version` when `FDI_AZURE_DI_MODEL`, the API
version, or `normalize_ocr`'s schema changes — those define the output.

---

## 5. Steps 3–5 — Document structure

### 5.1 `structure` (v4) — deps `(ocr,)`

Two jobs:

1. **Drop chrome.** Paragraphs whose canonical role is `HEADER`, `FOOTER`, or
   `PAGE_NUMBER` are removed.
2. **Stitch cross-page tables** (`core/structure.py::merge_cross_page_tables`) —
   Microsoft's vertical-merge heuristic. A structural guard on column headers
   blocks the known false-merge shape (the "Note 2 Fixed Assets" case). Merged
   tables carry forward `caption`/`footnotes` and record `source_indices`.
3. **Canonicalize every grid** (`core/table_grid.py::canonicalize_table`):
   expand spanning cells into a full rectangle, and resolve nested/multi-row
   column headers into ONE joined-path convention.

Output: `{schema_version: 3, blocks: [...], tables: [{... , grid: {n_rows,
n_cols, header_row_count, column_headers, grid, cell_regions, cell_pages}}]}`.

The `grid` is the single most important downstream artifact — it is what gets
rendered into the LLM prompt at extraction time (§9.8).

### 5.2 `normalize` (v3) — deps `(ocr,)`

Maps OCR paragraphs 1:1 into the unified schema. Two things it does beyond a
rename:

* Paragraphs that are actually **table cells** (reachable from
  `tables[].cells[].elements`) are re-roled `TABLE_CELL` rather than keeping
  their Azure role — otherwise a cell masquerades as a heading.
* Every block carries `source_index` — its position in `ocr["paragraphs"]`.
  This index is what makes provenance an O(1) lookup instead of a text match.

### 5.3 `ast` (v4) — deps `(normalize, ocr)`

`build_ast` turns the flat block list into `{title, sections: [{heading,
paragraphs, *_source_index(es)}]}`. Table-cell blocks are excluded from section
paragraphs; non-first `title`-role blocks become sections instead of being
discarded. `attach_table_refs` then adds `table_refs: [int]` and `figure_refs`
to each section, derived from Azure's own `sections[].elements` hierarchy.

**The `s{i}` identity is born here.** Section *i* in the AST is node `s{i}` in
the graph, chunk `s{i}` in retrieval, and `{doc_id}:s{i}` in Milvus and Neo4j.
One identity across four stores.

---

## 6. Steps 6–8 — Geometry and basis

### 6.1 `graph` (v3) — deps `(ast, structure)`

Builds a typed node/edge graph: `CONTAINS` (doc→section→paragraph) and
`READING_ORDER` (sibling chain). v3 additionally emits `table`/`figure` nodes
with `CONTAINS` edges from their owning section. Every node carries
`source_index`.

### 6.2 `provenance` (v2) — deps `(graph, ocr)`

For every graph node, resolve the page + bounding polygon of the OCR paragraph
it came from — via **direct index lookup** `ocr["paragraphs"][node.source_index]`,
never a label match. (v1 matched on label text and was ambiguous; the version
bump forced every cached artifact to recompute.)

Output `node_provenance: [{node_id, page, bounding_regions}]`. This is the audit
trail: it is what lets an extracted number be highlighted on the page it came
from.

The same module also carries the **read side** used at extraction time:

* `resolve_chunk_regions(chunk_id)` — geometry behind one chunk;
* `resolve_evidence_regions(evidence_ids)` — union over a field's citations;
* `resolve_field_regions(keywords, …)` — **cell-precise** geometry, split into
  `{current: [...], prior: [...]}` when the field's keywords match a table row.

### 6.3 `basis` (v1) — deps `(ast, provenance)`

**The problem it solves:** an Indian company with subsidiaries files **two
near-duplicate statement sets** — Standalone (parent only) and Consolidated
(group) — with different numbers for the same line items. Mixing them produces
a balance sheet that does not balance. Measured on RIL FY2025-26: total
non-current assets read from p58 and total current liabilities from p100 — two
different statement sets, out by 998,799.

**Algorithm** (`core/basis.py::detect_basis_regions`):

1. Walk `ast["sections"]` in order. Classify each heading against
   regulator-mandated, company-invariant SA-700 / Schedule III captions:
   `"consolidated balance sheet"`, `"independent auditor's report on the
   consolidated financial statements"`, and the `"standalone"` counterparts.
   Anchors are **full captions, never a bare "consolidated"** — the bare word
   also appears inside narrative in either section.
2. Resolve each anchor's page via `provenance` (`s{i}` → page). An anchor with
   no resolvable page is skipped.
3. Each anchor opens a region running to the next anchor of a **different**
   basis (exclusive), or the document's last page.
4. **One-basis document** → a single region spanning the document's full
   min/max page, so front matter is covered too.
5. **No anchors** → `regions: []`, `default_basis: "unknown"`.

Output: `{regions: [{start_page, end_page, basis}], default_basis}`.
Verified on the real TCS report: `[standalone 93–143, consolidated 144–185]`.

Read helpers used downstream: `page_basis(artifact, page)`,
`basis_regions()`, `resolve_effective_basis()` (§9.5),
`basis_is_ambiguous()`.

---

## 7. Steps 9–11 — Semantics and the retrieval index

### 7.1 `semantic` (v1) — deps `(ast,)` — **LLM**

One Azure OpenAI call **per AST section**, classifying it into
`BALANCE_SHEET | INCOME_STATEMENT | CASH_FLOW | NOTES | MDNA | AUDITORS_REPORT |
DIRECTORS_REPORT | OTHER`.

Each call is **individually guarded**. Azure OpenAI's content filter rejects
ordinary annual-report paragraphs (POSH / safety-incident / geopolitical-risk
prose are common false positives) with a 400. Failing the whole stage — and so
every downstream stage — over one flagged paragraph on a 1000-section filing is
disproportionate, so a failure degrades *that section* to `OTHER` and logs.

Cached on `ast`'s output hash: a second run is a store cache hit that does not
call Azure again, and therefore does not bill again.

### 7.2 `chunks` (v4) — deps `(ast, semantic, provenance, structure, basis)`

A pure five-artifact join producing the retrieval unit of work. Two kinds of
chunk:

**Section chunks `s{i}`** — `heading + "\n" + paragraphs`, tagged with
`section_type` (from `semantic`, positionally aligned) and `page` (from
`provenance`).

**Table cards `t{i}`** — ONE card per canonicalized table. A card is a
**LOCATION summary, not the data**:

```
[Note 8 Property, plant and equipment | BS | basis=standalone | note 8 | Rs Crore | page 210]
Columns: As at March 31, 2026 | As at March 31, 2025
Rows: Land; Buildings; Plant and equipment; Computer equipment; ...
```

Three design decisions embedded there, each measured:

* **Card, not rows.** Phase 3a emitted one chunk per table *row* (`t{i}r{j}`).
  That flooded the reranker with same-scored fragments. Replacing ~1985 row
  fragments with ~767 cards cut embedding time from ~18 min to 210 s and *held*
  recall, because the values are fetched at prompt time (§9.8) rather than
  indexed.
* **Row labels come from the DESCRIPTIVE column, not column 0.** Indian
  statement tables lead with a roman-numeral index column (`I.`, `VIII`,
  `XIII.`). Reading labels from column 0 produced semantically empty cards that
  never retrieved. `_label_column_index` picks the column with the most
  *descriptive* cells (>3 chars, containing a space or a lowercase letter — a
  test that deliberately fails multi-char roman numerals). **This one fix lifted
  accuracy 156 → 173/188** and, as a side effect, fixed most of what had been
  diagnosed as "L3 wrong-row/sign" extraction errors — they were a retrieval bug.
* **`basis=` is baked into the card text** via `page_basis`, so no Milvus schema
  migration was needed to make basis searchable.

Finally a **global exact-text dedup** keeps the first occurrence; ids are never
renumbered, so gaps in `s{i}` are expected and contractual.

### 7.3 `embedding` (v2) — deps `(chunks,)`

Embeds every chunk's `text` with **BGE-small** (`BAAI/bge-small-en-v1.5`,
384-dim, local ONNX/CPU, batch 32). The model is constructed **lazily** on first
`embed()` — otherwise every API lifespan and every worker task would load ~130 MB
of weights even when embedding nothing.

Content-addressed on `chunks`' output hash: a re-run against the same chunks is
a cache hit that does not call the client (proven with a call-counting fake).

### 7.4 `index` (v3) — deps `(embedding, chunks)` — **terminal stage**

Upserts into Milvus, one row per chunk, id `{doc_id}:{chunk_id}`.

Collection schema v2 carries **both** channels:

| Field | Purpose |
|---|---|
| dense `vector` (384) | BGE similarity |
| sparse vector via **BM25 `Function`** over an analyzed `text` field | keyword |
| scalars: `text`, `doc_id`, `chunk_id`, `heading`, `section_type`, `page` | filter + citation |

Two constraints worth knowing: the BM25 Function and the analyzer **must** be
declared at collection creation (Milvus cannot add either to a live collection —
hence the versioned collection name), and `output_fields` must be listed
explicitly because Milvus refuses to output a `SPARSE_FLOAT_VECTOR` under `"*"`.

`doc_id` is written as a filterable scalar because a Stage has no `doc_key`/
`version` — the retrieval layer translates `doc_key/version → doc_ids` through
Postgres before building the Milvus filter.

The artifact is a small receipt (`{upserted: N}`); the write is the point. If
Milvus is unconfigured the stage returns `{upserted: 0, skipped: "vector_store
not configured"}` rather than raising — **an unconfigured optional backend is a
reportable outcome, not a KeyError.**

`index` reaching `done` is the definition of "this document is ready", and
`POST /extract/document` returns **409** until it does.

### 7.5 Side branches (built, but nothing downstream reads them)

| Stage | v | Deps | Output |
|---|---|---|---|
| `linking` | 1 | `ocr` | figures with captions, footnotes |
| `ontology` | 2 | `ast, structure` | heading→CanonicalConcept mappings + concept values off table grids |
| `quality` | 3 | `ast, structure, ontology` | AST defects, table shape defects, BS/PL/CF reconciliation checks |
| `knowledge` | 2 | `semantic, ontology, quality` | one consolidated per-document record |
| `graph_load` | 2 | `graph` | Neo4j MERGE; ids namespaced `{doc_id}:{bare_id}` (v1 collided every document's `s0` onto the first) |

The design doc §6.3 proposes removing `linking`, `graph_load`, and the
`ontology → quality → knowledge` loop from the default DAG (verified by grep:
nothing reads them). **This has not been done** — they still run.

---

## 8. What ingestion leaves behind

For one document, after a full run:

```
MinIO   documents/{checksum}.pdf
        artifacts/{doc_id}/ocr/v5/{hash}.json
        artifacts/{doc_id}/structure/v4/{hash}.json
        …one per stage…
        artifacts/{doc_id}/index/v3/{hash}.json
        schema/field-extraction-schema.json          (only if PUT /schema was used)
        extractions/{doc_id}/v2/{schema_hash}.json   (written by extraction)

Postgres  document row (id, checksum, doc_key, version, filename, byte_size)
          stage_run rows (stage, version, input_hash, output_hash,
                          artifact_key, status, error, correlation_id)
          ocr_usage_event / llm_usage_event rows

Milvus    fdi_chunks_v2: one row per chunk, dense + BM25 sparse
Neo4j     namespaced nodes/edges (if configured)
```

Inspect any of it: `GET /documents/{doc_id}/stages`,
`GET /documents/{doc_id}/artifacts/{stage}`.

---

## 9. Extraction — `POST /extract/document`

**Route:** [`api/routes.py:1040`](../../backend/src/fdi/api/routes.py#L1040)
**Engine:** [`pipeline/extractor.py`](../../backend/src/fdi/pipeline/extractor.py)
+ [`core/extractor.py`](../../backend/src/fdi/core/extractor.py)

Request: `{doc_key, module?, force?}`. This runs **synchronously** — it is not a
Celery stage, has no `stage_run` row, and can take minutes.

### 9.1 Preconditions

1. `doc_key` → all versions → take the **latest**. 404 if none.
2. `latest_run(doc.id, "index").status == "done"`, else **409**. Without this
   check every unit's search legitimately returns nothing and extraction would
   emit 200 OK with a well-formed null for every field — indistinguishable from
   "this company reports nothing".
3. `extractor_client` configured (`FDI_GPT_ENDPOINT`/`FDI_GPT_KEY`), else 503.

### 9.2 Load the schema

`load_schema(store, seed_path)` is **read-through**:

* if `schema/field-extraction-schema.json` exists in MinIO → use it (source
  `"store"`);
* else read the on-disk seed (`FDI_EXTRACTION_SCHEMA_PATH`, default
  `docs/architecture/field-extraction-schema.json`) — source `"seed"`.

The on-disk file is an **immutable seed**: the Dockerfile COPYs it into the
image, so an in-container write vanishes on the next `up` or leaves each replica
with a private schema. `PUT /schema` writes the bucket copy and takes effect on
the very next extraction, no redeploy.

**Currently live:** the seed, **v3.2.0 — 15 modules, 17 Retrieval Units,
190 extractable fields.**
(The taxonomy-derived **v4.2.0** — 4 modules, 4 units, 402 fields, 368 with
`note_aliases` — is what the recent measurement runs used; it is loaded by
`PUT /schema`, not by default. See §12.)

### 9.3 Schema → `FieldSpec`s

`parse_field_specs(schema, module?)` flattens
`modules → retrieval_units → fields` (recursing into v4's **nested** group
nodes) into a flat `list[FieldSpec]`.

Rules applied while flattening:

* a node with a non-empty `fields` list is a **group** → recurse, emit nothing;
* `origin == "derived"` → skipped (it is computed, not extracted);
* `retrieval_query` is read off the **unit** (field-level fallback);
* `keywords` = the field's `aliases` **then** the unit's `locator_keywords`,
  order-preserving deduplicated — a duplicate would silently dilute
  `keyword_overlap`, which divides by `len(keywords)`;
* `basis_preference` is read off the **unit**, never the field (basis is a
  property of a whole statement, not a line), defaulting to `"standalone"`;
* `note_aliases` — report-side label variants inherited from the field's parent
  **concept**. A taxonomy leaf ("Buildings") almost never matches a report label
  ("Property, plant and equipment"); without this bridge only **4 of 402** v4.1
  leaves resolved a note at all.

### 9.4 Cache

`extractions/{doc_id}/v2/{sha256(field_paths)}.json`. Unless `force: true`, a
hit is returned verbatim with **zero retrieval and zero LLM calls**. Keyed on
the field schema, so re-extracting under a revised schema writes a *new* object
instead of overwriting — comparing the two is the point.

### 9.5 Read three artifacts ONCE, and resolve basis for the whole document

`provenance` (geometry), `structure` (geometry + table rendering), `basis`
(scoping) are read once per request — with 190–402 fields, a per-field read
would be hundreds of MinIO round trips.

Then `group_fields_by_unit(fields)` → `UnitSpec`s (order-preserving; a blank
`unit_id` becomes its own singleton), and:

```python
preferred        = units[0].basis_preference          # "standalone"
effective_basis  = resolve_effective_basis(basis_artifact, preferred)
```

**Basis is resolved once, per document — not per unit.** The per-unit fallback
("if filtering to the preference would empty this pool, keep it unfiltered") is
correct in isolation and wrong in aggregate: on a consolidated-only filing it
lets one unit filter to standalone while its neighbour falls back to
consolidated. That is exactly the RIL mixing described in §6.3. Resolution:
honour the preference when the document *has* that basis; otherwise adopt the
basis it does have (widest region wins).

### 9.6 Build the note map (per basis, once)

This is the deterministic half of retrieval, and the newest major addition.

**The insight:** a statutory balance sheet prints its own navigation.

```
Property, plant and equipment | 8(a) | 8,259
Cash and cash equivalents     | 6(c) | 1,833
```

That reference is a **fact, not a guess**. Similarity search cannot find the PPE
schedule from the word "Land" — the label is generic and the schedule sits ~30
pages away. Measured on TCS: ~50 taxonomy leaves were null purely because
retrieval never reached the note.

`_note_map_for(ctx, basis_artifact, preference)`:

1. Read the `ocr` artifact (paragraphs + tables).
2. `basis_bounds(basis, preference)` → the page span of that basis region.
   **Scoping is mandatory**: a report with both bases has two "note 8", and an
   unscoped index resolves a reference to whichever came first.
3. `find_statement_pages(paragraphs)` trims the tail — the **AGM notice** that
   follows the statements restarts its own `1)`, `4)`, `7)` numbering and would
   collide.
4. **Find face pages by table CONTENT, not by heading.** `_face_pages` scores
   every table against the line names Schedule III mandates
   (`STATEMENT_ANCHORS`: `total assets`, `revenue from operations`,
   `profit before tax`, …), requiring ≥3 distinct anchors. Heading matching
   returned 5 pages for TCS — three of them contents/segment pages with no face
   — and could not say *which* statement it had found. Content scoring returns
   exactly the consolidated and standalone faces, **typed**. Headings remain the
   fallback when nothing scores.
5. `build_note_map(paragraphs, tables, face_pages, page_lo, page_hi)`:
   * scan `sectionHeading` paragraphs → `8) Property, plant and equipment`
     (top level) and `(a) Investments` (sub-note, inheriting the open number);
   * read `label → ref` pairs off the face tables;
   * **the face table is authoritative for the number.** On TCS the `8)` heading
     was never emitted as a `sectionHeading`, so its sub-notes `(a)..(f)`
     inherited the previously-open note 7 (Leases) — PPE was filed as `7(a)` and
     a lookup of `8(a)` would have confidently returned the **wrong page**.
     When a ref does not resolve against the heading index, the face row's
     *label* is matched to a sub-note heading's *title* and rebound under the
     face's ref. This lifted resolvable references 75% → 95% and note recovery
     80% → 100%.

Result: `NoteMap{by_ref, by_label, unresolved}` with `pages_for_label(label) →
[pages]`.

### 9.7 Per unit: retrieve

For each `UnitSpec` (`extract_unit`):

```
top_k = max(20, min(60, n_fields*2 + 10))        # adaptive: big units get a bigger pool
pool  = hybrid_search(query=unit.retrieval_query, k=top_k*2,
                      filters={doc_key, version})
```

**`hybrid_search`** ([`pipeline/retrieval.py`](../../backend/src/fdi/pipeline/retrieval.py))
runs two channels over the same query and filters, each over-fetched 3×:

* **BM25** — `query` passed as **raw text**; Milvus tokenizes and scores
  server-side against the analyzed `text` field. (This replaced a Postgres
  `plainto_tsquery` channel that ANDed every lexeme and therefore returned
  **zero** hits for any sentence-shaped query.)
* **Dense** — embed the query with BGE, search Milvus, over-fetch 5× when a
  `section_type` filter is set (the store has no filter parameter for it).

Then **Reciprocal Rank Fusion**: `score = Σ 1/(60 + rank)` over both lists,
identified by `{doc_id}:{chunk_id}`, ties broken deterministically by that
string, richest citation kept. A chunk found by both channels routinely
outranks one found by only one.

*Measured:* dense-only answer-in-context recall 138/196 = 70.4%; hybrid
179/196 = **91.3%**. `RU_COMPANY` went 0/9 → 7/9.

The pool is over-fetched **relative to** `top_k`, not fixed — the filters run
*between* pooling and reranking, so a fixed 60 against an adaptive `top_k` of up
to 60 would leave big units with fewer post-filter candidates than `top_k`.

### 9.8 Per unit: filter, inject, rerank, render

**(a) `filter_candidates`** — drop whole-table chunks for units with no
numeric/list field, so short narrative paragraphs are not crowded out by
numeric schedules sharing a keyword. (`list` counts as table-needing: AOC-1
subsidiary disclosures *are* tables.)

**(b) `_filter_by_basis`** — drop candidates whose `page_basis` is the
**opposite** of the effective basis. `"unknown"` (front matter, unrecognized
tables) is always kept; `"both"` keeps everything.

> **Graceful fallback, with a recorded scar.** If filtering would drop *every*
> candidate, the unfiltered pool is kept. Refusing this fallback was measured on
> TCS: correct fields fell 19/24 → 9/24 and filled 125 → 71, taking every
> headline total to null. Extracting from the only basis present beats returning
> nothing. The comment in the code says the honest thing: *retrieval, not this
> filter, is the place to enforce basis.*

**(c) Note-follow injection** — **after** the filters, deliberately: these pages
were resolved from a printed reference, so no similarity or basis heuristic may
discard them.

```python
note_pages = ∪ over unit.fields of note_map.pages_for_label(field_name, *note_aliases)
injected   = chunks on those pages, score = 1000.0, source_tool = "note", capped at 12
scoped     = injected + scoped
```

Every alias contributes — a concept legitimately spans more than one note
(advances sit in "other assets", interest receivable in "other financial
assets"), so taking only the first match drops real evidence. The cap stops a
long schedule crowding out the search pool; the score of 1000 guarantees
reranking cannot drop a fetched note.

**(d) `rerank_candidates`** — model-free blend:
`score + 1.0 × keyword_overlap(text, keywords)`, stable-sorted descending,
truncated to `top_k`.

**(e) `_render_table_candidate`** — for each surviving **table card** `t{i}`,
fetch `structure["tables"][i]["grid"]`, run `repair_headers` (Azure DI collapses
real headers into `"Details, Details, Details"`), and replace the card's text
with a **GitHub-flavored markdown table** carrying the card's identity line.
Section chunks are untouched; any failure leaves the card's location-summary
text intact.

This is the step that turns a location summary back into data:

```
[Balance Sheet | basis=standalone | page 133]
| Particulars | As at March 31, 2026 | As at March 31, 2025 |
| --- | --- | --- |
| Total equity | 10901.47 | 9812.33 |
| Current tax  | 560.00   | (22.55)  |
```

### 9.9 Per unit: one LLM call

`build_unit_messages(unit, prompt_candidates)` — **"retrieve once, extract
many"**. One call covers every field in the unit; the response is a JSON object
**keyed by `field_path`** (globally unique, unlike `field_name`).

The system prompt is the shared rule block, and every clause in it closes a
measured miss:

| Rule | The failure it closes |
|---|---|
| Only the given context, never prior knowledge of a company | hallucinated well-known figures |
| First column = current year, second = prior-year comparative | reading the comparative as the answer |
| Prefer the PRIMARY statement over a note that restates it | duplicate figures disagreeing |
| Parentheses mean NEGATIVE — `(12.97)` is −12.97 | sign errors |
| Recognize label synonyms ("closing cash balance" = "Cash and cash equivalents") | vocabulary drift |
| Return the number exactly as printed; do not rescale | ×10 unit bugs |
| Indian digit grouping: `3,785` is 3785, `1,10,270` is 110270 | **the comma bug** — `8,685` was read as `8.685` → `8` |
| Not dual-period → return only the current year | prior-year leakage |

Azure OpenAI is called in **JSON mode**, `temperature=0`.

### 9.10 Assemble

`assemble_unit_results(unit, raw, ranked)` produces one output-contract record
per field, in unit order, looking each up by `field_path` then `field_name`:

```json
{ "field_name": "...", "tag": "...", "origin": "extracted",
  "value": <scalar | {current, prior} | null>,
  "source": {"page": int|null, "snippet": str|null, "evidence_ids": [...]},
  "confidence": 0.0-1.0,
  "answered": true|false }
```

Every missing/malformed key falls back to the top-ranked candidate, so a garbled
response still yields well-formed records.

**`answered` is a deliberate distinction.** A field the model returned as `null`
("this document does not report it") and a field it never returned a key for
("the batch dropped it") are different facts. Collapsing them hides our own
recall loss: a 76-field unit gets the same 60-candidate budget as a 25-field
one, so deep leaves fall out silently and afterwards read as though the company
simply had nothing to disclose. Coverage can now separate **absent** from
**missed**.

### 9.11 Restore order, attach geometry, persist

* Order is restored positionally via a per-identity queue (a plain `{id(f): i}`
  dict breaks when the same `FieldSpec` object appears twice).
* `_attach_regions` adds `source.bounding_regions = {current: [...], prior: [...]}`
  — cell-precise when the field's keywords match a table row, else the
  whole-table/whole-section fallback.
* `build_extraction_record` → `extractions/{doc_id}/v2/{schema_hash}.json`.
  A failed write is logged and the result is still returned: losing the durable
  copy is a degradation, not a reason to discard 200 fields already paid for.
* `await session.commit()` in the route — `record_llm_usage` only flushes.

### 9.12 The one non-obvious failure mode, recorded in the code

Usage bookkeeping has its own guard **and its own rollback**. A failed telemetry
insert poisons `ctx.session` — which `hybrid_search` shares. One failed insert
therefore silently killed retrieval for every **later** unit in the document:
TCS fell 125 → 71 filled and 19 → 9 correct, deterministically, early units fine
and the rest empty. Costing is bookkeeping; it must never take extraction down.

---

## 10. The audit layer — `POST /factsheet/document`

Runs the same extraction over the **whole** schema, then layers pure engines
(`core/factsheet.py::document_factsheet`):

1. **Derivation** — compute `do not tag` subtotals from extracted values via each
   subtotal's own formula, resolved to sibling tags by `sr_no`, run to a
   **fixpoint** so a subtotal built on other subtotals resolves once its inputs
   do. Confidence propagates as the MIN of inputs. A subtotal with unresolved or
   missing operands is emitted with `status: "unresolved"|"missing_inputs"` and
   **no value** — a bank product must never publish a guessed number.
2. **Ratios** — computed from the merged fact map.
3. **Validation** — three families, each fully traceable (rule, tags, expected vs
   actual, delta, tolerance, severity): *reconciliation* (reported total vs
   sum-of-parts), *identity* (Assets = Liabilities + Equity, seeded as `warn`),
   *domain* (a negative reported total wants a human).
4. **Confidence calibration** — `triangulate()` corroborates a tag read from both
   the face and its note (agreement boosts, disagreement lowers and flags —
   never silently picks a side); `calibrate()` folds validation outcomes into
   bands: high / needs_review / low / failed.
5. **Coverage** — against the fixed taxonomy tag list: found / derived /
   genuinely absent. The completeness assertion an auditor tests, inverted into
   something machine-checkable.
6. **`?format=xbrl`** → an XBRL instance document (`application/xml`);
   otherwise the JSON bundle plus `calculation_arcs`.

---

## 11. Running it end to end

```bash
# 0. stack
docker compose -f docker/compose.yml up -d          # add --profile ui for the frontend

# 1. ingest
curl -s -X POST localhost:8000/documents \
     -F file=@TCS-annual-report-FY2025-26.pdf \
     -F doc_key=TCS-FY2025-26
# -> {"id": "<doc_id>", "doc_key": "...", "version": 1, ...}

# 2. start the DAG (everything else chains itself)
curl -s -X POST localhost:8000/documents/<doc_id>/ocr

# 3. watch
curl -s localhost:8000/documents/<doc_id>/stages
#    wait for  index: done

# 4. extract
curl -s -X POST localhost:8000/extract/document \
     -H 'content-type: application/json' \
     -d '{"doc_key":"TCS-FY2025-26"}'

# optional: audit bundle / XBRL
curl -s -X POST 'localhost:8000/factsheet/document' -d '{"doc_key":"TCS-FY2025-26"}'
curl -s -X POST 'localhost:8000/factsheet/document?format=xbrl' -d '{"doc_key":"..."}'

# cost
curl -s localhost:8000/documents/<doc_id>/cost
```

Swap the schema without a redeploy: `PUT /schema` (validated by
`validate_extraction_schema` — the gate exists to reject a schema that parses to
**zero** fields, which would turn extraction into a silent 200-OK no-op),
then `POST /extract/document` with `force: true`.

**Rough cost/time for one ~300-page report:** OCR is one Azure DI call billed per
page; `semantic` is one LLM call per AST section (the dominant LLM cost, cached
forever); embedding ~210 s on CPU; extraction is one LLM call per Retrieval Unit
(17 units on v3.2, 4 on v4.2).

---

## 12. What is live vs what is designed but not wired

**Live and exercised end to end:**
all 17 DAG stages · hybrid (BM25 + dense) retrieval · basis regions ·
document-level basis resolution · table cards + markdown rendering ·
note-map construction and note-follow injection · content-anchored face-page
detection (`core/statements.py` via `_face_pages`) · unit-batched extraction ·
geometry attachment · extraction caching · costing · the factsheet/XBRL layer.

**In `fdi.core`, unit-tested, but NOT reachable from any route or stage yet:**

| Module | What it does | Status |
|---|---|---|
| `core/note_plan.py` | `plan_from_face` → `build_note_prompt` → `read_note_reply`: decide which notes to open (by reference, or by title when the face cites none) and what to ask each | imported only by tests |
| `core/compose.py` | Partial-sum composition with `coverage`/`complete`/`missing`, and reported-wins checks | imported only by tests |
| `core/numbers.py` | Deterministic `parse_amount` (the comma-bug fix) | used by `core/statements.py`; the extraction prompt still asks the model for numbers |
| `core/mapping.py`, `core/memory.py`, `core/concepts.py` | push-model label→concept mapper, mapping memory, bounded candidate sets | `concepts` used by `mapping`/`memory`/`note_plan`; none wired to a route |
| `NoteIndexStage`, `FaceIndexStage` (design §6.4) | cache the note/face index as inspectable artifacts | not implemented — both are rebuilt in memory per extraction request |

**Schema state.** The v4.3 per-statement files
(`field-extraction-schema.v4_3-{pl,assets,liabilities,equity,cashflow}.json`)
are the note-driven design's target shape. They carry no `extraction_prompt` and
no unit `retrieval_query`, so **`parse_field_specs` cannot consume them** — they
are not runnable through today's `/extract/document`. `core/schema.py::
validate_composition` is the machine-checked gate on them (`derivation.inputs ==
child concept tags`, one formula per concept, no duplicate tags).

**Known open issues:**

* `FDI_MILVUS_URI` is not set in `.env` and `Settings.milvus_uri` defaults to
  `None` → `index` returns `{skipped: "vector_store not configured"}` and
  extraction retrieval finds nothing. Set it (`http://milvus:19530`) for any
  real run. Same for `FDI_EXTRACTION_SCHEMA_PATH` if you want a non-default seed.
* `graph_load` fails or skips whenever Neo4j is down; nothing reads its output.
* Duplicate taxonomy tag `WCL` (working-capital loans at sr 1.2, Buyers Credit at
  sr 1.3) — whichever loads second is unreachable. Needs a taxonomy decision.
* `RIL` has no note column on its P&L face at all, so note-following cannot
  degrade gracefully there — it must fall back to face-only + section search.
  This is *absent ≠ missed*, and the strongest argument that note-following is
  an accelerator, never a prerequisite.

---

## 13. Measured numbers (for calibration)

| What | Number | Source |
|---|---|---|
| Answer-in-context recall, dense only | 138/196 = 70.4% | RT-9 A/B, real Milvus |
| Answer-in-context recall, hybrid | 179/196 = **91.3%** | same |
| Rendered-context recall (TR-1) | 173/188 = 92.0% | LLM-free render proof |
| Field accuracy after the L1 card-label fix | 156 → **173/188** | L1 measurement |
| Generalization | TCS 187/190, RIL 186/190 | basis-fallback run |
| TCS P&L via the ported note-driven core | recall 66.0% (31/47), precision 93.9% (31/33), 9 LLM calls | design §7.2 |
| Notes resolved | TCS 9/11, RITES 8/10, RIL 0 (no note column) | design §7.1 |
| Embedding time, row fragments → cards | ~18 min → 210 s | TR-1 verification |

---

## 14. Fast index — where to look

| Concern | File |
|---|---|
| Stage contract | [`pipeline/stage.py`](../../backend/src/fdi/pipeline/stage.py) |
| Caching, hashing, persistence, billing | [`pipeline/run.py`](../../backend/src/fdi/pipeline/run.py) |
| DAG topology / staleness | [`pipeline/resolver.py`](../../backend/src/fdi/pipeline/resolver.py) |
| Celery task rules | [`workers/tasks.py`](../../backend/src/fdi/workers/tasks.py) |
| Intake | [`pipeline/intake.py`](../../backend/src/fdi/pipeline/intake.py) |
| Stage bodies | [`pipeline/stages/`](../../backend/src/fdi/pipeline/stages/) |
| Chunking / RRF fusion | [`core/retrieval.py`](../../backend/src/fdi/core/retrieval.py) |
| Search channels | [`pipeline/retrieval.py`](../../backend/src/fdi/pipeline/retrieval.py) |
| Basis regions | [`core/basis.py`](../../backend/src/fdi/core/basis.py) |
| Note index | [`core/notes.py`](../../backend/src/fdi/core/notes.py) |
| Face detection / parsing | [`core/statements.py`](../../backend/src/fdi/core/statements.py) |
| Number parsing | [`core/numbers.py`](../../backend/src/fdi/core/numbers.py) |
| Table grid / markdown | [`core/table_grid.py`](../../backend/src/fdi/core/table_grid.py), [`core/table_render.py`](../../backend/src/fdi/core/table_render.py) |
| Prompts, rerank, assemble | [`core/extractor.py`](../../backend/src/fdi/core/extractor.py) |
| Extraction orchestration | [`pipeline/extractor.py`](../../backend/src/fdi/pipeline/extractor.py) |
| Schema validation | [`core/schema.py`](../../backend/src/fdi/core/schema.py) |
| Audit layer | [`core/factsheet.py`](../../backend/src/fdi/core/factsheet.py) |
| HTTP surface | [`api/routes.py`](../../backend/src/fdi/api/routes.py) |

**Related design docs:**
[note-driven extraction](../superpowers/specs/2026-08-01-note-driven-extraction-design.md) ·
[table representation (TR-1)](../superpowers/specs/2026-07-22-table-representation-design.md) ·
[ingestion audit](2026-07-19-ingestion-audit-and-improvement-plan.md) ·
[retrieval roadmap](retrieval-roadmap.md)
