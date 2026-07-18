# RT-4 — Hybrid Retrieval (Reciprocal Rank Fusion)

- **Status:** Draft — 2026-07-18. Fourth sub-project of the retrieval layer
  (`docs/architecture/retrieval-roadmap.md`), Wave C (needs RT-2 + RT-3, both
  done). Light weight per the roadmap: one pure fusion function, one
  orchestrator, one route.
- **Depends on:**
  - **RT-2** (`docs/superpowers/specs/2026-07-18-rt2-vector-retrieval-design.md`,
    implemented): `fdi.pipeline.retrieval.vector_search(*, session, store,
    embedder, vector_store, query, k, filters=None) -> list[dict[str, object]]`
    — hits shaped `{doc_id, doc_key, version, chunk_id, heading, page,
    section_type, text, score}`, best-match-first, already citation-complete
    (RT-2's own `build_vector_results` never re-sorts).
  - **RT-3** (`docs/superpowers/specs/2026-07-18-rt3-keyword-retrieval-design.md`,
    implemented): `fdi.pipeline.retrieval.keyword_search(*, session, query,
    k=5, section_type=None, doc_key=None, version=None) ->
    list[dict[str, object]]` — the SAME nine-key hit shape, `ts_rank`-ordered,
    best-first. `chunk.id` (`fdi.platform.db.models.Chunk`) reuses the unified
    chunk identity `f"{doc_id}:{chunk_id}"` (roadmap cross-cutting decision 1),
    the same identity Milvus already uses — RT-4's fusion compares a keyword
    hit and a vector hit directly, no re-keying.
  - **RT-1** (`fdi.core.retrieval`, implemented): the module RT-4's `fuse`
    joins, alongside `build_chunks`/`build_vector_results`/
    `chunk_rows_for_index`. RT-1's `s{i}` chunk identity and RT-2/RT-3's
    convergence on the same nine-key hit shape are what make fusion "a plain
    merge on id, no field-renaming layer" (RT-3's own handoff note, roadmap
    "What RT-3 shipped").
  - **`fdi.api.deps`** (`EmbedderDep`/`VectorStoreDep`, both `T | None`,
    request-scoped from `app.state`, wired in `api/app.py`'s lifespan) — the
    503-or-degrade decision below is exactly about how `POST /retrieve`
    handles these two dependencies being `None`.

## 1. Purpose

RT-2 answers "what's semantically similar"; RT-3 answers "what matches this
vocabulary exactly, ranked by term relevance." Neither alone is the whole
story for a financial document corpus: an exact line-item label
(`"goodwill impairment"`) is a keyword win, a paraphrased question
("how did asset quality change") is a vector win, and a real user query is
often a mix of both. RT-4 closes the gap with the classic model-free fusion
technique — **Reciprocal Rank Fusion (RRF)** — and one new route:

1. **`fuse(vector_hits, keyword_hits, *, k, rrf_k=60)`** (`fdi.core.retrieval`)
   — a **pure** function. Each hit list is best-first (rank 1 = best). Every
   unique chunk (`{doc_id}:{chunk_id}`) scores `sum over lists of
   1/(rrf_k + rank)`; a chunk found by both lists sums two terms and
   routinely outranks a chunk found by only one. Output is sorted by fused
   score descending, ties broken deterministically by the `{doc_id}:{chunk_id}`
   string, truncated to `k`.
2. **`hybrid_search(*, session, store, embedder, vector_store, query, k,
   filters=None)`** (`fdi.pipeline.retrieval`) — calls `vector_search` and
   `keyword_search` with the SAME query/filters/over-fetch `k`, `fuse`s the
   two hit lists, truncates to `k`. Degrades to keyword-only when
   `embedder`/`vector_store` are unconfigured — see decision 2.
3. **`POST /retrieve`** — a thin route over the orchestrator. Every result
   carries the same nine-field citation RT-2/RT-3 already established, with
   `score` replaced by the fused RRF score.

## 2. Scope decisions (autonomous; documented)

1. **`fuse`'s identity is computed, not read off a field.** Neither
   `vector_search`'s nor `keyword_search`'s hit dicts carry a combined `id`
   field — both expose `doc_id` and `chunk_id` separately (RT-2 §3.1, RT-3
   §3.5). `fuse` builds `f"{doc_id}:{chunk_id}"` itself from each hit, per
   the roadmap's cross-cutting decision 1 (the same identity Milvus and
   `chunk.id` already use). A hit missing a `str` `doc_id` or `chunk_id` is
   **skipped, not erred** — mirrors `resolve_chunks`' "malformed id is
   omitted" posture (RT-1 §4) rather than raising, since `fuse` must stay
   pure and total over whatever its two callers hand it.
