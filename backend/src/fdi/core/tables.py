"""Numeric table-cell parsing + concept->row matching over canonicalized table
grids (fdi.core.table_grid.canonicalize_table's output shape:
`{n_rows, n_cols, header_row_count, column_headers, grid}`). Pure -- no I/O.

This is the RT-6 "deferred numeric extraction" fdi.core.ontology's
map_document docstring points at: a table's row-label cell (grid column 0) is
already a bare label with no trailing number baked in (unlike an AST
paragraph, where a real line item mixes label+number in one string), so this
module's matching problem is "which concept does this label mean", not "strip
the number first". See
docs/superpowers/specs/2026-07-18-rt6-structured-financial-retrieval-design.md
for the full design rationale (decisions 4-8 in particular).
"""

import re
from collections.abc import Mapping
from typing import cast

from fdi.core.ontology import CanonicalConcept, normalize_label, synonyms_for

_BLANK_TOKENS = {
    "", "-", "--", "–", "—",
    "n/a", "na", "nil", "nm", "n.a.", "n.m.",
}
_CURRENCY_RE = re.compile(r"(?:rs\.?|inr|usd|₹|\$|€|£)", re.IGNORECASE)
_NUMERIC_BODY_RE = re.compile(r"^\d+(\.\d+)?$")


def parse_numeric(cell: object) -> float | None:
    """Parse a table cell's raw content into a float.

    Handles: thousands separators of any grouping ("12,345", Indian
    "1,00,000" -- ALL commas are stripped, no grouping-shape validation),
    parenthetical negatives ("(1,234)" -> -1234.0), a leading -/+ sign, a
    trailing "%" (returned as the printed number, NOT divided by 100 -- RT-6
    has no unit-of-measure model), a leading/embedded currency marker (Rs.,
    INR, USD, currency symbols -- stripped, no FX conversion), and
    surrounding whitespace.

    Returns None for: blanks/dashes/"N/A" family (any case; a financial
    statement's "no value reported" marker, never coerced to 0.0), non-str/
    non-(int|float) input, bool (Python bool is an int subclass but is never
    a table cell value), and any text that still isn't a plain decimal after
    the above stripping. Never raises.
    """
    if isinstance(cell, bool):
        return None
    if isinstance(cell, int | float):
        return float(cell)
    if not isinstance(cell, str):
        return None

    text = cell.strip()
    if text.lower() in _BLANK_TOKENS:
        return None

    negative = False
    if len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    text = _CURRENCY_RE.sub("", text).strip()

    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()

    if text.endswith("%"):
        text = text[:-1].strip()

    text = text.replace(",", "").replace(" ", "")

    if not text or not _NUMERIC_BODY_RE.match(text):
        return None

    value = float(text)
    return -value if negative else value


def _contains_subsequence(haystack: list[str], needle: list[str]) -> bool:
    """True if `needle` (non-empty) appears as a CONTIGUOUS run inside
    `haystack`, at any offset."""
    if not needle:
        return False
    span = len(needle)
    return any(
        haystack[i : i + span] == needle for i in range(len(haystack) - span + 1)
    )


def find_concept_rows(
    grid: Mapping[str, object], concept: CanonicalConcept
) -> list[dict[str, object]]:
    """Match `concept`'s ontology synonyms against `grid`'s row-label column
    (column 0) using normalized word-token-subsequence containment -- e.g.
    synonym tokens ["revenue","from","operations"] match inside label tokens
    ["total","revenue","from","operations"], so "Total revenue from
    operations" matches REVENUE even though map_label's exact lookup would
    not. Header rows (`grid["header_row_count"]`) are never treated as data.

    For each matched row, returns {row_label, values: [{column_header, raw,
    value}, ...]} covering every non-label column (column_index >= 1);
    `value` is `parse_numeric(raw)` -- ALL numeric columns are returned, this
    function never picks "the" period.

    KNOWN LIMITATION (documented, not solved): a synonym word can appear
    inside a label that means a different concept -- "Cost of revenue"
    contains the REVENUE synonym "revenue" and WILL match REVENUE. See the
    design spec's decision 5.

    Degrades to [] on a malformed/empty `grid` (missing keys, n_cols == 0,
    ragged rows shorter than n_cols) -- never raises.
    """
    n_rows_raw = grid.get("n_rows")
    n_rows = n_rows_raw if isinstance(n_rows_raw, int) else 0
    n_cols_raw = grid.get("n_cols")
    n_cols = n_cols_raw if isinstance(n_cols_raw, int) else 0
    header_row_count_raw = grid.get("header_row_count")
    header_row_count = header_row_count_raw if isinstance(header_row_count_raw, int) else 0

    rows_raw = grid.get("grid")
    rows = cast("list[object]", rows_raw) if isinstance(rows_raw, list) else []
    headers_raw = grid.get("column_headers")
    column_headers = cast("list[object]", headers_raw) if isinstance(headers_raw, list) else []

    synonym_tokens = [
        tokens
        for s in synonyms_for(concept)
        if (tokens := normalize_label(s).split())
    ]

    matches: list[dict[str, object]] = []
    for r in range(header_row_count, n_rows):
        row_obj = rows[r] if r < len(rows) else None
        row = cast("list[object]", row_obj) if isinstance(row_obj, list) else []

        label_obj = row[0] if row else ""
        label_raw = label_obj if isinstance(label_obj, str) else ""
        label_tokens = normalize_label(label_raw).split()
        if not label_tokens:
            continue
        if not any(_contains_subsequence(label_tokens, syn) for syn in synonym_tokens):
            continue

        values: list[dict[str, object]] = []
        for c in range(1, n_cols):
            raw_obj = row[c] if c < len(row) else ""
            raw = raw_obj if isinstance(raw_obj, str) else ""
            header_obj = column_headers[c] if c < len(column_headers) else ""
            header = header_obj if isinstance(header_obj, str) else ""
            values.append({"column_header": header, "raw": raw, "value": parse_numeric(raw)})
        matches.append({"row_label": label_raw, "values": values})

    return matches


