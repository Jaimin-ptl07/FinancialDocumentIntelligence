# RT-2 — Semantic (Vector) Retrieval

- **Status:** Draft — 2026-07-18. Second sub-project of the retrieval layer (`docs/architecture/
  retrieval-roadmap.md`), Wave B (parallel with RT-3/RT-5/RT-6, all depend only on RT-1).
- **Depends on:**
  - **RT-1** (`fdi.core.retrieval.build_chunks`, `ChunksStage`, and — the two primitives this spec
    builds directly on — `resolve_chunk`/`resolve_chunks` (`fdi.pipeline.retrieval`), which turn a
    `"{doc_id}:{chunk_id}"` hit id into `{doc_id, chunk_id, heading, text, section_type, page,
    para_count}`; and `IndexStage` v2's enriched Milvus metadata `{heading, chunk_id, section_type,
    page}` — `section_type` is what makes a vector-layer filter possible at all).
  - **SP-14** (`fdi.pipeline.vectorstore.VectorStore` protocol — `search(vector, k) ->
    [{id, score, metadata}]`, best-match-first; `FakeVectorStore`/`MilvusVectorStore`; `POST /search`;
    `EmbedderDep`/`VectorStoreDep` (`fdi.api.deps`, both `T | None`, wired from `app.state` in
    lifespan); the 503-when-unconfigured gate `/search` uses, which RT-2 reuses verbatim for
    `/retrieve/vector`).
  - **SP-13 / SP-13b** (`fdi.pipeline.embeddings.EmbeddingClient` protocol — `embed(texts) ->
    list[list[float]]`; `FakeEmbeddingClient`; `BgeEmbeddingClient`, local BGE-small/fastembed, no
    endpoint/key. RT-2's `vector_search` embeds the query through this same seam).
  - **SP-4** (document intake: `Document` model `{id, checksum, filename, byte_size, content_type,
    doc_key, version}`, both `doc_key`/`version` nullable; `DocumentRepository`. RT-2's `doc_key`/
    `version` filter is a Postgres join against this table keyed on the hit's `doc_id` — Milvus
    metadata deliberately excludes them, per RT-1 decision 6).
  - **SP-16** (`fdi.pipeline.crossdoc.resolve_entities` — the precedent for an on-demand, task-free API
    route that composes platform stores (repository + artifact store) directly, no stage/worker
    round-trip. `fdi.pipeline.retrieval.vector_search` is the same shape: `pipeline` importing
    `platform` + `core`, called synchronously from a route).

## 1. Purpose

RT-1 gave every retrieval tool a way to turn a hit id into text (`resolve_chunks`) and gave Milvus
metadata enough to filter by statement type (`section_type`). Nothing yet **uses** either: `POST
/search` still embeds a query, calls `VectorStore.search`, and returns the raw hit —
`id + score + {heading, chunk_id}` (or, since RT-1's `IndexStage` v2 landed, whatever richer metadata
happens to be on the hit; `/search` never reads it specially) — with **no text, no citation, no
filters**. A caller cannot ask "find balance-sheet passages about total assets in the 2024 10-K" — it
can only ask "find passages near this vector" and then separately re-derive everything else by hand.

RT-2 closes that gap with one orchestrator and one route:

1. **`vector_search(query, filters, k)`** (`fdi.pipeline.retrieval`) — embeds the query (BGE, via the
   injected `EmbeddingClient`), runs `VectorStore.search`, optionally filters by `section_type` (Milvus
   metadata, already on the hit — cheap, in-memory) and/or `doc_key`/`version` (NOT on the hit — a
   batched Postgres join on the hits' `doc_id`s), attaches text + citation via RT-1's `resolve_chunks`
   in one pass, and returns up to `k` results in the vector store's original best-match-first order.
2. **`POST /retrieve/vector`** — a thin route over the orchestrator, gated by the same
   missing-embedder/missing-vector_store 503 `/search` already uses. Every result carries a full
   citation: `{doc_id, doc_key, version, chunk_id, heading, page, section_type, text, score}`.

`POST /search` is **left unchanged** — see decision 5 below for why, and why that is a deliberate
divergence from the roadmap's literal "upgrades `/search` to return text" wording that a human should
confirm before implementation.

## 2. Scope decisions (autonomous; documented)

1. **Orchestrator signature and location.** `fdi.pipeline.retrieval.vector_search(*, session:
   AsyncSession, store: ArtifactStore, embedder: EmbeddingClient, vector_store: VectorStore, query: str,
   k: int, filters: Mapping[str, object] | None = None) -> list[dict[str, object]]`. It lives beside
   `resolve_chunk`/`resolve_chunks` in `fdi.pipeline.retrieval` (same file, same layer: pipeline imports
   platform + core, does I/O) rather than a new module — RT-2 is RT-1's first consumer, not a new
   subsystem. `filters` is a plain `Mapping` (not a dataclass) at this layer, matching `resolve_chunks`'
   own preference for primitive shapes over the pipeline/platform boundary; the API layer's Pydantic
   model (§3.3) is where the named, validated shape lives.
2. **Pure/impure split, mirroring `resolve_entities`/`fdi.core.crossdoc`.** The actual filter + join +
   truncate logic is a **pure** function, `fdi.core.retrieval.build_vector_results` (new function in the
   existing `fdi.core.retrieval` module, alongside `build_chunks`): given an already-fetched hit list,
   an already-resolved chunk map, and an already-fetched `{doc_id: {doc_key, version}}` dict, it filters,
   joins, and truncates to `k` — no I/O, no ORM types crossing in (docs are plain dicts, not `Document`
   rows), fully unit-testable without Docker. `vector_search` (pipeline) does only the I/O: embed, search,
   batch-fetch `Document` rows, call `resolve_chunks`, then delegates the assembly to
   `build_vector_results`. Same shape as `resolve_entities` delegating clustering to
   `fdi.core.crossdoc.cluster`/`consolidate_entity`.
3. **Filtering split — `section_type` in-memory, `doc_key`/`version` via a single batched Postgres
   join, over-fetch factor `5`, single-shot (no adaptive re-query).** `VectorStore.search(vector, k)`
   has no filter parameter (SP-14's protocol, unchanged by RT-1) and this spec does **not** extend it —
   see decision 4. So:
   - If no filter is set, `vector_search` requests exactly `k` from `VectorStore.search` (unchanged cost
     from today's `/search`).
   - If any filter (`section_type`, `doc_key`, or `version`) is set, it requests `k * _OVERFETCH_FACTOR`
     (`_OVERFETCH_FACTOR = 5`, a private module constant in `fdi.pipeline.retrieval`, **not** a new
     `FDI_` setting — there is no deployment-time reason yet to tune it, and the assignment's own
     guidance is to prefer a constant when one is sensible). `section_type` is checked first, in memory,
     against each hit's existing metadata (no extra I/O). `doc_key`/`version` are checked against a
     **single** `DocumentRepository.get_many(doc_ids)` batched query over the *distinct* `doc_id`s among
     the (possibly section_type-filtered) surviving hits — never one query per hit (that would be the
     N+1 RT-1 explicitly avoided in `resolve_chunks` by reading each doc's `chunks` artifact once).
   - This is single-shot: if fewer than `k` hits survive filtering, `vector_search` returns fewer than
     `k` — it does **not** loop with a larger `k'` to top back up. §6 defers adaptive over-fetch.
   - The `Document` batch-fetch happens **whether or not** `doc_key`/`version` filters are set, because
     the *citation* on every returned result always carries `doc_key`/`version` (decision 6) — filtering
     and citation-joining share the one query.
