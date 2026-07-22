"""Header repair + markdown rendering for canonicalized table grids.

Consumes the shape produced by `fdi.core.table_grid.canonicalize_table`
(`{n_rows, n_cols, header_row_count, column_headers, grid, cell_regions,
cell_pages}`) and turns it into LLM-ready context: real column headers even
when Azure DI collapses them (the "Details, Details, Details" failure), and a
GitHub-flavored markdown rendering with an identity title line. Pure -- no
I/O, no config, no clock/RNG, no logging. (TR-1 P3.)
"""

import re
from collections.abc import Mapping

# Header cells carry PREFIXES ("As at 31-03-2024", "For the year ended
# 31-03-2024") that a `^`-anchored pattern misses entirely -- only the
# leading `^` is dropped (matched with `re.search`, not `re.match`/
# `fullmatch`); the trailing `$` stays, since the period token is always the
# last thing printed in a header cell.
_DATE_DMY = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}$")
_DATE_YMD = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
_YEAR = re.compile(r"\d{4}$")
_FY_TOKEN = re.compile(r"FY-?\d{2,4}$", re.IGNORECASE)
_FISCAL_RANGE_2 = re.compile(r"\d{4}-\d{2}$")
_FISCAL_RANGE_4 = re.compile(r"\d{4}-\d{4}$")

# Spelled-month dates ("31st March, 2024", "March 31, 2024") in either
# day-month-year or month-day-year order, with an optional ordinal suffix on
# the day and an optional comma before the year. No anchors -- `re.search`
# finds the date wherever it sits in the cell.
_MONTH_NAMES = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_SPELLED_MONTH_DAY_FIRST = re.compile(
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAMES}\.?,?\s+\d{{4}}", re.IGNORECASE
)
_SPELLED_MONTH_MONTH_FIRST = re.compile(
    rf"{_MONTH_NAMES}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}", re.IGNORECASE
)

_YEAR_MIN = 2019
_YEAR_MAX = 2099


def _as_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_list(value: object) -> list[object] | None:
    return value if isinstance(value, list) else None


def _is_period(text: str) -> bool:
    """True when `text` CONTAINS (not just equals) a reporting period: a
    date -- numeric ("31-03-2024") or spelled-month ("31st March, 2024",
    "March 31, 2024") -- a bare year, or a fiscal-year token (`FY24`,
    `FY2024`, `2023-24`, `2023-2024`). Real header cells carry prefixes
    ("As at 31-03-2024", "For the year ended 31 March 2024"), so every
    pattern is searched, not fullmatched."""
    if _DATE_DMY.search(text) or _DATE_YMD.search(text):
        return True
    if _SPELLED_MONTH_DAY_FIRST.search(text) or _SPELLED_MONTH_MONTH_FIRST.search(text):
        return True
    if _FY_TOKEN.search(text):
        return True
    if _FISCAL_RANGE_2.search(text) or _FISCAL_RANGE_4.search(text):
        return True
    year_match = _YEAR.search(text)
    if year_match:
        return _YEAR_MIN <= int(year_match.group()) <= _YEAR_MAX
    return False


def _is_collapsed(header_row_count: int, data_headers: list[str]) -> bool:
    """True when the header row is missing or unusable: no header rows at
    all, every data-column header is blank, or they've all collapsed to the
    same duplicated value (the Azure-DI "Details, Details" failure)."""
    if header_row_count == 0:
        return True
    if not data_headers:
        return False
    if all(not h.strip() for h in data_headers):
        return True
    return len(data_headers) >= 2 and len({h.strip() for h in data_headers}) == 1


def _positional_header(col: int) -> str:
    if col == 1:
        return "Column 1 (most recent)"
    if col == 2:
        return "Column 2 (prior)"
    return f"Column {col}"


def _extract_headers(grid: Mapping[str, object], n_cols: int) -> list[str]:
    raw_headers = _as_list(grid.get("column_headers"))
    headers = [_as_str(h) for h in raw_headers] if raw_headers is not None else []
    if n_cols > 0:
        if len(headers) < n_cols:
            headers = headers + [""] * (n_cols - len(headers))
        elif len(headers) > n_cols:
            headers = headers[:n_cols]
    return headers


def _extract_grid_rows(grid: Mapping[str, object]) -> list[list[str]]:
    raw_grid = _as_list(grid.get("grid"))
    rows: list[list[str]] = []
    if raw_grid is not None:
        for row in raw_grid:
            row_list = _as_list(row)
            rows.append([_as_str(c) for c in row_list] if row_list is not None else [])
    return rows


def repair_headers(grid: Mapping[str, object]) -> dict[str, object]:
    """Return a copy of `grid` with `column_headers` repaired when they are
    missing or collapsed. See module docstring / TR-1 spec section 5.2.

    Total: never raises, regardless of input shape.
    """
    src: Mapping[str, object] = grid if isinstance(grid, Mapping) else {}
    out: dict[str, object] = dict(src)

    n_cols = _as_int(out.get("n_cols"), 0)
    header_row_count = _as_int(out.get("header_row_count"), 0)
    headers = _extract_headers(out, n_cols)
    if n_cols <= 0:
        n_cols = len(headers)

    data_headers = headers[1:n_cols] if n_cols > 1 else []
    if not _is_collapsed(header_row_count, data_headers):
        out["column_headers"] = headers
        return out

    grid_rows = _extract_grid_rows(out)
    first_data_row: list[str] = (
        grid_rows[header_row_count] if 0 <= header_row_count < len(grid_rows) else []
    )

    period_headers: dict[int, str] = {}
    for c in range(1, n_cols):
        cell_text = first_data_row[c].strip() if c < len(first_data_row) else ""
        if cell_text and _is_period(cell_text):
            period_headers[c] = cell_text

    new_headers = list(headers)
    if period_headers:
        for c in range(1, n_cols):
            new_headers[c] = period_headers.get(c, "")
    else:
        for c in range(1, n_cols):
            new_headers[c] = _positional_header(c)

    out["column_headers"] = new_headers
    return out


def render_table_markdown(
    grid: Mapping[str, object], *, identity: str = "", units: str | None = None
) -> str:
    """Render a canonicalized table grid as a GitHub-flavored markdown table,
    repairing collapsed headers first. See module docstring / TR-1 spec
    section 5.1.

    Total: never raises, regardless of input shape; an empty/malformed grid
    returns "" or just the title line when `identity` is given.
    """
    title = ""
    if identity:
        title = f"Table: {identity}"
        if units:
            title += f" -- {units}"

    repaired = repair_headers(grid)

    n_cols = _as_int(repaired.get("n_cols"), 0)
    header_row_count = _as_int(repaired.get("header_row_count"), 0)
    headers = _extract_headers(repaired, n_cols)
    if n_cols <= 0:
        n_cols = len(headers)

    grid_rows = _extract_grid_rows(repaired)

    if n_cols <= 0 or not grid_rows:
        return title

    def _escape(cell: str) -> str:
        return cell.replace("|", "\\|")

    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("")

    header_cells = [headers[c] if c < len(headers) else "" for c in range(n_cols)]
    lines.append("| " + " | ".join(_escape(h) for h in header_cells) + " |")
    lines.append("| " + " | ".join(["---"] * n_cols) + " |")

    for r in range(max(header_row_count, 0), len(grid_rows)):
        row = grid_rows[r]
        row_cells = [row[c] if c < len(row) else "" for c in range(n_cols)]
        lines.append("| " + " | ".join(_escape(c) for c in row_cells) + " |")

    return "\n".join(lines)
