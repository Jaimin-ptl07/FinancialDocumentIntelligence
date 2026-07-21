# RT-9 — BM25 Keyword Retrieval on Milvus

- **Status:** Designed — 2026-07-21. Replaces the Postgres full-text keyword
  channel (RT-3) with Milvus-native BM25, after an end-to-end run proved the
  Postgres channel returns nothing for every real query.
- **Depends on:**
  - **RT-2** (`fdi.pipeline.retrieval.vector_search`, implemented): the dense
    channel. Unchanged in contract; its internals simplify here.
  - **RT-4** (`fdi.pipeline.retrieval.hybrid_search` + `fdi.core.retrieval.fuse`,
    implemented): the orchestrator and the pure RRF fusion. `hybrid_search`
    keeps its exact signature; `fuse` is unchanged and keeps its consumer.
  - **EX-1** (`docs/superpowers/specs/2026-07-20-extractor-unit-batching-design.md`,
    merged at `6a20b35`): unit-batched extraction. Its 18 retrieval-unit
    queries are the workload this spec exists to serve.
  - **Milvus 2.6.20** (`docker/compose.yml`, `vector` profile) and
    **pymilvus >= 2.6** (`backend/pyproject.toml`). BM25 via `Function` is a
    2.5+ feature; both are already deployed.

## 1. Purpose

`fdi.pipeline.retrieval.keyword_search` builds its query with
`plainto_tsquery`, which **ANDs every lexeme**. Every retrieval query in this
system is a sentence, so every query demands that one chunk contain all of its
terms. Measured against the live 2752-chunk corpus for the STTL FY2023-24
report:

| query | hits |
|---|---:|
| RU_COMPANY's full 18-word `retrieval_query` | **0** |
| `"corporate information"` | 9 |
| `"CIN"` | 13 |
| the same terms with OR semantics | 138 |

**All 18 retrieval-unit queries return 0 keyword hits.** `hybrid_search`
therefore fuses a populated vector list with an empty keyword list and
silently degrades to vector-only — for every unit, on every document, with no
error and no log. The end-to-end EX-1 run landed at 133/198 populated fields
against the offline audit's 173/198 correct; the audit's harness used its own
keyword search with OR / count-of-matching-terms scoring, which is the
difference.

This is the largest single accuracy lever available: the retrieval system is
running on one of its two channels.

## 2. Scope

**In scope**

- A BM25 keyword channel served by Milvus, replacing the Postgres one.
- The Milvus collection schema change that BM25 requires, and the reindex.
- Deleting the Postgres full-text path outright.

**Out of scope**

- `fuse` / RRF behaviour. Unchanged, and it keeps its two callers.
- Reranking (`rerank_candidates`). A prior layer attribution measured **zero**
  failures attributable to reranking; it is not touched.
- The dense channel's embedding model, dimensions, or index type.
- Prompting and extraction. EX-1 owns those.

## 3. Design

### 3.1 Why the collection schema must change

Milvus computes BM25 **server-side**: a `Function` of type `BM25` reads a
`VARCHAR` field that has `enable_analyzer=True`, tokenizes it, and writes a
`SPARSE_FLOAT_VECTOR`, which is indexed `SPARSE_INVERTED_INDEX` with
`metric_type="BM25"`. Search is then issued with **raw query text**, not a
vector.

The collection today stores no text at all — only `id`, `vector`, and the
metadata `heading` / `chunk_id` / `section_type` / `page`; hit text is
resolved afterwards from the `chunks` artifact via `resolve_chunks`. BM25
cannot work against that. Storing `text` in Milvus is therefore not a
convenience, it is the precondition, and it is what makes the rest of this
design fall out.

**Collection schema v2:**

| field | type | purpose |
|---|---|---|
| `id` | `VARCHAR(512)`, primary | unchanged — `f"{doc_id}:{chunk_id}"` |
| `vector` | `FLOAT_VECTOR(384)` | unchanged — dense BGE-small |
| `text` | `VARCHAR(65535)`, `enable_analyzer=True` | BM25 input; the largest real chunk measured is 10,253 chars, so 65535 is comfortably above the ceiling |
| `sparse` | `SPARSE_FLOAT_VECTOR` | BM25 output, written by the `Function` |
| `doc_key` | `VARCHAR(256)`, nullable | server-side filtering |
| `version` | `INT64`, nullable | server-side filtering |
| `section_type` | `VARCHAR(64)`, nullable | server-side filtering |
| `heading` | `VARCHAR(1024)`, nullable | citation |
| `page` | `INT64`, nullable | citation |

