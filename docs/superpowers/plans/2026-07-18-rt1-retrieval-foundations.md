# RT-1 Retrieval Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the retrieval foundation — a per-document `chunks` artifact that resolves any hit id `s{i}` to its text + section_type + page, enriched Milvus metadata, and `resolve_chunk`/`resolve_chunks` helpers every later retrieval tool uses.

**Architecture:** A pure `fdi.core.retrieval.build_chunks` joins `ast` (text) + `semantic` (type) + `provenance` (page) under the shared `s{i}` identity (mirroring `EmbeddingStage`'s enumerate-and-skip). A `ChunksStage` (`deps=("ast","semantic","provenance")`) materializes it. `IndexStage` bumps to v2 (`deps=("embedding","chunks")`) to enrich Milvus metadata. `fdi.pipeline.retrieval.resolve_chunk(s)` reads the artifact to turn hit ids into text+citation.

**Tech Stack:** Python 3.13, `uv`, SQLAlchemy async, pytest + testcontainers. Reuses ingestion artifacts already in the store.

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive), CI-enforced; stay "1 kept, 0 broken". `fdi.core.retrieval` is PURE: no I/O/config/clock/RNG, and no import of `fdi.core.ast`/`semantic`/`provenance` (read inputs as plain `Mapping`).
- Determinism: no wall-clock/RNG; chunk order follows `ast` section order.
- **Chunk identity `s{i}` MUST mirror `EmbeddingStage`** (`backend/src/fdi/pipeline/stages/embedding.py`): `for i, section in enumerate(ast["sections"])`, skip non-`dict` (consumes index `i`, emits nothing). Never re-index/compact — a Milvus hit `{doc_id}:s{i}` must join back to `chunks[…].chunk_id == "s{i}"`.
- `build_chunks` never raises; malformed/partial input degrades per-field (`section_type`/`page` → `None`).
- Per-task gates before every commit: `uv run --directory backend ruff check .` · `uv run --directory backend mypy --strict src` · `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter` · the task's pytest. Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) → commit.

---

### Task 1: `fdi.core.retrieval.build_chunks` (pure join)

**Files:**
- Create: `backend/src/fdi/core/retrieval.py`
- Test: `backend/tests/unit/test_retrieval_chunks.py`

**Interfaces:**
- Produces: `build_chunks(ast: Mapping[str, object], semantic: Mapping[str, object], provenance: Mapping[str, object]) -> dict[str, object]` → `{schema_version:1, chunks:[{chunk_id, heading, text, section_type, page, para_count}]}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_retrieval_chunks.py
from fdi.core.retrieval import build_chunks


def _ast(sections: list[object]) -> dict[str, object]:
    return {"schema_version": 1, "title": "T", "sections": sections}


def test_build_chunks_joins_text_type_page() -> None:
    ast = _ast([
        {"heading": "Balance Sheet", "paragraphs": ["Assets 100", "Liab 40"]},
        {"heading": "Notes", "paragraphs": ["Note 1"]},
    ])
    semantic = {"sections": [{"type": "BALANCE_SHEET"}, {"type": "NOTES"}]}
    provenance = {"node_provenance": [
        {"node_id": "s0", "page": 3}, {"node_id": "s1", "page": 7},
    ]}
    out = build_chunks(ast, semantic, provenance)
    assert out["schema_version"] == 1
    assert out["chunks"] == [
        {"chunk_id": "s0", "heading": "Balance Sheet",
         "text": "Balance Sheet\nAssets 100\nLiab 40",
         "section_type": "BALANCE_SHEET", "page": 3, "para_count": 2},
        {"chunk_id": "s1", "heading": "Notes",
         "text": "Notes\nNote 1", "section_type": "NOTES", "page": 7, "para_count": 1},
    ]


def test_text_matches_embedding_construction() -> None:
    # byte-identical to EmbeddingStage: heading + "\n" + "\n".join(paragraphs)
    heading, paragraphs = "H", ["a", "b"]
    ast = _ast([{"heading": heading, "paragraphs": paragraphs}])
    out = build_chunks(ast, {}, {})
    assert out["chunks"][0]["text"] == heading + "\n" + "\n".join(paragraphs)


def test_chunk_id_mirrors_enumerate_skip() -> None:
    # index 1 is a non-dict -> skipped, consumes the index; next real section is s2
    ast = _ast([
        {"heading": "A", "paragraphs": []},
        "not-a-dict",
        {"heading": "C", "paragraphs": []},
    ])
    out = build_chunks(ast, {}, {})
    assert [c["chunk_id"] for c in out["chunks"]] == ["s0", "s2"]


def test_section_type_degrades_to_none() -> None:
    ast = _ast([{"heading": "A", "paragraphs": []}, {"heading": "B", "paragraphs": []}])
    semantic = {"sections": [{"type": 123}]}  # non-str type, and only one entry
    out = build_chunks(ast, semantic, {})
    assert [c["section_type"] for c in out["chunks"]] == [None, None]


def test_page_degrades_to_none_when_provenance_missing() -> None:
    ast = _ast([{"heading": "A", "paragraphs": []}, {"heading": "B", "paragraphs": []}])
    provenance = {"node_provenance": [{"node_id": "s0", "page": 5}]}  # s1 absent
    out = build_chunks(ast, {}, provenance)
    assert [c["page"] for c in out["chunks"]] == [5, None]


def test_empty_inputs() -> None:
    assert build_chunks({}, {}, {}) == {"schema_version": 1, "chunks": []}


def test_none_heading_kept_as_none_but_text_uses_empty() -> None:
    ast = _ast([{"heading": None, "paragraphs": ["x"]}])
    out = build_chunks(ast, {}, {})
    assert out["chunks"][0]["heading"] is None
    assert out["chunks"][0]["text"] == "\nx"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_chunks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.retrieval'`.

