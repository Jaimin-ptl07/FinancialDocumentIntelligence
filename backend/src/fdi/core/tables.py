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
