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
| **RT-2** | Semantic (vector) retrieval | medium | RT-1 | ✅ done (2026-07-18). `vector_search(query, filters, k)` (`fdi.pipeline.retrieval`) — embed query (BGE), Milvus search with `section_type`/`doc_key`/`version` filters, resolve hits to text+citation via RT-1's `resolve_chunks`. `POST /retrieve/vector`. `POST /search` deliberately left unchanged (design spec §2 decision 5) — see handoff below. |
| **RT-3** | Lexical / keyword retrieval | medium | RT-1 | ✅ done (2026-07-18). Postgres full-text (`tsvector`) over a `chunk` table populated from the `chunks` artifact; `keyword_search(query, filters)`. `POST /retrieve/keyword`. |
| **RT-4** | Hybrid retrieval + rerank | light | RT-2, RT-3 | pure `fuse(vector_hits, keyword_hits)` (Reciprocal Rank Fusion); one `POST /retrieve` (hybrid) tool. |
| **RT-5** | Graph retrieval (+ corpus-graph fix) | heavy | ingestion | ✅ done (2026-07-18). Fixed `graphload` id-namespacing (`{doc_id}:{id}`) + real `:Document/:Section/:Paragraph` labels & `:CONTAINS/:READING_ORDER` rel-types (validated allowlist, injection-safe); `Neo4jGraphStore`/`FakeGraphStore` read methods `neighbors`/`document_structure`/`reading_order` (mockable, agree on shape); `fdi.pipeline.graphquery` attaches a `{doc_id, doc_key, version, heading, page, bounding_regions}` citation to every node. `POST /graph/neighbors`, `GET /documents/{id}/graph/structure`, `GET /documents/{id}/graph/reading-order` (503-gated). |
| **RT-6** | Structured financial retrieval | heavy | RT-1 | ✅ done (2026-07-18). `fdi.core.tables.parse_numeric` (financial cell parsing: thousands incl. Indian lakh grouping, parenthetical negatives, leading sign, percent-as-printed, currency strip/no-FX, blanks/dashes/N-A→`None`, never raises) + `find_concept_rows` (normalized word-token-subsequence match of a concept's ontology synonyms against grid column 0, returns ALL numeric columns per matched row) + `filter_tables` (keyword over column headers) — pure, `fdi.core.tables`' only cross-core import is `fdi.core.ontology` (new public `normalize_label`/`synonyms_for`). `fdi.pipeline.tables.get_tables`/`concept_lookup` read the EXISTING `structure` artifact on demand (no new stage/artifact). `GET /documents/{doc_id}/tables?keyword=`, `GET /documents/{doc_id}/concepts/{concept}`, citation-carrying (`doc_id`, `table_index`, `row_label`, `column_header`). |
| **RT-7** | Cross-document / entity retrieval | medium | RT-6 (concepts), crossdoc | ✅ done (2026-07-18). `fdi.core.entities.build_entity_timeline`/`build_concept_comparison` (pure) reshape `fdi.core.crossdoc.consolidate_entity`'s output into a `doc_key`-scoped, version-ordered timeline and assemble a cross-version concept comparison from RT-6's `concept_lookup` results, `status ∈ {matched, no_match, no_structure}` per version. `fdi.pipeline.entities.entity_timeline`/`compare_concept_across_versions` orchestrate I/O (`DocumentRepository.list_by_doc_key`, the newly-public `fdi.pipeline.crossdoc.read_artifact`, RT-6's `concept_lookup`), `None` on an unknown `doc_key`. `GET /entities/{doc_key}/timeline`, `POST /entities/compare` (404 on unknown `doc_key`) — see handoff below. |
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

## What RT-6 shipped that RT-7 must know

- **`fdi.pipeline.tables.concept_lookup(*, session, store, doc_id, concept) -> list[dict] | None`**
  is the per-document primitive RT-7's `compare_concept_across_versions(entity, concept)` calls once
  per `doc_id` from a `list_documents()` loop — the same shape `resolve_entities` (crossdoc) already
  loops `_read_artifact` per document. `None` means "no `structure` artifact yet for this doc" (skip
  it, do not treat as a zero/empty result); `[]` means "has `structure`, concept just wasn't found
  there" — a real, distinct outcome RT-7 must not conflate with the `None` case.
- **Citation shape**: each match is `{table_index, row_label, values: [{column_header, raw, value},
  ...]}`. `table_index` is only stable within one `structure` artifact for a given `doc_id` (not a
  corpus-wide durable id) — RT-7 must always carry `doc_id` alongside a `table_index` citation, same
  posture as RT-1's `chunk_id`. `values` covers EVERY numeric column (all periods found in the row);
  RT-7 owns picking which `column_header` (period) answers a specific comparison question — RT-6
  deliberately never guesses "the" period.
- **Known false-positive RT-7 inherits (spec decision 5, unsolved by design):** `concept_lookup(...,
  concept=REVENUE)` will also match a row like `"Cost of revenue"` (the synonym word "revenue" is a
  token-subsequence of the label) even though that row is a cost line, not revenue. RT-6 does not
  disambiguate — it always returns `row_label` precisely so a human/agent can catch the false
  positive by inspection. If RT-7's cross-version comparison surfaces this as a real product problem
  (rather than a recoverable-by-inspection one), disambiguation is RT-7's or a follow-on's call, not
  something already fixed underneath it.
- **Two forks intentionally left open** (spec §8, not resolved by RT-6's implementation): (1)
  on-demand vs. persisted `tables`/`concepts` artifact — RT-6 reads `structure` fresh on every call
  for freshness/simplicity; if RT-7's corpus-wide loop turns out to be query-time-hot, a persisted
  stage can be added later without changing `fdi.core.tables`' pure functions, only their caller; (2)
  synonym-matching aggressiveness (word-token-subsequence containment) — whether the REVENUE/"Cost of
  revenue" false-positive class needs disambiguation now vs. later is a product-risk call RT-7 (or a
  human) should make with real corpus data, not something this spec settles.
## What RT-2 shipped that RT-3…RT-8 must know

- **`vector_search`/`build_vector_results`'s pure/impure split is the template for later retrieval
  tools.** `fdi.core.retrieval.build_vector_results(hits, resolved, docs, *, section_type, doc_key,
  version, k)` is pure — no I/O, no ORM types (docs is a plain `{doc_id: {doc_key, version}}` dict,
  never a `Document` row) — and does all the filtering, citation-joining, and truncation, never
  re-sorting (output order is a strict sub-sequence of the input, best-match-first, order).
  `fdi.pipeline.retrieval.vector_search` does only the I/O (one embed call, one
  `vector_store.search`, one `DocumentRepository.get_many`, one `resolve_chunks`) and delegates
  assembly to the pure function. RT-5 (graph retrieval) and RT-6 (table retrieval) should mirror this
  shape: a pure `fdi.core.retrieval` (or sibling) assembler + a pipeline-layer orchestrator that does
  the fetching, matching `resolve_entities`/`fdi.core.crossdoc`'s precedent.
- **`DocumentRepository.get_many(ids: list[str]) -> dict[str, Document]`** (`fdi.platform.db.
  repository`) is a reusable batched-doc-lookup primitive: empty input → no query, unknown ids are
  simply absent (never an error). Any later tool that needs to join multiple hits' `doc_id`s back to
  `doc_key`/`version` in one round trip (RT-6's table retrieval, RT-7's cross-document entity work)
  should reuse this rather than re-implementing a per-id lookup.
- **`_OVERFETCH_FACTOR = 5`** (private constant, `fdi.pipeline.retrieval`) is how RT-2 filters
  `section_type`/`doc_key`/`version` without a `VectorStore.search` protocol change: when any filter
  is set, `vector_search` requests `k * 5` from the vector store (in-memory `section_type` check +
  one batched Postgres join for `doc_key`/`version`), single-shot — no adaptive re-query if fewer than
  `k` survive filtering. `VectorStore.search(vector, k)`'s signature was deliberately NOT extended
  with a filter parameter (would touch both `FakeVectorStore` and `MilvusVectorStore`, shared by
  RT-3/RT-5/RT-6); revisit only if a real corpus shows the over-fetch factor is measurably too
  expensive.
- **`POST /search` was deliberately left unchanged — not silently dropped from scope.** RT-2's design
  spec (§2 decision 5) chose to keep `/search` as the raw, uncited, never-dropping probe it already
  was (its own test indexes a synthetic vector with no backing `Document` row or `chunks` artifact —
  routing it through `resolve_chunks`/citation joins would silently return zero results for that
  exact, already-tested case) and ship `POST /retrieve/vector` as a strictly additive, citation-first
  endpoint instead. **RT-8's `/ask` agent should register `vector_search`/`POST /retrieve/vector` as
  its citation-bearing semantic-search tool, not `/search`.**

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

## What RT-5 shipped that RT-7/RT-8 must know

- **The `{doc_id}:s{i}` section-node identity now matches Milvus/RT-1 exactly.** `GraphLoadStage` v2
  (`deps=("graph",)`, version bumped `1 → 2`) namespaces every node id `{doc_id}:{bare_id}` (section
  nodes → `{doc_id}:s{i}`, byte-identical to a Milvus hit id and RT-1's `chunks[…].chunk_id` join key)
  and every edge's `source`/`target` the same way, before calling `GraphStore.load`. This is the fix
  for the pre-RT-5 bug where every document reused the identical bare ids (`doc`, `s0`, `s0p1`, …) and
  a second document's load collided onto the first's (MERGE last-writer-wins) — the collision-fix
  regression test (`test_graph_load_namespaces_ids_no_collision_across_documents`) and its live-Neo4j
  counterpart (`test_real_load_two_documents_do_not_collide`, opt-in `neo4j` marker) both cover this.
  A future cross-store join (vector hit → graph neighbors → chunk text) needs no id translation for
  section nodes.
- **`Neo4jGraphStore._load_tx` MERGEs under real Cypher labels/rel-types** (`:Document`/`:Section`/
  `:Paragraph`, `:CONTAINS`/`:READING_ORDER`) via a closed, validated allowlist (`_cypher_label`/
  `_cypher_rel_type` in `fdi.platform.graphstore`) — only the allowlist's fixed literal values ever
  reach an f-string, never the raw `type` field, so a malformed artifact cannot inject Cypher via the
  label/rel-type position. An unknown `type` raises `ValueError` rather than falling back to a generic
  label. Every node also carries a `doc_id` property (not just the namespaced id), so a document-scoped
  read is an indexable exact-equality property match, not a string-prefix scan.
- **The `GraphStore` read seam — `neighbors(node_id)`, `document_structure(doc_id)`,
  `reading_order(doc_id)`** — joins `load` on the existing protocol (`fdi.pipeline.graphstore.
  GraphStore`), implemented identically in shape by `Neo4jGraphStore` (real Cypher) and
  `FakeGraphStore` (in-memory, used by the default test suite — no live Neo4j required). Both share one
  pure ordering algorithm, `fdi.core.graph.order_reading_chain`, since node id string order is NOT
  reading order (`"s10" < "s2"` lexicographically) — only the `READING_ORDER` edge chain is. All three
  methods agree on "unknown id/doc → empty result, never raise."
- **The citation shape every graph tool attaches**: `{doc_id, doc_key, version, heading, page,
  bounding_regions}`, built by `fdi.pipeline.graphquery` (`neighbors`/`document_structure`/
  `reading_order`, all `async`, take `session, store, graph_store` + the relevant id). It splits a
  namespaced node id on the FIRST `:` to recover `(doc_id, bare_id)` (`intake_document` generates
  `doc_id` via `str(uuid.uuid4())`, verified to never contain `:`), reads the `provenance` artifact and
  the `Document` row ONCE per call (not per node), and degrades `page`/`bounding_regions` to `None`
  — never omits the node, never raises — when no provenance record matches.
- **Two forks flagged, not resolved**: (1) no raw-Cypher route — the three read methods are the entire
  sanctioned graph surface, an admin-only ad-hoc-query route is a human decision for later if a real
  need appears; (2) stale pre-fix `:Node`-labeled nodes from any earlier unnamespaced load are NOT
  migrated — `MERGE` only adds/updates, so they sit inert alongside the new namespaced nodes; whoever
  owns the Neo4j deployment should start from a fresh volume or run a one-time manual cleanup
  (`MATCH (n:Node) DETACH DELETE n`) before relying on a clean corpus graph.
- **API wiring added, did not exist before**: `GraphStoreDep`/`get_graph_store` (`api/deps.py`),
  `app.state.graph_store = Neo4jGraphStore.from_settings(settings)` (`api/app.py`'s lifespan). The
  `graph_store` wired in `fdi.workers.tasks` is unrelated (that's for the `graph_load` STAGE); this is
  the first route-side wiring.

## What RT-7 shipped that RT-8 must know

- **Entity identity is `doc_key`, NOT a crossdoc `entity_id`.** `entity_timeline(doc_key)` and
  `compare_concept_across_versions(doc_key, concept)` resolve their member set via
  `DocumentRepository.list_by_doc_key(doc_key)` — an indexed, O(versions-of-one-key) query, never a
  corpus scan or clustering pass. `resolve_entities` (crossdoc/SP-16) stays the corpus-wide DISCOVERY
  step — "which `doc_key`s probably belong to the same company" — and is unchanged and still the only
  way to find `doc_key`s in the first place; its output's `members[].doc_key` field is exactly the input
  RT-7's tools want. RT-8's agent that needs to answer "how did ACME's revenue move" should call
  `POST /entities/resolve` first to discover candidate `doc_key`s, then loop RT-7's tools per `doc_key`
  — RT-7 does NOT fuse a multi-`doc_key` crossdoc cluster into one timeline in this slice (deferred,
  design spec §6). `build_entity_timeline` never emits `entity_id` in its response — it is intentionally
  removed, not aliased to `doc_key`, so nothing implies a stable id that isn't `doc_key` itself.
- **The `status` field on every comparison version — `"matched" | "no_match" | "no_structure"`.**
  `compare_concept_across_versions` preserves RT-6's `concept_lookup` `None` ("no `structure` artifact
  yet") vs. `[]` ("has `structure`, concept's synonyms matched no row") distinction all the way to the
  API response instead of collapsing both to an indistinguishable empty `rows` list. Every version of a
  `doc_key` is always present in the output — no version is ever silently dropped for having no data.
  RT-8's agent (and any UI) should render `"no_structure"` ("not yet processed") differently from
  `"no_match"` ("processed, concept not reported this way").
- **Both functions reuse existing algorithms unchanged, no reimplementation.** `entity_timeline` calls
  `fdi.core.crossdoc.consolidate_entity` once (version-ordering, `None`-version-last, per-member
  `section_count`/`concept_count`/`quality_ok`, `concept_presence`, `quality_trend` — all inherited, not
  recomputed) and overrides its `entity_id` with the caller's `doc_key`. `compare_concept_across_versions`
  calls RT-6's `fdi.pipeline.tables.concept_lookup` once per member document, unchanged — no new numeric
  extraction or concept matching. RT-7 also inherits RT-6's documented false-positive class (a synonym
  word inside an unrelated label, e.g. "Cost of revenue" matching `REVENUE`) unsolved, same as RT-6 left
  it — `row_label` is surfaced unchanged in every comparison row so a human/agent can catch it by
  inspection.
- **No persisted entity index — on-demand, like `resolve_entities`/RT-6.** `list_by_doc_key` is a single
  indexed `WHERE doc_key = ...` query; there is no O(N²) corpus scan here to amortize with a cache
  (unlike `resolve_entities`'s clustering pass). Revisit only if a real deployment shows a `doc_key` with
  hundreds of versions and query-time cost becomes visible.
- **`fdi.pipeline.crossdoc._read_artifact` is now public `read_artifact`** (pure rename, no behavior
  change) — `fdi.pipeline.entities.entity_timeline` reuses it for the same "latest artifact or `None`,
  isolate one document's store failure rather than aborting the whole call" posture `resolve_entities`
  already relied on. `resolve_entities`'s own call sites were updated in the same commit;
  `test_resolve_entities.py` passes unchanged.
- **Citations are `{doc_id, doc_key, version, table_index, row_label, column_header}`** on every matched
  comparison row — `rows` is `concept_lookup`'s own row shape, untouched, so RT-6's citation contract
  carries through unchanged.

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
