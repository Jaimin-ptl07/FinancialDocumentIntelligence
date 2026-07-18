# Phase 0 — OCR artifact enrichment (consume Azure structure we discard)

- **Status:** Design — 2026-07-19. Phase 0 of the ingestion improvement plan
  (`docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md`). The foundation: stop
  discarding Azure DI structure the downstream phases need.
- **Depends on:** SP-5/SP-7 (`fdi.core.ocr_schema.normalize_ocr`, `OcrStage` v4, schema_version 4 —
  the current OCR artifact). No other stage changes in this phase.
- **Enables:** Phase 1 (hierarchy AST + pollution filter — needs `cells[].elements`), Phase 2
  (deterministic provenance + per-cell citations — needs `cells[].bounding_regions`), Phase 4
  (statement identification — needs table `caption`/`footnotes`), Phase 5 (low-confidence flagging —
  needs per-word `confidence`).

## 1. Purpose

The audit found the pipeline **re-derives the document model with heuristics while discarding the
structured model Azure DI already emits**. `normalize_ocr` (schema 4) keeps content/pages/paragraphs/
tables/sections/figures, and already retains `sections[].elements` (the hierarchy) and
`figures[].bounding_regions` — but it **drops the fields Phases 1–5 need**:

- **`cells[].elements`** — each table cell's JSON-pointer refs to the paragraph(s) it came from. 8,816
  of 11,148 cells have these on the real doc. This is the exact set that lets Phase 1 remove the
  duplicated table-cell paragraphs (42.8% of AST paragraphs are bare numbers) **by reference, not
  guesswork**.
- **`cells[].bounding_regions`** — each cell's page + polygon (11,148/11,148 present). Phase 2's
  per-cell provenance / click-to-highlight citations.
- **table `caption` + `footnotes`** — 1 caption + 23 footnote sets; statement identification +
  restatement caveats (Phase 4).
- **per-word `confidence`** — every word carries it (658 words <0.9); the only signal for flagging
  low-confidence numeric cells (Phase 5).
- **`selectionMarks`** — 1,402 per-page checkbox marks (CARO-style compliance signals).
- **page `spans`** — each page's `content` offset range (deterministic element→page fallback).

Phase 0 adds exactly these to the OCR artifact, **additively** (every existing key unchanged), bumping
`schema_version 4 → 5` and `OcrStage.version 4 → 5`.

## 2. Scope decisions (autonomous; documented)

1. **Purely additive.** No existing field of the OCR artifact changes shape or is removed. Downstream
   stages (`normalize`, `structure`, `linking`, everything that reads `ocr`) keep working unchanged;
   the new fields are ignored by anyone who doesn't read them. This bounds blast radius: Phase 0 alone
   changes no downstream output except by triggering a re-run (below).