4. **`VectorStore.search(vector, k)`'s signature is NOT extended with a filter parameter.** Pushing
   `section_type` into a Milvus query expression was considered and rejected for this slice: it would
   change the `VectorStore` protocol (both `FakeVectorStore` and `MilvusVectorStore`), and the over-fetch
   approach above gets the same functional result (fewer candidates reach the client) without touching a
   protocol RT-3/RT-5/RT-6 also depend on mid-Wave-B. RT-1's own §6 already deferred pushing `doc_key`/
   `version` into Milvus; this spec extends that deferral to `section_type` too, for the same reason —
   revisit only if a real corpus shows the over-fetch factor is measurably too expensive.
5. **`POST /search` is left unchanged; `POST /retrieve/vector` is the new, additive endpoint. This is
   the genuine fork the assignment calls out ("upgrade `/search` too ... or keep it and add
   `/retrieve/vector`; decide and document") and a human should confirm the choice before
   implementation, because the roadmap's RT-2 row literally says "upgrades `/search` to return text."**
   Reasons for keeping `/search` as-is:
   - **Contract mismatch.** `/search` today returns *exactly* what `VectorStore.search(vector, k)`
     returned, verbatim metadata, never dropping a hit. Both RT-2 filters and `resolve_chunks` are
     drop-on-miss by design (§2 decision 3 above; RT-1 §4). Grafting either onto `/search` means either
     (a) inventing a third, non-dropping variant of the same logic — duplicated complexity for no
     stated need — or (b) silently changing `/search`'s "always exactly `k` results" behavior, which is
     itself the kind of silent behavior change this codebase's own conventions treat as a regression
     (see `platform/config.py`'s stray-env-var scan for the house style on "silent" being the thing to
     avoid).
   - **A real test would regress.** `backend/tests/integration/test_api.py::
     test_search_returns_the_best_match_first` upserts synthetic vectors directly into a
     `FakeVectorStore` with **no** backing Postgres `Document` row and **no** `chunks` artifact — the
     exact "probe the raw index" scenario `/search` exists for (e.g. inspecting what got indexed before
     a corpus has been fully processed through `chunks`). Routing `/search` through
     `resolve_chunks`/citation joins would silently return **zero** results for that hit (both the
     chunk resolution and the doc lookup would come back empty), turning a documented "upgrade" into an
     observed regression for that exact, already-tested use case.
   - **`/retrieve/vector` is strictly additive** and is what the roadmap's *tool* description
     (`vector_search(query, filters, k)` → citations) actually specifies; every future caller who wants
     text/filters/citations uses it. `/search` remains available as the raw, uncited, never-dropping
     probe it already is — undocumented as "upgraded" but not removed or behaviorally changed either.
   - If a human overrules this, the mechanical change is small (extend `SearchResultView` with optional
     `text`/`section_type`/`page`/`doc_key`/`version` fields, resolve best-effort without dropping) —
     but it is a second, additive-only code path, not a reuse of `vector_search`, precisely because
     `vector_search`'s drop-on-miss contract cannot serve `/search`'s never-drop one.
6. **Citation shape is exactly the roadmap's five extra fields plus score/id parts**: `{doc_id, doc_key,
   version, chunk_id, heading, page, section_type, text, score}`. `text` is always a non-`None` `str`
   (RT-1's `build_chunks` guarantees this — even a heading-less, paragraph-less section is `""`).
   `heading`/`page`/`section_type` are `str | None` / `int | None` / `str | None`, following `resolve_chunk`'s
   record verbatim. `doc_key`/`version` are `str | None` / `int | None`: `None` when the `Document` row's
   own column is `None` (SP-4 allows a nullable `doc_key`/`version`) **or** — only possible when neither
   `doc_key` nor `version` filters are set (§2 decision 3's join-happens-regardless rule) — when no
   `Document` row exists at all for the hit's `doc_id`. If a `doc_key`/`version` filter IS set, a hit
   whose `Document` row is missing or whose column doesn't match is dropped, never returned with a
   `None` that silently fails to "match" the filter it was asked to satisfy.
7. **Order is preserved, never re-sorted.** `VectorStore.search` returns best-match-first (SP-14).
   `build_vector_results` filters and truncates but never re-sorts — the returned list's order is a
   strict sub-sequence of the (over-fetched) hit list's order, so score-descending is preserved for
   free without re-deriving it from `score` a second time (which would also be wrong once ties exist).
8. **Route:** `POST /retrieve/vector`, request `{query: str, k: int = 5, filters:
   {section_type: str|None, doc_key: str|None, version: int|None} | None = None}`, response
   `{results: [{doc_id, doc_key, version, chunk_id, heading, page, section_type, text, score}]}`. Same
   503 gate as `/search` (`missing = [name for name, svc in (("embedder", embedder), ("vector_store",
   vector_store)) if svc is None]`), same detail-message shape, so an operator sees one consistent error
   contract across both vector endpoints.
9. **No new `FDI_` config var.** `_OVERFETCH_FACTOR` is a private constant (decision 3). Nothing else in
   this slice needs a knob: `k`/filters are per-request (already the case for `/search`'s `k`), and
   `resolve_chunks`/`DocumentRepository.get_many` need no new settings. If the config trio (`config.py` +
   `compose.yml` + `.env.example`) is ever touched, it is only because a later measurement shows the
   over-fetch factor needs to be tunable — not part of this slice.

## 3. Architecture

### 3.1 `fdi.core.retrieval.build_vector_results` (pure, appended to the existing module)

```python
def build_vector_results(
    hits: list[Mapping[str, object]],
    resolved: Mapping[str, Mapping[str, object]],
    docs: Mapping[str, Mapping[str, object]],
    *,
    section_type: str | None,
    doc_key: str | None,
    version: int | None,
    k: int,
) -> list[dict[str, object]]:
    """Pure post-processing of an ALREADY-FETCHED vector-store hit list into
    RT-2's cited result records. `hits` is VectorStore.search's return shape
    ({id, score, metadata}, best-match-first); `resolved` is a resolve_chunks()
    result ({"{doc_id}:{chunk_id}": chunk record}); `docs` is a plain
    {doc_id: {"doc_key": ..., "version": ...}} dict (already read from Postgres
    by the caller -- no ORM type crosses into core). Filters, joins citations,
    and truncates to k WITHOUT re-sorting -- the input order (best-match-first)
    is preserved. A hit is dropped, not erred, when: its section_type doesn't
    match; a doc_key/version filter is set and its Document row is missing or
    doesn't match; or its chunk id is absent from `resolved`. No I/O."""
