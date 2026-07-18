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
| **RT-1** | Retrieval foundations — unified chunk record + filterable index | heavy | ingestion | `fdi.core.retrieval.build_chunks` + `ChunksStage` (`deps=("ast","semantic","provenance")`) → per-doc `chunks` artifact `[{chunk_id, heading, text, section_type, page, para_count}]`; enrich `IndexStage` so Milvus metadata carries `{doc_key, version, section_type, page}`; a `resolve_chunk(doc_id, chunk_id)` helper every text-returning tool uses. |
| **RT-2** | Semantic (vector) retrieval | medium | RT-1 | `vector_search(query, filters, k)` — embed query (BGE), Milvus search with metadata filters, resolve hits to text+citation via RT-1. `POST /retrieve/vector`; upgrades `/search` to return text. |
| **RT-3** | Lexical / keyword retrieval | medium | RT-1 | Postgres full-text (`tsvector`) over a `chunk` table populated from the `chunks` artifact; `keyword_search(query, filters)`. `POST /retrieve/keyword`. |
| **RT-4** | Hybrid retrieval + rerank | light | RT-2, RT-3 | pure `fuse(vector_hits, keyword_hits)` (Reciprocal Rank Fusion); one `POST /retrieve` (hybrid) tool. |
| **RT-5** | Graph retrieval (+ corpus-graph fix) | heavy | ingestion | Fix `graphload` id-namespacing (`{doc_id}:{id}`) + real `:Document/:Section/:Paragraph` labels & `:CONTAINS/:READING_ORDER` rel-types; `Neo4jGraphStore` read methods (mockable); tools `neighbors`, `document_structure`, `reading_order`, `provenance(node)→page/bbox`. `POST /graph/*`. |
| **RT-6** | Structured financial retrieval | heavy | RT-1 | Query the canonicalized table `grid`s: `get_tables(doc_id, filters)`, `concept_lookup(concept)` linking `CanonicalConcept`→grid rows and extracting the numeric cell (the deferred numeric extraction). `POST /tables/*`, `/concepts/*`. |
| **RT-7** | Cross-document / entity retrieval | medium | RT-6 (concepts), crossdoc | `entity_timeline(entity)`, `compare_concept_across_versions(entity, concept)`; optionally persist the entity index for fast lookup. `GET /entities/{id}/timeline`, `POST /entities/compare`. |
| **RT-8** | Retrieval agent — `POST /ask` | heavy | RT-2…RT-7 | Register every RT tool as an Azure OpenAI function schema; an agent loop (plan → call tools → synthesize) returns a **cited** answer `{answer, citations, tool_trace}`. Mockable LLM seam (`FakeAgentClient` in the default suite; real Azure OpenAI behind an opt-in marker). The capstone that makes the tools "tools." |

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