2. **`OcrStage.version 4 → 5`** (and artifact `schema_version 4 → 5`). Per the DAG's content-addressing,
   bumping the version changes every document's `ocr` `input_hash`, so `ocr` and its whole downstream
   graph re-run on next `run_stage`. That is the intended, safe propagation mechanism (SP-2). No stage
   downstream needs code changes in Phase 0 — they recompute from the same inputs and produce identical
   outputs (the additive OCR fields don't affect them yet).
3. **`cells[].elements` retained verbatim** as the list of Azure JSON-pointer strings (e.g.
   `"/paragraphs/2371"`). `normalize_ocr` preserves paragraph/table/figure ORDER, so pointer index N
   still addresses `ocr["paragraphs"][N]` — the refs stay valid against the normalized artifact. No
   resolution/dereferencing here (that is Phase 1's job); just carry the strings.
4. **`cells[].bounding_regions`** normalized the same way as paragraph/table regions (the existing
   `_normalize_bounding_regions` → `[{page_number, polygon}]`).
5. **Table `caption`/`footnotes`** — Azure's `table.caption` is `{content, boundingRegions, spans}` and
   `table.footnotes` is a list of the same. Retain `caption` as `{content, bounding_regions}` (content
   string + geometry, mirroring the existing figure-caption handling) and `footnotes` as a list of
   `{content, bounding_regions}`. Missing → `caption: None`, `footnotes: []`.
6. **Per-word confidence: a compact low-confidence list, not all 74k words.** Storing every word's
   confidence would bloat the artifact ~5×. Instead add a top-level `low_confidence_words:
   [{page_number, content, confidence, polygon}]` for words with `confidence < 0.7` (the documented
   threshold constant) — ~hundreds of entries, directly usable by Phase 5 to flag numeric cells whose
   region overlaps a low-confidence word. Keep the existing per-page `word_count`. (Rationale: the
   downstream need is "which numbers are shaky", not "every word's score".)
7. **`selectionMarks`** retained per page as `selection_marks: [{state, confidence, polygon}]` (Azure
   `state` ∈ selected/unselected). Missing → `[]`.
8. **Page `spans`** retained per page via the existing `_normalize_spans` → `[{offset, length}]`.
9. **No new config, no new external call.** `normalize_ocr` is pure; this is a pure-function change +
   a stage version bump. The `AzureOcrClient` already returns the full `AnalyzeResult` dict
   (`result.as_dict()`), so all these fields are already in `raw` — Phase 0 just stops dropping them.

## 3. Architecture

### 3.1 `fdi.core.ocr_schema.normalize_ocr` (pure) — the only code change

Additions (every existing key stays; new keys are added):

```
# per CELL (inside each table's cells[]):
+  "elements": <list[str]  # Azure cell.elements JSON pointers, verbatim; [] if absent>
+  "bounding_regions": _normalize_bounding_regions(cell.get("boundingRegions"))

# per TABLE:
+  "caption": {"content": str, "bounding_regions": [...]} | None   # from table.caption
+  "footnotes": [{"content": str, "bounding_regions": [...]}, ...] # from table.footnotes

# per PAGE:
+  "spans": _normalize_spans(page.get("spans"))
+  "selection_marks": [{"state": str|None, "confidence": float|None, "polygon": [...]}, ...]

# top-level:
+  "low_confidence_words": [{"page_number": int, "content": str, "confidence": float,
                             "polygon": [...]}, ...]   # words with confidence < 0.7

  "schema_version": 5   # was 4
```

`low_confidence_words` is built by iterating `pages[].words[]` (which `normalize_ocr` currently reads
only to count) and keeping those with a numeric `confidence < _LOW_CONFIDENCE_THRESHOLD` (0.7).
`content`/`length`/`polygon` come from the word; `page_number` from the enclosing page. A word missing
`confidence` is skipped (not flagged). Reuse a small `_normalize_caption` helper for
caption/footnote objects (content + bounding_regions), mirroring the figure-caption logic already
present.

All additions are defensive (`isinstance` guards, `.get`), never raise — same posture as the current
function. The `ValueError` on missing `content` is unchanged.

### 3.2 `fdi.pipeline.stages.ocr.OcrStage`

`version = 5` (was 4), with the comment updated: "version 5: Phase 0 retains cell elements + cell
bounding_regions, table caption/footnotes, page spans + selection_marks, and a low-confidence word
list — the Azure structure Phases 1–5 consume." No other change (`run` still calls `normalize_ocr`).

## 4. Error handling

- Missing `cells[].elements` / `boundingRegions` → `[]` for that cell (a table with no cell refs still
  produces a well-formed artifact).
- Missing `table.caption` → `None`; missing/empty `footnotes` → `[]`.
- Missing `page.spans` → `[]`; missing `selectionMarks` → `[]`.
- A word with non-numeric/absent `confidence` → skipped from `low_confidence_words` (never raises).
- `normalize_ocr({})` still raises `ValueError("AnalyzeResult has no content")` (unchanged contract).
- Every other malformed shape degrades to empty, exactly as today.

## 5. Testing

- **Unit** (`backend/tests/unit/test_ocr_schema.py` — extend): a crafted AnalyzeResult with
  - a table whose cell has `elements: ["/paragraphs/5"]` + `boundingRegions: [{pageNumber, polygon}]`,
    and a `caption`/`footnotes` → assert the artifact's cell carries `elements` + `bounding_regions`,
    and the table carries `caption`/`footnotes`.
  - a page with `spans`, `selectionMarks`, and `words` including one `confidence: 0.4` and one `0.99`
    → assert `page.spans`/`selection_marks` present and `low_confidence_words` contains only the 0.4
    word (with page_number), not the 0.99 one.
  - `schema_version == 5`.
  - **Backward-additivity:** the existing schema-4 assertions still hold (content/pages/paragraphs/
    tables/sections/figures unchanged in shape) — every current `test_ocr_schema` assertion passes
    as-is except the `schema_version` bump.
  - malformed inputs (cell without elements, table without caption, page without selectionMarks, word
    without confidence) → well-formed artifact, no raise.
- **Real-doc smoke** (optional, opt-in, no new marker): a scratch check that `normalize_ocr` on the
  STTL azure_result.json now yields cells with `elements` (≈8,816), cells with `bounding_regions`
  (≈11,148), table footnotes (≈23), and a non-empty `low_confidence_words` — documented in the plan,
  not a committed test (references a sibling path).
- **Gates:** the OCR schema bump ripples into any test asserting `ocr` `schema_version == 4` or
  `OcrStage.version == 4`; grep for those and update to 5 (expected: the OcrStage version test + any
  golden-artifact test). Full suite green.

## 6. Explicitly deferred (later phases)

- **Using** `cells[].elements` to filter table-cell paragraphs → Phase 1.
- **Using** `cells[].bounding_regions` for provenance/citations → Phase 2.
- **Using** table `caption`/`footnotes` for statement identification → Phase 4.
- **Using** `low_confidence_words`/`selection_marks` for cell flagging / compliance → Phase 5.
- Retaining full per-word geometry/confidence (only the low-confidence subset is kept now); `styles`,
  page `angle` — not needed by any planned phase, left dropped.

## 7. Done criteria

1. `normalize_ocr` (schema_version 5) additively adds `cells[].elements`, `cells[].bounding_regions`,
   table `caption`/`footnotes`, page `spans`/`selection_marks`, and top-level `low_confidence_words`
   (words < 0.7) — every existing field unchanged; pure; never raises (except the existing
   missing-`content` `ValueError`).
2. `OcrStage.version == 5`; the version bump re-runs `ocr` + downstream via content-addressing with no
   other stage code change and no downstream output change.
3. Unit tests cover the new fields, the low-confidence threshold, backward-additivity, and malformed
   degradation; any `schema_version==4`/`version==4` assertion updated to 5.
4. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); `core`
   stays pure. Phase-1..5 usages remain out of scope (§6).
