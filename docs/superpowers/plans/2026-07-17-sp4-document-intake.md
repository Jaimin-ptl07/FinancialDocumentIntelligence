# SP-4 — Document Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Upload a PDF, content-address it by sha256, dedup, version per logical `doc_key`, register it — populating the `document.checksum` SP-2 left nullable.

**Architecture:** `POST /documents` (multipart) → `intake_document` computes the sha256, dedups on `checksum` (idempotent), stores raw bytes at `documents/{checksum}.pdf`, and creates a `document` row with `version = max(version for doc_key)+1`. Reuses SP-2's `ArtifactStore`, `document` table, and import contract.

**Tech Stack:** Python 3.13, FastAPI (multipart via `python-multipart`), SQLAlchemy 2 async + Alembic, MinIO, Postgres, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` pure (hashing only).
- Determinism: checksum + content-addressed key deterministic; ids/timestamps/version in Postgres. Re-intake of identical bytes is a no-op returning the same row.
- New nullable columns only (SP-2's synthetic ping documents must keep working with no backfill). `UNIQUE(checksum)` is load-bearing for dedup integrity; NULLs are exempt (ping rows stay legal).
- Any new `FDI_` Settings field MUST be added to BOTH `docker/compose.yml`'s `x-backend-env` allowlist AND `.env.example` (the SP-1 inert-config / empty-string-crash lesson).
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.
- Integration tests: reuse per-file `session_factory` (from `migrated_engine`) + `store` (from `minio_client`, `ensure_bucket()`); mint unique ids/checksums per test (session-scoped DB).

**Canonical commands:** fast `pytest -m "not integration and not e2e" -q`; integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `sha256_hex` (core, pure)

**Files:** Create `backend/src/fdi/core/hashing.py`; Test `backend/tests/unit/test_hashing.py`.

**Interfaces:** Produces `sha256_hex(data: bytes) -> str` — 64-char lowercase hex sha256.

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_hashing.py
from fdi.core.hashing import sha256_hex


def test_sha256_hex_is_deterministic_and_known() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(b"abc") == sha256_hex(b"abc")
    assert sha256_hex(b"abc") != sha256_hex(b"abd")
    assert len(sha256_hex(b"x")) == 64
```
- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement**
```python
# backend/src/fdi/core/hashing.py
"""Pure hashing. No I/O — core."""

import hashlib


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/hashing.py backend/tests/unit/test_hashing.py && git commit -m "feat(core): sha256_hex"`

---

### Task 2: `ArtifactStore.put_bytes` / `get_bytes` (platform storage)

**Files:** Modify `backend/src/fdi/platform/storage/artifact_store.py`; Test `backend/tests/integration/test_artifact_store.py` (ADD).

