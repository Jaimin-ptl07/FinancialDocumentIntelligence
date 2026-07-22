# RT-9 BM25 Keyword Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Postgres full-text keyword channel — which returns zero hits for every real query — with Milvus-native BM25, so hybrid retrieval actually fuses two channels instead of silently running vector-only.

**Architecture:** Milvus 2.6 computes BM25 server-side: a `Function` reads an analyzed `VARCHAR` field and writes a `SPARSE_FLOAT_VECTOR`, searched with raw text. That forces the collection to store chunk text, which in turn lets both channels return their own citations and removes the per-search artifact read. Fusion stays in our existing pure `fuse()`.

**Tech Stack:** Python 3.13, pymilvus ≥2.6, Milvus 2.6.20, SQLAlchemy async, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-07-21-rt9-bm25-keyword-retrieval-design.md`

**Baseline:** `main` == `feature/sp-1-project-setup` == `6a20b35`, 811 passed / 9 skipped.

## Global Constraints

- `fdi.core.*` MUST remain pure: no I/O, no config, no clock/RNG, no logging, and no import of `fdi.pipeline.*` / `fdi.platform.*`.
- `fdi.platform.*` MUST NOT import `fdi.pipeline.*`. `MilvusVectorStore` satisfies the `VectorStore` protocol **structurally**, without importing it.
- `fdi.core.retrieval.fuse` MUST NOT be modified. Its existing tests must stay green **unedited** — if a `fuse` test needs changing, the change has escaped scope.
- `rerank_candidates` MUST NOT be modified. Layer attribution measured zero failures attributable to reranking.
- Every Milvus search MUST pass an explicit `output_fields` list. **Never `["*"]`** — Milvus refuses to output a `SPARSE_FLOAT_VECTOR` field, so `["*"]` breaks once `sparse` exists.
- `vector_store` / `embedder` are **required, non-`None`** parameters of the retrieval functions. The `None` check lives in `fdi/api/routes.py` **alone**, which already raises 503 for `/retrieve/vector`.
- No live Milvus in the default suite. Integration tests that need one carry the existing `milvus` marker.
- Run everything from the worktree root. Fast loop: `uv run --directory backend pytest -m "not integration and not e2e" -q`. Gates: `uv run --directory backend ruff check .` and `uv run --directory backend mypy --strict src`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/src/fdi/pipeline/vectorstore.py` | `VectorStore` protocol + `FakeVectorStore` | Modify — add `search_text`, `filter` |
| `backend/src/fdi/platform/vectorstore.py` | Milvus client: schema v2, BM25 function, both searches | Modify |
| `backend/src/fdi/pipeline/stages/index.py` | Writes text + scalars into Milvus | Modify — version 2 → 3 |
| `backend/src/fdi/core/retrieval.py` | `build_vector_results` (pure) | Modify — drop `resolved` |
| `backend/src/fdi/pipeline/retrieval.py` | `bm25_search`, `vector_search`, `hybrid_search` | Modify — delete Postgres keyword path |
| `backend/src/fdi/api/routes.py` | Retrieve routes | Modify — repoint keyword, 503 branch, delete index_fts |
| `backend/src/fdi/pipeline/agent_tools.py` | `search` tool | Modify — drop `store`, guard `None` |
| `backend/src/fdi/pipeline/extractor.py` | `_pool_for` | Modify — drop `store=` |
| `backend/src/fdi/platform/db/models.py` | `Chunk` model | Modify — delete |
| `backend/alembic/versions/0007_drop_chunk.py` | Drop `chunk` table | Create |
| `backend/src/fdi/platform/config.py` | `milvus_collection` default | Modify |

---

### Task 1: `VectorStore` seam gains `search_text` and `filter`

**Files:**
- Modify: `backend/src/fdi/pipeline/vectorstore.py`
- Test: `backend/tests/unit/test_vectorstore.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `VectorStore.search_text(self, query: str, k: int, *, filter: str = "") -> list[dict[str, object]]`
  - `VectorStore.search(self, vector: list[float], k: int, *, filter: str = "") -> list[dict[str, object]]`
  - `FakeVectorStore` implements both; hits are `{"id": str, "score": float, "metadata": dict}`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/unit/test_vectorstore.py`:

```python
def _item(item_id: str, vector: list[float], **metadata: object) -> dict[str, object]:
    return {"id": item_id, "vector": vector, "metadata": metadata}


def test_fake_search_text_scores_by_term_overlap() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("d:s1", [1.0, 0.0], text="revenue from operations total income"),
        _item("d:s2", [0.0, 1.0], text="revenue only"),
        _item("d:s3", [0.0, 1.0], text="unrelated narrative paragraph"),
    ])
    hits = store.search_text("revenue from operations", 10)
    ids = [h["id"] for h in hits]
    # 3-of-3 terms outranks 1-of-3; a zero-overlap chunk is not returned at all
    assert ids[0] == "d:s1"
    assert "d:s2" in ids
    assert "d:s3" not in ids


def test_fake_search_text_respects_k() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("d:s1", [1.0, 0.0], text="alpha beta"),
        _item("d:s2", [0.0, 1.0], text="alpha gamma"),
    ])
    assert len(store.search_text("alpha", 1)) == 1


def test_fake_search_text_empty_query_returns_nothing() -> None:
    store = FakeVectorStore()
    store.upsert([_item("d:s1", [1.0, 0.0], text="alpha")])
    assert store.search_text("   ", 10) == []


def test_fake_search_text_filter_scopes_by_doc_id() -> None:
    """The `in [...]` form is the ONLY filter production emits (see
    `_milvus_filter` in Task 5). The fake must parse exactly that."""
    store = FakeVectorStore()
    store.upsert([
        _item("a:s1", [1.0, 0.0], text="revenue", doc_id="a"),
        _item("b:s1", [0.0, 1.0], text="revenue", doc_id="b"),
    ])
    hits = store.search_text("revenue", 10, filter='doc_id in ["a"]')
    assert [h["id"] for h in hits] == ["a:s1"]


def test_fake_filter_accepts_multiple_doc_ids() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("a:s1", [1.0, 0.0], text="revenue", doc_id="a"),
        _item("b:s1", [0.0, 1.0], text="revenue", doc_id="b"),
        _item("c:s1", [0.0, 1.0], text="revenue", doc_id="c"),
    ])
    hits = store.search_text("revenue", 10, filter='doc_id in ["a", "c"]')
    assert sorted(h["id"] for h in hits) == ["a:s1", "c:s1"]


def test_fake_filter_rejects_a_shape_it_cannot_parse() -> None:
    """A fake that silently matched everything on an unrecognised filter would
    make a broken filter look like a passing test."""
    store = FakeVectorStore()
    store.upsert([_item("a:s1", [1.0, 0.0], text="revenue", doc_id="a")])
    with pytest.raises(ValueError):
        store.search_text("revenue", 10, filter="page > 5")


def test_fake_search_filter_scopes_vector_search_too() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("a:s1", [1.0, 0.0], doc_id="a"),
        _item("b:s1", [1.0, 0.0], doc_id="b"),
    ])
    hits = store.search([1.0, 0.0], 10, filter='doc_id in ["b"]')
    assert [h["id"] for h in hits] == ["b:s1"]


def test_fake_search_without_filter_is_unscoped() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("a:s1", [1.0, 0.0], doc_id="a"),
        _item("b:s1", [1.0, 0.0], doc_id="b"),
    ])
    assert len(store.search([1.0, 0.0], 10)) == 2
```