```

Algorithm (mirrors `build_chunks`' degrade-not-raise discipline):
```
results = []
for hit in hits:
    full_id = str(hit["id"])
    if section_type is not None:
        meta = hit.get("metadata")
        hit_type = meta.get("section_type") if isinstance(meta, Mapping) else None
        if hit_type != section_type:
            continue

    doc_id = full_id.split(":", 1)[0] if ":" in full_id else full_id
    doc = docs.get(doc_id)
    if doc_key is not None and (doc is None or doc.get("doc_key") != doc_key):
        continue
    if version is not None and (doc is None or doc.get("version") != version):
        continue

    rec = resolved.get(full_id)
    if rec is None:
        continue

    score = hit.get("score")
    results.append({
        "doc_id": rec.get("doc_id"),
        "doc_key": doc.get("doc_key") if doc is not None else None,
        "version": doc.get("version") if doc is not None else None,
        "chunk_id": rec.get("chunk_id"),
        "heading": rec.get("heading"),
        "page": rec.get("page"),
        "section_type": rec.get("section_type"),
        "text": rec.get("text"),
        "score": float(score) if isinstance(score, (int, float)) else 0.0,
    })
    if len(results) >= k:
        break
return results
```

### 3.2 `fdi.pipeline.retrieval.vector_search` (orchestrator, appended to the existing module)

```python
_OVERFETCH_FACTOR = 5


