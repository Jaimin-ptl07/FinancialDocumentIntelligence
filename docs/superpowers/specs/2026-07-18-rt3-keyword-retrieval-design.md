# RT-3 — Lexical / Keyword Retrieval (Postgres full-text over the `chunk` table)

- **Status:** Implemented — 2026-07-18. Wave B of the retrieval layer
  (`docs/architecture/retrieval-roadmap.md`), parallel with RT-2 (vector), RT-5
  (graph), RT-6 (tables) — independent of all three, depends only on RT-1.
- **Depends on:**
  - **RT-1** (`docs/superpowers/specs/2026-07-18-rt1-retrieval-foundations-design.md`,
    implemented): the per-document **`chunks` artifact**
    (`{schema_version:1, chunks:[{chunk_id, heading, text, section_type, page,
    para_count}]}`, one record per AST section, `chunk_id = "s{i}"`) is RT-3's
    only content source. `text` is the exact string embedded (`heading + "\n" +
    "\n".join(paragraphs)`), so it already contains the heading — load-bearing
    for decision 3 below. `resolve_chunk`/`resolve_chunks`
    (`fdi.pipeline.retrieval`) already exist; RT-3 extends the same module.
  - **SP-2** (stage/DAG framework — `StageContext` has no Postgres session,
    which is why RT-3's population step is deliberately NOT a `Stage`; see
    decision 6).
  - **Existing Postgres schema** (`fdi.platform.db.models`, Alembic head
    `0005_document_intake_columns`): `Document(id, checksum, filename,
    byte_size, content_type, doc_key, version, created_at)`.

## 1. Purpose

Milvus (RT-2) answers "what's semantically similar", but has no exact-phrase or
term-frequency search, and its metadata (`heading, chunk_id, section_type,
page`) still has no queryable body text. Financial documents have exact
vocabulary (`"goodwill impairment"`, `"EBITDA"`, a specific line-item label) a
user or an agent will want to match literally, ranked by relevance — the
classic complement to embeddings, and RT-4's other half of hybrid search.
Postgres is already the system of record for documents and stage runs, already
in the **default** testcontainers suite (unlike Milvus/Neo4j, which are
opt-in), and ships a mature full-text engine (`tsvector`/`tsquery`, GIN
indexes, `ts_rank`) — no new infrastructure. RT-3 builds:

1. A **`chunk` table** — one row per RT-1 `chunks` artifact record, carrying
   text + citation fields (`doc_id`, `chunk_id`, `doc_key`, `version`,
   `heading`, `text`, `section_type`, `page`) plus a **stored generated**
   `tsvector` column (`tsv`) with a **GIN index**, so Postgres maintains the
   index automatically on every write and a query never risks staleness
   against manually-maintained tokens.
2. **`index_chunks_fts(session, store, doc_id)`** (`fdi.pipeline.retrieval`) —
   reads the `chunks` artifact and upserts it into `chunk`, idempotently.
   Triggered synchronously via `POST /documents/{doc_id}/index_fts` (decision 6
   — a genuine fork from ocr/semantic/embedding/graph_load's queued-Celery
   shape, documented below).
3. **`keyword_search(session, query, k, section_type, doc_key, version)`**
   (`fdi.pipeline.retrieval`) — `plainto_tsquery` + `ts_rank` against `chunk`,
   filterable, returning hits shaped as full citations:
   `{doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
   score}`. `POST /retrieve/keyword` exposes it.

Same discipline as every prior sub-project: thin slice, pure-core-first where
a pure transform actually exists, mockable/testable without heavy infra
(Postgres is real in the default suite — no fake needed here, unlike Milvus's
`FakeVectorStore`).

## 2. Scope decisions (autonomous; documented)

1. **`chunk.id` reuses the unified chunk identity `"{doc_id}:{chunk_id}"`
   (roadmap cross-cutting decision 1) as its own primary key**, not a fresh
   UUID. It is simultaneously the natural key, the row identity, and the
   `ON CONFLICT` target — re-running `index_chunks_fts` for a reprocessed
   document is a plain idempotent upsert keyed on the same id Milvus already
   uses for its vectors, so a keyword hit id and a vector hit id are
   trivially comparable (useful groundwork for RT-4's fusion, though RT-4 is
   out of scope here).
