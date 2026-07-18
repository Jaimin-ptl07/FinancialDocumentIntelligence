# Financial Document Intelligence — Retrieval Layer Roadmap

- **Status:** Agreed 2026-07-18. The **second half** of the system: retrieval tools over the artifacts,
  vectors, graph, tables, and cross-document entities the ingestion layer (SP-1…SP-18, complete)
  produced. **User-selected shape:** (1) each tool is BOTH an HTTP retrieval endpoint AND an
  LLM-callable function tool, with a `POST /ask` agent capstone that plans tool calls and returns a
  cited answer; (2) the corpus Neo4j graph is made cross-document-safe as part of RT-5; (3) full
  RT-1…RT-8 in scope. Same discipline as ingestion: brainstorm → spec → plan → execute per
  sub-project, pure-core-first, mockable seams for every external dependency, four green gates
  (`ruff` · `mypy --strict` · `lint-imports` 1-kept/0-broken · full suite), git-worktree waves for
  independent sub-projects.

## What the ingestion layer left for retrieval (surface map, 2026-07-18)

Every artifact is per-document JSON in MinIO, read via `latest_run`→`get_json`; `GET
/documents/{id}/artifacts/{stage}` is the only generic reader (whole-body). What each store gives —
and its gaps — drives this roadmap:

- **Text** lives ONLY inside the whole `ast` (sections→paragraphs, untruncated), `structure`
  (blocks + canonicalized table `grid`s with resolved `column_headers`), and `ocr` blobs. Milvus,
  Neo4j, `embedding`, `semantic`, `ontology`, `knowledge` store ids/labels/vectors — **no body text**.