Import `FakeVectorStore` from `fdi.pipeline.vectorstore` if the file does not already.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/unit/test_vectorstore.py -q`
Expected: FAIL — `AttributeError: 'FakeVectorStore' object has no attribute 'search_text'`

- [ ] **Step 3: Implement**

In `backend/src/fdi/pipeline/vectorstore.py`, extend the protocol:

```python
@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, items: list[Mapping[str, object]]) -> dict[str, object]: ...

    def search(
        self, vector: list[float], k: int, *, filter: str = ""
    ) -> list[dict[str, object]]: ...

    def search_text(
        self, query: str, k: int, *, filter: str = ""
    ) -> list[dict[str, object]]: ...
```

Add the fake's filter helper and `search_text`, and thread `filter` through `search`:

```python
_FILTER_IN_RE = re.compile(r'\s*(\w+)\s+in\s+\[(.*)\]\s*')
_FILTER_EQ_RE = re.compile(r'\s*(\w+)\s*==\s*"([^"]*)"\s*')


def _matches_filter(metadata: Mapping[str, object], filter: str) -> bool:
    """Understands ONLY the expression shapes this codebase emits:
    `field in ["a", "b"]` (what `_milvus_filter` produces) and
    `field == "value"`, joined by ` and `.

    This is a test double, not a query engine -- an unrecognised shape raises,
    loudly. A fake that silently matched everything on a filter it did not
    understand would make a BROKEN filter look like a passing test, which is
    the failure mode this whole sub-project exists to correct."""
    if not filter.strip():
        return True
    for clause in filter.split(" and "):
        m_in = _FILTER_IN_RE.fullmatch(clause)
        if m_in is not None:
            field = m_in.group(1)
            wanted = {v.strip().strip('"') for v in m_in.group(2).split(",") if v.strip()}
            if str(metadata.get(field)) not in wanted:
                return False
            continue
        m_eq = _FILTER_EQ_RE.fullmatch(clause)
        if m_eq is None:
            raise ValueError(f"FakeVectorStore cannot parse filter clause: {clause!r}")
        if str(metadata.get(m_eq.group(1))) != m_eq.group(2):
            return False
    return True


def _tokenize(text: str) -> set[str]:
    return {t for t in re.split(r"\W+", text.casefold()) if t}
```

`search` gains `*, filter: str = ""` and skips non-matching items. `search_text`:

```python
    def search_text(
        self, query: str, k: int, *, filter: str = ""
    ) -> list[dict[str, object]]:
        """Deterministic term-overlap stand-in for Milvus BM25. NOT BM25 -- no
        IDF, no length normalisation -- but monotone in the count of query
        terms present, which is the only property the pipeline layer asserts.
        Zero-overlap items are omitted entirely, matching BM25's behaviour of
        never returning a document that shares no term with the query."""
        terms = _tokenize(query)
        if not terms:
            return []
        scored: list[dict[str, object]] = []
        for item_id, item in self._items.items():
            metadata = dict(cast(Mapping[str, object], item.get("metadata") or {}))
            if not _matches_filter(metadata, filter):
                continue
            overlap = len(terms & _tokenize(str(metadata.get("text", ""))))
            if overlap == 0:
                continue
            scored.append({"id": item_id, "score": float(overlap), "metadata": metadata})
        scored.sort(key=lambda r: cast(float, r["score"]), reverse=True)
        return scored[:k]
```

Add `import re` at the top.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/unit/test_vectorstore.py -q`
Expected: PASS

- [ ] **Step 5: Gates and commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/pipeline/vectorstore.py backend/tests/unit/test_vectorstore.py
git commit -m "feat(pipeline): VectorStore gains search_text + filter

The BM25 keyword channel searches by raw text, not a vector, so the seam
needs a second search method. FakeVectorStore scores by term overlap --
not BM25, but monotone in term count, which is the property the pipeline
layer asserts; its filter parser understands only the one expression
shape this codebase emits and RAISES on anything else, so a broken
filter cannot masquerade as a passing test."
```

---

### Task 2: Milvus collection schema v2 with the BM25 function

**Files:**
- Modify: `backend/src/fdi/platform/vectorstore.py`
- Test: `backend/tests/unit/test_milvus_vectorstore.py`

**Interfaces:**
- Consumes: the `VectorStore` shape from Task 1 (satisfied structurally, never imported).
- Produces: `MilvusVectorStore.search_text(query, k, *, filter="")` and `search(vector, k, *, filter="")`; `_ensure_collection` builds schema v2.

- [ ] **Step 1: Write the failing tests**

The suite monkeypatches `fdi.platform.vectorstore.MilvusClient`. Add to `backend/tests/unit/test_milvus_vectorstore.py`:

```python
def test_ensure_collection_declares_bm25_function_and_sparse_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, object] = {}

    class _FakeSchema:
        def __init__(self) -> None:
            self.fields: list[dict[str, object]] = []
            self.functions: list[object] = []

        def add_field(self, field_name: str, datatype: object, **kw: object) -> None:
            self.fields.append({"name": field_name, "type": datatype, **kw})

        def add_function(self, fn: object) -> None:
            self.functions.append(fn)

    class _FakeIndexParams:
        def __init__(self) -> None:
            self.indexes: list[dict[str, object]] = []

        def add_index(self, field_name: str, **kw: object) -> None:
            self.indexes.append({"field_name": field_name, **kw})

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, collection_name: str) -> bool:
            return False

        def create_schema(self, **kw: object) -> _FakeSchema:
            return _FakeSchema()

        def prepare_index_params(self) -> _FakeIndexParams:
            return _FakeIndexParams()

        def create_collection(self, **kw: object) -> None:
            created.update(kw)

        def upsert(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://x", collection="c", dim=384)
    store.upsert([{"id": "d:s1", "vector": [0.0] * 384, "metadata": {"text": "hello"}}])

    schema = created["schema"]
    names = {f["name"] for f in schema.fields}
    assert {"id", "vector", "text", "sparse", "doc_id", "chunk_id",
            "section_type", "heading", "page"} <= names

    text_field = next(f for f in schema.fields if f["name"] == "text")
    assert text_field["enable_analyzer"] is True

    # the BM25 function must be declared BEFORE create_collection -- it cannot
    # be added to a live collection
    assert len(schema.functions) == 1

    index_params = created["index_params"]
    sparse_index = next(i for i in index_params.indexes if i["field_name"] == "sparse")
    assert sparse_index["index_type"] == "SPARSE_INVERTED_INDEX"
    assert sparse_index["metric_type"] == "BM25"
    dense_index = next(i for i in index_params.indexes if i["field_name"] == "vector")
    assert dense_index["metric_type"] == "COSINE"


def test_search_text_passes_raw_text_and_explicit_output_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, collection_name: str) -> bool:
            return True

        def search(self, **kw: object) -> list[list[dict[str, object]]]:
            seen.update(kw)
            return [[{"id": "d:s1", "distance": 3.5,
                      "entity": {"text": "revenue", "page": 7}}]]

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://x", collection="c", dim=384)
    hits = store.search_text("revenue from operations", 5, filter='doc_id == "d"')

    assert seen["data"] == ["revenue from operations"]   # raw text, NOT a vector
    assert seen["anns_field"] == "sparse"
    assert seen["filter"] == 'doc_id == "d"'
    # NEVER ["*"]: Milvus refuses to output a sparse vector field
    assert "*" not in cast(list[str], seen["output_fields"])
    assert "sparse" not in cast(list[str], seen["output_fields"])
    assert "text" in cast(list[str], seen["output_fields"])
    assert hits == [{"id": "d:s1", "score": 3.5,
                     "metadata": {"text": "revenue", "page": 7}}]