2. **`doc_key`/`version` are denormalized onto every `chunk` row at write
   time**, not joined from `Document` at query time. RT-1 deliberately kept
   these OUT of Milvus metadata because `StageContext` has no Postgres access
   (RT-1 §2.6); `index_chunks_fts` is a **pipeline-layer function**, which DOES
   have a session (it is handed one, same as `resolve_entities`), so it reads
   `Document.doc_key`/`.version` once per call and copies them onto every row.
   The payoff: `keyword_search`'s `doc_key`/`version` filters are plain
   `WHERE` clauses on `chunk` — no join to `document` on the hot path.
3. **The generated column indexes `text` alone: `to_tsvector('english',
   text)`, not `heading || ' ' || text`.** RT-1 guarantees `chunks[i].text ==
   heading + "\n" + "\n".join(paragraphs)` — the heading is already the first
   line of `text` by construction. Concatenating `heading` again would just
   double-weight the same lexemes in `ts_rank`'s frequency term for no
   benefit; indexing `text` alone captures it once, correctly, with no
   `coalesce` needed (`text` is `NOT NULL` — RT-1's `build_chunks` never
   omits it, defaulting to `""` at worst). `heading` stays a separate,
   plain (non-indexed-again) column purely for citation display.
4. **The `tsvector` column is a Postgres `GENERATED ALWAYS AS (...) STORED`
   column** (SQLAlchemy `Computed(..., persisted=True)`), not a value
   `index_chunks_fts` computes and inserts itself. Postgres recomputes it on
   every `INSERT`/`UPDATE` to `text`, so it can never drift from the row it
   indexes, and the upsert in decision 6 never touches `tsv` at all — a
   generated column cannot be targeted by `INSERT`/`UPDATE`'s column list,
   only read back. A **GIN index** on `tsv` (`ix_chunk_tsv`) is what makes
   `@@` queries fast at more than a handful of rows; without it every keyword
   search is a sequential scan.
5. **Query function uses `plainto_tsquery`, not `to_tsquery`.** `to_tsquery`
   requires the caller to speak `tsquery` operator syntax (`&`, `|`, `!`,
   `<->`) and **raises a syntax error** on ordinary free text containing a
   stray `&` or unbalanced parenthesis — exactly the kind of input an
   HTTP/LLM caller will send. `plainto_tsquery('english', query)` treats the
   whole string as an AND of its lexemes and never raises on arbitrary text
   (including `""`, which produces an empty `tsquery` that matches nothing —
   `keyword_search("")` returns `[]`, not an error; verified in §5). Both
   functions use the SAME `'english'` config as `tv`'s generation expression
   — a config mismatch between generation and query would silently degrade
   matching (different stemming), so this pairing is deliberate, not
   incidental.
