"""Pure parsing of the customer taxonomy raw grid into a normalized taxonomy
document. No I/O.

The xlsx is dumped to a raw grid JSON by `scripts/dump_taxonomy_grid.py`
(openpyxl, outside the backend venv); every semantic decision -- header
detection, column mapping, flag interpretation, section-header detection,
duplicate reporting -- lives HERE so it is unit-tested without a spreadsheet
library. See docs/superpowers/specs/2026-07-28-m1-fact-model-and-taxonomy-
converter-design.md.

The four sheets are NOT laid out identically (header row, tag column, and
particulars column all move between P&L and the balance-sheet sheets, and the
flag/"Comments" column is unlabelled on three of the four). So columns are
mapped by header TEXT, and the flag column -- when no header names it -- is
found as the still-unmapped column richest in the known flag vocabulary.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Final

# raw flag text (normalized: lower, single-spaced) -> canonical flag key
_FLAG_MAP: Final[dict[str, str]] = {
    "new": "new",
    "donot tag": "derived",
    "do not tag": "derived",
    "donot tag - old tag": "derived",
    "do not tag - old tag": "derived",
    "not tagged": "derived",
    "total to tag": "total_tagged",
    "not considered": "excluded",
}
_FLAG_VOCAB: Final = frozenset(_FLAG_MAP)

_SR_NO_RE: Final = re.compile(r"^\d+(\.\d+)*$")


def _norm(text: object) -> str:
    """lower, strip, collapse internal whitespace (incl. newlines) to one space."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _collapse(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    out = re.sub(r"\s+", " ", text).strip()
    return out or None


def _statement_of(title: str) -> str:
    t = title.lower()
    if "profit" in t or "p&l" in t or "p & l" in t:
        return "profit_and_loss"
    if "asset" in t:
        return "assets"
    if "liab" in t:
        return "liabilities"
    if "equity" in t:
        return "equity"
    return "unknown"


def _header_row_index(grid: Sequence[Sequence[object]]) -> int | None:
    for i, row in enumerate(grid):
        if any(_norm(c) == "particulars" for c in row):
            return i
    return None


def _map_columns(header: Sequence[object]) -> dict[str, int]:
    """Map logical column -> index by header text. `sr` matches "Sr. No." but
    not "serial no" ('serial' has no 'sr' substring). Redirection columns are
    split into dataset-2 (the one whose header names '2') vs dataset-1/Q3."""
    cols: dict[str, int] = {}
    for idx, raw in enumerate(header):
        h = _norm(raw)
        if not h:
            continue
        hns = h.replace(" ", "")
        if "redir" in h and "tag" not in h:
            if "2" in h:
                cols.setdefault("redir2", idx)
            else:
                cols.setdefault("redir1", idx)
        elif "tag" in h:
            cols.setdefault("tag", idx)
        elif "particular" in h:
            cols.setdefault("part", idx)
        elif "block" in h:
            cols.setdefault("block", idx)
        elif "sr" in hns and "no" in hns:
            cols.setdefault("sr", idx)
        elif "comment" in h:
            cols.setdefault("flag", idx)
    return cols


def _detect_flag_column(
    grid: Sequence[Sequence[object]], start: int, taken: set[int]
) -> int | None:
    """The unmapped column with the most cells whose text is known flag
    vocabulary (or begins with 'donot tag'/'do not tag'). Used when no header
    labels the flag column (Assets/Liabilities/Equity)."""
    best_idx: int | None = None
    best_hits = 0
    width = max((len(r) for r in grid[start + 1 :]), default=0)
    for idx in range(width):
        if idx in taken:
            continue
        hits = 0
        for row in grid[start + 1 :]:
            if idx >= len(row):
                continue
            v = _norm(row[idx])
            if v in _FLAG_VOCAB or v.startswith("donot tag") or v.startswith("do not tag"):
                hits += 1
        if hits > best_hits:
            best_hits, best_idx = hits, idx
    return best_idx if best_hits > 0 else None


def _flag_key(raw: object) -> tuple[str | None, bool]:
    """(canonical flag key, is_unknown). Empty -> (None, False)."""
    n = _norm(raw)
    if not n:
        return None, False
    if n in _FLAG_MAP:
        return _FLAG_MAP[n], False
    if n.startswith("donot tag") or n.startswith("do not tag"):
        return "derived", False
    return None, True