def find_field_rows(
    grid: Mapping[str, object], keywords: tuple[str, ...]
) -> list[dict[str, object]]:
    """Match `keywords` (a field's own tokens -- FieldSpec.keywords, already
    aliases + locator_keywords, order-preserving deduplicated) against
    `grid`'s row-label column (column 0), using the SAME normalized
    word-token-subsequence containment `find_concept_rows` uses for ontology
    synonyms -- keyed on a field directly instead of a CanonicalConcept, since
    a FieldSpec has no ontology concept of its own. Header rows
    (`grid["header_row_count"]`) are never treated as data.

    Unlike `find_concept_rows` (which returns every column's value -- it
    never picks "the" period), this returns each match's ROW INDEX, so the
    caller can look up `cell_regions[row][col]` directly for a specific
    column's geometry. `{row, row_label}` per match, in row order.

    Same KNOWN LIMITATION as find_concept_rows: a keyword can appear inside a
    label that means something else. Degrades to [] on a malformed/empty
    `grid`. Never raises."""
    n_rows_raw = grid.get("n_rows")
    n_rows = n_rows_raw if isinstance(n_rows_raw, int) and not isinstance(n_rows_raw, bool) else 0
    header_row_count_raw = grid.get("header_row_count")
    header_row_count = (
        header_row_count_raw
        if isinstance(header_row_count_raw, int) and not isinstance(header_row_count_raw, bool)
        else 0
    )

    rows_raw = grid.get("grid")
    rows = cast("list[object]", rows_raw) if isinstance(rows_raw, list) else []

    keyword_tokens = [
        tokens for k in keywords if (tokens := normalize_label(k).split())
    ]
    if not keyword_tokens:
        return []

    matches: list[dict[str, object]] = []
    for r in range(header_row_count, n_rows):
        row_obj = rows[r] if r < len(rows) else None
        row = cast("list[object]", row_obj) if isinstance(row_obj, list) else []

        label_obj = row[0] if row else ""
        label_raw = label_obj if isinstance(label_obj, str) else ""
        label_tokens = normalize_label(label_raw).split()
        if not label_tokens:
            continue
        if not any(_contains_subsequence(label_tokens, kw) for kw in keyword_tokens):
            continue

        matches.append({"row": r, "row_label": label_raw})

    return matches


def filter_tables(
    tables: list[Mapping[str, object]], keyword: str | None
) -> list[Mapping[str, object]]:
    """Keep only tables where some `grid["column_headers"][c]` contains
    `keyword` (case-insensitive substring). `keyword` None or "" returns
    `tables` unchanged. A table with a missing/malformed `grid`/
    `column_headers` is dropped when a keyword IS given (nothing to match
    against), kept unchanged when it isn't."""
    if not keyword:
        return tables
    needle = keyword.lower()
    out: list[Mapping[str, object]] = []
    for t in tables:
        grid = t.get("grid")
        headers = grid.get("column_headers") if isinstance(grid, Mapping) else None
        if isinstance(headers, list) and any(
            isinstance(h, str) and needle in h.lower() for h in headers
        ):
            out.append(t)
    return out