The `Function` and the analyzer **must be declared at collection creation**;
neither can be added to an existing collection. `_ensure_collection` therefore
moves off the simple `create_collection(dimension=...)` form onto the full
schema API (`client.create_schema()` → `add_field` → `add_function` →
`prepare_index_params` → `create_collection`).

Analyzer: the built-in `standard` tokenizer with a `lowercase` filter. No
custom stop-word list in v1 — financial identifiers (`L72200GJ1995PLC024465`)
and label fragments are exactly what we want indexed, and an unexamined stop
list is how the current channel came to drop everything.

### 3.2 The `VectorStore` seam

The protocol in `fdi.pipeline.vectorstore` gains one method beside `search`:

```python
def search_text(self, query: str, k: int, *, filter: str = "") -> list[dict[str, object]]
```

Returning the same `{id, score, metadata}` shape `search` already returns, so
callers treat the two channels identically. `FakeVectorStore` implements it
with a deterministic token-overlap score — not real BM25, but monotone in
term overlap, which is the property the pipeline-layer tests actually assert.

`search` gains the same optional `filter`. Both must pass an **explicit
`output_fields` list**, never `["*"]`: Milvus refuses to output a sparse
vector field, so `["*"]` breaks the moment `sparse` exists. This is a silent
trap for anyone copying the current line.

### 3.3 Retrieval layer

```python
async def bm25_search(*, vector_store, query, k, filters=None) -> list[dict]
```

Mirrors `keyword_search`'s old signature and hit shape
(`{doc_id, doc_key, version, chunk_id, heading, page, section_type, text, score}`),
so `POST /retrieve/keyword` and `fuse` are unaffected by the swap.

`hybrid_search` keeps its **shape** — fetch `k * _FUSE_OVERFETCH_FACTOR` from
each channel, hand both lists to `fuse`, truncate to `k` — and its hit
contract. Its **parameter list shrinks**, see below.

Because both channels now carry their own `text`, `doc_key`, and `version`,
`vector_search` no longer needs `resolve_chunks` or
`DocumentRepository.get_many` — filtering moves into a Milvus `filter`
expression and the citation is already on the hit. That removes one S3 read
and one Postgres round-trip per query. `build_vector_results` loses its
`resolved` and `docs` parameters accordingly.

**Consequence, stated explicitly because it contradicts a first reading of
"unchanged":** with the artifact read and the Postgres join both gone, neither
channel touches Postgres or the object store, so `session` and `store` become
dead parameters on `vector_search`, `bm25_search`, and `hybrid_search`. They
are **removed**, not retained-but-ignored — a parameter that exists and does
nothing is exactly the kind of silent lie this spec is cleaning up. Three call
sites update with them:

| caller | file |
|---|---|
| `_pool_for` (unit-batched extraction) | `fdi/pipeline/extractor.py` |
| the `search` agent tool | `fdi/pipeline/agent_tools.py` |
| `POST /retrieve`, `POST /retrieve/keyword`, `POST /retrieve/vector` | `fdi/api/routes.py` |

This is the one place RT-9 reaches into EX-1's just-merged code. `_pool_for`
drops two arguments and nothing else; `extract_unit`, `extract_document`, and
the batching behaviour are untouched.

`resolve_chunk` / `resolve_chunks` themselves stay: they are still the way a
caller turns an arbitrary chunk id back into text, and `read_chunks` has a
second consumer in `extract_document`'s consolidated-boundary detection.

### 3.4 What is deleted

- `keyword_search` (Postgres implementation)
- `index_chunks_fts` and `POST /documents/{doc_id}/index_fts`
- `chunk_rows_for_index` (`fdi.core.retrieval`)
- the `chunk` table, its `tsv` generated column and four indexes — via a
  reversible Alembic migration
- the tests covering the above

`POST /retrieve/keyword` is **kept and repointed** at `bm25_search`. The
Postgres implementation dies entirely, but the endpoint becomes a keyword-only
retrieval capability that works for the first time; deleting a feature at the
moment it starts working is the wrong trade. Its request and response models
are unchanged.

### 3.5 Migration and cutover

The BM25 `Function` cannot be retrofitted, so the collection is recreated
rather than altered:

- `FDI_MILVUS_COLLECTION` default moves `fdi_sections` → `fdi_chunks_v2`. The
  old collection is left in place, so rollback is a one-line config revert.
- `IndexStage.version` bumps 2 → 3. The DAG is content-addressed
  (`artifacts/{doc_id}/{stage}/v{version}/{input_hash}.json`), so a version
  bump is already the mechanism that forces a stage to re-run; no bespoke
  backfill script.
