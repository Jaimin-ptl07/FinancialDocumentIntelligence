# SP-2 execution ledger

Plan: docs/superpowers/plans/2026-07-16-sp2-base.md
Base: 8f366f4

Task 1: complete (commits 8f366f4..8d15473, review clean)
Task 2: complete (commits 8d15473..4f18dca, review clean)
Task 3: complete (commits 4f18dca..0792892, review clean, integ 10 passed)
Task 4: complete (commits 0792892..a316fbe, review clean)
Task 5: complete (commits a316fbe..628df3d, review clean)
Task 6: complete (commits 628df3d..6e82d0f, review clean, 7 passed)
Task 7: complete (commit 87534f8, review clean, 15 passed; additive, Job intact)
Task 8: complete (commit 0c11ac6, review clean, repo 13 + resolver 7 pass, contract 1 kept 0 broken; RunFacts->core)
Task 9: complete (commit 3809e74, review clean, cache-hit stage.calls==1, contract 1 kept 0 broken)
Task 10: complete (commit 25dcccf, review clean, 1 passed)
Task 11: complete (commit 7e4391e + isolation fix; additive run_stage_task + StageDispatcher; full non-e2e suite 152 passed, contract 1 kept 0 broken)
Task 12a: complete (commit 646e112, review clean, test_api 30 + non-e2e 152 passed, contract 1 kept 0 broken; queued-aggregation)
Task 12: complete (12a 646e112 + 12b b91cc38; API reframed to framework, SP-1 machinery retired, jobs dropped; non-e2e 123 passed, contract 1 kept 0 broken)
Task 13: complete (commit 86cb6de, review clean, test_api 33 + non-e2e 126 passed, contract 1 kept 0 broken)
Task 14: complete (commit pending echo; e2e assertion updated to content-addressed key; 3 skipped stack-down; LIVE run deferred — ghcr unreachable)
Task 15: complete (SP-2 audit; 126 passed; gates green; docs updated)
SP-2 COMPLETE: 15/15

## SP-3 eval harness (plan 2026-07-17-sp3-eval-harness.md, base a12caee)
SP-3 Task 1: complete (335de4a, metrics, 2 passed)
SP-3 Task 2: complete (44daa54, eval_types, 1 passed, cast fix)
SP-3 Task 3: complete (21e1729, evaluation runner+registry, 5 passed, contract kept)
SP-3 Task 4: complete (46f32c1, dataset_store, 1 passed, contract kept)
SP-3 Task 5: complete (093128c, eval tables + migration 0004, 3 passed, contract kept)
SP-3 Task 6: complete (2fe9b32, EvalRepository leaderboard, 2 passed, contract kept)
SP-3 Task 7: complete (d2c522e, UppercaseCandidate + toy_dataset, 2 passed, gates clean)
SP-3 Task 8: complete (1fa7101, eval_service e2e agg 1.0, non-e2e 141 passed, contract kept)
SP-3 Task 9: complete (a3f9d68, POST /eval + leaderboard, test_api 35, non-e2e 143 passed, contract kept)
SP-3 COMPLETE: 10/10

## SP-4 document intake (plan 2026-07-17-sp4-document-intake.md, base fcea2f8)
SP-4 Task 1: complete (3ba1edd, sha256_hex, 1 passed, gates clean)
SP-4 Task 2: complete (bc1f4ee, put_bytes/get_bytes, 11 passed, contract kept)
SP-4 Task 3: complete (e2f03e5, document intake columns + UNIQUE(checksum) mig 0005, 5 passed, contract kept)
SP-4 Task 4: complete (4a1d02f, DocumentRepository, 1 passed, contract kept)
SP-4 Task 5: complete (8131765, intake_document, non-e2e 149 passed, contract kept)
SP-4 Task 6: complete (e7992a0, POST /documents upload + FDI_MAX_UPLOAD_MB, test_api 37, non-e2e 151 passed, contract kept; fixed cross-file checksum collision)
SP-4 COMPLETE: 7/7

