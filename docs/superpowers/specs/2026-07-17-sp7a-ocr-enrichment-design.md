# SP-7a — Enrich the OCR artifact (structural payload)

- **Status:** Approved (autonomously, per standing mandate) 2026-07-17. Not yet implemented.
- **Depends on:** SP-5 (OCR stage + `normalize_ocr`), SP-2 (version-stamped stages).
- **Research:** `docs/architecture/research/2026-07-17-sp7-structural.md` ("First, enrich the OCR artifact").
- **Part of:** SP-7 Structural Intelligence. This is 7a's foundation; 7b (cross-page stitching), 7c (table
  internals), 7d (linking) build the structural processing on the payload this preserves.

## 1. Purpose

SP-5's `normalize_ocr` reduced Azure's rich `AnalyzeResult` to a thin subset (content + paragraph
role/text + per-page/per-table COUNTS). That was right for the naive spine, but it discards everything the
structural layer needs: paragraph/table geometry (for header/footer and cross-page reasoning), table cell
content + `kind` + spans (for grids and nested headers), and the section hierarchy. SP-7a **enriches the OCR
artifact to preserve that payload**, bumping `OcrStage.version` so it regenerates. Downstream stays green —
`normalize`/`ast` read `content` + `paragraphs[role,content]`, which remain.

Thin slice: a richer `normalize_ocr` (`schema_version: 2`) + the version bump + fixture/test updates. No new
stage, no stitching yet (that's 7b+). This unblocks all structural work.

## 2. Scope decisions (autonomous; grounded in research)

1. **Preserve geometry at the paragraph/table level, not per word.** Per-word polygons for a 300-600pg
   report are millions of entries; the structural layer reasons over paragraphs, tables, and their bounding
   regions. Keep paragraph `bounding_regions` + `spans`, table `bounding_regions` + `cells`. Per-word/line
   geometry is deferred (add if a measured candidate needs it).
2. **Keep table cells fully** (`content`, `row_index`, `column_index`, `row_span`, `column_span`, `kind`) —
   these drive 7c's grid + nested-header canonicalization. The research: Azure's cell model expresses nested
   headers but needs post-processing; we must at least PRESERVE it.
3. **Keep the `sections` pointer tree as-is** (`[{elements: [str]}]` of RFC-6901 pointers). 7b will
   dereference + stitch it; 7a just stops throwing it away.
4. **Bump `OcrStage.version` 1 → 2.** The OCR output definition changed; the version stamp regenerates OCR
   (and, because their `input_hash` derives from OCR's `output_hash`, normalize + ast) — exactly the
   version-driven reprocessing SP-2 built. (No real OCR artifacts exist yet; the cost is zero now.)
5. **Additive downstream.** `normalize_document` reads `content` + `paragraphs[{role,content}]`; those keys
   are unchanged (paragraphs gain fields, lose none). `build_ast` reads the unified doc. Both keep working.

## 3. Architecture

### Enriched OCR artifact (`fdi.core.ocr_schema.normalize_ocr`, schema_version 2)

```json
{
  "schema_version": 2,
  "content": "<full text>",
  "pages": [{"page_number": 1, "width": 8.5, "height": 11.0, "unit": "inch",
             "line_count": 40, "word_count": 320}],
  "paragraphs": [{"role": "sectionHeading", "content": "Directors' Report",
                  "bounding_regions": [{"page_number": 1, "polygon": [x1,y1, x2,y2, ...]}],
                  "spans": [{"offset": 128, "length": 17}]}],
  "tables": [{"row_count": 3, "column_count": 4,
              "bounding_regions": [{"page_number": 5, "polygon": [...]}],
              "cells": [{"row_index": 0, "column_index": 0, "row_span": 1, "column_span": 2,
                         "kind": "columnHeader", "content": "Year ended 31 March"}]}],
  "sections": [{"elements": ["/paragraphs/0", "/sections/1"]}]
}
```
Mapping from Azure `AnalyzeResult` (camelCase → snake_case): `boundingRegions[].pageNumber/polygon`,
`spans[].offset/length`, `cells[].rowIndex/columnIndex/rowSpan/columnSpan/kind/content`,
`sections[].elements`. Missing optional fields default to empty lists; missing top-level `content` still
raises `ValueError` (unchanged). Pure; deterministic; no SDK import.

### `OcrStage.version = 2`
The only stage change. Its comment already says "bump when the DI model or output definition changes" —
this is that.

## 4. Error handling

- Same as SP-5: missing `content` → `ValueError`. Optional structural fields absent → empty lists (a
  read-only OCR of a cover page may have no tables/sections; that is valid, not an error).
- `cells`/`bounding_regions` with unexpected shapes → the mapper uses defensive `.get(...)`; a cell missing
  `content` becomes `""` (Azure emits empty cells).

## 5. Testing

- **Unit** (no Docker): `normalize_ocr` over a canned `AnalyzeResult` with paragraphs (role + boundingRegions
  + spans), a table (cells with kind + spans + content + table boundingRegions), and a sections tree →
  asserts the enriched `schema_version: 2` output preserves all of it; an OCR with no tables/sections →
  empty lists; missing `content` still raises.
- **Integration** (testcontainers, fake OCR): the existing `ocr → normalize → ast` chain tests, with
  FakeOcrClient canned dicts UPDATED to the richer AnalyzeResult shape and the `ocr` artifact key updated to
  `version 2`; assert normalize + ast still produce their artifacts (additive-downstream proof).

## 6. Explicitly deferred (YAGNI → 7b/7c/7d)

- Per-word/line geometry; header/footer *detection* beyond Azure roles (repeated-text); cross-page reading
  order + section + table stitching (the MS merge heuristic); table-grid + nested-header canonicalization;
  caption/footnote/cross-ref linking. A `StructureStage` consuming this enriched artifact is 7b's opener.

## 7. Done criteria

1. `normalize_ocr` returns `schema_version: 2` preserving paragraph `bounding_regions` + `spans`, table
   `bounding_regions` + `cells` (with `kind`/spans/content), and the `sections` tree; empty-structure and
   missing-`content` cases correct.
2. `OcrStage.version == 2`.
3. The `ocr → normalize → ast` integration chain passes with the enriched artifact (downstream unaffected).
4. All four static gates green; `core` stays pure; full suite passes with no live Azure call.