**Interfaces:** Produces `async def put_bytes(self, key: str, data: bytes, content_type: str) -> str` (stores raw bytes, returns key) and `async def get_bytes(self, key: str) -> bytes`. (`get_raw` may already exist — if so, `get_bytes` can delegate or you keep `get_raw`; read the file. Add only what's missing.)

- [ ] **Step 1: Failing test** (ADD, reuse the `store` fixture)
```python
async def test_put_and_get_bytes(store) -> None:
    key = await store.put_bytes("documents/deadbeef.pdf", b"%PDF-1.7 ...", "application/pdf")
    assert key == "documents/deadbeef.pdf"
    assert await store.get_bytes("documents/deadbeef.pdf") == b"%PDF-1.7 ..."
```
- [ ] **Step 2: Run → fails** (`AttributeError`).
- [ ] **Step 3: Implement** — mirror the existing `put_json`/`get_raw` methods (they use `self._client.put_object` / `get_object` via `asyncio.to_thread`). `put_bytes` wraps the data in `io.BytesIO`, passes `length=len(data)`, `content_type=content_type`. If `get_raw` already returns bytes, implement `get_bytes` as a thin alias or reuse it. Read the file first and match its idiom (`self._client`, `self._bucket`).
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/platform/storage/artifact_store.py backend/tests/integration/test_artifact_store.py && git commit -m "feat(storage): ArtifactStore put_bytes/get_bytes for raw blobs"`

---

### Task 3: Extend `Document` + migration 0005 (platform db)

**Files:** Modify `backend/src/fdi/platform/db/models.py`; Create `backend/alembic/versions/0005_document_intake_columns.py`; Test `backend/tests/integration/test_migrations.py` (ADD + bump head).

**Interfaces:** `Document` gains `filename: str | None`, `byte_size: int | None`, `content_type: str | None`, `doc_key: str | None`, `version: int | None`, and a `UNIQUE(checksum)` constraint. `checksum` stays `str | None` (nullable). Head migration `revision="0005"`, `down_revision="0004"`.

- [ ] **Step 1: Failing test** (ADD to `test_migrations.py`; bump the head assertion `"0004"`→`"0005"`)
```python
async def test_document_checksum_is_unique(migrated_engine) -> None:
    import sqlalchemy
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text(
            "INSERT INTO document (id, checksum, version) VALUES ('d-u1','abc123',1)"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO document (id, checksum, version) VALUES ('d-u2','abc123',1)"))


async def test_two_null_checksums_are_allowed(migrated_engine) -> None:
    # ping's synthetic checksum-less rows must remain legal under UNIQUE(checksum)
    import sqlalchemy
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text("INSERT INTO document (id) VALUES ('d-n1')"))
        await conn.execute(sqlalchemy.text("INSERT INTO document (id) VALUES ('d-n2')"))  # no error
```
- [ ] **Step 2: Run → fails** (missing columns/constraint, wrong head).
- [ ] **Step 3: Implement** — add the columns to the `Document` model (match the SP-2 idiom: `Mapped[str | None] = mapped_column(String(n))`, `Mapped[int | None] = mapped_column()`, `content_type String(128)`, `doc_key String(256)`, `filename Text`, `byte_size Integer`) and `__table_args__ = (UniqueConstraint("checksum", name="uq_document_checksum"),)`. Hand-write the migration:
```python
# 0005_document_intake_columns.py
revision = "0005"; down_revision = "0004"; branch_labels = None; depends_on = None
from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.add_column("document", sa.Column("filename", sa.Text(), nullable=True))
    op.add_column("document", sa.Column("byte_size", sa.Integer(), nullable=True))
    op.add_column("document", sa.Column("content_type", sa.String(128), nullable=True))
    op.add_column("document", sa.Column("doc_key", sa.String(256), nullable=True))
    op.add_column("document", sa.Column("version", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_document_checksum", "document", ["checksum"])

def downgrade() -> None:
    op.drop_constraint("uq_document_checksum", "document", type_="unique")
    for col in ("version", "doc_key", "content_type", "byte_size", "filename"):
        op.drop_column("document", col)
```
- [ ] **Step 4: Run → passes** (unique bites; two NULLs allowed).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/platform/db/models.py backend/alembic/versions backend/tests/integration/test_migrations.py && git commit -m "feat(db): document intake columns + UNIQUE(checksum)"`

---

### Task 4: `DocumentRepository` (platform db)

**Files:** Modify `backend/src/fdi/platform/db/repository.py`; Test `backend/tests/integration/test_document_repository.py`.

**Interfaces:** `class DocumentRepository` (wraps `AsyncSession` as `self._s`): `async def get(self, doc_id) -> Document | None`; `async def get_by_checksum(self, checksum) -> Document | None`; `async def next_version(self, doc_key) -> int` (max version for that doc_key + 1, else 1); `async def create(self, *, id, checksum, filename, byte_size, content_type, doc_key, version) -> Document`; `async def list_by_doc_key(self, doc_key) -> list[Document]` (ordered by version).

- [ ] **Step 1: Failing test** (unique uuids/checksums)
```python
# backend/tests/integration/test_document_repository.py
import uuid
from fdi.platform.db.repository import DocumentRepository
# session_factory fixture from migrated_engine


async def test_create_get_and_version(session_factory) -> None:
    key = f"key-{uuid.uuid4()}"
    async with session_factory() as s:
        repo = DocumentRepository(s)
        assert await repo.next_version(key) == 1
        d1 = await repo.create(id=str(uuid.uuid4()), checksum=str(uuid.uuid4()),
                               filename="a.pdf", byte_size=10, content_type="application/pdf",
                               doc_key=key, version=1)
        await s.commit()
    async with session_factory() as s:
        repo = DocumentRepository(s)
        assert (await repo.get(d1.id)).filename == "a.pdf"
        assert await repo.next_version(key) == 2
        assert await repo.get_by_checksum(d1.checksum) is not None
        d2 = await repo.create(id=str(uuid.uuid4()), checksum=str(uuid.uuid4()),
                               filename="a2.pdf", byte_size=11, content_type="application/pdf",
                               doc_key=key, version=2)
        await s.commit()
        versions = await repo.list_by_doc_key(key)
    assert [d.version for d in versions] == [1, 2]
```
- [ ] **Step 2: Run → fails** (`ImportError`).
- [ ] **Step 3: Implement** — mirror `RegistryRepository`'s style (`sqlalchemy.select`, `self._s.add`, `flush`). `next_version` = `select(func.max(Document.version)).where(Document.doc_key == doc_key)` → `(max or 0) + 1`.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/platform/db/repository.py backend/tests/integration/test_document_repository.py && git commit -m "feat(db): DocumentRepository"`

---

### Task 5: `intake_document` service (pipeline)

**Files:** Create `backend/src/fdi/pipeline/intake.py`; Test `backend/tests/integration/test_intake.py`.

**Interfaces:** Produces `document_key(checksum: str) -> str` → `f"documents/{checksum}.pdf"`; and
```python
async def intake_document(
    *, pdf: bytes, filename: str, doc_key: str, content_type: str,
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore,
) -> Document:
```
Returns the (existing, on dedup) or newly-created `Document`. Flow per spec §3.3: checksum → get_by_checksum (return existing) → put_bytes → next_version → create → commit. On the concurrent-insert race, catch `IntegrityError` on create and re-read `get_by_checksum` (return the winner's row).

- [ ] **Step 1: Failing test** (integration; dedup + versioning)
```python
# backend/tests/integration/test_intake.py
import uuid
from fdi.pipeline.intake import document_key, intake_document
# session_factory + store fixtures


async def test_intake_stores_dedups_and_versions(session_factory, store) -> None:
    key = f"doc-{uuid.uuid4()}"
    a = await intake_document(pdf=b"PDF-A", filename="a.pdf", doc_key=key,
                              content_type="application/pdf", session_factory=session_factory, store=store)
    assert a.version == 1 and a.checksum is not None
    assert await store.get_bytes(document_key(a.checksum)) == b"PDF-A"

    # same bytes -> dedup -> same id, still version 1
    a2 = await intake_document(pdf=b"PDF-A", filename="a-again.pdf", doc_key=key,
                               content_type="application/pdf", session_factory=session_factory, store=store)
    assert a2.id == a.id and a2.version == 1

    # different bytes, same doc_key -> version 2
    b = await intake_document(pdf=b"PDF-B", filename="b.pdf", doc_key=key,
                              content_type="application/pdf", session_factory=session_factory, store=store)
    assert b.version == 2 and b.id != a.id
```
- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** per spec §3.3 (import `sha256_hex` from `fdi.core.hashing`, `DocumentRepository`, `Document`). Type `session_factory: async_sessionmaker[AsyncSession]`, `store: ArtifactStore`.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/intake.py backend/tests/integration/test_intake.py && git commit -m "feat(pipeline): intake_document — checksum, dedup, version, store"`

---

### Task 6: API `POST /documents` + reads + `FDI_MAX_UPLOAD_MB` (api, config, compose, env)

**Files:** Modify `backend/src/fdi/api/routes.py`, `backend/src/fdi/api/schemas.py`, `backend/src/fdi/platform/config.py`, `docker/compose.yml`, `.env.example`; Test `backend/tests/integration/test_api.py` (ADD). Confirm `python-multipart` is a dependency (`grep multipart backend/pyproject.toml`); if absent, `uv add --directory backend python-multipart` and commit the lockfile change with this task.

**Interfaces:** `POST /documents` (multipart: `file`, optional form `doc_key`) → 201 `DocumentView`; dedup returns the existing doc (200 or 201 — use 201 for created, 200 for dedup-hit, or 201 always for simplicity — **use 201 always**, body carries the id so the client can tell). Oversized (> `FDI_MAX_UPLOAD_MB`) → 413; non-`application/pdf` → 415. `GET /documents/{id}` → 200 `DocumentView` / 404. `GET /documents?doc_key=` → `{versions: [DocumentView]}`.

- [ ] **Step 1: Add the config field** — in `config.py` add `max_upload_mb: int = 50`. Add `FDI_MAX_UPLOAD_MB: ${FDI_MAX_UPLOAD_MB}` to `docker/compose.yml`'s `x-backend-env` block, and `FDI_MAX_UPLOAD_MB=50` to `.env.example` (BOTH — an allowlisted field absent from `.env.example` injects `""` and crashes pydantic at boot; the SP-1 lesson).

- [ ] **Step 2: Failing test** (ADD to `test_api.py`)
```python
async def test_document_upload_dedup_and_versions(client) -> None:
    r = await client.post("/documents", files={"file": ("a.pdf", b"PDF-A", "application/pdf")},
                          data={"doc_key": "annual-2023"})
    assert r.status_code == 201, r.text
    doc = r.json(); assert doc["version"] == 1 and doc["checksum"]

    same = await client.post("/documents", files={"file": ("a2.pdf", b"PDF-A", "application/pdf")},
                             data={"doc_key": "annual-2023"})
    assert same.json()["id"] == doc["id"]  # dedup

    r2 = await client.post("/documents", files={"file": ("b.pdf", b"PDF-B", "application/pdf")},
                           data={"doc_key": "annual-2023"})
    assert r2.json()["version"] == 2

    got = await client.get(f"/documents/{doc['id']}")
    assert got.status_code == 200 and got.json()["filename"] == "a.pdf"
    versions = (await client.get("/documents?doc_key=annual-2023")).json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]


async def test_non_pdf_is_415(client) -> None:
    r = await client.post("/documents", files={"file": ("x.txt", b"hi", "text/plain")})
    assert r.status_code == 415
```
- [ ] **Step 3: Run → fails** (404/route missing).
- [ ] **Step 4: Implement** — add `DocumentView` + `DocumentVersionsView` schemas; the routes call `intake_document` with `app.state.session_factory` + `app.state.artifact_store` (add `SessionFactoryDep`/`StoreDep` — `SessionFactoryDep` exists from SP-3). Read the upload via `UploadFile`; enforce `content_type == "application/pdf"` (415) and `len(bytes) <= max_upload_mb*1024*1024` (413, read `get_settings().max_upload_mb`). `doc_key` defaults to the filename when the form field is absent. Import `UploadFile`, `File`, `Form` from fastapi.
- [ ] **Step 5: Run → passes**, then full non-e2e suite green.
- [ ] **Step 6: Commit** — `git add backend/src/fdi/api backend/src/fdi/platform/config.py docker/compose.yml .env.example backend/pyproject.toml backend/uv.lock backend/tests/integration/test_api.py && git commit -m "feat(api): POST /documents upload (dedup+versioning) + reads"`

---

### Task 7: Audit + docs

**Files:** SP-4 spec status line; `docs/architecture/ingestion-roadmap.md` (SP-4 → done + handoff).

- [ ] **Step 1: Static gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports --config .importlinter` (`1 kept, 0 broken`). Also re-verify the `x-backend-env` ↔ `.env.example` ↔ `Settings` fields are consistent (the new `FDI_MAX_UPLOAD_MB` present in all three).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e skips).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 upload+store, 2 dedup, 3 versioning, 4 UNIQUE bites, 5 413/404, 6 columns populated + ping still works, 7 gates) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-17.**
- [ ] **Step 5: Roadmap SP-4 → ✅ done** + a handoff note: documents are content-addressed at `documents/{checksum}.pdf`, deduped on `checksum`, versioned per `doc_key`; `DocumentRepository` + `intake_document` are the accessors; **SP-5 runs the OCR stage over an intake'd document** (`run_stage(doc_id, "ocr")`) using Azure DI (`FDI_AZURE_DI_*`), and must decide OCR-artifact content-addressing given DI's non-determinism (key on PDF checksum + DI model version, per roadmap §7).
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-4 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1(checksum) T2(blob store) T3(columns+UNIQUE) T5(dedup+version+race) T6(upload+size cap). §3 architecture → core T1, storage T2, db T3/T4, pipeline T5, api T6. §4 errors → T6 (413/415/404), T5 (race). §5 testing → unit T1, integration T2/T3/T4/T5, api T6. §7 done criteria → T7 audit.

**Type consistency:** `intake_document(*, pdf, filename, doc_key, content_type, session_factory, store) -> Document` (T5) matches its call in T6. `DocumentRepository.get/get_by_checksum/next_version/create/list_by_doc_key` (T4) match T5. `document_key`/`sha256_hex`/`put_bytes`/`get_bytes` consistent T1/T2/T5. `FDI_MAX_UPLOAD_MB` added in Settings + compose + env (T6) and re-verified (T7).

**Placeholder scan:** no TODO/TBD; the 201-vs-200 choice is resolved (201 always). `get_raw`-vs-`get_bytes` in T2 is a "read the file and add only what's missing" instruction, not a blank.
