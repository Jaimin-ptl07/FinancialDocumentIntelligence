# RT-3 Keyword Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Postgres full-text keyword search over section text — a `chunk`
table (generated `tsvector` + GIN index) populated from the RT-1 `chunks`
artifact via a new synchronous route, and a `keyword_search` query function
exposed as `POST /retrieve/keyword`.

**Architecture:** A pure `fdi.core.retrieval.chunk_rows_for_index` shapes RT-1
`chunks` artifact records + `Document.doc_key`/`.version` into upsert-ready
rows. A new `Chunk` SQLAlchemy model (`fdi.platform.db.models`) + Alembic
migration `0006` adds the `chunk` table, whose `tsv` column is a Postgres
`GENERATED ALWAYS AS (to_tsvector('english', text)) STORED` column with a GIN
index. `fdi.pipeline.retrieval.index_chunks_fts` (new, same module as RT-1's
`resolve_chunk`/`resolve_chunks`) upserts a document's rows; `POST
/documents/{doc_id}/index_fts` triggers it **synchronously** (no Celery — see
Global Constraint 5). `fdi.pipeline.retrieval.keyword_search` runs
`plainto_tsquery` + `ts_rank` against `chunk`, exposed as `POST
/retrieve/keyword`.

**Tech Stack:** Python 3.13, `uv`, SQLAlchemy 2.0 async + `postgresql.insert`
(`ON CONFLICT DO UPDATE`), Alembic, pytest + testcontainers (Postgres is in the
**default** integration suite — no opt-in marker needed for this sub-project).

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive),
  CI-enforced; stay "1 kept, 0 broken". `fdi.core.retrieval` stays PURE: no
  I/O/config/clock/RNG. `Chunk` lives in `fdi.platform.db.models` (platform
  layer, same as `Document`/`StageRun`); `index_chunks_fts`/`keyword_search`
  live in `fdi.pipeline.retrieval` (pipeline layer, imports platform + core);
  the two new routes live in `fdi.api.routes` (api layer, imports pipeline).
  No changes to `fdi.pipeline.stages` — this sub-project deliberately does
  NOT add a `Stage` (see Global Constraint 5).
- Determinism: no wall-clock/RNG in `core`; Postgres's own clock
  (`server_default=func.now()`) is the only timestamp source, consistent with
  every other table in `models.py`.
- **`chunk.id` MUST equal `f"{doc_id}:{chunk_id}"`** — the same identity
  Milvus already uses (RT-1). Never a fresh UUID. This is both the primary
  key and the `ON CONFLICT` target, so reprocessing a document is a plain
  idempotent upsert.
- **The generated column indexes `text` ALONE**: `to_tsvector('english',
  text)`. Do NOT concatenate `heading` — RT-1's `chunks[i].text` already
  begins with `heading + "\n"` by construction (verified in RT-1's own
  `test_text_matches_embedding_construction`), so re-adding `heading` would
  double-weight the same lexemes for no benefit.
- **`plainto_tsquery`, never `to_tsquery`**, for `keyword_search`'s query
  parameter — `to_tsquery` raises on ordinary free text containing a stray
  operator character; `plainto_tsquery` never raises and degrades an
  empty/whitespace query to "matches nothing" rather than an error.
- **Population is a SYNCHRONOUS route (`SessionDep` + `StoreDep`, inline
  commit), not a Celery dispatch.** `index_chunks_fts` makes no external API
  call (no Azure/Milvus/Neo4j) — same cost profile as the existing `/search`
  and `/entities/resolve` routes, both already synchronous. Do NOT add a
  `Stage`, a Celery task, or a `DispatcherDep` for this. (This is a genuine
  design fork the spec flags for human review — §2 decision 6 of the design
  doc; implement the chosen synchronous shape, but do not treat the
  alternatives as foreclosed if a reviewer prefers one of them.)
- Per-task gates before every commit: `uv run --directory backend ruff check .`
  · `uv run --directory backend mypy --strict src` ·
  `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter`
  · the task's pytest. Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) → commit.
- Integration tests use the **real testcontainers Postgres** already wired in
  `backend/tests/integration/conftest.py` (`migrated_engine`,
  `_alembic_applied`, `postgres_url`) — no new fixture infra needed, and no
  opt-in `pytest.mark` (Postgres is in the default suite, unlike
  `milvus`/`neo4j`/`azure`/`bge`).

---

### Task 1: `fdi.core.retrieval.chunk_rows_for_index` (pure)

**Files:**
- Modify: `backend/src/fdi/core/retrieval.py` (append)
- Modify: `backend/tests/unit/test_retrieval_chunks.py` (append — same file
  RT-1's `build_chunks` tests live in; keeps `fdi.core.retrieval`'s tests in
  one place) OR create `backend/tests/unit/test_retrieval_chunk_rows.py` if
  the executor prefers a dedicated file — either is fine, no existing
  precedent forces one over the other for this module.

**Interfaces:**
- Produces: `chunk_rows_for_index(*, doc_id: str, doc_key: str | None,
  version: int | None, chunks: Sequence[Mapping[str, object]]) ->
  list[dict[str, object]]`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_retrieval_chunk_rows.py
from fdi.core.retrieval import chunk_rows_for_index


def test_builds_rows_with_shared_doc_key_and_version() -> None:
    chunks = [
        {"chunk_id": "s0", "heading": "Balance Sheet", "text": "Balance Sheet\nAssets 100",
         "section_type": "BALANCE_SHEET", "page": 3, "para_count": 1},
        {"chunk_id": "s1", "heading": "Notes", "text": "Notes\nNote 1",
         "section_type": "NOTES", "page": 7, "para_count": 1},
    ]
    rows = chunk_rows_for_index(doc_id="d1", doc_key="annual-report", version=2, chunks=chunks)
    assert rows == [
        {"id": "d1:s0", "doc_id": "d1", "chunk_id": "s0", "doc_key": "annual-report",
         "version": 2, "heading": "Balance Sheet", "text": "Balance Sheet\nAssets 100",
         "section_type": "BALANCE_SHEET", "page": 3},
        {"id": "d1:s1", "doc_id": "d1", "chunk_id": "s1", "doc_key": "annual-report",
         "version": 2, "heading": "Notes", "text": "Notes\nNote 1",
         "section_type": "NOTES", "page": 7},
    ]


def test_skips_record_with_non_str_or_empty_chunk_id() -> None:
    chunks = [
        {"chunk_id": 123, "heading": "A", "text": "A\n"},
        {"chunk_id": "", "heading": "B", "text": "B\n"},
        {"chunk_id": "s2", "heading": "C", "text": "C\n"},
    ]
    rows = chunk_rows_for_index(doc_id="d1", doc_key=None, version=None, chunks=chunks)
    assert [r["chunk_id"] for r in rows] == ["s2"]


def test_degrades_heading_section_type_page_text_to_defaults() -> None:
    chunks = [{"chunk_id": "s0", "heading": 1, "text": None, "section_type": 2, "page": "x"}]
    rows = chunk_rows_for_index(doc_id="d1", doc_key=None, version=None, chunks=chunks)
    assert rows == [
        {"id": "d1:s0", "doc_id": "d1", "chunk_id": "s0", "doc_key": None, "version": None,
         "heading": None, "text": "", "section_type": None, "page": None}
    ]


def test_empty_chunks_returns_empty_list() -> None:
    assert chunk_rows_for_index(doc_id="d1", doc_key=None, version=None, chunks=[]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_chunk_rows.py -v`
