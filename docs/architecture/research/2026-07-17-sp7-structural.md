# SP-7 Structural Intelligence — research findings (2026-07-17)

Source: deep-research pass (102 agents, 25 claims adversarially verified 3-0 unless noted), run
`wf_f8341e39-7cb`. Full report: `.../tasks/wgulqg95h.output`. This doc is the durable, citable capture
that the SP-7 spec/plan build on. **Scope: POST-Azure structural processing** — the OCR engine (Azure DI
`prebuilt-layout`, api-version 2024-11-30) is settled (SP-5).

## The load-bearing findings

1. **Azure reading order is WITHIN-page only.** It does not order content across page boundaries, and within a
   page it falls back to left-to-right / top-to-bottom when ambiguous — **the classic failure mode on dense
   multi-column financial pages** (balance sheets, notes). Cross-page ordering + section/paragraph stitching
   is entirely the consumer's job. [MS Learn analyze-document-response, doc-intel-4.0.0]

2. **Multi-page tables come back as separate per-page table objects.** One table object CAN carry
   multi-page `boundingRegions`, but Azure does not semantically merge continuations; the official layout doc
   even suggests splitting the PDF per page and post-processing into one table yourself. Cross-page table
   stitching is an industry-wide required post-OCR step (AWS Textract is identical: per-page TABLE blocks +
   a separate `trp` merge parser). [MS Learn layout + analyze-response; AWS Textract multipage-tables blog]

3. **Microsoft ships an official cross-page table-merge heuristic** (Azure-Samples RAG notebook
   `sample_identify_and_merge_cross_page_tables.ipynb`). Concrete rules to reuse:
   - **VERTICAL (continuation) merge:** pages are adjacent AND only `pageHeader`/`pageFooter`/`pageNumber`
     paragraphs lie between the two tables AND `column_count` is equal AND the span-offset gap ≤ 2 chars.
   - **HORIZONTAL (left/right split) merge:** `row_count` equal AND first table's right edge > 99% of page
     width AND second table's left edge < 1% of page width.
   - Handles 3+ page runs, repeating headers, continuation cells. **Caveat:** engineering defaults from a
     sample, not benchmarked on Indian schedules — repeated-header detection + column-width-signature matching
     are the robustness levers to add. **Disambiguation gap:** when multiple distinct tables span the same
     boundary, both the Azure sample and AWS `trp` struggle (Textract issue #46) — no validated algorithm.

4. **Nested / multi-row headers** (e.g. "Year ended 31 March | 2023 | 2022") are the hardest case. Azure's
   cell model DOES express them (a row/column may carry multiple header cells; cell `kind` ∈
   general/rowHeader/columnHeader/stubHead/description; row/column spans), but the docs themselves warn
   extracting structured data "generally requires significant post processing" and header→field
   normalization. TSR literature agrees: **table structure is NOT uniquely determined — canonicalize to ONE
   convention before use** (aligning annotation conventions raised TATR exact-match on FinTabNet 42%→65%).
   Note: "projected row headers" are in-body group rows, NOT the mechanism for nested COLUMN headers (that is
   the column-header region + spanning cells) — a common misattribution. [MS Learn; TATR/PubTables-1M
   arXiv 2303.00716; TableFormer arXiv 2203.01017]

5. **Reading-order REPAIR is a candidate, not a given.** LayoutReader (learned text+layout seq2seq) and
   XY-Cut++ (geometric, with a pre-mask step) both beat naive LTR/TTB on their benchmarks (LayoutReader
   0.98 vs 0.70 BLEU on ReadingBank; XY-Cut++ 98.8 BLEU on DocBench-100). BUT those benchmarks are Word-
   derived single-column / the authors' own 100-page set — **NEITHER validates dense multi-column Indian
   financial statements**, XY-Cut++ is an unreviewed preprint, and the "near-perfect reading order" framing
   was REFUTED (1-2). Treat learned/geometric reordering as **a candidate mitigation to MEASURE via SP-3's
   harness**, not to adopt blindly. [LayoutReader EMNLP 2021 arXiv 2108.11591; XY-Cut++ arXiv 2504.10258]

6. **Realistic accuracy expectations:** even SOTA TSR gets ~1-in-6 tables not perfectly reconstructed
   cell-for-cell (TATR PubTables-1M: GriTS ~0.98 but whole-table exact-match ~0.82); a Nov-2025 VLLM
   benchmark found humans beat the best model on nested-header understanding by 15 points. **Build
   review/validation logic; do not blindly trust table structure.** [arXiv 2511.08298]

7. **Representation: a normalized structural Document AST, fed by (not equal to) Azure's raw output.**
   Azure's `sections` is a tree of RFC-6901 JSON pointers (`/paragraphs/0`, `/sections/1`) — usable but
   vendor-specific, needs dereferencing AND cross-page stitching before it's a clean global hierarchy.
   Prefer a normalized model (Docling's `DoclingDocument` is the reference exemplar — one Pydantic type
   unifying text, tables, pictures, hierarchy, layout/bbox, provenance) with **reading-order correction and
   figure-caption matching as an explicit post-processing STAGE**. [MS Learn layout; Docling arXiv 2501.17887]

## The open questions — and why SP-3 answers them

The research could NOT find domain-specific accuracy for Indian annual reports; those are internal-benchmark
questions, which is exactly what **SP-3's eval harness + ground truth** exists for:
- Measured accuracy of Azure reading order / sections / roles / table structure on our corpus.
- Precision/recall of Azure's `pageHeader`/`pageFooter`/`pageNumber` roles → trust them, or add
  repeated-text-across-pages detection (same text at same y over many pages)?
- Whether a learned/geometric reorder actually improves order on real Azure-OCR'd financial pages, at what
  cost/latency.
- Multi-table-same-boundary stitching disambiguation.

**These become eval datasets + candidates**, not guesses. Structural choices are measured, not asserted.

## SP-7 design direction (for the spec)

- **First, enrich the OCR artifact.** SP-5's `normalize_ocr` kept only counts. SP-7 must keep Azure's
  structural payload — words/lines with polygons, table cells with content + `kind` + row/column spans +
  boundingRegions, paragraph roles + boundingRegions, the `sections` pointer tree — and **bump
  `OcrStage.version`** (which regenerates OCR + normalize + ast downstream via the version stamp). Guard the
  enriched OCR artifact behind a `schema_version` bump.
- **Then a structural stage** (`deps=("ocr",)` or a new `structure` stage feeding a richer AST) that:
  removes header/footer/page-number (trust Azure roles v1; measure), stitches cross-page reading order +
  sections + tables (MS heuristic), and canonicalizes table grids + nested headers to ONE convention.
- **Candidates measured via SP-3:** reading-order strategy (Azure-native stitched vs XY-Cut++ vs LayoutReader),
  table-stitch thresholds, header/footer strategy — each a `Candidate` with a metric (e.g. reading-order
  Kendall-τ / BLEU, table cell-F1 / GriTS) over an annotated ground-truth dataset.
- **Decompose per roadmap:** 7a page layout (enrich + reading order + header/footer) · 7b cross-page
  stitching (reading order + sections + tables) · 7c table internals (grid + nested headers) · 7d linking
  (captions + footnotes + cross-refs). Naive-then-deepen: start with Azure-native + the MS heuristic; add
  candidates and measure.

## Time-sensitivity

Microsoft Q&A indicates native cross-page table support is a PLANNED future update — re-check the layout
release notes before building the stitcher; a native capability could obsolete the sample heuristics.