## SP-5 OCR Azure DI (plan 2026-07-17-sp5-ocr-azure-di.md, base dbef4fa)
SP-5 Task 1: complete (c20c29c, normalize_ocr, 2 passed, pure)
SP-5 Task 2: complete (f7383e8, OcrClient + FakeOcrClient, 1 passed, contract kept)
SP-5 Task 3: complete (9535184, StageContext read_source+services, non-e2e 155 passed backward-compat, contract kept)
SP-5 Task 4: complete (9b518ea, FDI_AZURE_DI_* config in all 3 files, 5 passed, contract kept)
SP-5 Task 5: complete (1570763, AzureOcrClient + azure-ai-documentintelligence dep, 3 passed, contract kept)
SP-5 Task 6: complete (acf0f9d + stale-test fix; OcrStage fake.calls==1 cache-proof; non-e2e 160 passed, contract kept)
SP-5 Task 7: complete (06345fa, worker Azure wiring + POST /documents/{id}/ocr, test_api 39, non-e2e 162 passed, contract kept)
SP-5 Task 8: complete (adaf2d7, opt-in real-DI smoke test, 1 skipped honest)
SP-5 COMPLETE: 9/9

## SP-6 normalization (plan 2026-07-17-sp6-normalization.md, base 59e10a9)
SP-6 Task 1: complete (ed25c14, Role enum + canonical_role, 9 passed)
SP-6 Task 2: complete (6174b76, normalize_document, 2 passed, isinstance-narrowed)
SP-6 Task 3: complete (6e3f145, NormalizeStage auto-chained, non-e2e 174 passed, contract kept)
SP-6 COMPLETE: 4/4

## SP-8 naive AST (plan 2026-07-17-sp8-ast-naive.md, base f172985)
SP-8 Task 1: complete (1b31f28, build_ast, 3 passed, pure)
SP-8 Task 2: complete (c208c6a, AstStage auto-chained, non-e2e 178 passed, contract kept)
SP-8 COMPLETE: 3/3

## SP-18 thin UI (plan 2026-07-17-sp18-thin-ui.md, base 630736d)
SP-18 Task 1: complete (8fc84a7, stages + artifact-proxy endpoints, non-e2e 179 passed, contract kept)
SP-18 Task 2: complete (44f708f, document-journey UI, lint+build clean)
SP-18 COMPLETE: 3/3 -- STAGE 1 COMPLETE

## SP-7a OCR enrichment (plan 2026-07-17-sp7a-ocr-enrichment.md, base 9296c36)
SP-7a Task 1: complete (5aad44a, enriched OCR schema 2 + OcrStage.version 2, non-e2e 180 passed, normalize/ast self-adjusted, contract kept)
SP-7a COMPLETE: 2/2

## SP-7b cross-page tables (plan 2026-07-17-sp7b-cross-page-tables.md, base e70d08d)
SP-7b Task 1: complete (82f1853, table spans schema 3 + OcrStage.version 3, non-e2e 180 passed, self-adjusted, contract kept)
SP-7b Task 2: complete (fe6caec, merge_cross_page_tables MS heuristic, 6 passed, pure, contract kept)
SP-7b Task 3: complete (9934108, StructureStage stitch+filter, non-e2e 187 passed, contract kept)
SP-7b CORE COMPLETE: 4/4
SP-17: MERGED to feature/sp-1-project-setup (parallel worktree exec; 205 non-e2e passed, gates green)
SP-7c: MERGED (parallel worktree exec; base now 211 non-e2e passed, gates green)
SP-10: MERGED (last of parallel wave; base 222 non-e2e passed, gates green)
PARALLEL WAVE COMPLETE: SP-7c + SP-10 + SP-17 merged
WAVE 2 DISPATCHED (base 7e8aa13): SP-9 graph + SP-11 ontology + SP-7d linking — parallel worktree exec, TDD
  merge order plan: SP-9 (disjoint) -> SP-11 (shares stages/__init__ w/ SP-9) -> SP-7d (shares __init__ + bumps OcrStage.version 4)
