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