- [ ] **Step 3: Write the implementation**

```python
# backend/src/fdi/core/retrieval.py
"""Retrieval foundations (pure; no I/O, config, clock, RNG).

build_chunks joins a document's `ast` (section body text), `semantic`
(section_type), and `provenance` (page) into one per-document `chunks` record --
one chunk per AST section, under the shared identity `s{i}` that EmbeddingStage/
IndexStage/Milvus already use. Every text-returning retrieval tool resolves a hit
id back to text via this record. Reads inputs as plain Mappings -- no import of
fdi.core.ast/semantic/provenance. Never raises: malformed/partial input degrades
per-field (section_type/page -> None).
"""

from collections.abc import Mapping


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build_chunks(
    ast: Mapping[str, object],
    semantic: Mapping[str, object],
    provenance: Mapping[str, object],
) -> dict[str, object]:
    page_by_node: dict[str, int] = {}
    node_prov = provenance.get("node_provenance")
    if isinstance(node_prov, list):
        for np in node_prov:
            if not isinstance(np, Mapping):
                continue
            node_id = np.get("node_id")
            page = _int_or_none(np.get("page"))
            if isinstance(node_id, str) and page is not None:
                page_by_node[node_id] = page

    sem_raw = semantic.get("sections")
    sem_sections = sem_raw if isinstance(sem_raw, list) else []

    sections_raw = ast.get("sections")
    sections = sections_raw if isinstance(sections_raw, list) else []

    chunks: list[dict[str, object]] = []
    k = 0  # position among EMITTED chunks (indexes into sem_sections)
    for i, section in enumerate(sections):
        if not isinstance(section, Mapping):
            continue  # consumes index i (keeps s{i} aligned with EmbeddingStage), emits nothing
        heading_raw = section.get("heading")
        heading = heading_raw if isinstance(heading_raw, str) else ""
        paragraphs_raw = section.get("paragraphs")
        paragraphs = (
            [p for p in paragraphs_raw if isinstance(p, str)]
            if isinstance(paragraphs_raw, list)
            else []
        )
        chunk_id = f"s{i}"
        text = heading + "\n" + "\n".join(paragraphs)

        section_type: str | None = None
        if k < len(sem_sections) and isinstance(sem_sections[k], Mapping):
            t = sem_sections[k].get("type")
            section_type = t if isinstance(t, str) else None

        chunks.append({
            "chunk_id": chunk_id,
            "heading": heading_raw if isinstance(heading_raw, str) else None,
            "text": text,
            "section_type": section_type,
            "page": page_by_node.get(chunk_id),
            "para_count": len(paragraphs),
        })
        k += 1

    return {"schema_version": 1, "chunks": chunks}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_chunks.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/retrieval.py backend/tests/unit/test_retrieval_chunks.py
git commit -m "feat(core): retrieval.build_chunks -- join ast+semantic+provenance into chunk records"
```

