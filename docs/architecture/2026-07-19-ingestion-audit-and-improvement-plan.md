# Ingestion Pipeline — Deep Audit Findings & Improvement Plan

- **Date:** 2026-07-19. **Status:** Findings final; plan agreed. **Progress: Phase 0 ✅, Phase 1 ✅,
  Phase 2 ✅, Phase 3a ✅ (all merged to `feature/sp-1-project-setup`).** Remaining: Phase 3b (graph
  table/figure nodes), Phase 4 (values + reconciliation), Phase 5 (robustness).
  - **Phase 0** (`fd11ee3`): OCR schema 4→5 — retains `cells[].elements`/`bounding_regions`, table
    caption/footnotes, page spans/marks, low-confidence words.
  - **Phase 1** (`942256f`): table-cell paragraphs filtered by `cells[].elements` + dropped-titles
    fix. Measured on STTL: AST bare-numeric-paragraph share **42.8% → 1.2%**; sections 538→607;
    Cash Flow heading recovered; blocks still 1:1 (0 data loss).
  - **Phase 2** (`dedaed0`): deterministic provenance via `source_index` (O(1) page lookup, text-
    matching deleted) + per-cell geometry + full labels. Measured: 0 wrong-page collapses across 354
    ambiguous groups; "Opinion" → `[83,90,141]` not all-83.
  - **Phase 3a** (`a67e538`): table-derived row chunks (`t{i}r{j}`) + whole-table chunks + dedup +
    embedding-reads-chunks. Measured: 1,985 row chunks; every headline GT number now in a structured,
    page-correct row chunk (BS→p91, P&L→p92), searchable via hybrid_search.
  - Integrated suite green: **632 passed / 12 skipped**, all four gates.
- **Method:** Six independent layer-auditors run in parallel over the **real Silver Touch Technologies
  Ltd FY2023-24 annual report** (185 pages, 261 tables, 11,428 paragraphs, 538 AST sections), each
  materializing its artifact from the real Azure DI OCR JSON and measuring against the source + the
  ground-truth financials. Plus a direct inspection of the Azure DI `AnalyzeResult` structure. Every
  number below is measured, not estimated.
- **Companion artifacts:** the extraction audit (`backend/scripts/audit_extraction.py`) and the six
  per-layer reports (`audit_ocr/ast/structure/graph/chunks/meaning.md`) are the regression harness for
  this plan.

## 1. Verdict

The pipeline is **structurally faithful** — OCR text is byte-exact, all 261 tables are captured, grids
are 79–96% accurate to ground truth. The accuracy gaps are **not corruption**; they are four things:

1. **Table content never reaches the *retrieval* corpus in structured form** (tables = 25.1% of the
   document and 100% of the numbers, but are absent from AST, chunks, and the graph).
2. **A real provenance page-citation bug** (global first-match on ambiguous labels → wrong pages).
3. **No numeric value or reconciliation anywhere in the meaning layer.**
4. **A few concrete bugs** (dropped title blocks, unreachable stitching threshold, paragraph pollution).

**The reframing that drives the plan:** most of these are fixable **not by new heuristics, but by
consuming Azure DI structure the pipeline currently discards** — Azure already emits a fully nested,
page-anchored, cross-referenced document model, and the pipeline re-derives (and mis-derives) it from
flat text/heading roles.

