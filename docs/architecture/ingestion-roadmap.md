# Financial Document Intelligence — Ingestion Layer Roadmap

- **Status:** Roadmap agreed 2026-07-15. **STAGE 1 COMPLETE** (SP-1–SP-6, SP-8, SP-18). **Stage 2 underway:** SP-7 research done (`docs/architecture/research/2026-07-17-sp7-structural.md`). **Done: SP-7a, SP-7b, SP-7c, SP-7d, SP-9, SP-10, SP-11, SP-17, SP-17b** — landed across two parallel git-worktree waves. OCR enriched to `schema_version 4` (geometry, spans, cells, sections, and figures + captions); `StructureStage` (v2) stitches cross-page tables, removes header/footer, and canonicalizes each table's grid (`canonicalize_table`); `LinkingStage` surfaces figures (with captions) and footnote paragraphs into a `linking` artifact; `GraphStage` builds the `CONTAINS`/`READING_ORDER` node/edge graph over the AST (JSON artifact; Neo4j loading deferred); `SemanticStage` classifies AST sections into 8 `SectionType`s (Azure OpenAI, mockable); `OntologyStage` maps AST headings/paragraphs onto 10 `CanonicalConcept`s via a pure dictionary lookup (deterministic, no LLM; an LLM mapper is an SP-3 candidate); `QualityStage` (v2) validates the AST AND the structure artifact's tables in one merged report (SP-17b). **Next: SP-12 provenance, SP-13 embeddings → SP-14 indexes, SP-15–SP-16.** A future sub-project must add the `graph` Compose profile (Neo4j container) and a loader projecting SP-9's JSON artifact (`{schema_version, nodes[{id,type,label}], edges[{source,target,type}]}`) into Neo4j, keyed by the same `id`s — no schema change anticipated. All measured via SP-3's harness.

### What SP-17b shipped that the next quality follow-on must know