def _cell(row: Sequence[object], idx: int | None) -> object:
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def _redirect(row: Sequence[object], idx: int | None) -> str | None:
    """A rollup target tag, or None. 'NOT CONSIDERED' in a redirection cell
    means 'no rollup target', not a tag."""
    v = _collapse(_cell(row, idx))
    if v is None or _norm(v) == "not considered":
        return None
    return v.upper()


def _depth(sr_no: str | None) -> int | None:
    if sr_no and _SR_NO_RE.match(sr_no):
        return sr_no.count(".")
    return None


def _parse_sheet(
    sheet: Mapping[str, object], warnings: list[str]
) -> dict[str, object] | None:
    title = sheet.get("title")
    grid_obj = sheet.get("grid")
    if not isinstance(title, str) or not isinstance(grid_obj, list):
        return None
    grid: list[list[object]] = [list(r) if isinstance(r, list) else [] for r in grid_obj]

    hr = _header_row_index(grid)
    if hr is None:
        warnings.append(f"{title}: no 'Particulars' header row found; sheet skipped")
        return None

    cols = _map_columns(grid[hr])
    if "flag" not in cols:
        taken = set(cols.values())
        found = _detect_flag_column(grid, hr, taken)
        if found is not None:
            cols["flag"] = found

    rows: list[dict[str, object]] = []
    seen_tags: set[str] = set()
    for row in grid[hr + 1 :]:
        particulars = _collapse(_cell(row, cols.get("part")))
        tag_raw = _collapse(_cell(row, cols.get("tag")))
        tag = tag_raw.upper() if tag_raw else None
        if particulars is None and tag is None:
            continue  # spacer row

        flag, unknown = _flag_key(_cell(row, cols.get("flag")))
        if unknown:
            warnings.append(
                f"{title}: unknown flag "
                f"{_collapse(_cell(row, cols.get('flag')))!r} on {particulars!r}"
            )
        is_section_header = tag is None and particulars is not None
        if is_section_header:
            origin = "section"
        elif flag == "derived":
            origin = "derived"
        else:
            origin = "extracted"

        if tag is not None:
            if tag in seen_tags:
                warnings.append(f"{title}: duplicate tag {tag!r} on {particulars!r}")
            seen_tags.add(tag)

        sr_no = _collapse(_cell(row, cols.get("sr")))
        rows.append({
            "block_id": _collapse(_cell(row, cols.get("block"))),
            "tag": tag,
            "sr_no": sr_no,
            "particulars": particulars,
            "flag": flag,
            "origin": origin,
            "is_new": flag == "new",
            "excluded": flag == "excluded",
            "is_section_header": is_section_header,
            "depth": _depth(sr_no),
            "redirect_dataset2": _redirect(row, cols.get("redir2")),
            "redirect_dataset1": _redirect(row, cols.get("redir1")),
        })

    # Carry BlockID forward: it is stated once per block and blank on the rows
    # beneath, but every field needs its block to be grouped into a unit.
    last_block: str | None = None
    for r in rows:
        block = r["block_id"]
        if isinstance(block, str) and block:
            last_block = block
        elif last_block is not None:
            r["block_id"] = last_block

    return {"sheet": title, "statement": _statement_of(title), "rows": rows}


def parse_taxonomy(raw: Mapping[str, object]) -> dict[str, object]:
    """Normalize the raw taxonomy grid.

    Returns `{schema_version, source, statements: [{sheet, statement, rows}],
    warnings}`. Pure and total: a malformed sheet is skipped with a warning
    rather than raising.
    """
    warnings: list[str] = []
    statements: list[dict[str, object]] = []
    sheets = raw.get("sheets")
    if isinstance(sheets, list):
        for sheet in sheets:
            if isinstance(sheet, Mapping):
                parsed = _parse_sheet(sheet, warnings)
                if parsed is not None:
                    statements.append(parsed)

    source = raw.get("source")
    return {
        "schema_version": 1,
        "source": source if isinstance(source, str) else None,
        "statements": statements,
        "warnings": warnings,
    }