The earlier "row-shift" extraction misses were **cleared of the pipeline**: the grids are 100% correct
(MSME dues are genuinely NIL in the filing; `trade_payables_total` isn't even in the source) — those
were downstream extraction / ground-truth artifacts, not structural corruption.

## 2. Per-layer audit findings (measured on STTL 2023-24)

### OCR (`fdi.core.ocr_schema.normalize_ocr`)
- Text/words/paragraphs kept **exactly**: content 500,180 chars in = out; 74,395 words, 15,637 lines,
  11,428 paragraphs verified. No correctness bugs — only omissions.
- **Dropped:** per-word `confidence` (all 74,395; 658 words <0.9), `selectionMarks` (1,402; 61
  selected), table `caption`/`footnotes` (1 caption, 23 footnotes), per-cell `boundingRegions`/
  `elements`/`spans`, `styles`, page `angle`, `apiVersion`/`modelId`/`contentFormat`.

### Normalize + AST (`fdi.core.unified.normalize_document`, `fdi.core.ast.build_ast`)
- Text survival: 93.7% of paragraph chars into AST paragraphs, 98.2% retained somewhere; 1.09%
  deliberately dropped (header/footer/page_number, correct).
- **Bug:** only the first of 70 `title`-role blocks becomes `ast["title"]`; the other **69 (3,496 chars)
  are silently discarded** — including the Cash Flow Statement heading (p.93) and "Notes forming part
  of the standalone financial statements" (p.131).
- **Tables 100% absent from the AST tree** (25.1% of document content).
- **Pollution:** Azure double-emits every table cell as a loose `paragraphs[]` entry; nothing filters
  them → **8,816/11,428 (77%) of raw paragraphs are ex-table-cell fragments**, and **42.8% of AST
  paragraphs are bare numeric tokens** (e.g. `'2126.88'`), 65.3% are ≤15 chars.
- Section splitting: 538 sections, mean 19.4 para/section; 60 empty; 9 headings >150 chars (body
  mis-tagged as heading; one heading is literally `'4'`).

### Structure / tables (`stages/structure`, `core.structure`, `core.table_grid`)
- **Table capture 261/261 (100%)**, zero dropped/duplicated cells across 11,148 cells.
- **Cross-page stitching 0/261 (0%)** — `_can_merge` requires `gap<=2` chars but real page furniture is
  ≈28 chars → threshold unreachable on any real doc. ~83% of sampled candidate pairs are genuine
  continuations emitted as split tables.
- Naive threshold-loosening would cause a **wrong merge** (Note 2 Fixed Assets current/prior-year
  schedules) because Azure mis-tags `"PREVIOUS YEAR DETAILS"` as `pageHeader` furniture.
- Grid accuracy: **P&L 23/24 (96%)** (1 genuine upstream OCR glyph drop — missing negative paren on
  OCI), **Balance Sheet 19/24 (79%)**; multi-row headers + span expansion correct; 55% of tables have
  spanning cells.
- **Row-shift verdict: NOT the grid** — canonical grid is correct; the miss is downstream blank-cell
  skip-ahead + ground-truth field-name/value offset.
- No per-cell provenance retained past canonicalization; no structural self-checks.

### Graph + provenance (`core.graph`, `core.provenance`)
- Graph: 10,989 nodes (1 doc / 538 section / 10,450 paragraph), 21,497 edges; **only 3 node types, 2
  edge types** — no table/figure/footnote nodes, no cross-reference edges.
- **Provenance page bug:** coverage looks ~100% (10,988/10,989), but resolution is a **global
  first-match** on label text and **75.5% (7,888/10,450) of paragraph labels are ambiguous
  document-wide** → wrong pages. Every `"Particulars"` node → page 2; "Opinion" (real pages 83/90/141)
  → all page 83; 108/538 (20%) headings duplicated this way.
- 80-char paragraph-label truncation (10.9% lose avg 72% of text).

### Chunks + embedding (`core.retrieval.build_chunks`, `stages/embedding`)
- Tables absent from chunks (chunks = AST section text only; no table ever gets a vector). But **100%
  of the 32 headline GT values still leak into chunk text** via the duplicated table-cell paragraphs —
  structure-free and ambiguous (`inventories=66.88` literally matches 47 chunks).
- **18.4% (99/538) of chunks are exact duplicates**; 11.2% (60/538) near-empty; 4.6% huge (≥3,000
  chars, dilution); page resolution 537/538.
- Whole-section granularity, no overlap, no row-level chunking; the real structured channel
  (`get_tables`) is a separate tool the `search` path never sees.

### Meaning + validation (semantic, ontology, knowledge, quality, linking)
- **Ontology:** 32 mappings, 7/9 concepts (COGS, GROSS_PROFIT never matched); **labels only, no
  numbers** — vs 147 numeric GT leaf fields.
- **Quality:** 4 validators (sections/content/title/rectangular-tables); ok=True, 0 errors on the real
  doc; **no numeric reconciliation** (BS balances 16,427.37=16,427.37 but nothing checks it).
- **Linking:** 34 figures (all `bounding_regions` discarded; all captions empty), 0 footnotes (Azure
  tagged 0/11,428 as footnote; no fallback); no cross-reference resolution.
- **Knowledge:** counts + concept set + quality echo; no numeric roll-ups (ontology feeds it no values).
- **Semantic:** 8 coarse `SectionType`s; **up to 538 sequential Azure OpenAI calls per document.**

## 3. What Azure DI already carries (and we discard)

Direct inspection of the `AnalyzeResult` — every item confirmed present in the STTL JSON:

| # | Azure structure | Currently | Enables |
|---|---|---|---|
| **1** | **`sections[].elements` hierarchy** — 609 sections; refs resolve to 608 section-nestings, **261 tables, 33 figures, 2,195 paragraphs** | **ignored** | true nested AST with table/figure **membership**; table/figure graph nodes; table-in-section chunks |
| **2** | **`boundingRegions[].pageNumber`** on every paragraph (11,428/11,428), section, and **cell (11,148/11,148)** | mostly **dropped** | deterministic exact page for every node/cell |
| **3** | **`cells[].elements`** (cell→paragraph refs; 8,816) | **unused** | exact identification + removal of the duplicated table-cell paragraphs |
| **4** | **`cells[].boundingRegions` + polygon** | **dropped** | per-cell provenance / click-to-highlight citations |
| **5** | **`spans` (offset/length) everywhere** + `pages[].spans` | partial | deterministic reading order + element→page mapping |
| **6** | **per-word `confidence`** | **dropped** | flag low-confidence numeric cells |
| **7** | **table `caption`/`footnotes`** | **dropped** | statement identification + restatement caveats |
| **8** | **`figures[].boundingRegions`/`elements`** | geometry **dropped** | figure nodes with geometry + membership |
| **9** | **`selectionMarks`** (1,402) | **dropped** | checkbox/compliance (CARO-style) data |

## 4. Combined mapping — gap × Azure fix × phase

| Audit gap | Azure fix | Phase |
|---|---|---|
| P1-1 tables absent from AST/chunks/graph | #1 hierarchy + #3 cell→para | 1, 3 |
| P1-2 provenance first-match → wrong pages | #2 pageNumber + #5 spans | 2 |
| P1-3 blank-cell skip-ahead extraction | #4 cell bboxes + blank signal | 5 |
| P2-4 cross-page stitching never fires | heuristic fix (#5 assist) | 5 |
| P2-5 cell-paragraph pollution | #3 cell→para (exact filter) | 1, 3 |
| P2-6 no concept→value linking | grids + #7 captions | 4 |
| P2-7 69 titles dropped | #1 hierarchy | 1 |
| P3-8 no reconciliation | (needs P2-6 values) | 4 |
| P3-9 OCR drops confidence/marks/captions | #6, #7, #9 | 0 |
| P3-10 reinvents flat splitting | #1 hierarchy | 1 |
| P3-11 no table/figure nodes, no cross-refs | #1, #8 | 3 |
| P3-12 semantic 538 calls, coarse types | batch + hierarchy | 5 |
| P3-13 truncation / figure geom / cell provenance | #4, #8 | 2 |

## 5. The plan — 6 phases

Each phase is one sub-project (spec → plan → TDD execute → gated merge), with the audits re-run
before/after as the quantified proof of lift.

### Phase 0 — Enrich the OCR artifact (foundation)
`normalize_ocr` / `OcrStage` (v4→v5) stops discarding structure: retain `sections[].elements`,
per-element `boundingRegions[].pageNumber`, `spans`, `cells[].elements`, `cells[].boundingRegions`,
per-word `confidence`, table `caption`/`footnotes`, figure `boundingRegions`, `selectionMarks`.
**Closes P3-9; enables #1–#9.** Everything else depends on this.

### Phase 1 — Rebuild the document model from Azure's hierarchy
New AST builder consumes `sections[].elements` → a true nested tree with paragraphs + **table/figure
references as section members**; fixes the 69-dropped-titles bug; uses `cells[].elements` to **exactly
exclude the 8,816 table-cell paragraphs**. Kept **additive** where possible (extend the `sections`
shape rather than break it) to bound blast radius. **Closes P1-1 (partial), P2-5, P2-7, P3-10.**

### Phase 2 — Deterministic provenance + per-cell citations
Rewrite `build_provenance` to read `boundingRegions[].pageNumber` directly (no global text match);
thread `cells[].boundingRegions` through `canonicalize_table`; fix 80-char truncation; retain figure
geometry. **Closes P1-2, P3-13.**

### Phase 3 — Tables into the retrieval corpus
Emit **table-derived, row-level chunks** (row-label + column-header + value + page) into
chunk→embedding→FTS; **dedup** chunks; add **table & figure graph nodes** with `CONTAINS` edges from
the hierarchy. **Closes P1-1 (full), P2-5 (dedup), P3-11.**

### Phase 4 — Financial semantics: values + reconciliation (parallel)
Extend `ontology` to parse label+adjacent-number rows (reuse `tables.find_concept_rows`) →
concept→value mappings; add `quality` reconciliation validators (BS balances, P&L foots, CF ties);
use table `caption`/`footnotes` for statement identification. **Closes P2-6, P3-8.**

### Phase 5 — Structural robustness + cost (parallel)
Fix cross-page stitching (raise threshold **and** require matching `column_headers`); blank-vs-populated
cell signal + extractor prompt guardrail; batch `SemanticStage` calls + expand `SectionType`;
low-confidence numeric flagging. **Closes P1-3, P2-4, P3-12.**

## 6. Sequencing, validation, risk

```
Phase 0 (enrich OCR)
   └─► Phase 1 (hierarchy AST + filter pollution)
          ├─► Phase 2 (provenance + cell geometry)
          └─► Phase 3 (table chunks + graph nodes + dedup)
   Phase 4 (values + reconciliation) ── parallel
   Phase 5 (stitching, extractor, semantic, confidence) ── parallel
```

- **Regression harness:** after each phase re-run (a) the 6-layer artifact audit and (b) the extraction
  accuracy audit. Target signals: page-match recall 60.9%→~95% (Phase 2); Notes-field answer-in-context
  recall up (Phase 3); numeric accuracy up (Phases 3+5).
- **Risk:** Phase 0/1 change the core document model every downstream stage reads. Mitigate with
  per-stage version bumps (content-addressed → safe re-runs), additive artifact changes where possible,
  and gating each merge on the full test suite **and** the audit regression. Phases 4/5 are low-risk,
  independently mergeable early wins.
- **Effort:** treat as a program ("ingestion accuracy") of 6 sub-projects; Phase 0→1 solo (foundation),
  then 2/3 and 4/5 as parallel worktree waves.