SP-9: MERGED (wave 2; base 229 non-e2e passed target, gates green)
SP-11: MERGED (wave 2; base 251 non-e2e passed, gates green)
SP-7d: MERGED (wave 2; ocr v4; base 262 non-e2e passed, gates green)
PARALLEL WAVE 2 COMPLETE: SP-9 + SP-11 + SP-7d merged; 10 stages registered
WAVE 3 DISPATCHED (base 7cf6719): SP-12 provenance + SP-17b structure-quality — parallel worktree exec, first 2-dep stages
SP-17b: MERGED (wave 3; QualityStage 2-dep v2; base 267 passed, gates green)
SP-12: MERGED (wave 3; ProvenanceStage 2-dep; base 276 passed, gates green)
PARALLEL WAVE 3 COMPLETE: SP-12 + SP-17b merged; 11 stages; first two 2-dep stages
WAVE 4 DISPATCHED (base 03ba130): SP-13 embeddings + SP-9b neo4j-loader — store layers, mockable seam + real clients (opt-in). SP-9b adds neo4j dep + FDI_NEO4J_*; SP-13 adds FDI_EMBED_*. Merge SP-13 first (no dep), then SP-9b; preserve config trio + resolve additive config/compose/.env/worker/__init__ conflicts.
SP-13: MERGED (wave 4; EmbeddingStage; base 286 passed, gates green)
SP-9b: MERGED (wave 4; GraphLoadStage; 6 additive conflicts resolved incl config trio; base 294 passed, gates green)
PARALLEL WAVE 4 COMPLETE: SP-13 + SP-9b merged; 13 stages; real Azure-embed + Neo4j clients (opt-in). NOTE: merge tail-truncation nearly missed 3 conflicts (test_config/compose/roadmap) — caught via pytest collection error; always full marker-sweep after a wide merge.
SP-14 DRAFT DISPATCHED (base 318c5e6): vector retrieval — VectorStore seam + IndexStage + POST /search; Milvus opt-in. Single SP (parallelism shrinking — SP-15/16 likely depend on SP-14/graph). Next: execute in a worktree, then assess SP-15/16.
SP-14: MERGED (vector retrieval; IndexStage + POST /search + Milvus opt-in; base 306 passed, gates green). Clean merge (no parallel partner).
NOTE: pymilvus resolved 3.0.0 vs compose milvus 2.6.20 server — live-integration compat to verify when the vector profile is exercised (opt-in; does not affect the mocked suite).
RESUMED post-restart. State: SP-14 merged (306 passed); SP-13 shipped Azure embeddings but user wants BGE-small — correcting.
WAVE 5 DISPATCHED: SP-13b (BGE-small via fastembed, 384-dim, replaces Azure embed backend; config restructured) + SP-15 (KnowledgeStage, first 3-dep). Disjoint; parallel worktrees. Merge SP-13b first (config trio), then SP-15 (roadmap-only conflict).

## SP-19 UI backend enablement (plan 2026-07-20-sp19-ui-backend-enablement.md, base 4b97648)
Baseline fd64833: v3.1 schema + get_tables removal committed (was uncommitted WIP; 507 non-e2e passed)
SP-19 Task 1: complete (5af27c9, review clean; 29 unit tests, fast suite 513 passed; extraction revived 0 -> 198 specs; integration gate UNRUN - Docker down)

## EX-1 unit-batched extraction (plan 2026-07-20-ex1-unit-batched-extraction.md, base 996c6f7)
EX-1 Task 1: complete (commit d714624, review clean, 32 passed, ruff+mypy green; FieldSpec +field_path/periods/locator_keywords)
EX-1 Task 2: complete (commit 5aaeeed, review clean, 41 passed, ruff+mypy green; UnitSpec + grouping/top_k/needs_tables)
EX-1 Task 3: complete (commit 576001c, review clean, 54 passed, ruff+mypy green; detector verified =133 on real 2752-chunk doc by controller AND implementer)
  Minor (defer to final review): filter_candidates page check does not exclude bool (harmless, matches existing Candidate-trusting convention)
  Minor (defer to final review): min(pages) could pick a table-of-contents mention of "Consolidated Balance Sheet" earlier than true section start; brief-specified algorithm, correct on real doc
  Note: pre-existing test helper _cand(evidence_id,text,score) renamed to _scored_cand across ~12 sites; reviewer verified every site individually, no semantic change
EX-1 Task 4: complete (commits ee11a4a + fix b082e2e, review clean, 62 unit / 546 fast passed, ruff+mypy green)
  Reviewer caught Important plan-mandated defect: shared-rules refactor replaced the current-period IMPERATIVE with a
  neutral description, and the existing guidance test passed only INCIDENTALLY (noun phrases survived as substrings).
  Human adjudicated -> fix. Imperative restored for non-dual-period fields alongside the description; test strengthened
  to pin "not dual-period" + "never the prior-year comparative" so removal now fails it. Re-review APPROVED.
EX-1 Task 5: complete (commit 3348fe7, review clean, 546 fast passed, ruff+mypy green; extract_unit + rewire)
  Controller overrode brief twice: (1) _read_chunks renamed public read_chunks (2nd consumer), all 4 sites verified;
  (2) order restoration uses per-identity QUEUE not {id(f):i}, so a duplicated FieldSpec object cannot collide.
  Minor (defer to final review): blanket contextlib.suppress(Exception) in _pool_for/_consolidated_start_page could
  hide a genuine bug; pre-existing pattern carried over, not a regression.