6. **Population is a synchronous, task-free route
   (`POST /documents/{doc_id}/index_fts`), not a Celery-dispatched trigger —
   THE genuine design fork in this spec, flagged for review.** `index_chunks_fts`
   makes no external API call (no Azure DI, no Azure OpenAI, no Milvus, no
   Neo4j) — it reads one already-cached MinIO artifact and writes to Postgres,
   the same cost profile as `POST /search` and `POST /entities/resolve`, both
   of which are already synchronous "task-free" routes in this codebase
   (`routes.py`'s own section comments name them that). `ocr`/`semantic`/
   `embedding`/`graph_load` are queued through `StageDispatcher` → Celery
   precisely BECAUSE they call a slow external service and must not block the
   request; that reason does not apply here. Three shapes were considered:
   - **(chosen) A dedicated synchronous route**, `SessionDep` + `StoreDep`,
     calling `index_chunks_fts` inline and committing before returning. Wins
     on simplicity (no Celery/broker involvement, testable with plain httpx,
     no `run_stage_task` special-casing) and precedent-fit (`/search`,
     `/entities/resolve`).
   - **A queued trigger mirroring ocr/semantic/embedding/graph_load**
     (`dispatcher.dispatch(doc_id, "index_fts", correlation_id)` through a NEW
     Celery task built like `run_stage_task` but writing to Postgres instead
     of MinIO). Consistent with the trigger-route family's *shape*, but adds a
     whole Celery round-trip and a bespoke non-`Stage` task for work that
     costs single-digit milliseconds — asynchrony bought for a cost that
     doesn't exist here.
   - **Auto-fold into the `chunks` stage's successor dispatch** in
     `workers/tasks.py::run_stage_task` (`if stage_name == "chunks":
     await index_chunks_fts(...)` before enqueuing successors) — fully
     automatic, no manual call ever needed. Rejected for THIS slice because it
     special-cases one stage name inside the generic "run this stage, dispatch
     successors" worker loop, blurring the line between the reusable Stage
     runner and a `chunks`-specific side effect; a human may reasonably prefer
     this once RT-3 stabilizes (it removes a manual step from every future
     ingestion flow) — noted as a natural follow-on, not built now.
   A human should confirm the chosen shape (explicit manual trigger, like
   ocr/semantic/embedding/graph_load conceptually but synchronous like
   /search) is what's wanted for how RT-8's agent / a future ingestion
   orchestrator is meant to call the retrieval layer, versus the fully-
   automatic alternative.
7. **`index_chunks_fts` degrades gracefully, never raises** — same posture as
   `resolve_chunk`. Unknown `doc_id` → `0` (no row touched). Document with no
   `chunks` artifact yet (upstream stage not run) → `0`. This makes the route
   safe to call repeatedly/early without a client having to pre-check
   readiness, and keeps its shape symmetric with `resolve_chunk`'s `None` on
   the unresolvable case.
8. **A NEW pure function, `fdi.core.retrieval.chunk_rows_for_index`, is
   extracted** for the defensive `chunks` → upsert-row coercion (mirrors every
   `isinstance` guard already established in `build_chunks`): given `doc_id`,
   `doc_key`, `version`, and the `chunks` artifact's records, produce the flat
   dicts `index_chunks_fts` upserts. This is the one piece of RT-3 that IS a
   pure transform (RT-1's own "thin slice, pure-core-first" habit) — the rest
   of RT-3 (the upsert itself, the `tsquery`/`ts_rank` search) is Postgres
   I/O by nature and is not artificially split into a "core" layer that would
   just re-describe SQL in Python and risk drifting from what Postgres
   actually does. This mirrors RT-1's `IndexStage` v2, which likewise added no
   new core function for its (trivial) metadata-merge step.
9. **No new `FDI_` config.** `database_url` is already a *required* Settings
   field (unlike `milvus_uri`/`neo4j_uri`, which are optional so the stack
   boots without those profiles) — so unlike `/search`, `/retrieve/keyword`
   and `/documents/{id}/index_fts` need no "service not configured" 503
   branch; Postgres is always present by construction.

## 3. Architecture

### 3.1 `fdi.core.retrieval.chunk_rows_for_index` (pure, new)

```python
def chunk_rows_for_index(
    *,
    doc_id: str,
    doc_key: str | None,
    version: int | None,
    chunks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """From RT-1's `chunks` artifact records (`{chunk_id, heading, text,
    section_type, page, ...}`), build the flat rows `index_chunks_fts` upserts
    into `chunk`. Pure; degrades per-record (a record with a non-str/empty
    `chunk_id` is skipped; `heading`/`section_type`/`page` fall back to None;
    `text` falls back to ""), never raises."""
```

Skips a record whose `chunk_id` is not a non-empty `str` (defensive; RT-1's
`build_chunks` never emits such a record, but `core` must not assume an
upstream invariant it cannot see). Every emitted row carries the SAME
`doc_id`/`doc_key`/`version` — the join RT-1 could not do at the Milvus layer,
done here where a Postgres session is available.

### 3.2 `chunk` table (`fdi.platform.db.models`, new `Chunk` model)

```python
class Chunk(Base):
    """RT-3: Postgres full-text keyword index. One row per RT-1 `chunks`
    artifact record, upserted by `fdi.pipeline.retrieval.index_chunks_fts`.

    `id` reuses the unified chunk identity `f"{doc_id}:{chunk_id}"` (roadmap
    cross-cutting decision 1) as PK and upsert conflict target -- reprocessing
    a document is a plain idempotent upsert. `doc_key`/`version` are
    denormalized from `Document` at write time (§2 decision 2) so a
    keyword-filtered query needs no join. `tsv` is a Postgres STORED
    generated column over `text` alone (§2 decision 3) -- Postgres maintains
    it on every insert/update; `index_chunks_fts` never computes or targets
    it directly (a generated column cannot appear in an INSERT/UPDATE column
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

`onupdate=func.now()` fires for ORM-driven updates only; the raw Core
`ON CONFLICT DO UPDATE` in §3.3 does NOT go through the ORM unit-of-work, so
`index_chunks_fts` sets `updated_at: func.now()` explicitly in its `set_`
clause (noted so the two are not assumed redundant).

### 3.3 Alembic migration `0006_chunk_fts.py` (chains from head `0005`)

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

Postgres 18.4 (the testcontainers image already pinned in
`tests/integration/conftest.py`) supports stored generated columns since PG12
— no version gap to worry about; `alembic/env.py` needs no change (it already
reads `Base.metadata` from `fdi.platform.db.models`, so the new `Chunk` class
is picked up by import alone; the migration itself is hand-written, matching
every prior migration in this repo, not autogenerated).

### 3.4 `fdi.pipeline.retrieval.index_chunks_fts` (new, appended to the
existing RT-1 module)

```python
async def index_chunks_fts(*, session: AsyncSession, store: ArtifactStore, doc_id: str) -> int:
    """Upsert `doc_id`'s `chunks` artifact into the `chunk` table. Returns the
    number of rows upserted (0 if the document is unknown or has no `chunks`
    artifact yet -- degrades, never raises, mirroring resolve_chunk). Caller
    owns the transaction (commits), same convention as every pipeline
    function in this module and fdi.pipeline.crossdoc.resolve_entities."""
    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    if doc is None:
        return 0
    chunks = await _read_chunks(repo, store, doc_id)
    rows = chunk_rows_for_index(doc_id=doc_id, doc_key=doc.doc_key, version=doc.version, chunks=chunks)
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

Reuses RT-1's private `_read_chunks(repo, store, doc_id)` helper already in
this module (same artifact read as `resolve_chunk`) — no new artifact-reading
code path.

### 3.5 `fdi.pipeline.retrieval.keyword_search` (new)

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
    on free text, unlike to_tsquery) + ts_rank, ordered best-first, optionally
    filtered by section_type/doc_key/version. Returns citation-shaped hits:
    {doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
    score} -- text + full citation in one record, no second resolve step
    (unlike a Milvus hit, chunk rows already carry the text)."""
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

### 3.6 HTTP surface (`fdi.api`)

`fdi.api.schemas`: `IndexFtsView{doc_id, chunks_indexed}`,
`KeywordSearchRequest{query, k=5, section_type=None, doc_key=None,
version=None}`, `KeywordHitView{doc_id, doc_key, version, chunk_id, heading,
page, section_type, text, score}`, `KeywordSearchResultsView{results}`.

`fdi.api.routes`:

```python
@router.post("/documents/{doc_id}/index_fts", response_model=IndexFtsView)
async def trigger_index_fts(doc_id: str, session: SessionDep, store: StoreDep) -> IndexFtsView:
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

Both routes are synchronous (§2 decision 6) — no `CorrelationIdDep`/
`DispatcherDep`, no `202 Accepted`, default `200`. `trigger_index_fts` 404s
only on an unknown DOCUMENT (mirrors `trigger_ocr`/`trigger_semantic`/
`trigger_embedding`/`trigger_graph_load`); a document with no `chunks`
artifact yet is NOT a 404 — it 200s with `chunks_indexed: 0` (§2 decision 7).

## 4. Error handling

- `chunk_rows_for_index`: a record with a missing/non-`str`/empty `chunk_id`
  is skipped. `heading`/`section_type` default to `None` on a non-`str`
  value; `page` defaults to `None` on non-`int`/`bool` value (same
  `_int_or_none`-shaped guard as `build_chunks`); `text` defaults to `""` on
  a non-`str` value. Never raises. `chunk_rows_for_index(doc_id="d", doc_key=None,
  version=None, chunks=[])` → `[]`.
- `index_chunks_fts`: unknown `doc_id` → `0`, no write. Document with no
  `chunks` artifact (or an empty one) → `0`, no write. A re-run with an
  unchanged `chunks` artifact re-upserts the same values (idempotent, no
  duplicate rows — the `id` PK/conflict target guarantees this). Never raises
  for these cases; a genuine Postgres error (e.g. connection loss) propagates
  — this function does not swallow infrastructure failures, only "nothing to
  index" states.
- `keyword_search`: an empty or whitespace-only `query` → `plainto_tsquery`
  produces an empty `tsquery`, which matches nothing under `@@` — returns
  `[]`, not an error (verified in §5, not assumed). A `k <= 0` is not
  special-cased (falls straight into `LIMIT`, which Postgres treats as "no
  rows" for `0` and errors for negative — `KeywordSearchRequest.k` is left
  unconstrained at the pydantic layer to match `SearchRequest.k`'s existing
  precedent of no `Field(ge=...)`; a negative `k` reaching Postgres as
  `LIMIT -1` raises, surfacing as a 500 — accepted for this slice, same
  posture as `/search`, which validates `k` no more strictly).
- `trigger_index_fts` route: unknown document → 404. Everything else 200,
  including the "0 chunks indexed" case.
- `keyword_search_route`: never 404s or 503s — Postgres is mandatory config
  (§2 decision 9); a corpus with zero indexed chunks yields `{"results": []}`.

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_retrieval_chunk_rows.py`:
  - A 2-chunk `chunks` list + `doc_key`/`version` → two rows with `id ==
    f"{doc_id}:{chunk_id}"`, correct `doc_key`/`version` copied onto both.
  - A record with a non-`str` `chunk_id` is skipped; the rest still emit.
  - `heading`/`section_type` non-`str` → `None`; `page` non-`int`/`bool` →
    `None`; `text` non-`str` → `""`.
  - `chunk_rows_for_index(doc_id="d", doc_key=None, version=None, chunks=[])`
    → `[]`.
- **Integration** (real Postgres via `migrated_engine`), additions to
  `backend/tests/integration/test_migrations.py`:
  - `test_alembic_version_is_stamped` → bump expected value `"0005"` →
    `"0006"`.
  - `test_chunk_tsv_is_generated_and_searchable`: raw `INSERT INTO chunk
    (id, doc_id, chunk_id, text) VALUES (...)` (via a seeded `document` row
    for the FK), then `SELECT id FROM chunk WHERE tsv @@
    plainto_tsquery('english', 'balance sheet')` returns the expected row and
    NOT an unrelated one — proves the generated column populates without
    `index_chunks_fts` ever writing to it.
  - `test_chunk_tsv_index_is_gin`: query `pg_indexes` for `indexname =
    'ix_chunk_tsv' AND indexdef ILIKE '%USING gin%'` — proves the GIN index
    exists as declared, not just "an index".
  - `test_chunk_doc_id_fk_enforced`: inserting a `chunk` row with a
    `doc_id` that matches no `document.id` raises `IntegrityError` (same
    pattern as `test_eval_run_and_case_result_exist`'s FK check).
- **Integration** (testcontainers + fakes),
  `backend/tests/integration/test_index_chunks_fts.py`: reuse
  `test_resolve_chunk.py`'s `processed_doc` fixture verbatim (intake + run
  `ocr → … → chunks` with `FakeOcrClient`/`FakeSemanticClient`).
  - `index_chunks_fts` on a `processed_doc` returns the chunk count and each
    row (read back via a plain `select(Chunk)`) carries the SAME
    `doc_key`/`version` as the seeded `Document`, plus `heading`/`text`/
    `section_type`/`page` matching the `chunks` artifact.
  - Calling it TWICE on the same `processed_doc` is idempotent: same row
    count, no duplicates (`SELECT count(*) FROM chunk WHERE doc_id = :id`
    unchanged across both calls).
  - Unknown `doc_id` → `0`, no row written.
  - A `Document` with no `chunks` artifact (create one via
    `DocumentRepository.create` directly, skip the stage chain) → `0`, no row
    written.
- **Integration**, `backend/tests/integration/test_keyword_search.py`: seed
  `chunk` rows via `index_chunks_fts` over one or two `processed_doc`-shaped
  fixtures with distinct `section_type`s (e.g. `BALANCE_SHEET` vs
  `DIRECTORS_REPORT`, reusing the same fixture text as `test_resolve_chunk.py`).
  - A query matching the Balance Sheet chunk's distinctive text ranks it
    first (`ts_rank` ordering, asserted by which `chunk_id` comes first, not
    a specific float).
  - `section_type="BALANCE_SHEET"` filter excludes the Directors' Report
    chunk even if it would otherwise match.
  - `doc_key=`/`version=` filters scope to one document's rows.
  - `k=1` returns exactly one hit even when more match.
  - `query=""` → `[]` (pins §4's empty-tsquery behaviour against a REAL
    Postgres, not assumed from documentation).
  - A hit's shape has exactly the nine documented keys, `score` is a `float`
    and the top hit's score is `> 0.0`.
- **Integration** (HTTP), additions to `backend/tests/integration/test_api.py`:
  - `test_trigger_index_fts_on_a_document`: build a `processed_doc`-equivalent
    fixture through this file's own `session_factory`/`store` (mirroring
    `resolved_corpus_via_api`'s pattern one layer up — direct `run_stage`
    calls through `ocr → … → chunks`, not the queued triggers, since nothing
    in this suite runs a worker), then `POST /documents/{id}/index_fts` → 200,
    `chunks_indexed == 2` (or whatever the fixture's chunk count is).
  - `test_trigger_index_fts_unknown_document_is_404`.
  - `test_trigger_index_fts_no_chunks_artifact_returns_zero`: a plain
    `DocumentRepository.create`'d document with no stage runs → 200,
    `chunks_indexed == 0`.
  - `test_keyword_search_route_returns_hits`: after indexing the fixture
    above, `POST /retrieve/keyword` with a matching query → 200, the expected
    `chunk_id` present with `score > 0`, `text` non-empty.
  - `test_keyword_search_route_filters_by_section_type`: same corpus, a
    `section_type` filter narrows the result set (membership assertion over
    THIS fixture's own doc/chunk ids, per the corpus-scale precedent in
    `test_entities_resolve_returns_entities` — Postgres is session-scoped
    across the whole integration suite, so an exact total count is not
    stable).
  - `test_keyword_search_route_no_match_returns_empty_results`.

## 6. Explicitly deferred (YAGNI)

- **Automatic/queued triggering of `index_chunks_fts`** (the rejected
  alternatives in §2 decision 6) — the manual synchronous route is enough for
  RT-3; auto-chaining off the `chunks` stage's successor dispatch is a natural
  follow-on once the manual shape is validated, not built now.
- **`to_tsquery`/advanced query syntax** exposed to the caller (boolean
  operators, phrase search `<->`) — `plainto_tsquery` only, for safety against
  arbitrary free text (§2 decision 5); a power-query mode is a later
  extension, not this slice.
- **Cross-language / multi-config `tsvector`** — `'english'` only, matching
  the corpus's language; per-document language detection is out of scope.
- **RT-4's fusion** (combining `keyword_search` with RT-2's `vector_search`
  via Reciprocal Rank Fusion) — RT-3 ships the keyword half only; `chunk.id`'s
  shared identity with Milvus (§2 decision 1) is deliberate groundwork for it,
  not an implementation of it.
- **Snippet/highlight generation** (`ts_headline`) — `keyword_search` returns
  the full chunk `text`; excerpt highlighting around the matched terms is a
  presentation-layer concern for a future UI, not this API.
- **Deleting/pruning `chunk` rows for a document that is itself deleted** —
  there is no document-delete feature anywhere in this codebase yet; no
  `ON DELETE CASCADE` is added speculatively for a feature that does not
  exist (consistent with `StageRun`/`EvalCaseResult`'s existing FKs, which
  are likewise plain, no cascade).

## 7. Done criteria

1. `fdi.core.retrieval.chunk_rows_for_index(...)` is pure (no I/O, no import
   of `fdi.core.ast`/`semantic`/`provenance`/`platform.*`), degrades per-record
   per §4, never raises; unit-tested per §5 without Docker.
2. The `chunk` table exists via Alembic migration `0006` (chains from head
   `0005`), matches the `Chunk` SQLAlchemy model exactly, `tsv` is a stored
   generated column over `text` with a GIN index (`ix_chunk_tsv`); proven
   against real Postgres 18.4 per §5's migration tests, not merely declared.
3. `index_chunks_fts(session, store, doc_id)` upserts a document's `chunks`
   artifact into `chunk`, denormalizing `doc_key`/`version`; idempotent on
   repeat calls; degrades to `0` (never raises) for an unknown document or a
   missing `chunks` artifact, per §4.
4. `keyword_search(session, query, k, section_type, doc_key, version)`
   returns `ts_rank`-ordered, citation-shaped hits
   (`{doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
   score}`), filterable by all three named filters, `[]` on no match or an
   empty query, never raises for these cases.
5. `POST /documents/{doc_id}/index_fts` (200, `IndexFtsView`, 404 only for an
   unknown document) and `POST /retrieve/keyword` (200,
   `KeywordSearchResultsView`, never 503) are registered and exercised
   end-to-end via `httpx` against the real containers per §5.
6. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/0-
   broken, full suite); §2 decision 6's design fork is recorded in this spec
   for human review; deferred items (§6) remain out of scope; the retrieval
   roadmap's RT-3 row flips to done with a short "what RT-3 shipped" handoff
   (the `chunk` table shape, `chunk.id`'s shared identity with Milvus,
   `index_chunks_fts`/`keyword_search` as RT-4's second fusion input).