Expected: FAIL — `ImportError: cannot import name 'chunk_rows_for_index'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/src/fdi/core/retrieval.py` (after `build_chunks`; keep the
module's existing `_int_or_none` helper and reuse it here rather than
duplicating the guard):

```python
from collections.abc import Sequence  # add to the existing `from collections.abc import Mapping` line


def chunk_rows_for_index(
    *,
    doc_id: str,
    doc_key: str | None,
    version: int | None,
    chunks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """From RT-1's `chunks` artifact records, build the flat rows
    `index_chunks_fts` upserts into the `chunk` table. Pure; skips a record
    with a missing/non-str/empty chunk_id, degrades heading/section_type/page
    to None and text to "" on a malformed value. Never raises."""
    rows: list[dict[str, object]] = []
    for c in chunks:
        chunk_id = c.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            continue
        heading = c.get("heading")
        text = c.get("text")
        section_type = c.get("section_type")
        rows.append({
            "id": f"{doc_id}:{chunk_id}",
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "doc_key": doc_key,
            "version": version,
            "heading": heading if isinstance(heading, str) else None,
            "text": text if isinstance(text, str) else "",
            "section_type": section_type if isinstance(section_type, str) else None,
            "page": _int_or_none(c.get("page")),
        })
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_chunk_rows.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/retrieval.py backend/tests/unit/test_retrieval_chunk_rows.py
git commit -m "feat(core): retrieval.chunk_rows_for_index -- shape chunks artifact into upsert rows"
```

---

### Task 2: `chunk` table — SQLAlchemy model + Alembic migration `0006`

**Files:**
- Modify: `backend/src/fdi/platform/db/models.py` (add `Chunk`)
- Create: `backend/alembic/versions/0006_chunk_fts.py`
- Modify: `backend/tests/integration/test_migrations.py` (version bump + new
  tests)