EX-1 Task 6: complete (commit a504b3b, review APPROVED; integration 3 passed by name, ruff+mypy green)
  Implementer subagent HUNG (polling loop, never committed or reported). Controller took over, verified its partial
  edits, ran the tests, and committed.
  PLAN GAP CAUGHT BY FULL SUITE: test_api.py::test_extract_document_happy_path also scripts FakeExtractorClient with
  single-field dicts; the plan's breaking-change audit only examined test_extract_document.py. Full suite failed 1/765;
  fixed by re-keying, no assertion weakened. Swept all 5 FakeExtractorClient sites: 2 re-keyed, 2 empty-list, 1 new.
  Reviewer confirmed the batching test is non-vacuous (would fail at 5 calls) and input/unit orders genuinely differ.
EX-1 FINAL WHOLE-BRANCH REVIEW (opus, 996c6f7..a504b3b): CHANGES REQUESTED -> fixed d6fbb35 -> re-review READY TO MERGE
  BLOCKER found & fixed: find_consolidated_start_page used min(page) over marker matches. An annual report's Contents
  page ("Consolidated Balance Sheet ....... 140") matches the markers verbatim; a TOC hit at page ~4 would drop every
  candidate, empty every unit, short-circuit before the LLM, and return ALL 198 fields null with no error and no log.
  Real doc returned 133 only by luck (its TOC did not chunk with those phrases). Two independent guards added:
  dot-leader skip (\.{4,}) + ignore matches below 25% of max page (named constant + rationale).
  Controller independently verified 6 cases incl. synthetic TOC -> 140, only-early -> None, REAL DOC -> 133 unchanged.
  Also fixed: pipeline-layer logging of the boundary (core stays log-free), stale docstrings, single-field prompt now
  states dual-period status, weak/one-sided tests strengthened, bool exclusion made consistent.
  Residuals closed in c70123a: log chunk count on both outcomes (missing-artifact vs no-consolidated-section were
  indistinguishable); dual-period assertion made two-sided ("dual-period" is a substring of "not dual-period").
EX-1 ALL 6 TASKS COMPLETE. Definitive full suite at c70123a: 769 passed, 9 skipped, 0 failed (baseline was 513+9). Branch green, reviewed, NOT yet merged; end-to-end accuracy verification still outstanding.

## EX-1 END-TO-END VERIFICATION (real stack, real Azure OpenAI) -- 2026-07-21
Run: host-side against containerised Postgres/MinIO/Milvus (api/worker IMAGES are stale, so driving the
containerised API would have tested the OLD per-field extractor). ocr + semantic seeded from cache per spec.

PREMISE CONFIRMED:
  AZURE OPENAI EXTRACTION CALLS: 18 (expected 18)   <- was 198. The 11x cost win is real.
  Azure DI calls: 0 | semantic LLM calls: 0 | 198/198 fields returned | 2752 chunks indexed
  extractor.consolidated_boundary_found page=133    <- the TOC-guarded detector works in production

ACCURACY DID NOT REPRODUCE: 133/198 fields have a real (unwrapped) value vs the audit's 173/198 correct.
ROOT CAUSE FOUND -- PRE-EXISTING, NOT EX-1: fdi.pipeline.retrieval.keyword_search uses plainto_tsquery,
which ANDs EVERY term. Measured on the live corpus:
    full 18-word RU_COMPANY query -> 0 hits | "corporate information" -> 9 | "CIN" -> 13 | OR-semantics -> 138
  ALL 18 unit retrieval_query strings return 0 keyword hits. The keyword channel is 100% DEAD in production,
  so hybrid_search silently degrades to VECTOR-ONLY for every unit. The audit harness used its own
  keyword_search with OR/count-of-matching-terms semantics, which is why it scored 87.4%.
  Compounding: RU_COMPANY (narrative-only) got only TABLE chunks from the vector channel; the narrative-only
  filter then dropped them, leaving a poor pool -> all 9 fields null.
  NOTE: build_extraction_record's non_null_count counts {"current":None,"prior":None} as non-null (181 vs
  the true 133) -- misleading aggregate, worth fixing.
NEXT: fix keyword_search (websearch_to_tsquery / OR + ts_rank, or restore count-of-matching-terms). That is
the single highest-value retrieval fix on the board, and it is independent of EX-1.