- **Milvus** (`{doc_id}:s{i}` ids, correctly namespaced) stores one vector per AST section, metadata
  only `{heading, chunk_id}`. `POST /search` returns `id + score + {heading, chunk_id}` — **no text,
  no filters** (can't scope by doc, version, section type, or concept).
- **Neo4j is BROKEN cross-document.** `Neo4jGraphStore` does `MERGE (n:Node {id})` and the loader
  does NOT namespace ids by `doc_id` (`"doc"`, `"s0"`, `"s0p1"` reused every document) → loading a
  second document collides onto the first (last-writer-wins). Every node is `:Node`, every edge
  `[:REL {type}]` (semantic type is a property, not a Cypher label/rel-type). **No route reads Neo4j.**
  RT-5 fixes this first.
- **`semantic`** (per-section `type` ∈ 8 `SectionType`s, in AST order), **`ontology`** (`{label,
  concept}` mappings, `CanonicalConcept`; label-only, no numeric values), **`knowledge`** (per-doc
  counts/concepts/quality), **`provenance`** (graph `node_id`→page + bounding polygon), **`linking`**
  (figure captions + footnote text).
- **Postgres** catalogs docs (`doc_key`, `version`, `checksum`) and stage runs (status, `artifact_key`);
  stores no content. **Cross-doc entities** (`POST /entities/resolve`) recomputed on demand.

**Foundational gaps RT-1 must close:** (a) no way to turn a vector/graph hit into its text; (b) no
shared chunk identity across stores; (c) Milvus metadata too thin to filter; (d) no keyword/full-text
search; (e) numbers in table grids are unlinked to `CanonicalConcept`s.

## Sub-projects (RT-1 … RT-8)

Each row is one spec → plan → implement cycle. "Tool" = an HTTP endpoint AND (from RT-8) a registered
LLM function tool. Every retrieval result carries a **citation** (doc_id, doc_key, version, heading,
page/bbox where available).

| # | Sub-project | Weight | Depends on | Ships |
|---|---|---|---|---|
| **RT-1** | Retrieval foundations — unified chunk record + filterable index | heavy | ingestion | ✅ done (2026-07-18). `fdi.core.retrieval.build_chunks` + `ChunksStage` (`deps=("ast","semantic","provenance")`) → per-doc `chunks` artifact `[{chunk_id, heading, text, section_type, page, para_count}]`; `IndexStage` v2 (`deps=("embedding","chunks")`) enriches Milvus metadata to `{heading, chunk_id, section_type, page}`; `resolve_chunk`/`resolve_chunks` (`fdi.pipeline.retrieval`) every text-returning tool uses. |
| **RT-2** | Semantic (vector) retrieval | medium | RT-1 | `vector_search(query, filters, k)` — embed query (BGE), Milvus search with metadata filters, resolve hits to text+citation via RT-1. `POST /retrieve/vector`; upgrades `/search` to return text. |
| **RT-3** | Lexical / keyword retrieval | medium | RT-1 | ✅ done (2026-07-18). Postgres full-text (`tsvector`) over a `chunk` table populated from the `chunks` artifact; `keyword_search(query, filters)`. `POST /retrieve/keyword`. |
| **RT-4** | Hybrid retrieval + rerank | light | RT-2, RT-3 | pure `fuse(vector_hits, keyword_hits)` (Reciprocal Rank Fusion); one `POST /retrieve` (hybrid) tool. |
| **RT-5** | Graph retrieval (+ corpus-graph fix) | heavy | ingestion | Fix `graphload` id-namespacing (`{doc_id}:{id}`) + real `:Document/:Section/:Paragraph` labels & `:CONTAINS/:READING_ORDER` rel-types; `Neo4jGraphStore` read methods (mockable); tools `neighbors`, `document_structure`, `reading_order`, `provenance(node)→page/bbox`. `POST /graph/*`. |
| **RT-6** | Structured financial retrieval | heavy | RT-1 | Query the canonicalized table `grid`s: `get_tables(doc_id, filters)`, `concept_lookup(concept)` linking `CanonicalConcept`→grid rows and extracting the numeric cell (the deferred numeric extraction). `POST /tables/*`, `/concepts/*`. |
| **RT-7** | Cross-document / entity retrieval | medium | RT-6 (concepts), crossdoc | `entity_timeline(entity)`, `compare_concept_across_versions(entity, concept)`; optionally persist the entity index for fast lookup. `GET /entities/{id}/timeline`, `POST /entities/compare`. |
| **RT-8** | Retrieval agent — `POST /ask` | heavy | RT-2…RT-7 | Register every RT tool as an Azure OpenAI function schema; an agent loop (plan → call tools → synthesize) returns a **cited** answer `{answer, citations, tool_trace}`. Mockable LLM seam (`FakeAgentClient` in the default suite; real Azure OpenAI behind an opt-in marker). The capstone that makes the tools "tools." |

## What RT-1 shipped that RT-2…RT-8 must know

- **The `chunks` artifact** (`fdi.core.retrieval.build_chunks`, materialized per-document by
  `ChunksStage`, `deps=("ast","semantic","provenance")`): `{schema_version:1, chunks:[{chunk_id,
  heading, text, section_type, page, para_count}]}` — one record per AST section. `text` is the
  EXACT string `EmbeddingStage` embeds (`heading + "\n" + "\n".join(paragraphs)`), so it is the
  passage a vector hit actually represents. `section_type`/`page` degrade to `None` on missing or
  malformed upstream data — never absent as keys, never raise.
- **The `s{i}` chunk identity is load-bearing and now cross-store.** It mirrors `EmbeddingStage`'s
  `enumerate`-and-skip exactly (a non-`dict` AST section is skipped but still consumes its index), so
  a Milvus hit id `{doc_id}:s{i}` joins directly to `chunks[…].chunk_id == "s{i}"` — no re-indexing,
  no compaction, ever, in any future chunking change.
- **`resolve_chunks(session, store, ids: list[str])`** (`fdi.pipeline.retrieval`) is the batch
  text-attach primitive: give it a list of `"{doc_id}:{chunk_id}"` hit ids (from Milvus, later a
  namespaced graph node, etc.) and it reads each document's `chunks` artifact ONCE, returning
  `{full_id: {doc_id, chunk_id, heading, text, section_type, page, para_count}}`, silently omitting
  unresolvable ids. `resolve_chunk` is the single-id form. RT-2's `vector_search` should call
  `resolve_chunks` once over a whole hit list rather than resolving hits one at a time.
- **Milvus metadata is now `{heading, chunk_id, section_type, page}`** (`IndexStage` v2, `deps=
  ("embedding","chunks")`) — a vector search CAN filter by `section_type` at the vector layer.
  `doc_key`/`version` are deliberately NOT in Milvus metadata (`StageContext` has no Postgres
  access) — RT-2 must join those from Postgres via the hit's `doc_id`, not expect them on the hit.
- **The `index` stage_run re-ran for every already-indexed document** after the v1→v2 bump (new
  `input_hash` from the added `chunks` dep) — intended, not a regression; existing corpora reindex
  automatically with the richer metadata on next `run_stage("index", ...)`.

## What RT-3 shipped that RT-4…RT-8 must know

- **The `chunk` table** (`fdi.platform.db.models.Chunk`, Alembic `0006`, chained
  off head `0005`): one row per RT-1 `chunks` artifact record —
  `{id, doc_id, chunk_id, doc_key, version, heading, text, section_type,
  page, tsv, created_at, updated_at}`. `tsv` is a Postgres `GENERATED ALWAYS
  AS (to_tsvector('english', text)) STORED` column with a GIN index
  (`ix_chunk_tsv`) — Postgres maintains it on every write; nothing in the
  codebase computes or targets it directly. `doc_key`/`version` are
  denormalized onto every row at write time (the pipeline layer has a
  Postgres session, unlike a `Stage`), so `keyword_search`'s filters are
  plain `WHERE` clauses with no join to `document`.
- **`chunk.id` reuses the SAME identity Milvus already uses:
  `f"{doc_id}:{chunk_id}"`.** RT-4's fusion can compare a keyword hit id and
  a vector hit id directly — no re-keying, no join table. Built and enforced
  in exactly one place, `fdi.core.retrieval.chunk_rows_for_index` (pure), so
  it cannot drift.
- **`fdi.pipeline.retrieval.index_chunks_fts(session, store, doc_id)`**
  upserts (`ON CONFLICT DO UPDATE` on `id`) a document's `chunks` artifact
  into `chunk`; idempotent, degrades to `0` (never raises) for an unknown
  document or a document with no `chunks` artifact yet. Triggered via `POST
  /documents/{doc_id}/index_fts` — **synchronous, not auto-chained** (§2
  decision 6 of the design doc): nothing currently calls this automatically
  after the `chunks` stage finishes. RT-8's agent or a future ingestion
  orchestrator wiring a full auto-pipeline must either call this route
  explicitly per document or fold it into the `chunks` stage's successor
  dispatch (noted as a deferred follow-on, not built).
- **`fdi.pipeline.retrieval.keyword_search(session, query, k, section_type,
  doc_key, version)`** (`POST /retrieve/keyword`) returns `ts_rank`-ordered,
  citation-shaped hits: `{doc_id, doc_key, version, chunk_id, heading, page,
  section_type, text, score}`. This is the second half of RT-4's fusion
  input — `vector_search` (RT-2) is intended to converge on the SAME nine
  fields (RT-2 already returns `text` via `resolve_chunks`; matching the key
  set makes RRF fusion a plain merge on `id`, no field-renaming layer).
  `plainto_tsquery` (never `to_tsquery`) means the query parameter is always
  safe free text — an empty/whitespace query degrades to `[]`, not a 500.

## Build order (git-worktree waves)

- **Wave A (solo):** RT-1 — the chunk foundation; everything text-returning depends on it.
- **Wave B (parallel):** RT-2 (vector), RT-3 (keyword), RT-5 (graph+fix), RT-6 (tables) — independent after RT-1 (RT-5 needs only ingestion).
- **Wave C (parallel):** RT-4 (needs RT-2+RT-3), RT-7 (needs RT-6+crossdoc).
- **Wave D (solo):** RT-8 — the agent, needs the tool set complete.

## Cross-cutting decisions

1. **Unified chunk identity = `{doc_id}:{chunk_id}`** (already Milvus's id form; `chunk_id` = `s{i}`,
   one per AST section). RT-1's `chunks` artifact and RT-5's namespaced graph both adopt it, so a
   vector hit, a graph node, and a provenance record all join on the same key.
2. **Every tool is mockable and testable without heavy infra.** Vector tools use `FakeVectorStore`,
   graph tools `FakeGraphStore` read methods, the agent a `FakeAgentClient` — Milvus/Neo4j/Azure
   OpenAI only run behind opt-in markers, never the default suite (the ingestion discipline).
3. **Citations are first-class.** Every retrieval result and every `/ask` answer cites its source
   (doc_id, doc_key, version, heading, and page/bbox via provenance where available) — the platform's
   value is auditable financial answers, not just text.
4. **Layering unchanged:** pure algorithms in `fdi.core.retrieval` (fusion, chunk assembly, table
   query, citation formatting); orchestrators in `fdi.pipeline` (import platform stores + core, like
   `resolve_entities`); routes/agent in `fdi.api`. `api | workers → pipeline → platform → core`,
   exhaustive, stays 1-kept/0-broken.
5. **The `/ask` agent (RT-8) plans over the tool set**, it does not hard-code a retrieval pipeline —
   an LLM with the RT tool schemas decides which to call for a given question, then synthesizes a
   cited answer; a `FakeAgentClient` returns a scripted tool-call plan for deterministic tests.

## Explicitly deferred (beyond RT-8)

- Re-ranking with a cross-encoder model (RT-4 uses RRF, a model-free fusion).
- Streaming `/ask` responses; multi-turn conversational memory.
- A persisted, incrementally-maintained entity index at 5000-doc scale (RT-7 may add a basic one;
  incremental maintenance is later).
- Numeric reconciliation / cross-statement math (RT-6 extracts values; reconciling them is a
  quality-layer follow-on).