**Interfaces:**
- Produces: `Chunk` ORM model, table `chunk` with generated `tsv` column +
  GIN index `ix_chunk_tsv`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/integration/test_migrations.py`:

```python
async def test_alembic_version_is_stamped(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.connect() as conn:
        result = await conn.execute(sqlalchemy.text(
            "SELECT version_num FROM alembic_version"
        ))
        assert result.scalar_one() == "0006"  # was "0005"


async def test_chunk_tsv_is_generated_and_searchable(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text("INSERT INTO document (id) VALUES ('d-fts')"))
        await conn.execute(sqlalchemy.text(
            "INSERT INTO chunk (id, doc_id, chunk_id, text) VALUES "
            "('d-fts:s0', 'd-fts', 's0', 'Balance Sheet total assets'), "
            "('d-fts:s1', 'd-fts', 's1', 'Directors report narrative')"
        ))
    async with migrated_engine.connect() as conn:
        result = await conn.execute(sqlalchemy.text(
            "SELECT id FROM chunk WHERE tsv @@ plainto_tsquery('english', 'balance sheet')"
        ))
        ids = [row[0] for row in result.all()]
    assert ids == ["d-fts:s0"]


async def test_chunk_tsv_index_is_gin(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.connect() as conn:
        result = await conn.execute(sqlalchemy.text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunk_tsv'"
        ))
        indexdef = result.scalar_one()
    assert "USING gin" in indexdef


async def test_chunk_doc_id_fk_enforced(migrated_engine: AsyncEngine) -> None:
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO chunk (id, doc_id, chunk_id, text) VALUES "
                "('nope:s0', 'no-such-doc', 's0', 'x')"
            ))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_migrations.py -v`
Expected: FAIL — `version_num` mismatch, and `relation "chunk" does not
exist` for the other three.

- [ ] **Step 3: Add the `Chunk` model**

In `backend/src/fdi/platform/db/models.py`, extend the imports and append the
model (after `Document`, before `StageRun` — grouped with the other
per-document tables):

```python
from sqlalchemy import (
    CheckConstraint,
    Computed,          # add
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR   # add
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
```

```python
class Chunk(Base):
    """RT-3: Postgres full-text keyword index. One row per RT-1 `chunks`
    artifact record, upserted by `fdi.pipeline.retrieval.index_chunks_fts`.

    `id` reuses the unified chunk identity `f"{doc_id}:{chunk_id}"` (the same
    id Milvus already uses for vectors) as PK and upsert conflict target --
    reprocessing a document is a plain idempotent upsert. `doc_key`/`version`
    are denormalized from `Document` at write time (the pipeline layer HAS a
    session, unlike a Stage) so a keyword-filtered query needs no join. `tsv`
    is a Postgres STORED generated column over `text` ALONE (RT-1's `text`
    already begins with `heading + "\n"`, so re-adding heading would just
    double-weight the same lexemes) -- Postgres maintains it on every
    insert/update; nothing in this codebase ever computes or targets it
    directly (a generated column cannot appear in an INSERT/UPDATE column
    list). `ix_chunk_tsv` (GIN) is what makes `@@` queries fast.
    """

    __tablename__ = "chunk"
    __table_args__ = (
        Index("ix_chunk_doc_key_version", "doc_key", "version"),
        Index("ix_chunk_section_type", "section_type"),
        Index("ix_chunk_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("document.id"), index=True, nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    version: Mapped[int | None] = mapped_column(nullable=True)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    section_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page: Mapped[int | None] = mapped_column(nullable=True)
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 4: Write the migration**

Create `backend/alembic/versions/0006_chunk_fts.py`:

```python
"""chunk table for RT-3 full-text keyword search

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chunk",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("doc_id", sa.String(36), sa.ForeignKey("document.id"), nullable=False),
        sa.Column("chunk_id", sa.String(64), nullable=False),
        sa.Column("doc_key", sa.String(256), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section_type", sa.String(64), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chunk_doc_id", "chunk", ["doc_id"])
    op.create_index("ix_chunk_doc_key_version", "chunk", ["doc_key", "version"])
    op.create_index("ix_chunk_section_type", "chunk", ["section_type"])
    op.create_index("ix_chunk_tsv", "chunk", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_chunk_tsv", table_name="chunk")
    op.drop_index("ix_chunk_section_type", table_name="chunk")
    op.drop_index("ix_chunk_doc_key_version", table_name="chunk")
    op.drop_index("ix_chunk_doc_id", table_name="chunk")
    op.drop_table("chunk")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_migrations.py -v`
Expected: PASS (all, including the pre-existing ones — the version bump is
the only change to any prior assertion).

- [ ] **Step 6: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/platform/db/models.py backend/alembic/versions/0006_chunk_fts.py backend/tests/integration/test_migrations.py
git commit -m "feat(db): chunk table -- generated tsvector + GIN index (RT-3 migration 0006)"
```

---

### Task 3: `fdi.pipeline.retrieval.index_chunks_fts`

**Files:**
- Modify: `backend/src/fdi/pipeline/retrieval.py` (append)
- Create: `backend/tests/integration/test_index_chunks_fts.py`

**Interfaces:**
- Consumes: `chunk_rows_for_index` (Task 1); `Chunk` (Task 2);
  `RegistryRepository.get_document`/the existing `_read_chunks` helper.
- Produces: `index_chunks_fts(*, session: AsyncSession, store: ArtifactStore,
  doc_id: str) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_index_chunks_fts.py
"""index_chunks_fts: upsert a document's RT-1 `chunks` artifact into the
`chunk` table. Against real Postgres + MinIO with FAKE OCR + semantic
clients -- reuses test_resolve_chunk.py's `processed_doc` fixture verbatim."""

import uuid

import pytest
import sqlalchemy
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.retrieval import index_chunks_fts
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.chunks import ChunksStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import DocumentRepository
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


@pytest.fixture
async def processed_doc(session_factory, store) -> str:
    """Intake one FakeOcr document and run ocr -> ... -> chunks. Returns
    doc.id. Identical fixture to test_resolve_chunk.py's -- two chunks,
    BALANCE_SHEET (page 1) and DIRECTORS_REPORT (page 2)."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"role": "sectionHeading", "content": "Balance Sheet",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
                {"content": "Total Assets 100",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}]},
                {"role": "sectionHeading", "content": "Directors' Report",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"content": "The board is pleased.",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient(
        {
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Directors' Report": SectionType.DIRECTORS_REPORT.value,
        }
    )
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(),
                  GraphStage(), ProvenanceStage(), ChunksStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="semantic", run_id=str(uuid.uuid4()),
                     services={"semantic": fake_semantic}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id


async def _chunk_rows(session_factory, doc_id: str) -> list[sqlalchemy.Row]:
    async with session_factory() as s:
        result = await s.execute(
            sqlalchemy.text(
                "SELECT id, doc_id, chunk_id, doc_key, version, heading, text, "
                "section_type, page FROM chunk WHERE doc_id = :d ORDER BY chunk_id"
            ),
            {"d": doc_id},
        )
        return result.all()


async def test_index_chunks_fts_writes_rows_with_doc_key_and_version(
    session_factory, store, processed_doc
) -> None:
    async with session_factory() as s:
        doc = await DocumentRepository(s).get(processed_doc)
        count = await index_chunks_fts(session=s, store=store, doc_id=processed_doc)
        await s.commit()
    assert count == 2

    rows = await _chunk_rows(session_factory, processed_doc)
    assert [r.chunk_id for r in rows] == ["s0", "s1"]
    assert rows[0].id == f"{processed_doc}:s0"
    assert rows[0].doc_key == doc.doc_key and rows[0].version == doc.version
    assert rows[0].section_type == "BALANCE_SHEET" and rows[0].page == 1
    assert "Balance Sheet" in rows[0].text


async def test_index_chunks_fts_is_idempotent(session_factory, store, processed_doc) -> None:
    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=processed_doc)
        await s.commit()
    async with session_factory() as s:
        count2 = await index_chunks_fts(session=s, store=store, doc_id=processed_doc)
        await s.commit()
    assert count2 == 2
    rows = await _chunk_rows(session_factory, processed_doc)
    assert len(rows) == 2  # no duplicates


async def test_index_chunks_fts_unknown_document_returns_zero(session_factory, store) -> None:
    async with session_factory() as s:
        count = await index_chunks_fts(session=s, store=store, doc_id="no-such-doc")
        await s.commit()
    assert count == 0


async def test_index_chunks_fts_no_chunks_artifact_returns_zero(session_factory, store) -> None:
    async with session_factory() as s:
        doc = await DocumentRepository(s).create(
            id=str(uuid.uuid4()), checksum=str(uuid.uuid4()), filename="x.pdf",
            byte_size=1, content_type="application/pdf", doc_key="k", version=1,
        )
        await s.commit()
        count = await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()
    assert count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_index_chunks_fts.py -v`
Expected: FAIL — `ImportError: cannot import name 'index_chunks_fts'`.

- [ ] **Step 3: Implement**

Replace `backend/src/fdi/pipeline/retrieval.py`'s import block (top of file)
with the following — `func` and `insert` are new; `AsyncSession` was already
imported:

```python
from collections.abc import Mapping
from typing import cast

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.retrieval import chunk_rows_for_index
from fdi.platform.db.models import Chunk
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore
```

Then append the function (after `resolve_chunks`):

```python
async def index_chunks_fts(*, session: AsyncSession, store: ArtifactStore, doc_id: str) -> int:
    """Upsert `doc_id`'s `chunks` artifact into the `chunk` table. Returns the
    number of rows upserted (0 if the document is unknown or has no `chunks`
    artifact yet -- degrades, never raises, mirroring resolve_chunk). Caller
    owns the transaction (commits), same convention as every function in this
    module and fdi.pipeline.crossdoc.resolve_entities."""
    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    if doc is None:
        return 0
    chunks = await _read_chunks(repo, store, doc_id)
    rows = chunk_rows_for_index(
        doc_id=doc_id, doc_key=doc.doc_key, version=doc.version, chunks=chunks
    )
    if not rows:
        return 0

    stmt = pg_insert(Chunk).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "doc_key": stmt.excluded.doc_key,
            "version": stmt.excluded.version,
            "heading": stmt.excluded.heading,
            "text": stmt.excluded.text,
            "section_type": stmt.excluded.section_type,
            "page": stmt.excluded.page,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.flush()
    return len(rows)
```

`RegistryRepository.get_document` already exists; `DocumentRepository.get`
would also work but `RegistryRepository` is already imported in this module
for `_read_chunks`, so reuse the one repository instance rather than
constructing a second repository class over the same session.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_index_chunks_fts.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/retrieval.py backend/tests/integration/test_index_chunks_fts.py
git commit -m "feat(pipeline): index_chunks_fts -- upsert chunks artifact into the chunk table"
```

---

### Task 4: `fdi.pipeline.retrieval.keyword_search`

**Files:**
- Modify: `backend/src/fdi/pipeline/retrieval.py` (append)
- Create: `backend/tests/integration/test_keyword_search.py`

**Interfaces:**
- Consumes: `Chunk` (Task 2); rows written by `index_chunks_fts` (Task 3).
- Produces: `keyword_search(*, session: AsyncSession, query: str, k: int = 5,
  section_type: str | None = None, doc_key: str | None = None, version: int |
  None = None) -> list[dict[str, object]]`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_keyword_search.py
"""keyword_search: plainto_tsquery + ts_rank over the `chunk` table. Against
real Postgres, seeded via index_chunks_fts over a processed_doc fixture."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.retrieval import index_chunks_fts, keyword_search
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.chunks import ChunksStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


@pytest.fixture
async def indexed_doc(session_factory, store) -> tuple[str, str, int]:
    """One processed + FTS-indexed document (Balance Sheet + Directors'
    Report chunks). Returns (doc_id, doc_key, version)."""
    doc_key = f"annual-{uuid.uuid4()}"
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"role": "sectionHeading", "content": "Balance Sheet",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
                {"content": "Goodwill impairment charge of 100",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}]},
                {"role": "sectionHeading", "content": "Directors' Report",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"content": "The board is pleased with performance.",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient(
        {
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Directors' Report": SectionType.DIRECTORS_REPORT.value,
        }
    )
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(),
                  GraphStage(), ProvenanceStage(), ChunksStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="semantic", run_id=str(uuid.uuid4()),
                     services={"semantic": fake_semantic}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()
    return doc.id, doc_key, 1


async def test_keyword_search_ranks_the_matching_chunk_first(
    session_factory, indexed_doc
) -> None:
    doc_id, _, _ = indexed_doc
    async with session_factory() as s:
        hits = await keyword_search(session=s, query="goodwill impairment", k=5)
    ours = [h for h in hits if h["doc_id"] == doc_id]
    assert ours[0]["chunk_id"] == "s0"
    assert ours[0]["section_type"] == "BALANCE_SHEET"
    assert ours[0]["score"] > 0.0
    assert "Goodwill impairment" in ours[0]["text"]


async def test_keyword_search_filters_by_section_type(session_factory, indexed_doc) -> None:
    doc_id, _, _ = indexed_doc
    async with session_factory() as s:
        hits = await keyword_search(
            session=s, query="board", k=5, section_type="BALANCE_SHEET"
        )
    assert all(h["chunk_id"] != "s1" or h["doc_id"] != doc_id for h in hits)


async def test_keyword_search_filters_by_doc_key_and_version(session_factory, indexed_doc) -> None:
    doc_id, doc_key, version = indexed_doc
    async with session_factory() as s:
        hits = await keyword_search(
            session=s, query="board", k=5, doc_key=doc_key, version=version
        )
    assert any(h["doc_id"] == doc_id and h["chunk_id"] == "s1" for h in hits)
    async with session_factory() as s:
        none_hits = await keyword_search(
            session=s, query="board", k=5, doc_key="no-such-doc-key", version=1
        )
    assert none_hits == []


async def test_keyword_search_k_limits_results(session_factory, indexed_doc) -> None:
    async with session_factory() as s:
        hits = await keyword_search(session=s, query="the a", k=1)
    assert len(hits) <= 1


async def test_keyword_search_empty_query_returns_empty(session_factory, indexed_doc) -> None:
    async with session_factory() as s:
        hits = await keyword_search(session=s, query="", k=5)
    assert hits == []


async def test_keyword_search_hit_shape(session_factory, indexed_doc) -> None:
    doc_id, doc_key, version = indexed_doc
    async with session_factory() as s:
        hits = await keyword_search(session=s, query="goodwill", k=5)
    ours = next(h for h in hits if h["doc_id"] == doc_id)
    assert set(ours) == {
        "doc_id", "doc_key", "version", "chunk_id", "heading",
        "page", "section_type", "text", "score",
    }
    assert ours["doc_key"] == doc_key and ours["version"] == version
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_keyword_search.py -v`
Expected: FAIL — `ImportError: cannot import name 'keyword_search'`.

- [ ] **Step 3: Implement**

Update `backend/src/fdi/pipeline/retrieval.py`'s `sqlalchemy` import line
(added in Task 3) to also pull in `select`:

```python
from sqlalchemy import func, select
```

Then append the function (after `index_chunks_fts`):

```python
async def keyword_search(
    *,
    session: AsyncSession,
    query: str,
    k: int = 5,
    section_type: str | None = None,
    doc_key: str | None = None,
    version: int | None = None,
) -> list[dict[str, object]]:
    """Postgres full-text search over `chunk`. plainto_tsquery (never raises
    on free text, unlike to_tsquery) + ts_rank, best-first, optionally
    filtered. Returns citation-shaped hits carrying their own text -- unlike
    a Milvus hit, no second resolve step is needed."""
    tsquery = func.plainto_tsquery("english", query)
    score = func.ts_rank(Chunk.tsv, tsquery).label("score")
    stmt = select(Chunk, score).where(Chunk.tsv.op("@@")(tsquery))
    if section_type is not None:
        stmt = stmt.where(Chunk.section_type == section_type)
    if doc_key is not None:
        stmt = stmt.where(Chunk.doc_key == doc_key)
    if version is not None:
        stmt = stmt.where(Chunk.version == version)
    stmt = stmt.order_by(score.desc()).limit(k)

    rows = (await session.execute(stmt)).all()
    return [
        {
            "doc_id": c.doc_id, "doc_key": c.doc_key, "version": c.version,
            "chunk_id": c.chunk_id, "heading": c.heading, "page": c.page,
            "section_type": c.section_type, "text": c.text, "score": float(s),
        }
        for c, s in rows
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_keyword_search.py -v`
Expected: PASS (all 6).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/retrieval.py backend/tests/integration/test_keyword_search.py
git commit -m "feat(pipeline): keyword_search -- plainto_tsquery + ts_rank over the chunk table"
```

---

### Task 5: HTTP surface — `POST /documents/{doc_id}/index_fts` + `POST /retrieve/keyword`

**Files:**
- Modify: `backend/src/fdi/api/schemas.py` (append views)
- Modify: `backend/src/fdi/api/routes.py` (append routes)
- Modify: `backend/tests/integration/test_api.py` (append tests)

**Interfaces:**
- Consumes: `index_chunks_fts`, `keyword_search` (Tasks 3–4); `SessionDep`,
  `StoreDep` (existing).
- Produces: `IndexFtsView`, `KeywordSearchRequest`, `KeywordHitView`,
  `KeywordSearchResultsView` (schemas); two new routes.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_api.py` (after the existing
`# Task 5 (SP-9b): POST /documents/{id}/graph_load` block, before the
`# Task 1 (SP-18)` block, keeping the file's section-comment convention):

```python
# --------------------------------------------------------------------------
# RT-3: POST /documents/{id}/index_fts + POST /retrieve/keyword
# --------------------------------------------------------------------------


@pytest.fixture
async def rt3_indexed_doc(session_factory, store) -> tuple[str, str]:
    """One document run through ocr -> ... -> chunks DIRECTLY (run_stage, not
    the queued HTTP triggers -- nothing in this suite runs a worker; same
    posture as resolved_corpus_via_api above). Returns (doc_id, doc_key)."""
    from fdi.core.semantic import SectionType
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.semantic import SemanticStage

    doc_key = f"rt3-{uuid.uuid4()}"
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"role": "sectionHeading", "content": "Balance Sheet",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
                {"content": "Total Assets 100",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}]},
                {"role": "sectionHeading", "content": "Directors' Report",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"content": "The board is pleased.",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient(
        {
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Directors' Report": SectionType.DIRECTORS_REPORT.value,
        }
    )
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(),
                  GraphStage(), ProvenanceStage(), ChunksStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="semantic", run_id=str(uuid.uuid4()),
                     services={"semantic": fake_semantic}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id, doc_key


async def test_trigger_index_fts_on_a_document(
    client: AsyncClient, _alembic_applied, rt3_indexed_doc
) -> None:
    doc_id, _ = rt3_indexed_doc
    r = await client.post(f"/documents/{doc_id}/index_fts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_id"] == doc_id
    assert body["chunks_indexed"] == 2


async def test_trigger_index_fts_unknown_document_is_404(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/documents/00000000-0000-0000-0000-000000000000/index_fts")
    assert r.status_code == 404


async def test_trigger_index_fts_no_chunks_artifact_returns_zero(
    client: AsyncClient, _alembic_applied
) -> None:
    pdf = f"PDF-NOCHUNKS-{uuid.uuid4()}".encode()
    up = await client.post("/documents", files={"file": ("a.pdf", pdf, "application/pdf")})
    doc_id = up.json()["id"]
    r = await client.post(f"/documents/{doc_id}/index_fts")
    assert r.status_code == 200
    assert r.json()["chunks_indexed"] == 0


async def test_keyword_search_route_returns_hits(
    client: AsyncClient, _alembic_applied, rt3_indexed_doc
) -> None:
    doc_id, doc_key = rt3_indexed_doc
    await client.post(f"/documents/{doc_id}/index_fts")
    r = await client.post("/retrieve/keyword", json={"query": "Balance Sheet", "k": 5})
    assert r.status_code == 200
    ours = [h for h in r.json()["results"] if h["doc_id"] == doc_id]
    assert ours and ours[0]["chunk_id"] == "s0"
    assert ours[0]["doc_key"] == doc_key
    assert ours[0]["score"] > 0.0


async def test_keyword_search_route_filters_by_section_type(
    client: AsyncClient, _alembic_applied, rt3_indexed_doc
) -> None:
    doc_id, _ = rt3_indexed_doc
    await client.post(f"/documents/{doc_id}/index_fts")
    r = await client.post(
        "/retrieve/keyword",
        json={"query": "board", "k": 5, "section_type": "BALANCE_SHEET"},
    )
    assert r.status_code == 200
    assert all(
        not (h["doc_id"] == doc_id and h["chunk_id"] == "s1") for h in r.json()["results"]
    )


async def test_keyword_search_route_no_match_returns_empty_results(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post(
        "/retrieve/keyword", json={"query": "zzzznonexistenttoken9999"}
    )
    assert r.status_code == 200
    assert r.json()["results"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "index_fts or keyword_search" -v`
Expected: FAIL — `404 Not Found` for `/documents/{id}/index_fts` and
`/retrieve/keyword` (routes don't exist yet).

- [ ] **Step 3: Implement schemas + routes**

Append to `backend/src/fdi/api/schemas.py`:

```python
class IndexFtsView(BaseModel):
    doc_id: str
    chunks_indexed: int


class KeywordSearchRequest(BaseModel):
    query: str
    k: int = 5
    section_type: str | None = None
    doc_key: str | None = None
    version: int | None = None


class KeywordHitView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    chunk_id: str
    heading: str | None
    page: int | None
    section_type: str | None
    text: str
    score: float


class KeywordSearchResultsView(BaseModel):
    results: list[KeywordHitView]
```

In `backend/src/fdi/api/routes.py`: add `IndexFtsView`, `KeywordHitView`,
`KeywordSearchRequest`, `KeywordSearchResultsView` to the `fdi.api.schemas`
import block (alphabetical, matching the existing style), add
`from fdi.pipeline.retrieval import index_chunks_fts, keyword_search`
(alongside the existing `fdi.pipeline.crossdoc`/`eval_service`/`intake`
imports), and append the two routes after the
`# Task 5 (SP-9b): POST /documents/{id}/graph_load` block:

```python
# --------------------------------------------------------------------------
# RT-3: POST /documents/{id}/index_fts + POST /retrieve/keyword
# --------------------------------------------------------------------------


@router.post("/documents/{doc_id}/index_fts", response_model=IndexFtsView)
async def trigger_index_fts(doc_id: str, session: SessionDep, store: StoreDep) -> IndexFtsView:
    """Synchronous, task-free -- like /search and /entities/resolve, NOT like
    ocr/semantic/embedding/graph_load. Reads the already-cached `chunks`
    artifact from MinIO and upserts it into Postgres; no external API call
    (no Azure, no Milvus, no Neo4j), so there is nothing to queue.
    """
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    count = await index_chunks_fts(session=session, store=store, doc_id=doc_id)
    await session.commit()
    return IndexFtsView(doc_id=doc_id, chunks_indexed=count)


@router.post("/retrieve/keyword", response_model=KeywordSearchResultsView)
async def keyword_search_route(
    body: KeywordSearchRequest, session: SessionDep
) -> KeywordSearchResultsView:
    hits = await keyword_search(
        session=session, query=body.query, k=body.k,
        section_type=body.section_type, doc_key=body.doc_key, version=body.version,
    )
    return KeywordSearchResultsView(results=[KeywordHitView(**h) for h in hits])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "index_fts or keyword_search" -v`
Expected: PASS (all 6).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(api): POST /documents/{id}/index_fts + POST /retrieve/keyword"
```

---

### Task 6: Full gates, roadmap + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-rt3-keyword-retrieval-design.md`
  (Status → Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (RT-3 row → done + "what
  RT-3 shipped" handoff)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`,
  `PYTHONUTF8=1 lint-imports` (1 kept/0 broken). Grep-confirm
  `fdi.core.retrieval` still imports neither `fdi.platform.*` nor
  `fdi.core.ast`/`semantic`/`provenance`.

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`; green
  (e2e/azure/neo4j/milvus/bge skip; RT-3's tests run in the DEFAULT suite —
  confirm they are NOT accidentally marked with an opt-in marker). Note the
  passed count against the prior total.

- [ ] **Step 3: Audit RT-3 done criteria** (§7 of the spec) — map each to the
  tasks; confirm: the generated-column search test (Task 2) proves `tsv`
  populates without `index_chunks_fts` ever writing it; the idempotency test
  (Task 3) proves no duplicate rows on re-index; the empty-query test (Task 4)
  proves `plainto_tsquery("")` matches nothing against REAL Postgres, not by
  assumption; the 404-vs-200-with-zero distinction (Task 5) matches §4's
  documented split.

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps
  1–3 green).

- [ ] **Step 5: Roadmap** — flip the RT-3 row to `✅ done`; add a short "what
  RT-3 shipped" handoff for RT-4 (which fuses RT-2 + RT-3): the `chunk` table
  shape and its shared `f"{doc_id}:{chunk_id}"` identity with Milvus (RT-4's
  fusion can compare hit ids directly, no re-keying), `keyword_search`'s
  citation-shaped hit dict (identical field set intended for `vector_search`
  to also converge on for a uniform fusion input), and the §2 decision 6 fork
  (synchronous manual trigger, not auto-chained) as a flag for whoever wires
  RT-8's agent or a future ingestion orchestrator.

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: RT-3 done-criteria audit, spec status, roadmap handoff"
```

---

## Self-Review

**Spec coverage:** §3.1 `chunk_rows_for_index` → Task 1. §3.2/§3.3 `Chunk`
model + migration → Task 2. §3.4 `index_chunks_fts` → Task 3. §3.5
`keyword_search` → Task 4. §3.6 HTTP surface → Task 5. §2 decisions: shared
identity as PK (d1) → Task 2's model + Task 3's upsert; doc_key/version
denormalization (d2) → Task 1 + Task 3; `text`-only tsvector (d3) → Task 2's
`Computed(...)` + Global Constraint 2; generated+GIN column (d4) → Task 2;
`plainto_tsquery` (d5) → Task 4 + Global Constraint 3; synchronous route fork
(d6) → Task 5 + Global Constraint 5 (explicitly flagged, not silently
resolved); graceful-zero semantics (d7) → Task 3's tests + Task 5's 200-vs-404
split; pure `chunk_rows_for_index` extraction (d8) → Task 1; no new config
(d9) → nothing added, confirmed in Task 6 Step 1. §4 error handling → Task 1,
3, 4 tests. §5 testing → Tasks 1–5 (migration tests folded into Task 2, HTTP
tests folded into Task 5). §6 deferred → not built (Task 6 audits). §7 done
criteria → Task 6.

**Placeholder scan:** Tasks 1, 3, 4 carry complete, runnable code with no
`...`/TODO. Task 2's model + migration are complete SQL/ORM, no placeholders.
Task 5's route/schema code is complete; its test fixture
(`rt3_indexed_doc`) is fully written but DELIBERATELY duplicates the
`processed_doc`-shaped stage harness already established in
`test_resolve_chunk.py`/Task 3/Task 4 rather than importing a shared fixture
across files — `test_api.py` builds its own fixtures throughout (see
`resolved_corpus_via_api`) because its `client` fixture needs the SAME
`session_factory`/`store` (bucket `"artifacts"`) the running app reads
through, which differs from the plain integration tests' `"test-artifacts"`
bucket; this is intentional non-DRY-ness matching existing precedent, not an
oversight.

**Type consistency:** `chunk_rows_for_index(...) -> list[dict[str, object]]`
(Task 1) rows are consumed unchanged by `index_chunks_fts`'s
`pg_insert(Chunk).values(rows)` (Task 3) — the dict keys
(`id, doc_id, chunk_id, doc_key, version, heading, text, section_type, page`)
match `Chunk`'s mapped attribute names exactly (Task 2), so no key-renaming
layer exists to drift. `keyword_search(...) -> list[dict[str, object]]`'s
nine keys (Task 4) match `KeywordHitView`'s nine fields exactly (Task 5) —
`KeywordHitView(**h)` unpacks with no remapping. `index_chunks_fts(...) ->
int` (Task 3) is what `IndexFtsView.chunks_indexed` receives directly (Task
5). `Chunk.id` (Task 2) is always exactly `f"{doc_id}:{chunk_id}"`, produced
in exactly one place (`chunk_rows_for_index`, Task 1) — never recomputed
elsewhere, so the Milvus-shared-identity property (§2 decision 1) cannot
drift by construction.

**Risk flagged:** §2 decision 6 (synchronous route vs. queued Celery trigger
vs. auto-fold into the `chunks` stage's successor dispatch) is a genuine,
autonomously-resolved design fork — implemented as the synchronous route in
Task 5, but recorded for human review in the spec and restated in Global
Constraint 5 so a reviewer who prefers a different shape can redirect Task 5
before or after merge without touching Tasks 1–4 (the pipeline functions are
shape-agnostic to how they get called). Task 2's `Computed(...)` column
depends on Postgres 18.4's stored-generated-column support (available since
PG12, well within the pinned testcontainers image) — flagged only because it
is the one piece of DDL in this plan that is Postgres-version-sensitive, not
because there is any reason to expect it to fail against the pinned image.