async def vector_search(
    *,
    session: AsyncSession,
    store: ArtifactStore,
    embedder: EmbeddingClient,
    vector_store: VectorStore,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-2's semantic retrieval orchestrator: embed `query`, search `vector_store`,
    filter/join/cite via build_vector_results, attaching text through RT-1's
    resolve_chunks. Over-fetches k*_OVERFETCH_FACTOR from the vector store when any
    filter is set (VectorStore.search has no filter parameter -- see design spec
    sec 2.3-4), single-shot (no adaptive re-query on under-fill). Pipeline layer:
    imports platform (repository + store) and core, no I/O left inline -- the
    filter/join/truncate algorithm itself lives in fdi.core.retrieval, pure and
    independently unit-tested."""
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None
    has_filter = section_type is not None or doc_key is not None or version is not None

    [vector] = embedder.embed([query])
    fetch_k = k * _OVERFETCH_FACTOR if has_filter else k
    hits = vector_store.search(vector, fetch_k)

    doc_ids = {str(h["id"]).split(":", 1)[0] for h in hits if ":" in str(h["id"])}
    doc_rows = await DocumentRepository(session).get_many(list(doc_ids))
    docs: dict[str, Mapping[str, object]] = {
        doc_id: {"doc_key": d.doc_key, "version": d.version} for doc_id, d in doc_rows.items()
    }

    resolved = await resolve_chunks(session=session, store=store, ids=[str(h["id"]) for h in hits])

    return build_vector_results(
        hits,
        resolved,
        docs,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key),
        version=cast(int | None, version),
        k=k,
    )
```

### 3.3 `fdi.platform.db.repository.DocumentRepository.get_many` (new method)

```python
async def get_many(self, ids: list[str]) -> dict[str, Document]:
    """Every `Document` row whose id is in `ids`, keyed by id. Empty input -> no
    query (mirrors MilvusVectorStore.upsert's empty-batch skip: a batch fetch for
    zero ids is an unforced round trip, not a correct-but-wasteful one). The ONE
    query RT-2's vector_search uses for both the doc_key/version filter check and
    the doc_key/version citation join -- never one SELECT per hit."""
    if not ids:
        return {}
    rows = (await self._s.execute(select(Document).where(Document.id.in_(ids)))).scalars().all()
    return {d.id: d for d in rows}
```

### 3.4 `fdi.api.schemas` (new Pydantic models)

```python
class VectorSearchFilters(BaseModel):
    section_type: str | None = None
    doc_key: str | None = None
    version: int | None = None


class VectorSearchRequest(BaseModel):
    query: str
    k: int = 5
    filters: VectorSearchFilters | None = None


class VectorSearchResultView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    chunk_id: str
    heading: str | None
    page: int | None
    section_type: str | None
    text: str
    score: float


class VectorSearchResultsView(BaseModel):
    results: list[VectorSearchResultView]
```

### 3.5 `POST /retrieve/vector` (`fdi.api.routes`, new route)

```python
@router.post("/retrieve/vector", response_model=VectorSearchResultsView)
async def retrieve_vector(
    body: VectorSearchRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
) -> VectorSearchResultsView:
    missing = [
        name
        for name, svc in (("embedder", embedder), ("vector_store", vector_store))
        if svc is None
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                f"retrieve/vector is not configured: missing {', '.join(missing)} "
                "(set FDI_MILVUS_URI -- embeddings run locally via BGE and need no endpoint/key)"
            ),
        )
    assert embedder is not None
    assert vector_store is not None
    filters = {
        "section_type": body.filters.section_type if body.filters else None,
        "doc_key": body.filters.doc_key if body.filters else None,
        "version": body.filters.version if body.filters else None,
    }
    results = await vector_search(
        session=session,
        store=store,
        embedder=embedder,
        vector_store=vector_store,
        query=body.query,
        k=body.k,
        filters=filters,
    )
    return VectorSearchResultsView(
        results=[VectorSearchResultView.model_validate(r) for r in results]
    )
```

The route stays thin (assembly, dependency wiring, the 503 gate) — every real decision (over-fetch,
filtering, citation-joining) lives in `vector_search`/`build_vector_results`, testable without an ASGI
client.

## 4. Error handling

- **Missing embedder/vector_store:** `POST /retrieve/vector` returns 503 with the same detail-message
  shape as `/search` (§2 decision 8). Never a 500.
- **No filter set:** behaves like today's `/search` cost-wise — exactly `k` requested from the vector
  store — but every surviving, resolvable hit still gets a citation (doc_key/version may be `None` if
  no `Document` row exists, never an error).
- **Filter set, corpus has fewer than `k` matches:** `vector_search` returns fewer than `k` results — not
  an error, not a retry loop (§2 decision 3, single-shot).
- **A hit's chunk id is unresolvable** (no `chunks` artifact for that `doc_id`, or the specific
  `chunk_id` is absent — RT-1 §4's documented `resolve_chunks` behavior): the hit is dropped, silently,
  from `/retrieve/vector`'s results. Never raises.
- **A hit's `doc_id` has no `Document` row** (deleted, or a Milvus-only leftover from a differently-keyed
  test fixture): if no `doc_key`/`version` filter is set, the hit is still returned with `doc_key: null,
  version: null`. If a `doc_key`/`version` filter IS set, the hit is dropped (§2 decision 6).
- **`k <= 0`:** not specially validated in this slice (matches `/search`'s existing, equally
  unvalidated `k`) — `vector_store.search(vector, 0)` / `FakeVectorStore.search`'s `scored[:0]` both
  degrade to an empty result list, not an error; a negative `k` is out of scope (§6, consistent with
  `/search`'s pre-existing gap, not a new one this spec introduces).
- **`build_vector_results({}, {}, {}, ...)`-shaped empty input** → `[]`. Pure, total, never raises.
- **`DocumentRepository.get_many([])`** → `{}`, no query issued.

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_retrieval_vector_results.py`:
  - No filters set → every resolvable hit returned, in input order, truncated to `k`.
  - `section_type` filter → non-matching hits dropped; a hit with no `metadata` (or non-`Mapping`
    metadata) treated as non-matching, never raises.
  - `doc_key` filter, `version` filter, and both together → non-matching / missing-`Document`-row hits
    dropped; matching hits carry the filtered doc's `doc_key`/`version` on the citation.
  - `doc_key`/`version` both unset, and a hit's `doc_id` absent from `docs` → citation's `doc_key`/
    `version` are `None`, hit still returned (not dropped).
  - A hit whose id is absent from `resolved` → dropped, remaining hits' order/truncation unaffected.
  - Truncation: more resolvable/matching hits than `k` → exactly `k` returned, in original order (assert
    against a hit list that is NOT score-sorted going in, to prove `build_vector_results` does not
    re-sort).
  - Empty `hits`/`resolved`/`docs` → `[]`.
- **Integration** (testcontainers + fakes), `backend/tests/integration/test_document_repository.py`
  (extend the existing file): `get_many([])` → `{}`, no query; `get_many([id1, id2, unknown-id])` on two
  created documents → `{id1: Document, id2: Document}` (unknown id simply absent, not an error).
- **Integration** (testcontainers + fakes), `backend/tests/integration/test_vector_search.py`: reuse
  `test_index_stage.py`'s harness (a LOCAL `StageRegistry` running `ocr → … → chunks → embedding →
  index` with `FakeOcrClient`/`FakeSemanticClient`/`FakeEmbeddingClient`/`FakeVectorStore`) to fully
  process **two or three** documents with **distinct `doc_key`/`version`/`section_type`** combinations
  (e.g. two different `doc_key`s, one with two versions, headings classified into different
  `SectionType`s via `FakeSemanticClient`). Then, calling `vector_search` directly (not through HTTP):
  - No filters → results carry correct `doc_id`/`doc_key`/`version`/`chunk_id`/`heading`/`page`/
    `section_type`/`text`/`score` for the seeded documents (membership assertions over *this fixture's*
    doc ids — Postgres/MinIO are session-scoped across the suite, per the ingestion-suite precedent).
  - `section_type` filter → only chunks of that type come back, from any seeded document.
  - `doc_key` filter → only that document's chunks come back.
  - `doc_key` + `version` filter → only that exact version's chunks.
  - Over-fetch: a `FakeVectorStore` subclass (or a thin spy wrapping one) that records the `k` argument
    `search` was called with; assert it received `k * 5` when a filter was set and exactly `k` when none
    was.
  - A filter matching nothing → `[]`, not an error.
- **Integration** (testcontainers + fakes), extend `backend/tests/integration/test_api.py`: `POST
  /retrieve/vector` → 503 when `vector_store`/`embedder` unconfigured (mirrors
  `test_search_503_when_vector_store_unconfigured`); a success case with `dependency_overrides` for
  `get_embedder`/`get_vector_store` (mirrors `test_search_returns_the_best_match_first`) but built over a
  REAL processed document (via `session_factory`/`store`, same pattern as `test_vector_search.py`) so the
  route's Postgres join and `resolve_chunks` call have something real to resolve against; assert the
  response body's `results[0]` carries `text`/`doc_key`/`version`/`section_type`/`page` alongside
  `doc_id`/`chunk_id`/`score`. A second case exercises `filters.section_type` through the HTTP body.
  **`POST /search`'s existing tests are asserted unchanged** (no modification to
  `test_search_503_when_vector_store_unconfigured` / `test_search_returns_the_best_match_first`) — this
  is itself part of proving §2 decision 5's "keep `/search` as-is" choice didn't regress anything.

## 6. Explicitly deferred (YAGNI)

- **Adaptive/iterative over-fetch.** If `k * 5` under-fills after filtering (a very selective filter over
  a small corpus), RT-2 returns fewer than `k` rather than re-querying with a larger `k'` — a later
  optimization only if real corpora show the single-shot factor is insufficient often enough to matter.
- **Pushing `section_type` (or `doc_key`/`version`) into a Milvus query expression.** Would require
  extending the `VectorStore` protocol's `search` signature — deferred alongside RT-1's existing
  `doc_key`/`version`-in-Milvus deferral (§2 decision 4); revisit only if the over-fetch cost is measured
  to be a real problem.
- **A tunable `FDI_` over-fetch-factor setting.** `_OVERFETCH_FACTOR` is a constant (§2 decision 9); no
  operational need yet to change it without a redeploy.
- **Upgrading `/search` itself.** Deliberately not done this slice (§2 decision 5) — flagged for human
  confirmation, not silently dropped from scope.
- **Re-ranking (cross-encoder) or fusion with keyword results.** RT-4's concern (Reciprocal Rank Fusion
  over RT-2 + RT-3), not RT-2's.
- **A `GET`-shaped or query-string variant of `/retrieve/vector`.** `POST` matches `/search`'s existing
  convention (a request body carries `query`, and now `filters`) and the roadmap's literal `POST
  /retrieve/vector` spelling.

## 7. Done criteria

1. `fdi.core.retrieval.build_vector_results(hits, resolved, docs, *, section_type, doc_key, version, k)`
   implements §3.1 exactly: filters `section_type` in-memory, `doc_key`/`version` against the supplied
   `docs` dict, joins citations, truncates to `k` WITHOUT re-sorting, never raises on empty/malformed
   input; `core` stays pure (no I/O, no import of `fdi.platform.*`/`fdi.pipeline.*`).
2. `fdi.platform.db.repository.DocumentRepository.get_many(ids)` returns `{id: Document}` for exactly
   the ids that exist, `{}` for an empty or all-unknown input, no per-id query.
3. `fdi.pipeline.retrieval.vector_search(*, session, store, embedder, vector_store, query, k, filters)`
   implements §3.2 exactly: embeds via the injected `EmbeddingClient`, over-fetches `k*5` only when a
   filter is set (verified by a call-count/argument test), delegates all filtering/joining/truncation to
   `build_vector_results`, and its own I/O is exactly one `vector_store.search`, one
   `DocumentRepository.get_many`, and one `resolve_chunks` call (no N+1).
4. `POST /retrieve/vector` (`fdi.api.schemas.VectorSearchRequest`/`VectorSearchResultsView`) returns the
   full citation shape `{doc_id, doc_key, version, chunk_id, heading, page, section_type, text, score}`
   per result, gated by the same 503-when-unconfigured pattern as `/search`; `POST /search`'s existing
   behavior and existing tests are UNCHANGED (§2 decision 5, pending human confirmation of that choice).
5. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); §5's tests
   all pass, including the over-fetch-factor assertion and the doc_key/version-filter-drops-mismatches
   assertion; §6 deferred items remain out of scope; the retrieval roadmap's RT-2 row flips to done with
   a "what RT-2 shipped" handoff (the `vector_search`/`build_vector_results` split, the over-fetch
   constant, the `/search`-left-unchanged decision and its rationale, `DocumentRepository.get_many` as a
   reusable batched-doc-lookup primitive later RT tools may also want).
