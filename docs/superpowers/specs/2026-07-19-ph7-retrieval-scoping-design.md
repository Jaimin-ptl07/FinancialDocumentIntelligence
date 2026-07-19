# Phase 7 — Retrieval scoping (de-dilute the vector channel; diversity-cap rerank)

- **Status:** Design — 2026-07-19. Follow-on to the 6-phase ingestion improvement, motivated by the
  measured re-audit regression.
- **Depends on:** Phase 3a (`build_chunks` emits `s{i}` section, `t{i}` whole-table, and `t{i}r{j}`
  row chunks; `EmbeddingStage` embeds every chunk).

## 1. Purpose — the measured problem

The post-improvement extraction re-audit (real BGE + Azure, 160 fields) showed a **split result**:
the 32 headline fields rose **65.6% → 75.0%** (recall@8 96.8% → 100%), but the full 160-field set
**fell 53.1% → 48.1%** (2 fixed, 10 regressed). Root cause is **vector-channel dilution**: Phase 3a
embedded **1,985 fine-grained `t{i}r{j}` row chunks** (of 2,752 total), which are near-duplicates in
embedding space. A field's top-8 now fills with topically-similar rows from the *wrong* table/place,
so the LLM extracts an adjacent value (e.g. `net_block` 2126.88 → 2345.82; `receivables` −1501.78 →
−1829.64; `basic_eps` → `None`).

The insight: **table content belongs in the vector channel at TABLE granularity, and row granularity
belongs in the KEYWORD/FTS channel** (where exact-value substring matching is precise and dilution-free).
Whole-table chunks (`t{i}`, 257 of them) already carry the full grid — headers + all rows in order —
which is exactly the context the baseline used (via `get_tables`) to extract the right value. Row
chunks stay in the `chunks` artifact for keyword search + resolution; they just leave the vector index.

## 2. Scope decisions (autonomous; documented)

1. **`EmbeddingStage` embeds only NON-row chunks** — section chunks (`s{i}`) and whole-table chunks
   (`t{i}`); it SKIPS fine-grained row chunks (`t{i}r{j}`). This cuts the embedded/indexed vector set
   from ~2,752 → ~767 on the real doc (72% fewer vectors → less noise, faster embed/index). Row chunks
   remain in the `chunks` artifact untouched, so RT-3 keyword FTS (`index_chunks_fts`), `resolve_chunks`,
   and `get_tables` still see them. This is a refinement of Phase 3a's granularity choice, not a
   reversal — table content is still vector-searchable, at table granularity.
2. **Row-vs-non-row is decided by chunk_id shape.** A chunk is a "row chunk" iff its `chunk_id` matches
   `t{int}r{int}` (a `t`-prefixed id containing an `r` segment). `s{i}` and `t{i}` are kept. A tiny pure
   predicate `is_row_chunk_id(chunk_id: str) -> bool` in `fdi.core.retrieval` (so it's unit-testable and
   reused by the harness). EmbeddingStage filters its `records` through it.
3. **`rerank_candidates` gains an optional per-source diversity cap** — `max_per_source: int | None =
   None` (default None = current behavior, fully backward-compatible). When set, after blend-scoring,
   greedily take `top_k` but allow at most `max_per_source` candidates sharing a source key. The source
   key is derived from `Candidate.evidence_id`: the table index for `table:{doc}:{i}` / `{doc}:t{i}...`
   chunks, the section for `{doc}:s{i}`, else the whole id. This prevents any single table from
   consuming the context window with fragments and forces topical spread. Pure, deterministic, stable.
4. **The extractor opts into the cap** — `extract_field` passes `max_per_source=3` to `rerank_candidates`
   (a module constant `_MAX_PER_SOURCE`). Chosen so a field can still see a few rows/whole-table from its
   own table while leaving slots for alternatives. No other extractor change.
5. **Version bump:** `EmbeddingStage.version 2 → 3` (its output — which chunks are embedded — changes),
   cascading to `IndexStage` re-run via content-addressing (no IndexStage code change: it still upserts
   whatever `embedding.chunks` contains). `build_chunks`/`ChunksStage` UNCHANGED (row chunks still
   produced). No new config. Core stays pure; layering 1-kept/0-broken.

## 3. Architecture

### 3.1 `fdi.core.retrieval` (pure) — new predicate

