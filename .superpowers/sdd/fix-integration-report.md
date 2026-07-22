# Fix: 8 integration-test failures from RT-9 (Postgres keyword search -> Milvus BM25)

## Background

RT-9 replaced the Postgres full-text keyword-search channel with Milvus-native
BM25, changed `vector_search`'s signature (dropped `store`), bumped
`IndexStage.version` to 3 (metadata now also carries `text`/`doc_id`), and
deleted the Postgres degrade-to-keyword-only path entirely. Each RT-9 task ran
only the fast suite, so none of these three integration test files were
exercised until now. All three groups below are RT-9's own fallout; none is
pre-existing (confirmed: base commit before RT-9, `vector_search` took `store`
and all these tests passed).

## Group A -- `backend/tests/integration/test_vector_search.py` (5 tests)

`vector_search` no longer takes `store` (hits carry their own text/citation
straight off the Milvus/FakeVectorStore row; `session` remains, since
`doc_key`/`version` filters still need a Postgres translation).

- Removed `store=store` from all 6 `vector_search(...)` call sites (5 tests,
  one test calls it twice).
- `_SpyVectorStore.search` still had the OLD `search(self, vector, k)`
  signature; `VectorStore.search` now requires `*, filter: str = ""` (RT-9's
  Milvus filter-expression support). Updated the override to accept and
  forward `filter`, matching the base class.
- The `store` fixture itself, and its use in `intake_document(...)`, were left
  untouched -- still needed to seed documents.

Test-only changes; no production code touched.

## Group B -- `backend/tests/integration/test_index_stage.py` (1 test)

`test_index_stage_chains_off_embedding_and_chunks_and_caches` built its
artifact key with a hardcoded `IndexStage` version of `2`; RT-9 (Task 3)
bumped `IndexStage.version` to `3`, so reading `index/v2/...` 404'd
(`NoSuchKey`) against the real `index/v3/...` artifact.

- Changed the hardcoded `2` -> `3` in the `artifact_key(...)` call.
- Updated the `hits[0]["metadata"]` equality assertion to include the two
  keys `IndexStage` v3 now also writes: `"text"` (the chunk's body,
  `"Balance Sheet\nAssets 100"` for chunk `s0`, per this fixture's OCR
  paragraphs) and `"doc_id"` (`doc.id`). Kept as a full-dict equality (not
  weakened to a subset check), matching the sibling assertion already in
  `test_index_enrichment.py::test_index_upserts_enriched_metadata`, which
  covers the identical OCR fixture and was already updated for v3.

Test-only changes; no production code touched.

## Group C -- `backend/tests/integration/test_extract_document.py` (2 tests)

Both tests build `ExtractContext(..., embedder=None, vector_store=None, ...)`
and rely on `hybrid_search` degrading to Postgres keyword-search when no
vector store is configured. RT-9 deleted that degrade path: `_pool_for` now
returns `[]` outright whenever `ctx.vector_store is None`, so every unit got
an empty pool, the LLM was never called, and every field came back `None`.

### Fix applied

Mirrored `test_index_enrichment.py`'s pattern: the `processed_doc` fixture now
also registers and runs `EmbeddingStage` + `IndexStage` (after `chunks`),
injecting a `FakeEmbeddingClient(dim=8)` and a `FakeVectorStore()`, and
returns them (`doc, marker, embedder, vector_store`) so both tests can build
`ExtractContext` with a real, populated embedder/vector_store pair instead of
`None, None`. Both test functions were updated to unpack the 4-tuple and pass
the real fakes into `ExtractContext`.

- **`test_extract_document_makes_one_llm_call_per_unit`**: now passes
  cleanly. Both of its units (`U1`, `U2`) share one matchable
  `retrieval_query` against the one indexed narrative chunk (`s0`); batching
  behaviour (2 units -> 2 calls, input-order restoration) is unaffected by
  the retrieval-channel swap.
- **`test_extract_document_pools_keyword_candidates_only`**: `f0`
  (`"Company overview text"` -> `"ACME is a manufacturer."`) and `f1`
  (`"Total assets"` -> `12345.0`, citing `t0r1`) now retrieve correctly
  through the real BM25+vector hybrid pool. **`f2` ("Unmatchable field") does
  not** -- see BLOCKED below.

### BLOCKED: `f2`'s "empty pool" premise is structurally dead, not a fixture bug

`RU_NOWHERE`'s field ("Unmatchable field") is designed to prove: an
unmatchable query gets an empty pool, `extract_unit` short-circuits before
calling the client, and the record still comes back well-formed
(`value: None, confidence: 0.0, source: all-None/empty`). Under the OLD
design this worked because Postgres `plainto_tsquery` ANDs every lexeme, so a
multi-token nonsense query is guaranteed zero matches.

Under RT-9's real hybrid channel this guarantee is gone, and not fixable by
adjusting the query text or the fake embedder mapping:

- `bm25_search` (`FakeVectorStore.search_text`) genuinely returns `[]` for
  zero term overlap -- that half still works as designed.