---

### Task 2: `ChunksStage`

**Files:**
- Create: `backend/src/fdi/pipeline/stages/chunks.py`
- Modify: `backend/src/fdi/pipeline/stages/__init__.py` (register `chunks`)
- Test: `backend/tests/integration/test_chunks_stage.py`

**Interfaces:**
- Consumes: `build_chunks` (Task 1); `StageContext`; `REGISTRY`.
- Produces: `ChunksStage` (`name="chunks"`, `version=1`, `deps=("ast","semantic","provenance")`); a `chunks` artifact.

**Pre-check (do first, document finding in the stage docstring):** Read `backend/src/fdi/pipeline/stages/semantic.py` and confirm `SemanticStage` emits one `sections` entry per counted AST section in AST order (so RT-1's positional `section_type` alignment holds). If it filters/reorders, note it — the `None` fallback keeps output well-formed regardless.

- [ ] **Step 1: Write the failing test**

Reuse the harness from `backend/tests/integration/test_knowledge_stage.py` (a LOCAL `StageRegistry` with `OcrStage`+`NormalizeStage`+`AstStage`+`StructureStage`+`SemanticStage`+`GraphStage`+`ProvenanceStage`+`ChunksStage`, `FakeOcrClient`+`FakeSemanticClient`, driven via `run_stage`). Match its fixture names verbatim.

```python
# backend/tests/integration/test_chunks_stage.py
import pytest

pytestmark = pytest.mark.integration

# ... (build the local registry + fakes exactly as test_knowledge_stage.py does,
#      but register ChunksStage and its deps: ast, semantic, provenance + their deps)


async def test_chunks_stage_produces_records(session_factory, store) -> None:
    # intake a FakeOcr doc with >=2 headed sections; run_stage("ocr", ...) to fan out.
    # read the `chunks` artifact via latest_run -> get_json and assert each record has
    # chunk_id "s{i}", the section text, a section_type (or None), page (or None), para_count.
    ...


async def test_chunks_is_successor_of_all_three_deps() -> None:
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver
    import fdi.pipeline.stages  # noqa: F401 - registers built-ins
    resolver = DagResolver(REGISTRY.all())
    assert "chunks" in resolver.successors("ast")
    assert "chunks" in resolver.successors("semantic")
    assert "chunks" in resolver.successors("provenance")
```

> Fill the first test body using `test_knowledge_stage.py` as the template (same intake, same `run_stage`, same artifact-read helper, same `artifact_key(doc.id, "chunks", 1, derived_input_hash([...]))` or the read-via-latest_run helper it uses). Assert against the concrete FakeOcr fixture's sections.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_chunks_stage.py -v`
Expected: FAIL — `ModuleNotFoundError: fdi.pipeline.stages.chunks` / `chunks` not a successor.

- [ ] **Step 3: Implement the stage + register**

```python
# backend/src/fdi/pipeline/stages/chunks.py
"""ChunksStage: the RT-1 retrieval foundation. A DERIVED stage joining `ast`,
`semantic`, and `provenance` into a per-document `chunks` artifact -- one record
per AST section (text + section_type + page) under the shared `s{i}` identity.
No injected service, no external call: a pure function of three cached artifacts.

Pre-check finding: SemanticStage emits one `sections` entry per counted AST
section in AST order (see stages/semantic.py), so build_chunks' positional
section_type alignment holds; the None fallback covers any drift.
"""

from collections.abc import Mapping
from typing import cast

from fdi.core.retrieval import build_chunks
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class ChunksStage:
    name = "chunks"
    version = 1
    deps: tuple[str, ...] = ("ast", "semantic", "provenance")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        semantic = cast(Mapping[str, object], await ctx.read("semantic"))
        provenance = cast(Mapping[str, object], await ctx.read("provenance"))
        return build_chunks(ast, semantic, provenance)


REGISTRY.register(ChunksStage())
```

In `backend/src/fdi/pipeline/stages/__init__.py`, add the import (alphabetical, parenthesized, with the same `noqa` comment as its siblings):

```python
from fdi.pipeline.stages import (
    chunks as chunks,  # noqa: F401 - import for its registration side effect
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_chunks_stage.py -v`
Expected: PASS. Also run the whole suite's successor/registry tests to confirm no exact-list `successors(...)` assertion broke (grep `successors(` in tests; make any exact-list check membership).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/stages/chunks.py backend/src/fdi/pipeline/stages/__init__.py backend/tests/integration/test_chunks_stage.py
git commit -m "feat(pipeline): ChunksStage -- per-doc chunk records (ast+semantic+provenance)"
```

---

### Task 3: `IndexStage` v2 — enrich Milvus metadata from `chunks`

**Files:**
- Modify: `backend/src/fdi/pipeline/stages/index.py`
- Test: `backend/tests/integration/test_index_enrichment.py` (+ update any existing `test_index_stage.py` expectations for the new metadata keys / version)

**Interfaces:**
- Consumes: the `chunks` artifact (Task 2); `VectorStore`.
- Produces: `IndexStage` (`version=2`, `deps=("embedding","chunks")`) upserting metadata `{heading, chunk_id, section_type, page}`.

**Pre-check (document in the stage docstring):** Read `backend/src/fdi/platform/vectorstore.py` — confirm `MilvusVectorStore` accepts new scalar metadata keys `section_type`/`page` (str/int/None), or extend its collection metadata columns to include them. `FakeVectorStore` stores arbitrary metadata dicts already.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_index_enrichment.py
import pytest

pytestmark = pytest.mark.integration


async def test_index_upserts_enriched_metadata(session_factory, store) -> None:
    # Drive ocr -> ... -> chunks -> embedding -> index with a FakeVectorStore that
    # records upserted items (reuse test_index_stage.py's fake + harness).
    # Assert: each upserted item id == f"{doc_id}:{chunk_id}", and metadata now has
    # keys heading, chunk_id, section_type, page (section_type/page sourced from the
    # chunks artifact, matching build_chunks output for the fixture).
    ...


async def test_index_is_successor_of_embedding_and_chunks() -> None:
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver
    import fdi.pipeline.stages  # noqa: F401
    resolver = DagResolver(REGISTRY.all())
    assert "index" in resolver.successors("embedding")
    assert "index" in resolver.successors("chunks")
```

> Reuse `test_index_stage.py`'s FakeVectorStore-that-captures-items and harness. Update `test_index_stage.py` itself: its existing metadata assertion (`{heading, chunk_id}`) must become the enriched `{heading, chunk_id, section_type, page}`, and any `version==1` assertion for `index` → `2`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_index_enrichment.py tests/integration/test_index_stage.py -v`
Expected: FAIL — metadata lacks `section_type`/`page`; `index` not a successor of `chunks`.

- [ ] **Step 3: Implement v2**

Replace the class body in `backend/src/fdi/pipeline/stages/index.py`:

```python
class IndexStage:
    name = "index"
    version = 2
    deps: tuple[str, ...] = ("embedding", "chunks")

    async def run(self, ctx: StageContext) -> object:
        embedding = cast(Mapping[str, object], await ctx.read("embedding"))
        chunks_art = cast(Mapping[str, object], await ctx.read("chunks"))
        store = cast(VectorStore, ctx.services["vector_store"])

        chunks_raw = embedding.get("chunks")
        chunks = (
            [c for c in chunks_raw if isinstance(c, dict)] if isinstance(chunks_raw, list) else []
        )

        meta_raw = chunks_art.get("chunks")
        meta_by_chunk: dict[str, Mapping[str, object]] = {
            cid: c
            for c in (meta_raw if isinstance(meta_raw, list) else [])
            if isinstance(c, Mapping) and isinstance((cid := c.get("chunk_id")), str)
        }

        items: list[Mapping[str, object]] = []
        for chunk in chunks:
            meta = meta_by_chunk.get(str(chunk["chunk_id"]))
            items.append({
                "id": f"{ctx.doc_id}:{chunk['chunk_id']}",
                "vector": chunk["vector"],
                "metadata": {
                    "heading": chunk.get("heading"),
                    "chunk_id": chunk["chunk_id"],
                    "section_type": meta.get("section_type") if meta is not None else None,
                    "page": meta.get("page") if meta is not None else None,
                },
            })

        upserted = cast(int, store.upsert(items)["upserted"]) if items else 0
        return {"schema_version": 1, "upserted": upserted}
```

(Keep the module docstring; append the pre-check finding. `cid := c.get("chunk_id")` uses a walrus in the comprehension guard — if ruff/mypy dislike it, expand to a small loop building `meta_by_chunk`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_index_enrichment.py tests/integration/test_index_stage.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/stages/index.py backend/tests/integration/test_index_enrichment.py backend/tests/integration/test_index_stage.py
git commit -m "feat(pipeline): IndexStage v2 -- enrich Milvus metadata with section_type+page from chunks"
```

---

### Task 4: `resolve_chunk` / `resolve_chunks` (`fdi.pipeline.retrieval`)

**Files:**
- Create: `backend/src/fdi/pipeline/retrieval.py`
- Test: `backend/tests/integration/test_resolve_chunk.py`

**Interfaces:**
- Consumes: `RegistryRepository.latest_run` + `ArtifactStore.get_json` (existing); the `chunks` artifact.
- Produces:
  - `resolve_chunk(*, session: AsyncSession, store: ArtifactStore, doc_id: str, chunk_id: str) -> dict[str, object] | None`
  - `resolve_chunks(*, session: AsyncSession, store: ArtifactStore, ids: list[str]) -> dict[str, dict[str, object]]` (ids of the form `"{doc_id}:{chunk_id}"`; reads each doc's `chunks` artifact once).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_resolve_chunk.py
import pytest

from fdi.pipeline.retrieval import resolve_chunk, resolve_chunks

pytestmark = pytest.mark.integration


async def test_resolve_chunk_returns_record_with_doc_id(session_factory, store, processed_doc) -> None:
    # `processed_doc` intakes + runs ocr..chunks for one doc; returns its id.
    async with session_factory() as s:
        rec = await resolve_chunk(session=s, store=store, doc_id=processed_doc, chunk_id="s0")
    assert rec is not None
    assert rec["doc_id"] == processed_doc and rec["chunk_id"] == "s0"
    assert "text" in rec and "section_type" in rec and "page" in rec


async def test_resolve_chunk_unknown_returns_none(session_factory, store, processed_doc) -> None:
    async with session_factory() as s:
        assert await resolve_chunk(session=s, store=store, doc_id=processed_doc, chunk_id="s999") is None
        assert await resolve_chunk(session=s, store=store, doc_id="no-such-doc", chunk_id="s0") is None


async def test_resolve_chunks_batch(session_factory, store, processed_doc) -> None:
    ids = [f"{processed_doc}:s0", f"{processed_doc}:s1", "bogus-no-colon", f"{processed_doc}:s999"]
    async with session_factory() as s:
        out = await resolve_chunks(session=s, store=store, ids=ids)
    assert f"{processed_doc}:s0" in out and out[f"{processed_doc}:s0"]["doc_id"] == processed_doc
    assert "bogus-no-colon" not in out and f"{processed_doc}:s999" not in out
```

> `processed_doc` fixture: reuse the Task 2 chunks-producing harness (intake one FakeOcr doc, `run_stage("ocr", ...)`, return `doc.id`); factor it into the integration `conftest.py` if shared with Task 2/3.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_resolve_chunk.py -v`
Expected: FAIL — `ModuleNotFoundError: fdi.pipeline.retrieval`.

- [ ] **Step 3: Implement**

```python
# backend/src/fdi/pipeline/retrieval.py
"""Retrieval orchestrator helpers: turn a hit id `{doc_id}:{chunk_id}` (Milvus,
or later a graph node) into its full RT-1 chunk record (text + citation) by
reading the `chunks` artifact. Pipeline layer (does I/O) -- imports platform
(repo + store) + core, the same shape as resolve_entities. RT-2..RT-8 call
resolve_chunks to attach text to a whole result set in one read per document."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def _read_chunks(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str
) -> list[Mapping[str, object]]:
    run = await repo.latest_run(doc_id, "chunks")
    if run is None or run.artifact_key is None:
        return []
    body = cast(Mapping[str, object], await store.get_json(run.artifact_key))
    chunks = body.get("chunks")
    return [c for c in chunks if isinstance(c, Mapping)] if isinstance(chunks, list) else []


async def resolve_chunk(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, chunk_id: str
) -> dict[str, object] | None:
    repo = RegistryRepository(session)
    for c in await _read_chunks(repo, store, doc_id):
        if c.get("chunk_id") == chunk_id:
            return {"doc_id": doc_id, **c}
    return None


async def resolve_chunks(
    *, session: AsyncSession, store: ArtifactStore, ids: list[str]
) -> dict[str, dict[str, object]]:
    by_doc: dict[str, list[str]] = {}
    for full_id in ids:
        if ":" not in full_id:
            continue
        doc_id, chunk_id = full_id.split(":", 1)
        by_doc.setdefault(doc_id, []).append(chunk_id)

    repo = RegistryRepository(session)
    out: dict[str, dict[str, object]] = {}
    for doc_id, chunk_ids in by_doc.items():
        records = {c.get("chunk_id"): c for c in await _read_chunks(repo, store, doc_id)}
        for chunk_id in chunk_ids:
            rec = records.get(chunk_id)
            if rec is not None:
                out[f"{doc_id}:{chunk_id}"] = {"doc_id": doc_id, **rec}
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_resolve_chunk.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/retrieval.py backend/tests/integration/test_resolve_chunk.py
git commit -m "feat(pipeline): resolve_chunk/resolve_chunks -- hit id -> text+citation via chunks artifact"
```

---

### Task 5: Full gates, roadmap + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-rt1-retrieval-foundations-design.md` (Status → Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (RT-1 row → done + "what RT-1 shipped" handoff)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (1 kept/0 broken). Grep-confirm `fdi.core.retrieval` imports neither `fdi.core.ast`/`semantic`/`provenance`.

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`; green (e2e/azure/neo4j/milvus/bge skip). Note the passed count (prior 341 + RT-1 unit/integration tests).

- [ ] **Step 3: Audit RT-1 done criteria** (§7 of the spec) — map each to the tasks; confirm the `s{i}` alignment test, the enriched-metadata test, and `resolve_chunks` batch-read all pass; confirm both executor pre-checks (SemanticStage ordering, Milvus metadata acceptance) were verified and documented in the stage docstrings.

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps 1–3 green).

- [ ] **Step 5: Roadmap** — flip the RT-1 row to `✅ done`; add a short "What RT-1 shipped that RT-2..RT-8 must know" handoff: the `chunks` artifact shape, the `s{i}` identity contract, `resolve_chunks(ids)` as the text-attach primitive, and the enriched Milvus metadata (`section_type`, `page`; `doc_key`/`version` still a Postgres join in RT-2).

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: RT-1 done-criteria audit, spec status, roadmap handoff"
```

---

## Self-Review

**Spec coverage:** §3.1 `build_chunks` → Task 1. §3.2 `ChunksStage` → Task 2. §3.3 `IndexStage` v2 → Task 3. §3.4 `resolve_chunk`/`resolve_chunks` → Task 4. §2 decisions: identity mirroring (d2) → Task 1 skip-alignment test; text-as-embedded (d3) → Task 1 text test; positional section_type + None fallback (d4) → Task 1 + Task 2 pre-check; page join (d5) → Task 1; index enrichment (d6) → Task 3; resolver in pipeline (d7) → Task 4; no route (d8) → none needed. §4 error handling → Task 1 unit tests + Task 4 unknown/bogus cases. §5 testing → Tasks 1–4. §6 deferred → not built (Task 5 audit). §7 done criteria → Task 5.

**Placeholder scan:** Task 1, 3, 4 carry complete code. Tasks 2 and the fixture bodies in 3/4 intentionally delegate to a NAMED existing harness (`test_knowledge_stage.py`, `test_index_stage.py`) — the executor must read those and reuse their fixtures verbatim rather than invent infra; the integration harness (testcontainers + FakeOcr/FakeSemantic/FakeVectorStore + `run_stage`) already exists and is not re-derived here.

**Type consistency:** `build_chunks(Mapping,Mapping,Mapping)->dict` (Task 1) consumed unchanged by `ChunksStage` (Task 2). `chunks` artifact key `chunk_id`/`section_type`/`page` produced in Task 1 are read by Task 3 (`meta_by_chunk`) and Task 4 (`_read_chunks`) under the same names. `resolve_chunk(...)->dict|None` and `resolve_chunks(...)->dict[str,dict]` (Task 4) are the signatures RT-2 will consume. `IndexStage` id form `{doc_id}:{chunk_id}` unchanged from v1; only metadata grows.

**Risk flagged:** Task 3's `meta_by_chunk` walrus-in-comprehension (`(cid := c.get("chunk_id"))`) — if ruff (B-series) or mypy objects, expand to an explicit loop. Task 3 also re-runs `index` for existing docs (version 1→2, new input_hash) — intended; the enrichment test asserts the new metadata, not a cache hit.