- `embedding` artifacts are untouched and stay cached. **No re-embedding and
  no Azure cost** — the reindex re-upserts existing vectors alongside the
  newly-stored text.

**This is a hard cutover, not a gradual one.** Between the config change and
the reindex completing, the new collection is empty and retrieval returns
nothing for un-reindexed documents. That is acceptable for a single-corpus
development system and would not be for a live one; it is recorded here so the
decision is explicit rather than discovered.

## 4. Error handling

The layer's existing posture holds: retrieval helpers raise, and their callers
decide. `extract_unit`'s `contextlib.suppress` already degrades a failed pool
to an empty one, so a Milvus outage costs a unit its context rather than the
document its run.

**One deliberate reversal.** RT-4 decision 2 had `hybrid_search` degrade to
keyword-only when the embedder or vector store was unconfigured, so retrieval
never returned 503. With both channels in Milvus that fallback has nothing to
fall back to. RT-4 decision 2 is superseded, and this is the entry that
supersedes it.

Concretely, so this cannot be read two ways: `vector_store` and `embedder`
become **required, non-`None`** parameters of `bm25_search`, `vector_search`,
and `hybrid_search` — those functions do not check for `None` and do not
raise a bespoke error. The `None` check lives in **`fdi/api/routes.py`
alone**, which already holds `EmbedderDep`/`VectorStoreDep` as `T | None` and
already raises 503 on the unconfigured case for `POST /retrieve/vector`.
`POST /retrieve` and `POST /retrieve/keyword` adopt that same existing branch.

`_pool_for` in the extractor keeps its `contextlib.suppress`, so an
unconfigured or unreachable Milvus still degrades a unit's pool to empty
rather than failing the document's run.

An unconfigured Milvus is a configuration error, and a 503 says so. The
alternative — the current behaviour — is a system that returns a confident
empty list, which is precisely the failure this spec exists to correct.

## 5. Testing

**Pure / unit (no I/O):**
- `build_vector_results` after losing `resolved`/`docs`: filtering, truncation,
  citation assembly, and that output order is still a sub-sequence of input
  order.
- `fuse` is untouched; its existing tests must stay green unmodified. If a
  `fuse` test needs editing, the change has escaped its scope.

**Pipeline (fake vector store):**
- `bm25_search` returns the nine-key hit shape, best-first.
- `hybrid_search` still fuses both channels; a chunk found by both outranks a
  chunk found by one (the property RRF exists for, and the one the dead
  keyword channel has been silently failing to provide).
- `POST /retrieve` and `POST /retrieve/keyword` return **503** when the
  vector store is unconfigured — asserted at the route, since per §4 that is
  the only layer that checks for `None`.
- `_pool_for` still yields an empty pool (not an exception) when
  `hybrid_search` fails, so a Milvus outage costs a unit its context and not
  the document its run.

**Integration (live Milvus, `milvus` marker):**
- A collection built through the v2 schema path accepts a BM25 search issued
  with raw text.
- **BM25 actually discriminates**: a chunk matching 2 of 5 query terms
  outranks a chunk matching 1. A fake cannot establish this, and it is the
  claim the whole spec rests on.
- A `filter` expression scopes results to one `doc_key`/`version`.

**End-to-end (the real measurement):**
Re-run the EX-1 verification — seed `ocr` and `semantic` from cache, run the
DAG, `POST /extract/document`, and read the output against the schema's
`example` values directly. No scoring script: tooling bugs contaminating the
assessment is a failure this project has already hit twice (a date comparison
that manufactured 7 false failures, a containment probe that produced 6 more).

The number that matters is whether populated fields move from **133/198**
toward the audit's **173/198**, and specifically whether RU_COMPANY — 0/9
today, because the vector channel returned only table chunks for a
narrative-only unit — recovers once a working keyword channel can find
`s`-chunks by their labels.

## 6. Success criteria

1. Every one of the 18 retrieval-unit queries returns a non-empty keyword
   result set against the live corpus. (Today: all 18 return zero.)
2. `hybrid_search` fuses two non-empty channels; a both-channel hit outranks a
   single-channel hit.
3. `POST /retrieve/keyword` and `POST /retrieve` keep their request/response
   contracts; `fdi/api/schemas.py` view models are unchanged.
4. The Postgres `chunk` table and its full-text path no longer exist, and the
   Alembic migration is reversible.
5. No re-embedding: the `embedding` artifact for an already-ingested document
   is served from cache during the reindex.
6. The end-to-end run's populated-field count improves against 133/198, read
   directly against the schema examples.