2. **Degradation policy: `hybrid_search` degrades to keyword-only when
   `embedder`/`vector_store` is `None`; `POST /retrieve` never 503s.** This
   is the genuine fork the assignment calls out, and the chosen answer is a
   deliberate divergence from RT-2's/`/search`'s 503-when-unconfigured
   precedent — reasoning:
   - `keyword_search` has **no external dependency it can lack**: Postgres is
     *mandatory* `Settings` config (RT-3 §2 decision 9, unlike
     `milvus_uri`/`neo4j_uri`, which are optional so the stack boots without
     those profiles). A hybrid endpoint that 503s whenever Milvus is
     unconfigured would make the WHOLE endpoint unusable in every deployment
     that hasn't stood up Milvus yet, even though `keyword_search` alone is
     always able to answer *something*.
   - `vector_search`/`keyword_search` already fail independently and
     predictably: `hybrid_search` calls `keyword_search` unconditionally,
     and calls `vector_search` only `if embedder is not None and
     vector_store is not None`, else uses `vector_hits = []`. `fuse([],
     keyword_hits, k=k)` degrades correctly by construction (decision 1's
     "malformed/empty input never raises" + `fuse`'s own empty-list test) —
     no special-casing needed inside `fuse` itself, the degradation lives
     entirely in the orchestrator's dependency check.
   - **Consequence, flagged for human review:** unlike `/search` and
     `/retrieve/vector`, `POST /retrieve` has **no 503 branch at all** in
     this slice — a caller cannot distinguish "hybrid search answered from
     both tools" from "Milvus is unconfigured, this is keyword-only" purely
     from the response shape (only from the `score` field's magnitude, which
     is not documented as a reliable signal for that purpose). A human may
     want an explicit `degraded: bool` or `sources_used: list[str]` field on
     `HybridSearchResultsView` to make this visible to a caller/operator
     without requiring them to infer it from RRF score ranges. **Not
     built in this slice (§6)** — flagged, not silently decided.
3. **Dedup / citation-merge rule: the RICHEST citation wins.** RT-2's
   `build_vector_results` can legitimately return `doc_key: null, version:
   null` for a hit whose `Document` row doesn't exist (only when NEITHER
   filter is set — RT-2 §2 decision 6), while RT-3's `keyword_search` rows
   always carry `doc_key`/`version` because `index_chunks_fts` denormalizes
   them onto every `chunk` row at write time (RT-3 §2 decision 2) — so the
   SAME chunk found by both tools can arrive with citations of different
   completeness. `fuse` counts non-`None` values among `{doc_key, version,
   heading, page, section_type}` (`text` excluded — RT-1 guarantees it is
   always a `str`, possibly `""`, so it is never a completeness signal) for
   each candidate hit under a given `{doc_id}:{chunk_id}` and keeps whichever
   has the higher count; **a tie keeps whichever hit was seen first**, and
   `fuse`'s own argument order (`vector_hits` before `keyword_hits`) makes
   that deterministic — no reliance on dict-ordering as an implicit contract,
   the tie-break is an explicit `>` comparison, not a `>=`. This is the
   simplest deterministic policy that satisfies "keep the richest citation";
   a field-by-field merge (take whichever hit's `page` is non-`None`,
   independently of whichever hit's `doc_key` is non-`None`) was considered
   and rejected as unnecessary complexity — in practice RT-2 and RT-3 read
   from the SAME `chunks` artifact and the SAME `Document` row, so the two
   citations differ only in the narrow "no filter set, no Document row"
   corner case RT-2 §2 decision 6 documents, not in some fields being richer
   in one and other fields richer in the other.
4. **RRF constant `rrf_k = 60`, a private module constant
   (`_RRF_K`), not a new `FDI_` setting.** `60` is the standard value from
   the original RRF paper (Cormack, Clarke & Buettcher 2009) and the value
   most hybrid-search implementations default to; it is large relative to
   realistic rank positions (a handful to a few dozen), which is what gives
   RRF its characteristic property of caring more about "is this hit
   present, and roughly how high" than about precisely calibrating raw
   vector cosine scores against `ts_rank` floats (two incomparable scales —
   RRF's entire reason for existing over "just average the two scores" is
   that it never has to reconcile them). No deployment-time reason to tune
   it yet — mirrors RT-2's own `_OVERFETCH_FACTOR` precedent (a private
   constant, not a setting, "revisit only if a real corpus shows it's
   measurably wrong"). `fuse` still accepts `rrf_k` as an optional keyword
   argument (default `_RRF_K`) purely so the unit tests can pin the formula
   against a small, human-checkable `rrf_k` without being forced through
   `60`'s (correct, but larger) arithmetic on every assertion — the
   orchestrator and route never pass a non-default value.
5. **`hybrid_search` over-fetches `k * _FUSE_OVERFETCH_FACTOR` (factor `3`,
   a private module constant in `fdi.pipeline.retrieval`, distinct from
   RT-2's own `_OVERFETCH_FACTOR = 5`) from EACH of `vector_search` and
   `keyword_search`, then fuses and truncates to `k`.** Rationale: RRF's
   quality depends on the candidate pool each side contributes being deeper
   than the final `k` — a chunk that is the single best keyword match but
   only the 8th-best vector match still deserves to surface in a fused
   top-5 if `vector_search` was only asked for `k=5`. A factor of `3` is a
   documented **guess**, not a measurement (there is no real corpus to
   measure against yet) — chosen smaller than RT-2's own `5` because RT-4's
   over-fetch is doubled cost (BOTH tools over-fetch, not one), and `rrf_k =
   60`'s slow score decay (decision 4) means ranks past roughly the top
   dozen contribute little to the fused score regardless of pool depth, so
   a large multiplier buys little. **Flagged for human/measured revisit**
   once a real corpus shows whether `3` is too shallow (fused results miss
   good matches) or wastefully deep (no observed effect past a smaller
   factor) — §6.
6. **`hybrid_search`'s own `filters` argument is passed to BOTH
   `vector_search` and `keyword_search` unchanged** — same
   `{section_type, doc_key, version}` shape both already accept (RT-2 §2
   decision 1, RT-3's `keyword_search` signature). No new filter shape, no
   translation layer: a `section_type="NOTES"` hybrid query filters the
   Milvus-metadata check inside `vector_search` AND the Postgres `WHERE`
   clause inside `keyword_search` identically, so a chunk excluded by one
   tool's filter is also excluded by the other's (both would drop it before
   `fuse` ever sees it) — filtering is NOT re-implemented inside `fuse`,
   which stays a pure re-ranker over whatever its two inputs already
   contain.
7. **`fuse`'s output score is the fused RRF value, not either tool's raw
   `score`.** `HybridSearchResultView.score` is therefore NOT comparable to
   `VectorSearchResultView.score` (cosine similarity) or
   `KeywordHitView.score` (`ts_rank`) — a documented, deliberate unit change
   (both raw scores were already on incomparable scales to each other,
   which is decision 4's whole reason for choosing RRF over score-averaging;
   the fused output inherits that "don't compare across endpoints" posture,
   now applying to `/retrieve` vs `/retrieve/vector`/`/retrieve/keyword` as
   well as to vector-vs-keyword internally).
8. **No new HTTP route beyond `POST /retrieve`.** `fuse` gets no dedicated
   route of its own (it is not independently useful without two hit lists to
   fuse — nothing to call it with over HTTP) and `hybrid_search` is exposed
   through exactly one route, mirroring RT-2/RT-3's one-orchestrator-one-route
   shape.
9. **No new `FDI_` config.** `_RRF_K` and `_FUSE_OVERFETCH_FACTOR` are
   private constants (decisions 4–5). `hybrid_search` needs no new settings:
   `k`/`filters` are per-request (already true for `/retrieve/vector` and
   `/retrieve/keyword`), and Postgres/Milvus/BGE configuration is entirely
   inherited from RT-2/RT-3's existing wiring.

## 3. Architecture

### 3.1 `fdi.core.retrieval.fuse` (pure, appended to the existing module)

```python
_RRF_K = 60


def _citation_richness(hit: Mapping[str, object]) -> int:
    return sum(
        1
        for key in ("doc_key", "version", "heading", "page", "section_type")
        if hit.get(key) is not None
    )


def fuse(
    vector_hits: list[Mapping[str, object]],
    keyword_hits: list[Mapping[str, object]],
    *,
    k: int,
    rrf_k: int = _RRF_K,
) -> list[dict[str, object]]:
    """Reciprocal Rank Fusion (RRF) over two best-first, citation-shaped hit
    lists (RT-2's vector_search / RT-3's keyword_search output shape --
    {doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
    score}). Each hit is identified by "{doc_id}:{chunk_id}" (roadmap
    cross-cutting decision 1; computed here, not read off a field). A hit's
    fused score is the sum, over every list it appears in, of
    1/(rrf_k + rank), rank 1-based. A hit found by both lists therefore
    routinely outranks one found by only one. Output is sorted by fused
    score descending, ties broken by the "{doc_id}:{chunk_id}" string
    ascending (deterministic; no reliance on input order or sort stability
    once scores tie), truncated to k. When the same chunk appears in both
    lists with citations of different completeness, the RICHEST citation
    (most non-None among doc_key/version/heading/page/section_type) is kept
    on the fused record; a tie keeps whichever hit was seen first --
    vector_hits before keyword_hits, this function's own argument order. A
    hit missing a str doc_id/chunk_id is skipped, not erred. Pure, total,
    never raises. No I/O."""
    scores: dict[str, float] = {}
    richest: dict[str, Mapping[str, object]] = {}
    for hits in (vector_hits, keyword_hits):
        for rank, hit in enumerate(hits, start=1):
            doc_id = hit.get("doc_id")
            chunk_id = hit.get("chunk_id")
            if not isinstance(doc_id, str) or not isinstance(chunk_id, str):
                continue
            full_id = f"{doc_id}:{chunk_id}"
            scores[full_id] = scores.get(full_id, 0.0) + 1.0 / (rrf_k + rank)
            current = richest.get(full_id)
            if current is None or _citation_richness(hit) > _citation_richness(current):
                richest[full_id] = hit

    ordered_ids = sorted(scores, key=lambda fid: (-scores[fid], fid))
    results: list[dict[str, object]] = []
    for full_id in ordered_ids[:k]:
        rec = dict(richest[full_id])
        rec["score"] = scores[full_id]
        results.append(rec)
    return results
```

### 3.2 `fdi.pipeline.retrieval.hybrid_search` (orchestrator, appended to the
existing module)

```python
_FUSE_OVERFETCH_FACTOR = 3


async def hybrid_search(
    *,
    session: AsyncSession,
    store: ArtifactStore,
    embedder: EmbeddingClient | None,
    vector_store: VectorStore | None,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-4's hybrid retrieval orchestrator: fetch k*_FUSE_OVERFETCH_FACTOR
    from RT-3's keyword_search (always -- Postgres is mandatory config) and,
    if BOTH embedder and vector_store are configured, from RT-2's
    vector_search over the SAME query/filters; fuse via
    fdi.core.retrieval.fuse and truncate to k. Degrades to keyword-only
    (never raises, never needs a 503) when embedder/vector_store is None --
    see design spec sec 2 decision 2. Pipeline layer: imports platform +
    core, no I/O left inline -- fusion itself lives in fdi.core.retrieval,
    pure and independently unit-tested."""
    fetch_k = k * _FUSE_OVERFETCH_FACTOR
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None

    keyword_hits = await keyword_search(
        session=session,
        query=query,
        k=fetch_k,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key),
        version=cast(int | None, version),
    )

    vector_hits: list[dict[str, object]] = []
    if embedder is not None and vector_store is not None:
        vector_hits = await vector_search(
            session=session,
            store=store,
            embedder=embedder,
            vector_store=vector_store,
            query=query,
            k=fetch_k,
            filters=filters,
        )

    return fuse(vector_hits, keyword_hits, k=k)
```

### 3.3 `fdi.api.schemas` (new Pydantic models)

```python
class HybridSearchFilters(BaseModel):
    section_type: str | None = None
    doc_key: str | None = None
    version: int | None = None


class HybridSearchRequest(BaseModel):
    query: str
    k: int = 5
    filters: HybridSearchFilters | None = None


class HybridSearchResultView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    chunk_id: str
    heading: str | None
    page: int | None
    section_type: str | None
    text: str
    score: float


class HybridSearchResultsView(BaseModel):
    results: list[HybridSearchResultView]
```

Field-for-field identical to `VectorSearchResultView`/`KeywordHitView` except
`score`'s meaning (decision 7) — deliberately NOT documented differently in
the schema itself (Pydantic has no per-field prose docstring convention this
codebase uses elsewhere); the distinction lives in this spec and the route's
docstring/comment.

### 3.4 `POST /retrieve` (`fdi.api.routes`, new route)

```python
@router.post("/retrieve", response_model=HybridSearchResultsView)
async def hybrid_search_route(
    body: HybridSearchRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
) -> HybridSearchResultsView:
    filters = {
        "section_type": body.filters.section_type if body.filters else None,
        "doc_key": body.filters.doc_key if body.filters else None,
        "version": body.filters.version if body.filters else None,
    }
    results = await hybrid_search(
        session=session,
        store=store,
        embedder=embedder,
        vector_store=vector_store,
        query=body.query,
        k=body.k,
        filters=filters,
    )
    return HybridSearchResultsView(
        results=[HybridSearchResultView.model_validate(r) for r in results]
    )
```

No 503 gate (decision 2) — `embedder`/`vector_store` are passed straight
through to `hybrid_search`, which owns the degrade decision. The route stays
thin: assembly and dependency wiring only, same posture as `/retrieve/vector`.

## 4. Error handling

- **`fuse`:** empty `vector_hits`/`keyword_hits` (either or both) → the
  non-empty list (or `[]` if both empty) drives the output; never raises.
  A hit missing/non-`str` `doc_id` or `chunk_id` is skipped (decision 1),
  the rest of that list still contributes. `k <= 0` → `ordered_ids[:k]`
  slices to `[]` for `k == 0`; a negative `k` slices from the end
  (Python's own `list[:negative]` semantics) — not specially validated,
  same "not this slice's problem" posture RT-2/RT-3 already took for their
  own unvalidated `k`s (RT-2 §4, RT-3 §4).
- **`hybrid_search`:** `embedder is None` or `vector_store is None` (either
  alone, or both) → `vector_hits = []`, `keyword_hits` still computed
  normally; never raises for this case (decision 2). A genuine Postgres
  failure inside `keyword_search` (connection loss, etc.) propagates — this
  function does not swallow infrastructure failures, only "vector search
  unavailable" states, same posture RT-3's `index_chunks_fts` already
  documented for its own "nothing to index" vs. "real Postgres error"
  distinction.
- **`POST /retrieve`:** never 503s (decision 2). A malformed `filters` body
  fails Pydantic validation as a 422, same as every other `/retrieve/*`
  route. An empty/whitespace `query` degrades exactly as `keyword_search`'s
  own empty-query case does (RT-3 §4: `plainto_tsquery` produces an empty
  `tsquery`, matches nothing) combined with whatever `vector_search`
  returns for that same string (not specially handled — an empty embedding
  query is passed straight to `embedder.embed([""])`, out of scope for this
  spec exactly as it was out of scope for `/search`/`/retrieve/vector`).

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_retrieval_fuse.py`:
  - Empty `vector_hits`/`keyword_hits` (both) → `[]`.
  - A chunk in BOTH lists outranks one in only a single list (two-term
    accumulated score beats one-term), asserted with an exact `pytest.approx`
    RRF-formula score, not just relative order.
  - Ties (two hits, each rank 1 in a DISJOINT single list, so identical
    scores) are broken by the `{doc_id}:{chunk_id}` string ascending —
    asserted against a pair of ids chosen so alphabetical order is
    non-trivial (not already index order).
  - Disjoint lists merge correctly, both surviving with their own
    single-list score.
  - Truncation to `k` keeps exactly the top-`k` by fused score, in
    score-descending order.
  - Richest-citation dedup: same chunk in both lists, one citation
    all-`None` optional fields, the other fully populated → the fused
    record carries the fully-populated fields.
  - Richest-citation TIE (equal non-`None` counts, different values) →
    the `vector_hits`-side value wins (argument-order tie-break, decision
    3).
  - A hit missing `doc_id` and a hit missing `chunk_id` are both skipped,
    without affecting an adjacent well-formed hit's score/position.
  - Custom `rrf_k` changes the fused score value (asserted numerically)
    without changing relative order for a single-list-only hit.
  - The fused record's `text` is preserved verbatim from the winning
    (richest) citation and `score` is always a `float`.
- **Integration** (testcontainers + fakes),
  `backend/tests/integration/test_hybrid_search.py`: reuse
  `test_vector_search.py`'s `two_docs`-shaped fixture (a LOCAL
  `StageRegistry` running `ocr → … → chunks → embedding → index` with
  `FakeOcrClient`/`FakeSemanticClient`/`FakeEmbeddingClient`/
  `FakeVectorStore`), EXTENDED to also call `index_chunks_fts` per document
  (RT-3's `indexed_doc` fixture's step) so both the Milvus fake AND the real
  Postgres `chunk` table are populated for the same corpus. Embed a fresh
  `uuid4().hex` marker token into the "found by both tools" document's OCR
  text (Postgres' `chunk` table is session-scoped across the WHOLE
  integration suite — see `test_keyword_search.py`'s own comment on this;
  several OTHER fixtures in this suite already use the literal, unmarked
  "Balance Sheet"/"Total Assets 100" text, so an unmarked query risks
  non-deterministic collisions with unrelated rows inside the over-fetch
  window). Calling `hybrid_search` directly (not through HTTP):
  - A query built from the marked document's exact chunk text ranks that
    chunk first (found by both tools, `assert (doc_id, chunk_id) ==
    (expected_doc, "s0")` — not merely "present").
  - A `section_type` filter that excludes the marked document from BOTH
    tools (keyword: the marker never appears in the other section, so its
    `AND` clause never matches; vector: `section_type` mismatch drops it
    in-memory) leaves only the other document's chunk(s), still returned
    (proves the filter reaches both tools identically, decision 6).
  - `embedder=None, vector_store=None` → keyword-only results, the marked
    chunk still found, its score exactly `1/(60+1)` (single-list rank 1,
    pinned numerically, not just "non-empty").
  - Over-fetch: a `_SpyVectorStore` (same pattern as
    `test_vector_search.py`'s) records the `k` argument `vector_search`
    passed down to `VectorStore.search`; assert it equals
    `k * _FUSE_OVERFETCH_FACTOR` (i.e. `k * 3`).
  - An empty query over an empty (freshly-constructed, unindexed) corpus →
    `[]`, not an error.
- **Integration** (HTTP), additions to `backend/tests/integration/test_api.py`:
  a local `_seed_hybrid_doc(session_factory, store, *, marker)` helper
  (mirrors `_seed_processed_doc`, extended with an `index_chunks_fts` call
  and a marker-embedding OCR paragraph, for the same collision-avoidance
  reason as the pipeline-level test above).
  - `POST /retrieve` with NO `dependency_overrides` (default `client`
    fixture — `embedder`/`vector_store` both `None`, the exact condition
    `test_search_503_when_vector_store_unconfigured` 503s on) returns
    **200**, not 503, with the seeded document's chunk present via
    keyword-only matching — the test that most directly proves decision 2's
    fork against `/search`'s/`/retrieve/vector`'s existing precedent.
  - `POST /retrieve` for a query matching nothing → 200, `{"results": []}`.
  - `POST /retrieve` WITH `dependency_overrides` providing a fake
    embedder/vector_store pre-seeded with the marked chunk's own embedding →
    the fused result's score is strictly greater than the keyword-only
    `1/61` baseline (proves both tools actually contributed, not merely that
    the route didn't error).
  - `POST /retrieve` with `filters.section_type` set to a type the seeded
    document is NOT classified as → the seeded document's `doc_id` is absent
    from the response (filter reaches both tools through the route, same
    proof as the pipeline-level filter test, one layer up through HTTP +
    Pydantic).

## 6. Explicitly deferred (YAGNI)

- **A `degraded`/`sources_used` field on `HybridSearchResultsView`** —
  decision 2's flagged fork; not built this slice, a human should decide if
  the response needs to say "this answer came from keyword-only" explicitly
  rather than leaving it inferable (weakly) from score magnitude.
- **Tuning `_FUSE_OVERFETCH_FACTOR` from real corpus measurements** —
  decision 5's `3` is a documented guess; revisit once a real corpus shows
  whether it under- or over-fetches in practice.
- **A tunable `FDI_` RRF-k or over-fetch-factor setting** — both are private
  constants (decisions 4–5); no operational need yet to change either
  without a redeploy, mirroring RT-2's own `_OVERFETCH_FACTOR` precedent.
- **Cross-encoder re-ranking** — explicitly out of scope for RT-4 per the
  roadmap itself ("RT-4 uses RRF, a model-free fusion" — roadmap's own
  "Explicitly deferred" section); a later re-ranking stage, if ever built,
  would sit on top of `hybrid_search`'s output, not replace `fuse`.
  RT-4's row title says "Hybrid retrieval + rerank" in the roadmap table but
  the roadmap's own body text and "Explicitly deferred" section both commit
  to RRF-only for RT-4 — this spec follows the body text, and flags the
  table header's "+ rerank" wording as stale/aspirational, not a scope item
  this spec silently drops.
- **A field-by-field citation merge** (independently picking the least-`None`
  value per field rather than the richest WHOLE hit) — decision 3 explains
  why the simpler whole-hit policy is sufficient given RT-2/RT-3's actual
  citation sources.
- **Per-tool weighting** (e.g. weighting vector contributions higher than
  keyword, or vice versa) — standard RRF is unweighted by design; a weighted
  variant is a tuning knob for later if evaluation data ever justifies it,
  not part of this slice.

## 7. Done criteria

1. `fdi.core.retrieval.fuse(vector_hits, keyword_hits, *, k, rrf_k=60)`
   implements §3.1 exactly: RRF-sums `1/(rrf_k+rank)` per list per chunk,
   sorts by fused score descending with `{doc_id}:{chunk_id}`-ascending
   tie-break, truncates to `k`, keeps the richest citation on dedup
   (§2 decision 3), skips hits with a missing/non-`str` `doc_id`/`chunk_id`,
   never raises on empty/malformed input; `core` stays pure (no I/O, no
   import of `fdi.platform.*`/`fdi.pipeline.*`).
2. `fdi.pipeline.retrieval.hybrid_search(*, session, store, embedder,
   vector_store, query, k, filters=None)` implements §3.2 exactly:
   over-fetches `k * 3` from each of `keyword_search` (always) and
   `vector_search` (only when both `embedder`/`vector_store` are non-`None`),
   delegates fusion/truncation entirely to `fuse`, never raises or requires
   a 503 for the unconfigured-vector case.
3. `POST /retrieve` (`fdi.api.schemas.HybridSearchRequest`/
   `HybridSearchResultsView`) returns the citation shape
   `{doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
   score}` per result (`score` = fused RRF value, not either tool's raw
   score), NEVER 503s (§2 decision 2, the genuine documented fork), and its
   `filters` reach both `vector_search` and `keyword_search` identically.
4. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/
   0-broken, full suite); §5's tests all pass, including the exact-formula
   RRF score assertions, the tie-break assertion, the richest-citation-dedup
   assertion, and the never-503 assertion against the exact condition
   `/search`/`/retrieve/vector` DO 503 on; §6 deferred items remain out of
   scope; the retrieval roadmap's RT-4 row flips to done with a "what RT-4
   shipped" handoff (the `fuse` RRF formula and its constants, the
   keyword-always/vector-if-configured degradation policy and why `/retrieve`
   has no 503 branch, the richest-citation dedup rule, and the flagged
   `degraded`/`sources_used` fork for RT-7/RT-8 or a human to pick up).