def test_search_text_missing_collection_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, collection_name: str) -> bool:
            return False

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://x", collection="c", dim=384)
    assert store.search_text("anything", 5) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/unit/test_milvus_vectorstore.py -q`
Expected: FAIL — `AttributeError: 'MilvusVectorStore' object has no attribute 'search_text'`

- [ ] **Step 3: Implement schema v2**

In `backend/src/fdi/platform/vectorstore.py`, extend the import and add constants:

```python
from pymilvus import DataType, Function, FunctionType, MilvusClient

# Explicit, because Milvus REFUSES to output a SPARSE_FLOAT_VECTOR field --
# `output_fields=["*"]` fails the moment `sparse` exists on the schema.
_OUTPUT_FIELDS = ["text", "doc_id", "chunk_id", "section_type", "heading", "page"]

# The largest real chunk measured on the STTL FY2023-24 corpus is 10,253
# chars (section chunks have no size cap; only whole-table chunks are capped,
# at 4000). 65535 is Milvus's VARCHAR ceiling and costs nothing -- max_length
# is a validation bound, not an allocation.
_TEXT_MAX_LENGTH = 65535
```

Replace `_ensure_collection`:

```python
    def _ensure_collection(self) -> None:
        """Schema v2: dense vector AND a BM25 sparse vector derived from `text`.

        The BM25 Function and the analyzer MUST be declared here, at creation
        -- Milvus cannot add either to a live collection. That is why this
        moved off the simple create_collection(dimension=...) form, and why
        the collection name is versioned in config rather than mutated."""
        if self._client.has_collection(self._collection):
            return

        schema = self._client.create_schema(auto_id=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=512)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
        schema.add_field(
            "text", DataType.VARCHAR, max_length=_TEXT_MAX_LENGTH,
            enable_analyzer=True, analyzer_params={"type": "standard"},
        )
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=64)
        schema.add_field("section_type", DataType.VARCHAR, max_length=64, nullable=True)
        schema.add_field("heading", DataType.VARCHAR, max_length=1024, nullable=True)
        schema.add_field("page", DataType.INT64, nullable=True)
        schema.add_function(Function(
            name="text_bm25",
            input_field_names=["text"],
            output_field_names=["sparse"],
            function_type=FunctionType.BM25,
        ))

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="AUTOINDEX", metric_type="COSINE"
        )
        index_params.add_index(
            field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25"
        )
        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index_params
        )
```

- [ ] **Step 4: Implement both searches**

```python
    def _hits(self, raw: object) -> list[dict[str, object]]:
        rows = cast(list[list[Mapping[str, object]]], raw)
        if not rows:
            return []
        results: list[dict[str, object]] = []
        for hit in rows[0]:
            entity = dict(cast(Mapping[str, object], hit.get("entity", {})))
            results.append(
                {"id": hit["id"], "score": hit["distance"], "metadata": entity}
            )
        return results

    def search(
        self, vector: list[float], k: int, *, filter: str = ""
    ) -> list[dict[str, object]]:
        if not self._client.has_collection(self._collection):
            return []
        return self._hits(self._client.search(
            collection_name=self._collection,
            data=[vector],
            anns_field="vector",
            limit=k,
            filter=filter,
            output_fields=_OUTPUT_FIELDS,
        ))

    def search_text(
        self, query: str, k: int, *, filter: str = ""
    ) -> list[dict[str, object]]:
        """BM25 full-text search. `data` is RAW TEXT: Milvus tokenizes it with
        the collection's analyzer and scores against the sparse field
        server-side. No client-side embedding, no tokenization here."""
        if not self._client.has_collection(self._collection):
            return []
        return self._hits(self._client.search(
            collection_name=self._collection,
            data=[query],
            anns_field="sparse",
            limit=k,
            filter=filter,
            output_fields=_OUTPUT_FIELDS,
        ))
```

Note `search` no longer pops `vector` from the entity — `_OUTPUT_FIELDS` never requests it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/unit/test_milvus_vectorstore.py -q`
Expected: PASS

- [ ] **Step 6: Gates and commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/platform/vectorstore.py backend/tests/unit/test_milvus_vectorstore.py
git commit -m "feat(platform): Milvus collection schema v2 with BM25 function

Milvus computes BM25 server-side from an analyzed VARCHAR field, so the
collection must store chunk text -- it stored none. The Function and the
analyzer must be declared at creation; neither can be added to a live
collection, which is why this moves off the simple create_collection form
and why the collection name is versioned rather than mutated.