- **A quality report** `{schema_version:1, issues[{severity,code,message}], error_count, warning_count, ok}` (`fdi.core.quality.validate_all`, with `validate` now its single-pair case) at the `quality` artifact key spans BOTH AST-level and structure-level defects, merged into one report — the internal-defect check, distinct from SP-3's ground-truth comparison (§1.1 of the SP-17 spec). `ok = error_count == 0`; warnings never gate `ok`. Issues are ordered pair-by-pair, AST issues before structure issues.
- **`QualityStage` is now a TWO-dependency DERIVED stage** (`deps=("ast", "structure")`, `version=2` — the pipeline's first two-dep stage), auto-scheduled after BOTH `ast` and `structure` by the DAG (`successors("ast")` and `successors("structure")` both include `"quality"`), running `validate_all([(ast, AST_VALIDATORS), (structure, STRUCTURE_VALIDATORS)])` where `STRUCTURE_VALIDATORS = (tables_are_rectangular,)`. Its `input_hash` is `derived_input_hash([ast.output_hash, structure.output_hash])`. Chain `ocr → normalize → ast → {structure} → quality` (structure branches off `ocr` directly) is automatic from a single `run_stage("ocr", ...)`.
- **Next quality follow-on, not this one:** numeric-total reconciliation (waits on SP-11's ontology mapping + real numeric extraction) and cross-document consistency (waits on SP-16's entity resolution/version linking) are still the deferred validator additions — SP-17b only wired the already-written `tables_are_rectangular`, it added no new validator logic.

### What SP-7d shipped that SP-9 (canonical graph) must know

- **OCR enrichment now carries `figures`** (`fdi.core.ocr_schema.normalize_ocr`, `schema_version` 3→4):
  each Azure `figures[]` entry → `{"caption": str, "bounding_regions": [...]}` (missing `caption` key or
  missing `.content` → `""`). `OcrStage.version == 4` — this regenerated the OCR artifact (and, via the
  version-stamp mechanism, its derived `normalize`/`ast`/`structure` artifacts) once, same pattern as every
  prior 7-series bump.
- **The `linking` artifact** `{schema_version:1, figures:[{caption}], footnotes:[{text}]}`
  (`fdi.core.links.extract_links`, wired by `fdi.pipeline.stages.linking.LinkingStage`) sits at the
  `linking` artifact key, a DERIVED stage (`deps=("ocr",)`) alongside `normalize`/`structure` — an
  additional artifact off the same OCR root, auto-chained (`successors("ocr")` includes `"linking"`).
  Figures come straight from Azure's own caption grouping; footnotes are paragraphs with
  `role == "footnote"` — no spatial re-derivation or marker parsing in this slice.
- **Deferred, each a MEASURED (SP-3) candidate, not built here:** cross-reference detection (resolving an
  in-text marker like "see Note 5" to its target section/figure/footnote); footnote-marker →
  referencing-paragraph linking (associating a footnote's number/symbol with the body paragraph(s) that cite
  it); caption → nearest-figure spatial re-association beyond Azure's own `.caption` grouping (only worth
  building once ground truth shows Azure's grouping fails often enough to matter). SP-9's canonical graph
  should treat `linking`'s figures/footnotes as leaf nodes for now — cross-reference EDGES between them and
  body content are a later addition once the resolver above exists.

### What SP-10 shipped that SP-11 must know

- **The semantic artifact** `{schema_version:1, sections:[{heading, type, paragraph_count}]}`
  (`fdi.pipeline.stages.semantic.SemanticStage`) at the `semantic` artifact key — one entry per AST
  section, in the same order, `type` a `fdi.core.semantic.SectionType` value (`BALANCE_SHEET`,
  `INCOME_STATEMENT`, `CASH_FLOW`, `NOTES`, `MDNA`, `AUDITORS_REPORT`, `DIRECTORS_REPORT`, `OTHER`).
  **SP-11 (Ontology Mapping) consumes this** to map line items onto canonical concepts within each
  classified section — SP-11 should read `semantic` alongside `ast` (same `heading` order) rather than
  re-deriving section type itself.
- **`SemanticStage` is a DERIVED stage** (`deps=("ast",)`), auto-scheduled after `ast` by the DAG
  (`"semantic" in successors("ast")`), content-addressed on `derived_input_hash([ast.output_hash])` +
  `SemanticStage.version` — a deliberate exception to root-stage determinism (GPT is non-deterministic;
  "run once, cache" avoids re-paying for a re-classification). **Bump `SemanticStage.version`** when the
  prompt, `FDI_GPT_DEPLOYMENT`, or the label set changes, to force regeneration.
- **The LLM is an injected `SemanticClient` protocol** (`fdi.pipeline.semantic.SemanticClient`,
  `classify(heading, text) -> str`), with `fdi.platform.semantic.AzureOpenAIClient` (real, `openai` SDK's
  `AzureOpenAI` client) and `FakeSemanticClient` (test double) as the two implementations — same seam
  shape as SP-5's `OcrClient`. This means SP-3's harness can later measure prompt variants or a cheap
  non-LLM classifier as alternate `Candidate`s without touching the stage.
- **Config:** `FDI_GPT_ENDPOINT` / `FDI_GPT_KEY` / `FDI_GPT_DEPLOYMENT` / `FDI_GPT_MODEL` /
  `FDI_GPT_API_VERSION` in Settings + `docker/compose.yml` `x-backend-env` + `.env.example`; endpoint/key
  default `None` so the stack boots without Azure OpenAI configured (the worker only wires
  `services["semantic"]` when `AzureOpenAIClient.from_settings(...)` returns non-`None`).
- **Trigger:** `POST /documents/{id}/semantic` (202; 404 for an unknown document), mirroring SP-5's OCR
  trigger; the generic `POST /docs/{doc_id}/reprocess?from=semantic` also works.
- **No live Azure OpenAI call anywhere in the suite** — `FakeSemanticClient` proves the stage end-to-end;
  `AzureOpenAIClient`'s unit test monkeypatches `fdi.platform.semantic.AzureOpenAI`; the real deployment is
  exercised only by the opt-in `tests/azure/test_real_semantic.py` (`@pytest.mark.azure`, skipped unless
  `FDI_GPT_ENDPOINT`/`FDI_GPT_KEY` are set).

### What SP-11 shipped that SP-9 / SP-16 must know

- **The mappings artifact** `{schema_version:1, mappings:[{label, concept}]}` (`fdi.core.ontology.map_document`)
  at the `ontology` artifact key is the canonical-concept contract — `concept` is one of 10
  `fdi.core.ontology.CanonicalConcept` values (`REVENUE`, `COST_OF_GOODS_SOLD`, `GROSS_PROFIT`,
  `OPERATING_EXPENSE`, `NET_PROFIT`, `TOTAL_ASSETS`, `TOTAL_LIABILITIES`, `EQUITY`, `CASH`, `UNKNOWN`).
  `map_label(label) -> CanonicalConcept` is a pure, total, dictionary-based classifier (normalize →
  synonym lookup → `UNKNOWN` if unrecognized); never raises.
- **`OntologyStage` is a DERIVED stage** (`deps=("ast",)`), auto-scheduled after `ast` by the DAG
  (`"ontology" in successors("ast")` alongside `"quality"` and `"semantic"`); content-addressed on
  `derived_input_hash([ast.output_hash])` + `OntologyStage.version` — a real, unconditional determinism
  property (no LLM, no wall-clock, no RNG), unlike SP-10's semantic stage which had to special-case
  non-determinism. The full chain `intake → ocr → normalize → ast → {quality, semantic, ontology}` is
  automatic from a single `run_stage("ocr", ...)`.
- **Dictionary-based, thin slice — no LLM.** This slice maps every section heading AND every paragraph as
  a whole string, keeping non-`UNKNOWN` results; a real OCR line item like `"Revenue  12,345"` won't match
  (the trailing number breaks the exact lookup) — only bare labels map today. **SP-9 (Canonical Graph)**
  and **SP-16 (cross-document layer)** should consume `CanonicalConcept` as the comparison key across
  documents/sections instead of raw label text. Concrete follow-ons for the next iteration: an LLM-based
  mapper is a candidate for SP-3's harness once a label→concept ground-truth dataset exists; scoping
  mappings by `SectionType` (widen `deps` to `("ast", "semantic")`) and numeric-value extraction (feeding
  SP-17's numeric-total quality checks) are the two concrete accuracy/coverage follow-ons.

### What SP-8 shipped that SP-18 / SP-7 must know

- **The AST** `{schema_version:1, title, sections[{heading, paragraphs[]}]}` (`fdi.core.ast.build_ast`) at the `ast` artifact key is the walkable document tree. The full pipeline chain is `intake → ocr → normalize → ast` (each derived stage auto-scheduled by the DAG).
- **SP-18 (thin UI):** show a document's journey — upload (`POST /documents`), trigger OCR (`POST /documents/{id}/ocr`), poll `/jobs/{id}`, and render the `ast`/`normalize`/`ocr` artifacts (fetch via the presigned `artifact_url`). The Next.js shell + CORS from SP-1/SP-2 are the base.
- **SP-7 (structural, Stage 2)** will DEEPEN this: enrich the OCR artifact (words/cells/bboxes — bump `OcrStage.version`), correct reading order, detect (not just trust) header/footer, stitch multi-page tables, and produce a richer AST (nested subsections, page spans, table nodes) under a new AST `schema_version`. It gets a dedicated deep-research pass; candidates measured via SP-3's harness.

### What SP-6 shipped that SP-7 must know

- **The unified document is the vendor-neutral contract:** `{schema_version:1, content, page_count, blocks[{role,text}]}` at the `normalize` artifact key (`fdi.core.unified.normalize_document`). Downstream commits to THIS, never the OCR/Azure shape. Canonical `Role` enum: `title/heading/header/footer/page_number/footnote/body`.
- **`NormalizeStage` is a DERIVED stage** (`deps=("ocr",)`), auto-scheduled after `ocr` by the DAG (`successors("ocr")` = `["normalize"]`), `input_hash` from the OCR `output_hash`.
- **SP-7 needs richer geometry than SP-5's OCR artifact kept** (it kept counts, not words/cells/bboxes). SP-7 (structural: reading-order, header/footer removal, table stitching) must ENRICH the OCR artifact — keep words/lines/cells with polygons — and BUMP `OcrStage.version` (which regenerates OCR + normalize downstream via the version-stamp). SP-7 is the roadmap's highest-value/highest-risk tier and should get its own deep-research pass on reading-order + table-structure algorithms for Indian annual reports, with candidates measured through SP-3's harness.

### What SP-5 shipped that SP-6 must know

- **The OCR artifact is a normalized subset** `{schema_version:1, content, pages[], paragraphs[{role,content}], tables[{row_count,column_count,cell_count}]}` (`fdi.core.ocr_schema.normalize_ocr`), stored at the content-addressed key for stage `ocr` — SP-6 (Normalization) consumes THIS, not Azure's wire format, so an Azure schema change is absorbed in `normalize_ocr` alone.
- **`OcrStage` is a root stage** (`deps=()`, `version=1`) over the intake'd PDF. Its `version` encodes the DI model + api_version — bump it when `FDI_AZURE_DI_MODEL`/`FDI_AZURE_DI_API_VERSION` change (they define the output). Content-addressed on (PDF checksum + version); DI output is non-deterministic so it runs once and caches.
- **The stage framework now has `StageContext.read_source()` (raw PDF) + `ctx.services["..."]`** (injected services bag). SP-6's NormalizeStage is a DERIVED stage: `deps=("ocr",)`, reads the OCR artifact via `ctx.read("ocr")`, needs no external service.
- **OCR runs via `POST /documents/{id}/ocr`** → `run_stage(doc, "ocr")`. The `OcrClient` protocol (fake in tests, `AzureOcrClient` in prod) means SP-3's harness can measure `read` vs `layout` (or non-Azure) OCR candidates against ground truth.
- **Azure config is `FDI_AZURE_DI_*`** (endpoint/key optional → stack boots without Azure; OCR then fails cleanly). Creds copied from the sibling `D:\FinancialDocIntelligence\.env`. Real DI is exercised only by the opt-in `@pytest.mark.azure` smoke test.

### What SP-4 shipped that SP-5 must know

- **A document is content-addressed by sha256** and stored at `documents/{checksum}.pdf` (`ArtifactStore.get_bytes` reads it back); deduped on `document.checksum` (UNIQUE), versioned per `doc_key`.
- **`fdi.pipeline.intake.intake_document(...)` is the front door**; `DocumentRepository` (get / get_by_checksum / next_version / list_by_doc_key) is the accessor. `POST /documents` (multipart), `GET /documents/{id}`, `GET /documents?doc_key=`.
- **SP-5 runs OCR as the first real stage over an intake'd document:** an `OcrStage` (root stage, `deps=()`) whose `run(ctx)` reads `documents/{doc.checksum}.pdf`, calls **Azure Document Intelligence** (`FDI_AZURE_DI_*`, async poll), and emits the OCR artifact. Dispatch via `run_stage(doc_id, "ocr")`. **Content-addressing caveat (roadmap §7):** Azure DI output is not byte-stable, so key the OCR artifact on the PDF checksum + DI model version, NOT the response bytes — the SP-2 stage `input_hash` for a root stage already uses `document.checksum`, which is now populated by intake; fold the DI model version into the stage `version`.

### What SP-3 shipped that every layer must know

The eval harness is how each later layer picks its winning algorithm by measurement, not reputation:

- **A candidate is `predict(given) -> output`** with `name` + `version` (`fdi.pipeline.evaluation.Candidate`),
  self-registered into `CANDIDATES`. A layer's algorithm variants (OCR engines, reading-order strategies,
  table parsers) are wrapped as candidates and measured before one is promoted to a pipeline `Stage`.
- **A metric is pure `score(predicted, expected) -> [0,1]`** in `fdi.core.metrics` (ships `exact_match`;
  each layer ADDS its metric beside it — CER/WER for OCR, cell-F1 for tables, Kendall-τ for reading order).
- **Ground truth is a versioned dataset** — `Dataset(name, version, cases[given, expected])` stored at
  `ground-truth/{name}/v{version}.json` via `ArtifactStore` (`fdi.pipeline.dataset_store`).
- **`run_evaluation(...)` runs a candidate over a dataset, persists an `eval_run` + per-case results, and
  `EvalRepository.leaderboard(dataset, metric)` ranks candidates.** API: `POST /eval`, `GET /datasets/{name}/leaderboard`.
- **Evals are observations, not cached artifacts** — a new run row each time; intentionally NOT deduped.
  Real datasets (annotated Indian annual reports) are authored/uploaded as the layers arrive.

### What SP-2 shipped that SP-3/SP-4 must know

- **A stage is `name` + `version: int` + `deps: tuple[str, ...]` + `async run(ctx) -> body`.** It reads
  inputs via `ctx.read(upstream)` and returns a body; the framework owns hashing, storage, the DB, and
  scheduling. New phases (intake, OCR, normalize, …) are just `Stage` subclasses self-registered into
  `fdi.pipeline.registry.REGISTRY`.
- **Artifacts are content-addressed + version-stamped:** `artifacts/{doc}/{stage}/v{version}/{input_hash}.json`.
  A stage's `input_hash` is the sha256 over its upstream stages' recorded `output_hash`es (root stages hash
  the document checksum). Same input + version → same key → `run_stage` skips recompute. Bump a stage's
  `version` to force its downstream to regenerate.
- **Registry = `document` + `stage_run` (Postgres).** `stage_run` records `input_hash`, `output_hash`,
  `stage_version`, `artifact_key`, `status` (CHECK-constrained). `RegistryRepository` is the accessor;
  `runs_for_document` returns `{stage: RunFacts}` for the pure `DagResolver` staleness math.
- **Execution is one generic `run_stage_task` Celery task per stage**, chaining successors via the resolver.
  `StageDispatcher.dispatch(doc, stage, correlation_id)` publishes one. Determinism / core-purity / the
  one-`asyncio.run`-per-task shape all still hold (`RunFacts` and the NaN validator live in `fdi.core`).
- **Staleness is detect-and-report, not auto-cascade:** `GET /docs/{id}/stale`, `POST /docs/{id}/reprocess?from=`.
- **SP-4 owns `document.checksum`** (intake/dedup); it is nullable until then. **SP-6 owns the rich unified
  normalized-document schema**; SP-2 shipped only the artifact-body validator + envelope.
- **Last updated:** 2026-07-15
- **Purpose:** the single durable entry point for this project. It records the vision, the
  sub-project decomposition, the build order, and the invariants every sub-project must honor.

> **This is a map, not a task list.** Each sub-project gets its own design spec + TDD plan
> (`superpowers:brainstorming` → `superpowers:writing-plans`) at the time it is implemented,
> then executed with `superpowers:subagent-driven-development`. Do not implement directly from
> this doc — use it to scope the next sub-project's own spec.

---

## 1. The Vision

> **Process the document once → create reusable knowledge artifacts → every downstream task
> operates on knowledge instead of PDF.**

Target corpus: **5000+ annual reports, 300–600 pages each.** One report yields tens of MB of OCR
output; the corpus implies roughly **700 GB of artifacts** and ~2.5M pages of OCR.

The pipeline is a linear sequence of stages. Each stage consumes the previous stage's artifact and
emits its own. Artifacts are content-addressed and version-stamped, so **any single stage can be
improved and its downstream regenerated without re-running the stages above it** — most importantly,
without re-running (and re-paying for) OCR.

---

## 2. The 12 Ingestion Phases

```
PDF
 │
 ├─ 1  Document Intake            upload · checksum · dedup · versioning · registry
 ├─ 2  OCR & Layout Extraction    OCR JSON · images · bboxes · polygons · confidence
 ├─ 3  Normalization              vendor output → internal unified schema
 ├─ 4  Structural Intelligence    ◄── where almost every OCR defect is corrected
 ├─ 5  Document AST               hierarchical tree · sections · page spans
 ├─ 6  Canonical Structure Graph  typed edges: hierarchy · reading · spatial · table · xref
 ├─ 7  Semantic Intelligence      section classification · entities · statement detection
 ├─ 8  Ontology Mapping           Revenue│Sales│Turnover → CANONICAL_REVENUE
 ├─ 9  Provenance Graph           Fact → Cell → Table → Page → Polygon → Span → PDF
 ├─ 10 Embeddings                 document · section · paragraph · table summary · entity
 ├─ 11 Retrieval Indexes          metadata · entity · section · clause · table · vector · graph
 └─ 12 Knowledge Store
```

### Phase 4 in detail (the highest-value, highest-risk phase)

Reading-order recovery · header/footer removal · page-number detection · column detection ·
paragraph stitching · section stitching · multi-page table stitching · table header recovery ·
merged-cell recovery · nested-header recovery · caption linking · footnote linking ·
cross-reference detection · table-continuation detection.

---

## 3. Sub-Project Decomposition

Each row is one spec → plan → implement cycle.

| # | Sub-project | Phase | Research | Status |
|---|---|---|---|---|
| **SP-1** | Project setup & walking skeleton | setup | light | ✅ done |
| **SP-2** | The base — unified schema, artifact store, registry, **stage/DAG framework** | base | medium | ✅ done |
| **SP-3** | Eval harness & ground truth | — | medium | ✅ done |
| **SP-4** | Document Intake | 1 | light | ✅ done |
| **SP-5** | OCR & Layout Extraction | 2 | medium | ✅ done |
| **SP-6** | Normalization | 3 | light | ✅ done |
| **SP-7a** | Structural: page layout — reading order, columns, header/footer | 4 | **heavy** | 🔨 OCR enriched |
| **SP-7b** | Structural: cross-page stitching — paragraphs, sections, tables | 4 | **heavy** | 🔨 table stitching + header/footer done |
| **SP-7c** | Structural: table internals — headers, merged/nested cells, grid | 4 | **heavy** | ✅ done |
| **SP-7d** | Structural: linking — captions, footnotes, cross-references | 4 | **heavy** | ✅ done (captions + footnotes; cross-refs deferred) |
| **SP-8** | Document AST | 5 | medium | ✅ done (naive) |
| **SP-9** | Canonical Graph → Neo4j | 6 | medium | ✅ done (JSON graph; Neo4j loading deferred) |
| **SP-10** | Semantic Intelligence | 7 | **heavy** | ✅ done |
| **SP-11** | Ontology Mapping | 8 | **heavy** | ✅ done (thin) |
| **SP-12** | Provenance Graph | 9 | medium | ⬜ |
| **SP-13** | Embeddings → Milvus | 10 | **heavy** | ⬜ |
| **SP-14** | Retrieval Indexes | 11 | **heavy** | ⬜ |
| **SP-15** | Knowledge Store consolidation | 12 | light | ⬜ |
| **SP-16** | Cross-document layer — entity resolution, version linking, cross-doc refs | enterprise | **heavy** | ⬜ |
| **SP-17** | Quality validation framework | enterprise | medium | ✅ done (thin) |
| **SP-17b** | Structure-aware quality (SP-17 follow-on) | enterprise | light | ✅ done |
| **SP-18** | Next.js frontend | — | light | ✅ done (thin) |

### What SP-1 shipped that SP-2 must know

The walking skeleton left load-bearing seams and rules SP-2 builds on, not around:

- **The `core` / `platform` boundary is CI-enforced and `exhaustive`.** `.importlinter`
  declares the layers `api | workers → pipeline → platform → core` with
  `exhaustive = true`, so every module must belong to a named layer or the contract
  breaks. `fdi.core` is pure (no I/O, config, clock, RNG). Adding a subsystem in SP-2
  means placing it in a layer, not appending to a list.
- **The canonical task shape.** One `asyncio.run()` per Celery task, engine built
  *inside* the coroutine and disposed in `finally`; never `async def` under `@app.task`;
  never a loop/engine/pool at module import (fork copies it without its thread). An AST
  test and a fork-guard enforce these — SP-2's stages inherit the shape.
- **`canonical_json` and determinism (Global Constraint 6).** Artifact bodies are a
  byte-identical function of input; ids and timestamps live in Postgres rows, never in
  the artifact. SP-2's content-addressing depends on this already holding.
- **The `JobDispatcher` seam.** `api` and `workers` are independent siblings that may
  not import each other; the composition root (`api/app.py` lifespan) is the ONE place
  that wires a concrete task into an injected dispatcher. The single ignore-list entry
  `fdi.api.app -> fdi.workers.tasks` exempts exactly that line and nothing else —
  putting the import back in `routes.py` breaks the contract.
- **CORS is a dev-scoped hardcoded allowlist**, `.env` must be passed with `--env-file`,
  and the `vector` profile (Milvus) is the OOM risk on the ~9.6 GB Docker VM — see the
  README caveats before wiring new services.

### Why SP-2 is the highest-leverage sub-project

*Incremental reprocessing* and *artifact versioning* are **properties of the stage framework**, not
features that can be bolted on. If SP-2 does not deliver content-addressed, version-stamped
artifacts with a real dependency DAG, no later sub-project can retrofit them. Everything plugs into
whatever SP-2 decides.

**Carried into SP-2 from SP-1 (found while implementing `canonical_json`):** Python's `json.dumps`
defaults to `allow_nan=True`, so `float("nan")` serializes to a bare `NaN` token. That is
*deterministic* — it satisfies SP-1's byte-identical invariant — but it is **not valid JSON**, and
every strict parser downstream will reject it. For a pipeline whose whole point is extracting
financial ratios (where `0/0` is a live possibility), this is a real interop hazard. It belongs in
SP-2's **schema/validation** layer, before serialization — not in the serialization wrapper, which
correctly stayed a thin, branch-free pass-through. Decide it deliberately in SP-2's spec.

### Why SP-3 exists

The goal is *"the best algorithm at each step for most accurate functioning."* Research produces
**candidates** (XY-cut vs. LayoutReader vs. topological sort for reading order). It cannot say which
wins **on Indian annual reports** — only measurement does. Without an eval harness built *before*
the heavy layers, "best algorithm" is unfalsifiable and selection degenerates to reputation.
One annotated document with ground truth outweighs a week of literature review.

Research is therefore **per-layer and continuous**, feeding candidates into SP-3's harness, which
picks the winner.

---

## 4. Build Order — Hybrid (spine first, then deepen)

**Stage 1 — get a real document through a real stack.**

```
SP-1 setup → SP-2 base → SP-3 eval harness
  → SP-4 intake → SP-5 OCR → SP-6 normalize
  → SP-8 AST (naive) → SP-18 UI (thin)
```

Outcome: real OCR output and real measurements to design against, plus a demoable system.

**Stage 2 — deepen, in dependency order, measured by SP-3.**

```
SP-7a/b/c/d structural intelligence   ◄── the hard part, now with a feedback loop
SP-9 graph → SP-12 provenance
SP-10 semantic → SP-11 ontology
SP-13 embeddings → SP-14 indexes
SP-15 store · SP-16 cross-doc · SP-17 quality
```

**Why this is safe rather than sloppy:** naive-then-deepen is precisely what artifact versioning and
incremental reprocessing exist to enable. Phase 4 gets rebuilt later *without re-running OCR*. Strict
layer-by-layer was rejected because it defers the first end-to-end run to ~SP-14, lands all
Neo4j/Milvus integration risk last, and forces Phase 4's algorithms to be designed with no
downstream signal.

---

## 5. Hard Invariants

Every sub-project must honor these. They are cheap now and expensive to retrofit.

1. **`core` is pure.** No I/O, no config singleton, no wall-clock, no RNG. Enforced by an
   `import-linter` contract in CI: `api, workers → pipeline → platform → core`.
2. **Determinism.** Re-running a stage on the same input yields a byte-identical artifact. No
   wall-clock, no RNG, stable sorts, pinned float formatting. Assert it in an idempotency test.
3. **Provenance survives every transform.** Every derived object traces to page, polygon and source
   span. A node that moves or merges keeps its source reference.
4. **Prefork only.** Stages are CPU-bound; gevent/eventlet cannot parallelize them, starve the
   broker heartbeat, and cannot enforce time limits.
5. **`asyncio.run()` once per task, never at module import.** A loop created before fork is copied
   without its thread and hangs silently.
6. **Artifacts are content-addressed and version-stamped** with the code version that produced them.
7. **12-factor.** Config from env only; no host paths in code. This is what lets the same images run
   in cloud unchanged.

---

## 6. Deployment Posture

**Now:** local Docker Compose on a 19.8 GB / 4-core laptop, using Compose profiles so only the
services a sub-project needs are running (`core` ≈ 5 GB; `graph` (Neo4j) deferred past SP-9 — the
JSON graph artifact ships now, the Compose profile + loader land with the follow-on Neo4j
sub-project; `vector` at SP-13). Volumes live on **F:** (202 GB free), never D: (29 GB free).
Corpus: ~5–20 real reports.

**Later:** the same images lift to cloud — managed Postgres, S3/Blob, hosted Neo4j, managed vector
store, autoscaled workers. Expected around SP-13/SP-14, when the 5000-document run becomes real.
The 12-factor discipline above is what makes this a config change rather than a redesign.

## 7. External services (decided 2026-07-17)

- **OCR / layout: Azure Document Intelligence** (SP-5). Settles the "which OCR engine" research question.
- **LLM: Azure OpenAI (GPT)** — SP-7 semantic classification, SP-10 ontology mapping, and wherever an LLM is needed.
- **Credentials** live at `D:\FinancialDocIntelligence\.env` (a sibling dir OUTSIDE this repo — real secrets, never committed). Vars: `AZURE_DI_ENDPOINT`/`AZURE_DI_KEY`/`AZURE_DI_POLLING_TIMEOUT_S`; `GPT_ENDPOINT`/`GPT_DEPLOYMENT`/`GPT_MODEL_NAME`/`GPT_SUBSCRIPTION_KEY`/`GPT_API_VERSION`.
- **Wiring (at SP-5):** add `FDI_AZURE_DI_*` / `FDI_GPT_*` fields to `Settings` + the `x-backend-env` allowlist + `.env.example` (placeholders only); the operator copies real values into the repo's gitignored `.env`.
- **Determinism caveat:** Azure DI output is not byte-stable, so SP-5's OCR-artifact content-addressing must key on the input PDF + DI model version, not the response bytes — decide explicitly in the SP-5 spec.

---

## 7. Source-of-Truth Documents

- **This roadmap:** `docs/architecture/ingestion-roadmap.md`
- **SP-1 spec:** `docs/superpowers/specs/2026-07-15-project-setup-design.md`
- Sub-project specs: `docs/superpowers/specs/<date>-<slug>-design.md`
- Sub-project plans: `docs/superpowers/plans/<date>-<slug>.md`