## RT-9 BM25 keyword retrieval (plan 2026-07-21-rt9-bm25-keyword-retrieval.md, base 205d2e9)
Pre-flight: fixed plan bug (bc5d304) -- Task 1's fake filter parser handled only `field == "value"` and RAISED on
  anything else, but Task 5's _milvus_filter emits `doc_id in [...]`. Fake would have rejected the only shape
  production emits, while Task 1 tested a shape production never emits.
RT-9 Task 1: complete (commit 1bf47ba, review clean, 12 passed, ruff+mypy green; VectorStore.search_text + filter)
  Minor (defer to final review): _matches_filter `in [...]` value split is comma-naive -- `doc_id in ["a,b"]` parses
    as two values instead of one; not reachable from _milvus_filter (doc ids carry no commas)
  Minor: task-1-report mislabels a NEW test as pre-existing; cosmetic, report only
RT-9 Task 2: complete (commits 2adfacb + fix 508b579, re-review APPROVED, 7 unit / 594 fast passed, ruff+mypy green)
  Reviewer caught Important coverage regression: the rewritten test asserted only schema field NAMES, so an
  implementation hardcoding dim=384 instead of self._dim would have passed the entire suite. Fixed: store built with
  dim=4 and the vector field's dim asserted. Also fixed an ILLUSORY ordering assertion -- the fake captured the schema
  by reference, so "BM25 Function declared BEFORE create_collection" passed even if add_function ran after; the fake
  now snapshots the count inside create_collection. Restored the dropped max_length=512 rationale.
RT-9 Task 3: complete (commits e81b459 + fix 8ded8e4, re-review APPROVED, 4 integration / 594 fast passed)
  Reviewer caught Important gap: the "no matching chunks record" fallback was implemented but UNTESTED. If it ever
  emitted None instead of "", Milvus's str field would reject the upsert -- and a dropped chunk is a silent keyword
  retrieval hole. Test now pins `text == ""` explicitly (a falsy check would also pass for None).
PARALLEL WAVE: Tasks 4 and 8 dispatched concurrently (disjoint files; 8 worktreed from this commit because it needs
  Task 2's schema and cannot branch from mainline). Task 5 is a hard chain after 4. Tasks 6+7 pair after 5.
RT-9 Task 4: complete (commit d04cd4a, review clean, 16 unit / 591 fast passed, ruff+mypy green; build_vector_results
  reads text off the hit, `resolved` dropped, `docs` kept). test_retrieval_fuse.py verified UNEDITED.
  Minor (Task 5 closes both): vector_search still carries an unused `store` param; pipeline/retrieval.py module
  docstring still claims RT-2 calls resolve_chunks.
RT-9 Task 5: complete (bm25_search added, keyword_search/index_chunks_fts deleted; DocumentRepository.ids_for added;
  15 unit (bm25+fuse) / 598 fast passed, ruff clean; test_retrieval_fuse.py verified UNEDITED byte-for-byte).
  Unplanned but required: routes.py's top-level `from fdi.pipeline.retrieval import (... index_chunks_fts,
  keyword_search ...)` is a hard ImportError once those names are deleted, and fdi.api.app imports routes.py at
  module scope -- so tests/unit/test_cors.py (a genuinely fast test) would 500 the whole collection run without a
  fix. Fixed: dropped the dead import, dropped `store=` from the two remaining vector_search/hybrid_search calls,
  and deleted the two routes (`/documents/{id}/index_fts`, `/retrieve/keyword`) whose backing functions no longer
  exist -- no 503 branch added, that redesign stays Task 6's job. Same ImportError problem pre-existed in four
  integration test files that import the deleted names directly: test_keyword_search.py and
  test_index_chunks_fts.py test ONLY the deleted functions (deleted outright, nothing salvageable); test_hybrid_
  search.py and test_extract_document.py used index_chunks_fts merely to double-seed a Postgres `chunk` table
  their real subject (hybrid_search / extract_document) no longer reads (IndexStage already seeds Milvus) --
  trimmed that dead seeding step + the import, left the rest of both files (old `store=`/keyword-only-degrade
  call shapes) for Task 6. extractor.py and agent_tools.py were NOT touched -- their broken `hybrid_search(store=...)`
  calls are wrapped in try/except or only statically inspected by the fast suite, so they don't turn it red; left
  for Task 6 as planned. mypy --strict src: 8 EXPECTED errors across exactly those 3 untouched files (call-arg on
  `store`, arg-type on now-required embedder/vector_store) -- matches the plan's own sequencing note that this
  interim state goes red in mypy/full-suite terms until Task 6.