output_fields is an explicit list, never [\"*\"]: Milvus refuses to output
a SPARSE_FLOAT_VECTOR, so [\"*\"] fails the moment sparse exists."
```

---

### Task 3: `IndexStage` v3 writes text and scalars

**Files:**
- Modify: `backend/src/fdi/pipeline/stages/index.py`
- Test: `backend/tests/integration/test_index_enrichment.py`

**There is no unit test for `IndexStage`.** Its only coverage is
`tests/integration/test_index_enrichment.py`, which drives the real stage
chain (`ocr → normalize → structure → ast → semantic → graph → provenance →
chunks → embedding → index`) against real Postgres + MinIO with fake OCR/
semantic/embedding clients and a `FakeVectorStore` that records every upserted
item. Extend that file — do not invent a unit test with a `_ctx` helper, and
do not create `tests/unit/test_index_stage.py`.

**Interfaces:**
- Consumes: `VectorStore.upsert` (Task 1 shape).
- Produces: Milvus rows carrying `text`, `doc_id`, `chunk_id`, `section_type`, `heading`, `page`. `IndexStage.version == 3`.

**Why version 3:** the DAG is content-addressed at `artifacts/{doc_id}/{stage}/v{version}/{input_hash}.json`, so bumping the version is already the mechanism that forces a stage to re-run. No backfill script. `embedding` artifacts are untouched and stay cached — **no re-embedding, no Azure cost**.

**Note on `doc_key`/`version`:** `StageContext` exposes only `doc_id`, `read_artifact`, `read_source`, `services` — a stage has no session and no `doc_key`. That is why the collection filters on `doc_id` and the retrieval layer keeps its Postgres translation. Do not try to read `doc_key` here.

- [ ] **Step 1: Write the failing test**

Read `backend/tests/integration/test_index_enrichment.py` first. It already
asserts on the recorded upserted items — reuse its fixtures, its recording
`FakeVectorStore`, and its accessor for captured items verbatim; the names
below (`_recorded_items`) are a placeholder for whatever that file already
calls it.

Add a version assertion (a plain unit test, no fixtures) and extend the
existing enrichment assertions:

```python
def test_index_stage_version_is_3() -> None:
    """v3 rows carry `text` and `doc_id`; v2 rows carry neither. The bump is
    what re-runs `index` -- and only `index` -- for already-ingested
    documents, since the DAG is content-addressed on {stage}/v{version}."""
    assert IndexStage().version == 3
```

In the existing enrichment test, after the stage chain has run, add to the
assertions already made on the captured item:

```python
    # RT-9: the row now carries the BM25 input and the filterable doc scalar
    assert item["metadata"]["text"].startswith("Balance Sheet")
    assert item["metadata"]["doc_id"] == doc.id
    # still true from v2 -- these must not regress
    assert item["metadata"]["chunk_id"] == "s0"
    assert item["metadata"]["section_type"] == SectionType.BALANCE_SHEET.value
```

Match the chunk id, heading, and section type the fixture document actually
produces — read the file's existing assertions rather than assuming `s0`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/integration/test_index_enrichment.py -q`
Expected: FAIL — `KeyError: 'text'` and `assert 2 == 3`. (Needs Docker: this
test uses real Postgres + MinIO testcontainers.)

- [ ] **Step 3: Implement**

In `backend/src/fdi/pipeline/stages/index.py` set `version = 3` and replace the `items.append({...})` block:

```python
        items: list[Mapping[str, object]] = []
        for chunk in chunks:
            meta = meta_by_chunk.get(str(chunk["chunk_id"]))
            items.append({
                "id": f"{ctx.doc_id}:{chunk['chunk_id']}",
                "vector": chunk["vector"],
                "metadata": {
                    # `text` is the BM25 input -- schema v2's whole point. A
                    # chunk with no matching `chunks` record still indexes,
                    # with empty text, rather than being dropped.
                    "text": (meta.get("text") if meta is not None else None) or "",
                    "doc_id": ctx.doc_id,
                    "chunk_id": chunk["chunk_id"],
                    "heading": chunk.get("heading"),
                    "section_type": meta.get("section_type") if meta is not None else None,
                    "page": meta.get("page") if meta is not None else None,
                },
            })
```

Update the module docstring's "RT-1 v2" paragraph to describe v3 (text + doc_id now written; BM25's input).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/integration/test_index_enrichment.py -q`
Expected: PASS

- [ ] **Step 5: Gates and commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/pipeline/stages/index.py backend/tests/integration/test_index_enrichment.py
git commit -m "feat(pipeline): IndexStage v3 writes chunk text into the vector store

BM25 reads an analyzed text field server-side, so the row must carry the
text. doc_id is written as a filterable scalar because StageContext has no
doc_key/version -- a stage has no session, by design -- which is why the
retrieval layer keeps its Postgres translation.

The version bump is the re-index mechanism: the DAG is content-addressed
on {stage}/v{version}, so already-ingested documents re-run index and
ONLY index. Embedding artifacts stay cached -- no re-embedding, no cost."
```

---

### Task 4: `build_vector_results` reads text off the hit

**Files:**
- Modify: `backend/src/fdi/core/retrieval.py`
- Test: `backend/tests/unit/test_retrieval_vector_results.py`

**Interfaces:**
- Consumes: hit shape `{"id", "score", "metadata"}` from Tasks 1–2.
- Produces: `build_vector_results(hits, docs, *, section_type, doc_key, version, k) -> list[dict]` — the `resolved` parameter is **gone**; `docs` stays.

- [ ] **Step 1: Write the failing tests**

Rewrite the existing tests in `backend/tests/unit/test_retrieval_vector_results.py` to the new signature (delete the `resolved` fixtures):

```python
def _hit(full_id: str, score: float, **metadata: object) -> dict[str, object]:
    return {"id": full_id, "score": score, "metadata": metadata}


def test_build_vector_results_reads_text_off_the_hit() -> None:
    hits = [_hit("d1:s0", 0.9, text="Total assets 16,427.37",
                 heading="Balance Sheet", page=91, section_type="BALANCE_SHEET",
                 chunk_id="s0")]
    docs = {"d1": {"doc_key": "acme", "version": 2}}
    [rec] = build_vector_results(
        hits, docs, section_type=None, doc_key=None, version=None, k=10
    )
    assert rec == {
        "doc_id": "d1", "doc_key": "acme", "version": 2, "chunk_id": "s0",
        "heading": "Balance Sheet", "page": 91, "section_type": "BALANCE_SHEET",
        "text": "Total assets 16,427.37", "score": 0.9,
    }


def test_build_vector_results_filters_by_section_type() -> None:
    hits = [
        _hit("d1:s0", 0.9, text="a", section_type="BALANCE_SHEET", chunk_id="s0"),
        _hit("d1:s1", 0.8, text="b", section_type="NOTES", chunk_id="s1"),
    ]
    docs = {"d1": {"doc_key": "acme", "version": 2}}
    out = build_vector_results(
        hits, docs, section_type="NOTES", doc_key=None, version=None, k=10
    )
    assert [r["chunk_id"] for r in out] == ["s1"]


def test_build_vector_results_filters_by_doc_key_and_version() -> None:
    hits = [_hit("d1:s0", 0.9, text="a", chunk_id="s0"),
            _hit("d2:s0", 0.8, text="b", chunk_id="s0")]
    docs = {"d1": {"doc_key": "acme", "version": 2},
            "d2": {"doc_key": "other", "version": 1}}
    out = build_vector_results(
        hits, docs, section_type=None, doc_key="acme", version=2, k=10
    )
    assert [r["doc_id"] for r in out] == ["d1"]


def test_build_vector_results_truncates_without_resorting() -> None:
    hits = [_hit(f"d1:s{i}", 1.0 - i / 10, text="t", chunk_id=f"s{i}") for i in range(5)]
    docs = {"d1": {"doc_key": "acme", "version": 2}}
    out = build_vector_results(
        hits, docs, section_type=None, doc_key=None, version=None, k=2
    )
    assert [r["chunk_id"] for r in out] == ["s0", "s1"]


def test_build_vector_results_unknown_doc_degrades_citation_to_none() -> None:
    hits = [_hit("ghost:s0", 0.9, text="t", chunk_id="s0")]
    out = build_vector_results(
        hits, {}, section_type=None, doc_key=None, version=None, k=10
    )
    assert out[0]["doc_key"] is None and out[0]["version"] is None


def test_build_vector_results_malformed_metadata_never_raises() -> None:
    assert build_vector_results(
        [{"id": "d1:s0", "score": "x"}], {"d1": {"doc_key": "a", "version": 1}},
        section_type=None, doc_key=None, version=None, k=10,
    )[0]["score"] == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_vector_results.py -q`
Expected: FAIL — `TypeError: build_vector_results() missing 1 required positional argument: 'docs'`

- [ ] **Step 3: Implement**

Replace `build_vector_results` in `backend/src/fdi/core/retrieval.py`:

```python
def build_vector_results(
    hits: list[Mapping[str, object]],
    docs: Mapping[str, Mapping[str, object]],
    *,
    section_type: str | None,
    doc_key: str | None,
    version: int | None,
    k: int,
) -> list[dict[str, object]]:
    """Pure post-processing of an already-fetched vector-store hit list.

    RT-9: the hit now CARRIES its own text and citation metadata (the Milvus
    collection stores them), so there is no `resolved` chunk-record map any
    more and no artifact read behind it. `docs` remains: the collection is
    filtered on `doc_id` because a Stage has no `doc_key`/`version` to write,
    so the doc_key/version citation is still joined here from a map the
    caller read from Postgres. Filters, joins, truncates to k WITHOUT
    re-sorting. Never raises."""
    results: list[dict[str, object]] = []
    for hit in hits:
        full_id = str(hit.get("id"))
        meta_raw = hit.get("metadata")
        meta = meta_raw if isinstance(meta_raw, Mapping) else {}

        if section_type is not None and meta.get("section_type") != section_type:
            continue

        doc_id = full_id.split(":", 1)[0] if ":" in full_id else full_id
        doc = docs.get(doc_id)
        if doc_key is not None and (doc is None or doc.get("doc_key") != doc_key):
            continue
        if version is not None and (doc is None or doc.get("version") != version):
            continue

        chunk_id = meta.get("chunk_id")
        score = hit.get("score")
        results.append({
            "doc_id": doc_id,
            "doc_key": doc.get("doc_key") if doc is not None else None,
            "version": doc.get("version") if doc is not None else None,
            "chunk_id": chunk_id if isinstance(chunk_id, str)
            else (full_id.split(":", 1)[1] if ":" in full_id else ""),
            "heading": meta.get("heading"),
            "page": _int_or_none(meta.get("page")),
            "section_type": meta.get("section_type"),
            "text": meta.get("text") if isinstance(meta.get("text"), str) else "",
            "score": float(score) if isinstance(score, int | float) else 0.0,
        })
        if len(results) >= k:
            break

    return results
```

Update the module docstring: `build_vector_results` no longer consumes a `resolve_chunks()` result.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_vector_results.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/fdi/core/retrieval.py backend/tests/unit/test_retrieval_vector_results.py
git commit -m "refactor(core): build_vector_results reads text off the hit

Milvus now stores chunk text, so a vector hit is self-describing and the
per-search artifact read disappears -- the `resolved` parameter with it.
`docs` stays: the collection filters on doc_id (a Stage has no
doc_key/version to write), so that citation is still joined from a map
the caller read from Postgres."
```

---

### Task 5: `bm25_search`, and the Postgres keyword path deleted

**Files:**
- Modify: `backend/src/fdi/pipeline/retrieval.py`
- Test: `backend/tests/unit/test_retrieval_bm25.py` (create), `backend/tests/unit/test_retrieval_fuse.py` (must stay untouched)

**Interfaces:**
- Consumes: `VectorStore.search_text` (Task 1), `build_vector_results` (Task 4).
- Produces:
  - `bm25_search(*, session, vector_store, query, k, filters=None) -> list[dict]`
  - `vector_search(*, session, embedder, vector_store, query, k, filters=None) -> list[dict]` (**`store` removed**)
  - `hybrid_search(*, session, embedder, vector_store, query, k, filters=None) -> list[dict]` (**`store` removed**, `embedder`/`vector_store` now required non-`None`)
  - Deleted: `keyword_search`, `index_chunks_fts`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/unit/test_retrieval_bm25.py`:

```python
"""bm25_search + hybrid_search over a FakeVectorStore. No Postgres, no Milvus.

`_docs` stands in for the one DocumentRepository call the retrieval layer
makes; the tests monkeypatch it so the module's SQL never runs.
"""
import pytest

from fdi.pipeline import retrieval as r
from fdi.pipeline.vectorstore import FakeVectorStore


class _FakeEmbedder:
    model = "fake"

    def embed(self, texts: list[str]) -> list[list[float]]:
        # deterministic: "revenue" -> [1,0], anything else -> [0,1]
        return [[1.0, 0.0] if "revenue" in t.casefold() else [0.0, 1.0] for t in texts]


def _store() -> FakeVectorStore:
    s = FakeVectorStore()
    s.upsert([
        {"id": "d1:s0", "vector": [1.0, 0.0], "metadata": {
            "text": "revenue from operations total income", "doc_id": "d1",
            "chunk_id": "s0", "heading": "P&L", "page": 90, "section_type": None}},
        {"id": "d1:s1", "vector": [0.0, 1.0], "metadata": {
            "text": "unrelated narrative about governance", "doc_id": "d1",
            "chunk_id": "s1", "heading": "Report", "page": 30, "section_type": None}},
    ])
    return s


@pytest.fixture(autouse=True)
def _patch_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_docs(session: object, doc_ids: list[str]) -> dict[str, dict[str, object]]:
        return {d: {"doc_key": "acme", "version": 1} for d in doc_ids}

    monkeypatch.setattr(r, "_docs_for", _fake_docs)


async def test_bm25_search_returns_the_nine_key_hit_shape() -> None:
    hits = await r.bm25_search(
        session=None, vector_store=_store(), query="revenue from operations", k=5
    )
    assert hits
    assert set(hits[0]) == {
        "doc_id", "doc_key", "version", "chunk_id", "heading",
        "page", "section_type", "text", "score",
    }


async def test_bm25_search_ranks_by_term_overlap() -> None:
    hits = await r.bm25_search(
        session=None, vector_store=_store(), query="revenue from operations", k=5
    )
    # THE regression this whole sub-project exists for: a real query returns
    # something. The old plainto_tsquery path returned [] here.
    assert hits[0]["chunk_id"] == "s0"


async def test_bm25_search_empty_query_returns_empty() -> None:
    assert await r.bm25_search(
        session=None, vector_store=_store(), query="   ", k=5
    ) == []


async def test_hybrid_search_fuses_both_channels() -> None:
    hits = await r.hybrid_search(
        session=None, embedder=_FakeEmbedder(), vector_store=_store(),
        query="revenue from operations", k=5,
    )
    # s0 wins both channels, so RRF must rank it first
    assert hits[0]["chunk_id"] == "s0"
    assert len(hits) >= 1


async def test_hybrid_search_a_both_channel_hit_outranks_a_single_channel_hit() -> None:
    """The property RRF exists for -- and the one the dead keyword channel has
    been silently failing to provide, since an empty keyword list makes every
    fused score come from the vector channel alone."""
    hits = await r.hybrid_search(
        session=None, embedder=_FakeEmbedder(), vector_store=_store(),
        query="revenue from operations", k=5,
    )
    by_chunk = {h["chunk_id"]: h["score"] for h in hits}
    assert by_chunk["s0"] > by_chunk.get("s1", 0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_bm25.py -q`
Expected: FAIL — `AttributeError: module 'fdi.pipeline.retrieval' has no attribute 'bm25_search'`

- [ ] **Step 3: Implement**

In `backend/src/fdi/pipeline/retrieval.py`:

Delete `keyword_search` and `index_chunks_fts` entirely. Drop the now-unused imports (`func`, `select`, `pg_insert`, `Chunk`, `chunk_rows_for_index`).

Add the shared filter/doc helpers and `bm25_search`:

```python
def _milvus_filter(doc_ids: Sequence[str]) -> str:
    """Milvus filter expression scoping a search to specific documents.

    The collection stores `doc_id`, not `doc_key`/`version` -- a Stage has
    neither -- so a doc_key/version filter is resolved to doc_ids by the
    caller first. Empty string means unscoped."""
    if not doc_ids:
        return ""
    ids = ", ".join(f'"{d}"' for d in doc_ids)
    return f"doc_id in [{ids}]"


async def _docs_for(
    session: AsyncSession, doc_ids: list[str]
) -> dict[str, Mapping[str, object]]:
    rows = await DocumentRepository(session).get_many(doc_ids)
    return {
        doc_id: {"doc_key": d.doc_key, "version": d.version}
        for doc_id, d in rows.items()
    }


async def _doc_ids_for(
    session: AsyncSession, *, doc_key: str | None, version: int | None
) -> list[str]:
    """doc_key/version -> the doc_ids to scope a Milvus filter to. Returns []
    when no filter is requested (meaning unscoped, not 'match nothing')."""
    if doc_key is None and version is None:
        return []
    return await DocumentRepository(session).ids_for(doc_key=doc_key, version=version)


async def bm25_search(
    *,
    session: AsyncSession,
    vector_store: VectorStore,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-9's keyword channel: Milvus-native BM25 over the analyzed `text`
    field. `query` is passed as RAW TEXT -- Milvus tokenizes and scores it
    server-side; there is no client-side tokenization and no embedding call.

    Replaces RT-3's Postgres keyword_search, whose plainto_tsquery ANDed every
    lexeme and therefore returned zero hits for every real (sentence-shaped)
    query."""
    if not query.strip():
        return []
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None

    doc_ids = await _doc_ids_for(
        session, doc_key=cast(str | None, doc_key), version=cast(int | None, version)
    )
    if (doc_key is not None or version is not None) and not doc_ids:
        return []

    fetch_k = k * _OVERFETCH_FACTOR if section_type is not None else k
    hits = vector_store.search_text(query.strip(), fetch_k, filter=_milvus_filter(doc_ids))
    docs = await _docs_for(session, [str(h["id"]).split(":", 1)[0] for h in hits])
    return build_vector_results(
        cast(list[Mapping[str, object]], hits), docs,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key), version=cast(int | None, version), k=k,
    )
```

Rewrite `vector_search` to the same shape (embed the query, `vector_store.search(...)` with the same filter, then `build_vector_results`), dropping its `store` parameter and its `resolve_chunks` call.

Rewrite `hybrid_search` to drop `store`, require non-`None` `embedder`/`vector_store`, and call `bm25_search` where it called `keyword_search`. Its `fuse(...)` call is unchanged.

- [ ] **Step 4: Add `DocumentRepository.ids_for`**

In `backend/src/fdi/platform/db/repository.py`:

`DocumentRepository.__init__` stores its session as **`self._s`** (not
`self._session`) — verified at `repository.py:382`. Use that name:

```python
    async def ids_for(self, *, doc_key: str | None, version: int | None) -> list[str]:
        """Document ids matching a doc_key and/or version. Used to scope a
        Milvus filter, which keys on doc_id (a Stage has no doc_key to write).

        Both None -> every document id. Callers that mean "unscoped" must not
        call this at all; `bm25_search`/`vector_search` guard on that before
        reaching here."""
        stmt = select(Document.id)
        if doc_key is not None:
            stmt = stmt.where(Document.doc_key == doc_key)
        if version is not None:
            stmt = stmt.where(Document.version == version)
        return list((await self._s.execute(stmt)).scalars().all())
```

`select` and `Document` are already imported in that module.

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run --directory backend pytest tests/unit/test_retrieval_bm25.py tests/unit/test_retrieval_fuse.py -q
```
Expected: PASS, and **`test_retrieval_fuse.py` must pass unedited** — if it needed editing, the change escaped scope.

- [ ] **Step 6: Commit**

```bash
git add backend/src/fdi/pipeline/retrieval.py backend/src/fdi/platform/db/repository.py backend/tests/unit/test_retrieval_bm25.py
git commit -m "feat(pipeline): bm25_search on Milvus; delete the Postgres keyword path

keyword_search built its query with plainto_tsquery, which ANDs every
lexeme. Every retrieval query here is a sentence, so each demanded that one
chunk contain all its terms: all 18 retrieval-unit queries returned ZERO
hits against the live corpus, and hybrid_search has been fusing a populated
vector list with an empty keyword list -- silently vector-only, no error,
no log.

bm25_search passes raw text to Milvus, which tokenizes and scores it
server-side against the analyzed text field. doc_key/version filters
resolve to doc_ids first because the collection keys on doc_id.

fuse is untouched and keeps both callers."
```

---

### Task 6: Routes, agent tool, and extractor call sites

**Files:**
- Modify: `backend/src/fdi/api/routes.py`, `backend/src/fdi/pipeline/agent_tools.py`, `backend/src/fdi/pipeline/extractor.py`
- Test: `backend/tests/integration/test_api.py`

**Interfaces:**
- Consumes: `bm25_search` / `vector_search` / `hybrid_search` from Task 5.
- Produces: `POST /retrieve/keyword` served by Milvus BM25; `POST /retrieve` 503s when unconfigured; `POST /documents/{id}/index_fts` gone.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/integration/test_api.py`:

```python
async def test_retrieve_hybrid_503s_when_vector_store_unconfigured(make_client) -> None:
    """RT-4 decision 2 is superseded: with both channels in Milvus there is
    nothing to degrade to, so an unconfigured Milvus is a configuration error
    and says so -- rather than returning a confident empty list."""
    async with make_client() as client:
        r = await client.post("/retrieve", json={"query": "revenue", "k": 5})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


async def test_retrieve_keyword_503s_when_vector_store_unconfigured(make_client) -> None:
    async with make_client() as client:
        r = await client.post("/retrieve/keyword", json={"query": "revenue", "k": 5})
    assert r.status_code == 503


async def test_index_fts_route_is_gone(make_client) -> None:
    async with make_client() as client:
        r = await client.post("/documents/whatever/index_fts")
    assert r.status_code == 404
```

Delete any existing test that asserts `/retrieve` degrades to keyword-only, and any test of `/documents/{id}/index_fts` succeeding. Match the file's existing fixture names.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "retrieve or index_fts" -q`
Expected: FAIL — `/retrieve` returns 200, `/index_fts` returns 200 or 404-for-document rather than route-missing.

- [ ] **Step 3: Update `routes.py`**

Delete `trigger_index_fts` and the `POST /documents/{doc_id}/index_fts` route, and drop `index_chunks_fts` / `keyword_search` from the imports (add `bm25_search`).

Give `/retrieve` and `/retrieve/keyword` the same 503 branch `/retrieve/vector` already has:

```python
@router.post("/retrieve/keyword", response_model=KeywordSearchResultsView)
async def keyword_search_route(
    body: KeywordSearchRequest, session: SessionDep, vector_store: VectorStoreDep
) -> KeywordSearchResultsView:
    if vector_store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "retrieve/keyword is not configured: missing vector_store "
                "(set FDI_MILVUS_URI -- BM25 is served by Milvus)"
            ),
        )
    hits = await bm25_search(
        session=session, vector_store=vector_store, query=body.query, k=body.k,
        filters={"section_type": body.section_type, "doc_key": body.doc_key,
                 "version": body.version},
    )
    return KeywordSearchResultsView(results=[KeywordHitView(**h) for h in hits])
```

Give `hybrid_search_route` the identical `missing`/503 block `/retrieve/vector` uses, then call `hybrid_search` without `store=`. Update the RT-3/RT-4 comment blocks above these routes — they currently state the opposite of the new behaviour.

- [ ] **Step 4: Update `agent_tools.py` and `extractor.py`**

`_search` must guard the `None` case, matching the module's documented `{"error": ...}` posture:

```python
async def _search(args: BaseModel, ctx: AgentContext) -> dict[str, object]:
    a = cast(_SearchArgs, args)
    if ctx.embedder is None or ctx.vector_store is None:
        return {"error": "search is unavailable: vector store not configured"}
    filters = {
        "section_type": a.section_type.value if a.section_type is not None else None,
        "doc_key": a.doc_key, "version": a.version,
    }
    results = await hybrid_search(
        session=ctx.session, embedder=ctx.embedder,
        vector_store=ctx.vector_store, query=a.query, k=a.k, filters=filters,
    )
    return {"results": results}
```

In `backend/src/fdi/pipeline/extractor.py`, `_pool_for` drops `store=ctx.store` from its `hybrid_search(...)` call and nothing else. `ExtractContext` keeps `store` — `_consolidated_start_page` and `_persist_extraction` still use it.

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run --directory backend pytest tests/integration/test_api.py -q
uv run --directory backend pytest -m "not integration and not e2e" -q
```
Expected: both PASS.

- [ ] **Step 6: Gates and commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/api/routes.py backend/src/fdi/pipeline/agent_tools.py backend/src/fdi/pipeline/extractor.py backend/tests/integration/test_api.py
git commit -m "feat(api): serve /retrieve/keyword from Milvus BM25; 503 when unconfigured

The endpoint is kept and repointed rather than deleted -- the Postgres
implementation dies, but the capability starts working for the first time.

Supersedes RT-4 decision 2: with both channels in Milvus there is nothing
to degrade to, so /retrieve and /retrieve/keyword now 503 when Milvus is
unconfigured, matching /retrieve/vector. A confident empty list is the
failure this whole sub-project exists to correct.

The agent search tool degrades to {error: ...}, its documented posture.
_pool_for drops one argument; extraction batching is untouched."
```

---

### Task 7: Drop the `chunk` table and retire `chunk_rows_for_index`

**Files:**
- Create: `backend/alembic/versions/0007_drop_chunk.py`
- Modify: `backend/src/fdi/platform/db/models.py`, `backend/src/fdi/core/retrieval.py`
- Test: delete `backend/tests/unit/test_retrieval_chunk_rows.py`

**Interfaces:**
- Consumes: nothing from Tasks 1–6 (independent cleanup, sequenced last so the suite stays green throughout).
- Produces: no `chunk` table, no `Chunk` model, no `chunk_rows_for_index`.

- [ ] **Step 1: Write the migration**

Create `backend/alembic/versions/0007_drop_chunk.py`. Mirror `0006_chunk_fts.py`'s structure exactly, inverted — `downgrade()` must recreate the table, its `tsv` generated column, and all four indexes, so the migration is reversible:

```python
"""drop chunk table -- RT-9 moves keyword search to Milvus BM25

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_chunk_tsv", table_name="chunk")
    op.drop_index("ix_chunk_section_type", table_name="chunk")
    op.drop_index("ix_chunk_doc_key_version", table_name="chunk")
    op.drop_table("chunk")


def downgrade() -> None:
    op.create_table(
        "chunk",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("doc_id", sa.String(length=36), sa.ForeignKey("document.id"), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("doc_key", sa.String(length=256), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section_type", sa.String(length=64), nullable=True),
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
```

Open `0006_chunk_fts.py` and make `downgrade()` here mirror its `upgrade()` field-for-field, including the exact `doc_id` index name it created.

- [ ] **Step 2: Delete the model and the pure helper**

Remove the `Chunk` class from `backend/src/fdi/platform/db/models.py` (and any now-unused imports: `TSVECTOR`, `Computed`).

Remove `chunk_rows_for_index` from `backend/src/fdi/core/retrieval.py` and update the module docstring, which currently describes it.

Delete `backend/tests/unit/test_retrieval_chunk_rows.py`.

- [ ] **Step 3: Verify the migration round-trips**

Run against the live stack (`docker compose --env-file <repo>/.env -f docker/compose.yml --profile core up -d postgres`):

```bash
uv run --directory backend alembic upgrade head
uv run --directory backend alembic downgrade -1
uv run --directory backend alembic upgrade head
```
Expected: all three succeed. A downgrade that fails means the recreate is wrong — fix it rather than skipping the check.

- [ ] **Step 4: Full suite**

Run: `uv run --directory backend pytest -q`
Expected: no failures. Any test still importing `Chunk` or `chunk_rows_for_index` must be deleted, not stubbed.

- [ ] **Step 5: Gates and commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add -A backend/alembic backend/src/fdi/platform/db/models.py backend/src/fdi/core/retrieval.py backend/tests
git commit -m "refactor: drop the chunk table -- keyword search lives in Milvus now

The Postgres full-text path is fully replaced: table, tsv generated
column, four indexes, model, and chunk_rows_for_index all go. The
migration recreates every one of them on downgrade, so the cutover is
reversible."
```

---

### Task 8: Collection version bump and live-Milvus verification

**Files:**
- Modify: `backend/src/fdi/platform/config.py`, `.env.example`, `docker/compose.yml` (if it names the collection)
- Test: `backend/tests/integration/test_milvus_bm25.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: `milvus_collection` default `"fdi_chunks_v2"`; a live-Milvus test proving BM25 discriminates.

- [ ] **Step 1: Bump the collection name**

In `backend/src/fdi/platform/config.py`:

```python
    # Schema v2 (RT-9): dense vector + BM25 sparse vector + stored text. The
    # BM25 Function cannot be added to an existing collection, so the cutover
    # is a NEW collection rather than an alter -- `fdi_sections` is left
    # in place, which makes rollback a one-line revert of this default.
    milvus_collection: str = "fdi_chunks_v2"
```

Update `.env.example`'s `FDI_MILVUS_COLLECTION` line and its comment to match. Check `docker/compose.yml`'s `x-backend-env` — if `FDI_MILVUS_COLLECTION` is listed there, no value change is needed (it passes through), but confirm it is present.

- [ ] **Step 2: Write the live-Milvus test**

Create `backend/tests/integration/test_milvus_bm25.py`:

```python
"""BM25 against a REAL Milvus. Carries the `milvus` marker: skipped unless
FDI_MILVUS_URI is set and reachable.

A fake cannot establish the claim this whole sub-project rests on -- that
BM25 ranking actually discriminates by term overlap -- so this is the one
test that must run against the real engine.
"""
import os
import uuid

import pytest

from fdi.platform.vectorstore import MilvusVectorStore

pytestmark = pytest.mark.milvus


@pytest.fixture
def store() -> MilvusVectorStore:
    uri = os.environ.get("FDI_MILVUS_URI")
    if not uri:
        pytest.skip("FDI_MILVUS_URI not set")
    return MilvusVectorStore(
        uri=uri, collection=f"test_bm25_{uuid.uuid4().hex[:8]}", dim=4
    )


def test_bm25_ranks_by_term_overlap_against_real_milvus(
    store: MilvusVectorStore,
) -> None:
    store.upsert([
        {"id": "d1:s0", "vector": [1.0, 0.0, 0.0, 0.0], "metadata": {
            "text": "revenue from operations and other income total",
            "doc_id": "d1", "chunk_id": "s0", "heading": None,
            "section_type": None, "page": 1}},
        {"id": "d1:s1", "vector": [0.0, 1.0, 0.0, 0.0], "metadata": {
            "text": "revenue only", "doc_id": "d1", "chunk_id": "s1",
            "heading": None, "section_type": None, "page": 2}},
        {"id": "d1:s2", "vector": [0.0, 0.0, 1.0, 0.0], "metadata": {
            "text": "entirely unrelated governance narrative",
            "doc_id": "d1", "chunk_id": "s2", "heading": None,
            "section_type": None, "page": 3}},
    ])
    hits = store.search_text("revenue from operations", 10)
    ids = [h["id"] for h in hits]
    assert ids[0] == "d1:s0", f"expected the 3-term match first, got {ids}"
    assert "d1:s2" not in ids, "a zero-overlap chunk must not be returned"


def test_bm25_filter_scopes_to_one_document(store: MilvusVectorStore) -> None:
    store.upsert([
        {"id": "a:s0", "vector": [1.0, 0.0, 0.0, 0.0], "metadata": {
            "text": "revenue from operations", "doc_id": "a", "chunk_id": "s0",
            "heading": None, "section_type": None, "page": 1}},
        {"id": "b:s0", "vector": [0.0, 1.0, 0.0, 0.0], "metadata": {
            "text": "revenue from operations", "doc_id": "b", "chunk_id": "s0",
            "heading": None, "section_type": None, "page": 1}},
    ])
    hits = store.search_text("revenue from operations", 10, filter='doc_id in ["a"]')
    assert [h["id"] for h in hits] == ["a:s0"]
```

Confirm `milvus` is a registered marker in `backend/pyproject.toml` (it is — `"milvus: needs a live Milvus instance..."`).

- [ ] **Step 3: Run it against live Milvus**

```bash
docker compose --env-file <repo-root>/.env -f docker/compose.yml --profile core --profile vector up -d
FDI_MILVUS_URI=http://localhost:19530 uv run --directory backend pytest tests/integration/test_milvus_bm25.py -q
```
Expected: 2 passed. **If BM25 does not rank the 3-term match first, stop** — the premise is wrong and the design needs revisiting, not the test.

Milvus writes are not immediately readable; if a search returns nothing, add `client.flush(collection_name)` after upsert in `MilvusVectorStore.upsert` rather than sleeping in the test.

- [ ] **Step 4: Full suite and gates**

```bash
uv run --directory backend pytest -q
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
```

- [ ] **Step 5: Commit**

```bash
git add backend/src/fdi/platform/config.py .env.example backend/tests/integration/test_milvus_bm25.py
git commit -m "feat(config): collection fdi_chunks_v2 + live-Milvus BM25 test

The BM25 Function cannot be added to an existing collection, so the
cutover is a new collection rather than an alter; fdi_sections is left in
place, making rollback a one-line revert.

The live test is the one that matters: a fake cannot establish that BM25
ranking discriminates by term overlap, which is the claim this whole
sub-project rests on."
```

---

## Verification (after all tasks)

Not a code task — the accuracy measurement, per the spec's §5 decision that no scoring script is written.

- [ ] Bring up `core` + `vector` profiles with `--env-file <repo-root>/.env` (`COMPOSE_DATA_ROOT` lives only in the repo-root `.env`, which is gitignored and absent from a worktree; without it every volume bind resolves to a bare path and etcd/Milvus fail to mount).
- [ ] Re-run the EX-1 end-to-end script (`e2e_run.py`), which seeds `ocr` and `semantic` from cache. The `index` stage re-runs because of the version bump; `embedding` is served from cache. **Expect 0 Azure DI calls, 0 semantic calls, 18 extraction calls.**
- [ ] Confirm the keyword channel is alive: all 18 unit `retrieval_query` strings must return a non-empty BM25 result set. (Baseline: all 18 returned zero.)
- [ ] Read the output JSON against `docs/architecture/field-extraction-schema.json` `example` values **directly** — no scoring script. Report populated fields against the **133/198** baseline, and specifically whether **RU_COMPANY** recovers from 0/9.
- [ ] Note that ISO-8601 dates (`2024-03-31`) are CORRECT against `dd-mm-yyyy` examples — 7 fields are affected and are not failures.

## Self-Review

**Spec coverage:** §3.1 schema v2 → Tasks 2–3. §3.2 seam → Tasks 1–2. §3.3 retrieval layer + `store` removal + three call sites → Tasks 4–6. §3.4 deletions → Tasks 5–7. §3.5 migration/cutover → Tasks 3 (version bump) and 8 (collection name). §4 error handling / 503 → Task 6. §5 testing → every task, plus the Verification block. §6 success criteria → Verification block.

**Gap found and closed while writing:** the spec's original schema table listed `doc_key`/`version` as Milvus fields, but `StageContext` exposes only `doc_id`/`read_artifact`/`read_source`/`services` — a Stage has no session and no `doc_key`, so `IndexStage` cannot write them. The spec was corrected first (commit `741505b`) to filter on `doc_id` and keep the Postgres translation; this plan follows the corrected spec, which is why `session` survives on the retrieval functions and only `store` is removed.

**Sequencing note:** Task 7 (dropping the `chunk` table) is deliberately last among the code tasks. Doing it earlier would break `keyword_search` before `bm25_search` exists, leaving the suite red across several tasks and destroying the per-task review gate.

**Two path/API errors found and fixed during this review, both of which would
have sent an implementer down a wrong road:**
1. Task 3 originally named `tests/unit/test_index_stage.py`, which **does not
   exist** — `IndexStage`'s only coverage is
   `tests/integration/test_index_enrichment.py`, an integration test over the
   real stage chain. An implementer would have created a redundant unit test
   with an invented `_ctx` helper.
2. Task 5's `ids_for` snippet used `self._session`; `DocumentRepository`
   stores its session as `self._s` (`repository.py:382`).

**Type consistency:** `search_text(query, k, *, filter="")` is identical in Tasks 1, 2, 5, 8. `build_vector_results(hits, docs, *, section_type, doc_key, version, k)` is identical in Tasks 4 and 5. `bm25_search(*, session, vector_store, query, k, filters=None)` is identical in Tasks 5 and 6. `_docs_for` is the name monkeypatched in Task 5's fixture and defined in Task 5's implementation.
