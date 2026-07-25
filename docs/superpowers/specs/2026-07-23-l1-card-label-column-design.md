# L1 — Table-Card Label Column Fix

**Date:** 2026-07-23
**Base:** `main == 6ca11b1`
**Status:** Root cause confirmed on live data; fix approved.

## Problem (confirmed root cause)

The dominant extraction-miss bucket is **L1**: line-item values the LLM never
receives. Traced on the live index (STTL FY23-24):

- The values ARE captured through OCR → normalize → structure and live in the
  table grids (e.g. `total_expenses=18795.89` in `structure.tables[61]`).
- They are absent from `ast`/`chunks` **by design** — TR-1 cards carry row
  *labels*, not values; values are injected at extraction time by
  `_render_table_candidate` fetching the grid.
- **But the value-bearing table cards never enter the unit's retrieval pool**,
  so their grid is never rendered.

Why the cards don't retrieve — the card builder summarizes row labels from the
**leading grid column**, which for Indian statement/note tables is a
non-descriptive **index column** (roman numerals `I., II., III.`). Confirmed
from `tables[61]`:

```
n_cols=5  headers=['Particulars','Particulars','Note No.','31-03-2024','31-03-2023']
 col0 (index)      col1 (LABELS)              col2   col3        col4
 I.              | Revenue from operations  | 22  | 20676.58 | 15219.54
 II.             | Other income             | 23  |   248.45 |   265.07
 (blank)         | Purchases of Stock-in-   | 24  |  2659.85 |  1480.70
 (blank)         | Total expenses           |     | 18795.89 | 14211.52
```

`_row_label(row)` returns `row[0]`, so the card reads
`Rows: I.; ; II.; III.; IV.; V; ...` — zero semantic content. The card cannot
match "Statement of Profit and Loss" on dense OR BM25, never pools, and the
grid holding `18795.89` is never rendered. The real labels ("Revenue from
operations", "Total expenses") sit in **col1**, discarded.

This is a **card-construction bug**, not a retrieval-ranking limit, and it
plausibly hits statement/note tables in every Indian annual report (the
`I./II./III.` numbering column is standard presentation).

## Fix

In `core/retrieval._table_chunks` (TR-1 card builder), select the **label
column by text density** instead of hardcoding column 0:

- **`_label_column_index(rows, header_row_count, n_rows, n_cols) -> int`**:
  for each column, count DATA-row cells that are *descriptive* — a cell with
  ≥1 ASCII letter and stripped length > 3 (excludes roman-numeral index tokens
  `I.`/`II.`/`V`, note numbers `22`, and numeric values `18795.89`). Return the
  column with the highest count; ties → **leftmost**. If every column scores 0
  (no descriptive column), fall back to **0** (today's behaviour — never worse).
- Row labels come from that column.
- The `Columns:` header line lists every header EXCEPT the chosen label column
  (was: `column_headers[1:]`).

Generalizable: keys on cell text shape (letters + length), never on any
company/label/value. All-caps labels ("TOTAL ASSETS") still score (letters +
length > 3); numbering/note/value columns never do.

**Non-goals:** the secondary card-metadata gaps (`statement_type=unknown`,
`basis=unknown`, duplicated headers, `page=None`) are out of scope for L1 —
separate items. No sign/grounding logic (that was EX-2, reverted). No schema
change.

## Files

- **Modify** `backend/src/fdi/core/retrieval.py` — add
  `_label_column_index`; use it in `_table_chunks` for row labels and the
  `Columns:` exclusion; generalize `_row_label` to a column index.
- **Bump** `backend/src/fdi/pipeline/stages/chunks.py` `ChunksStage.version`
  (3 → 4) so re-ingest rebuilds cards; embedding + index re-run downstream.
- **Test** `backend/tests/unit/test_retrieval_chunks.py` — `_label_column_index`
  cases; a `_table_chunks` card whose grid has a leading index column now
  emits descriptive `Rows:`.

## Testing

Unit: label-column picks the descriptive column over a leading index column,
over note-number and numeric-value columns; ties → leftmost; all-numeric grid
→ 0 (fallback); ragged rows don't crash. Card integration: a grid modeled on
`tables[61]` yields `Rows: Revenue from operations; ...; Total expenses; ...`
and a `Columns:` line without the label column.

Full gate: ruff + mypy + unit + integration green.

## Success criteria

- Statement/note cards carry descriptive labels and retrieve for their unit.
- Re-ingest + re-index + re-extract; re-attribute: the L1 misses
  (`total_expenses`, `purchases_of_stock_in_trade`, `other_financial_assets_*`,
  `other_current_assets`, `unbilled_revenue`) recover, measured by hand vs the
  schema examples — no scoring script.
- No regression on fields already correct.