- `vector_search` (`FakeVectorStore.search`, exact cosine) has **no
  similarity floor** -- it always returns its top-`k` nearest vectors, however
  irrelevant, same as real Milvus ANN search with no `radius`/`range_filter`
  set. It never returns "no results" as long as the filtered corpus is
  non-empty.
- `_pool_for`'s `k` for any unit is `unit_top_k(n_fields) * _POOL_OVERFETCH`,
  and `hybrid_search` further multiplies by `_FUSE_OVERFETCH_FACTOR`.
  `unit_top_k` has a hard floor of `_UNIT_TOP_K_MIN = 20` (`core/extractor.py`
  line 97), so the floor on `vector_store.search(vector, fetch_k, ...)`'s
  `fetch_k` is `20 * 2 * 3 = 120` for ANY unit, regardless of field count.
- `processed_doc`'s corpus is 3 chunks (`s0`, `t0`, `t0r1`) -- and must stay
  that small, because `f0`/`f1` need exactly those chunks indexed. Since
  `fetch_k (>=120) > corpus size (3)`, `vector_search` always returns **all
  3** chunks for **every** query, non-blank or not, related or not.
  `filter_candidates` then drops the 2 table chunks (`RU_NOWHERE`'s field is
  `data_type="string"`, not table-needing) but keeps `s0` -- so `RU_NOWHERE`'s
  final `ranked` pool is `[s0]`, never `[]`.

Verified empirically (temporary debug print, since removed): `f2` came back
with `value: None, confidence: 0.0` (correct -- see why below) but
`source: {"page": 1, "snippet": "Overview\nACME Ltd is a manufacturing company ...", "evidence_ids": ["<doc>:s0"], "bounding_regions": [...]}`
and **`len(fake.calls) == 3`**, not 2: `extract_unit` does call
`client.extract` for `RU_NOWHERE` (ranked is non-empty), `FakeExtractorClient`
appends to `self.calls` *before* checking exhaustion, raises
`AssertionError("exhausted")`, which `extract_unit`'s
`contextlib.suppress(Exception)` swallows -- so `f2`'s value/confidence still
come back `None`/`0.0` (no raw entry to consume), but the call was made and
counted, and `source` now falls back to the top-ranked (bogus) candidate
instead of staying all-`None`.

This breaks the locked "exactly 2 unit calls" assertion, and there is no
fixture-only remedy:
- Blanking `RU_NOWHERE`'s `retrieval_query` would make `parse_field_specs`
  drop the field entirely (blank `retrieval_query` is rejected at parse
  time), breaking the `len(fields) == 3` assertion.
- The only mathematically-possible workaround is padding the corpus with
  >=120 hand-embedded "noise" chunks, engineered so they outrank `s0` for
  `RU_NOWHERE`'s query specifically while ranking below `s0`/`t0r1` for the
  other two units' own queries (whose `fetch_k` is the same 120-item floor) --
  exactly the "contorting the fixture beyond reason" case the task said to
  stop at, not a legitimate test fix.
- The actual fix for this behaviour is a similarity floor/threshold on
  `vector_search`/`hybrid_search` in `fdi/pipeline/retrieval.py` (production
  code) -- explicitly out of scope here ("Do NOT modify production code...
  If you find a real production bug, STOP and report BLOCKED").

**Left `test_extract_document_pools_keyword_candidates_only`'s assertions
completely unchanged** (not weakened, not deleted) per the task's explicit
instruction; it remains red, with the cause fully diagnosed above. The
`processed_doc` fixture change that surfaces this is still required and
correct -- it is what makes the sibling test in the same file
(`test_extract_document_makes_one_llm_call_per_unit`) pass, and it correctly
fixes `f0`/`f1` in this test too.

## No other production code changed

Confirmed via `git diff --stat`: only the three test files above (plus a
pre-existing, unrelated `.superpowers/sdd/progress.md` edit from the
controlling session) changed. No edits to `backend/src/**`,
`fdi.core.retrieval.fuse`, `rerank_candidates`, or
`backend/tests/unit/test_retrieval_fuse.py`.

## Verification output

### 1. `uv run --directory backend pytest tests/integration/test_vector_search.py tests/integration/test_index_stage.py -q`
```
......                                                                   [100%]
6 passed, 2 warnings in 49.59s
```

### 2. `uv run --directory backend pytest tests/integration/test_extract_document.py -q`
```
F..                                                                      [100%]
1 failed, 2 passed, 2 warnings in 33.27s
```
(`test_extract_document_pools_keyword_candidates_only` fails at the `f2`
`source` assertion -- see BLOCKED above; the value/confidence assertions for
`f2`, and all of `f0`/`f1`, pass.)

### 3. `uv run --directory backend pytest -q` (whole suite)
```
1 failed, 804 passed, 11 skipped, 2 warnings in 351.66s (0:05:51)
```
Only the one diagnosed-BLOCKED test fails; no other regressions anywhere in
the suite.

### 4. `uv run --directory backend ruff check .`
```
All checks passed!
```

### 5. `uv run --directory backend mypy --strict src`
```
Success: no issues found in 101 source files
```

## Commit

See git log for the commit SHA.