```python
import re
_ROW_CHUNK_RE = re.compile(r"^t\d+r\d+$")
def is_row_chunk_id(chunk_id: str) -> bool:
    """True for a fine-grained table-ROW chunk id (t{i}r{j}); False for section
    (s{i}) and whole-table (t{i}) chunk ids. Used to keep row chunks out of the
    vector channel (Phase 7) while leaving them in the chunks artifact for
    keyword/FTS + resolution."""
    return bool(_ROW_CHUNK_RE.match(chunk_id))
```

### 3.2 `fdi.pipeline.stages.embedding.EmbeddingStage`

`version = 3`. In `run`, filter records before embedding:
```python
records = [c for c in records if not is_row_chunk_id(str(c.get("chunk_id", "")))]
```
(applied after the existing `isinstance(c, dict)` filter). Everything else unchanged — the produced
`embedding.chunks` simply omits row chunks; `chunk_id` alignment with Milvus ids / resolve_chunks holds
for the chunks that remain.

### 3.3 `fdi.core.extractor.rerank_candidates` — diversity cap

Add `max_per_source: int | None = None`. When `None`, identical to today. When set:
```python
def _source_key(c: Candidate) -> str:
    # table:{doc}:{i} -> "t{i}";  {doc}:t{i}r{j}/{doc}:t{i} -> "t{i}";  {doc}:s{i} -> "s{i}"
    ...
ranked = sorted(candidates, key=blended, reverse=True)
if max_per_source is None:
    return ranked[:top_k]
out, per = [], {}
for c in ranked:
    k = _source_key(c)
    if per.get(k, 0) >= max_per_source:
        continue
    out.append(c); per[k] = per.get(k, 0) + 1
    if len(out) >= top_k:
        break
return out
```
Pure, deterministic (stable sort + greedy). `extract_field` passes `max_per_source=_MAX_PER_SOURCE` (3).

## 4. Error handling

- Malformed/absent `chunk_id` → `is_row_chunk_id` returns False (kept — conservative; a chunk without a
  parseable id is treated as non-row and embedded, never dropped silently in a way that loses a real
  section/table chunk).
- `max_per_source=None` preserves exact current rerank behavior (all existing tests pass unchanged).
- Empty candidates / all-same-source → cap yields ≤ top_k, never raises.

## 5. Testing

- **Unit** `test_retrieval_chunks.py` / new: `is_row_chunk_id("t3r5")` True; `("t3")`/`("s0")`/`("")`
  False.
- **Unit** `test_extractor.py`: `rerank_candidates` with `max_per_source=2` over candidates where 5
  share one table id → at most 2 of that table survive, remaining slots filled by other sources;
  `max_per_source=None` → identical to prior (regression-guard the existing tests).
- **Integration** `test_embedding_stage.py`: a `chunks` artifact with `s0`, `t0`, `t0r0`, `t0r1` →
  `embedding.chunks` contains `s0`+`t0` only (row chunks skipped); the embedder is called with 2 texts,
  not 4; version == 3.
- **Real-doc regression (scratchpad, not committed):** embedded vectors drop ~2,752 → ~767; re-run the
  extraction audit and compare to both the pre-improvement baseline AND the Phase-0-5 run — target:
  recover the 160-field set toward/above 53.1% while KEEPING the 32-headline ≥ 75%.
- **Gates:** grep for `EmbeddingStage`/`version == 2` assertions and update; full suite green.

## 6. Explicitly deferred

- Section/statement-type-scoped pooling (route a Balance-Sheet field only to BS tables) — needs a
  reliable field→statement map; the concept-values path (Phase 4) is the cleaner home for that, deferred.
- Routing numeric fields entirely through `concept_lookup`/reconciled `concept_values` instead of the
  vector channel — a larger extractor redesign, separate follow-on.
- MMR with candidate embeddings at rerank time — the id-based per-source cap is the cheap first cut.

## 7. Done criteria

1. `is_row_chunk_id` (pure) added; `EmbeddingStage` (v3) embeds only `s{i}`+`t{i}` chunks, skipping
   `t{i}r{j}`; row chunks remain in the `chunks` artifact (keyword/FTS/resolve unaffected).
2. `rerank_candidates` gains `max_per_source` (default None = unchanged); `extract_field` passes 3.
3. All four gates green; `EmbeddingStage`/version assertions updated; core pure; layering 1-kept/0-broken.
4. Real-doc re-audit run (scratchpad) reported head-to-head: recovers the 160-field set vs the diluted
   48.1% while holding the 32-headline gain — the quantified proof (or, if not, a documented next lever).
