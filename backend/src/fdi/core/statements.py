"""Find a statement's face table, read it, and locate the notes it points at.

This is the deterministic bootstrap of extraction: one parse of the face yields
the reported parent values, the note pointers, and the section captions -- with
no LLM involved.

THREE THINGS MEASUREMENT FORCED
-------------------------------
1. FIND STATEMENTS BY CONTENT, NOT BY HEADING. Heading detection on TCS returned
   pages 166, 167, 228, 252 and 253 -- three false positives -- and could not say
   which statement it had found. A face table is instead identified by its OWN
   rows, which Schedule III effectively mandates ("total assets", "profit before
   tax"). Verified on TCS/RITES/RIL: it locates both the consolidated and the
   standalone face, correctly typed.

2. LOCATE THE NOTE COLUMN ONCE, FROM THE HEADER. Scanning each row for "a cell
   that looks like a reference" read VALUES as references: TCS's OCI row
   `... | (69) | 45` produced "note 45", and following a wrong note is worse
   than following none.

3. A ROW WITH NO AMOUNT IS A CAPTION, NOT A CONCEPT. RIL prints "a) Owners of
   the Company" under three different captions; without the caption the label
   alone is undecidable, and mapping caption rows as if they were values
   produced several wrong assignments.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fdi.core.numbers import parse_amount

# A note reference in ANY issuer's grammar. The earlier `\d{1,2}(\([a-z]\))?`
# parsed 0 of RITES's 30 references, which are numbered 2.01 / 2.08.2.
_REF_ANY = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3})*)\s*(?:\(([a-z])\))?\s*$")
_RANGE_OR_LIST = re.compile(r"\b(to|and)\b|&", re.IGNORECASE)

STATEMENT_ANCHORS: Mapping[str, tuple[str, ...]] = {
    "balance_sheet": (
        "total assets", "total equity", "total current liabilities",
        "total non-current assets", "total non current assets",
        "equity and liabilities", "total current assets",
    ),
    "profit_and_loss": (
        "revenue from operations", "total income", "total expenses",
        "total expenditure", "profit before tax", "profit for the year",
        "profit after tax", "earnings per",
    ),
    # Widened after measuring: TCS and RIL both scored EXACTLY 3 against
    # MIN_ANCHORS=3, so a single OCR hiccup on one row would have lost the whole
    # statement. These extra anchors are lines every Ind AS 7 statement prints.
    "cash_flow": (
        "cash flows from operating activities", "cash flow from operating",
        "cash flows from investing", "cash flow from investing",
        "cash flows from financing", "cash flow from financing",
        "cash and cash equivalents at the end",
        "net cash flows generated from operating",
        "cash generated from operations",
        "cash and cash equivalents at the beginning",
        "closing balance of cash and cash equivalents",
        "opening balance of cash and cash equivalents",
        "net increase in cash", "net decrease in cash",
        "net cash used in investing", "net cash used in financing",
        "operating profit before working capital changes",
    ),
}

MIN_ANCHORS = 3
NOTE_TAIL = 20_000
_HEADER_SCAN = 5

# The heading that OPENS a note, in the same numbering system `parse_note_ref`
# reads off the face -- deliberately here and not a caller argument. When the
# caller supplied this grammar, the face parser accepted RITES's dotted refs
# while the heading matcher did not, so all 10 of its references resolved to
# nothing. One grammar, one place.
#   "10. Revenue from operations"     TCS, flat + separator + title
#   "2.17 Revenue From Operations"    RITES, dotted, separator optional
#   "2.18"                            RITES, dotted, title on the next line
_HEAD_DOTTED = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3})+)\s*[\).:]?\s*([A-Za-z].{0,80})?$")
_HEAD_FLAT = re.compile(r"^\s*(\d{1,2})\s*[\).]\s+([A-Za-z].{2,80})$")
_HEAD_SUB = re.compile(r"^\s*\(([a-z])\)\s+([A-Za-z].{2,80})$")


def parse_note_ref(text: object) -> str:
    """A note-column cell as a canonical reference, or "" if it is not one.

    Range and conjunction forms ("1 to 44", "1 & 2") do not refer to a single
    note and are rejected rather than mis-parsed into a lookup."""
    if not isinstance(text, str):
        return ""
    raw = text.strip()
    if not raw or _RANGE_OR_LIST.search(raw):
        return ""
    match = _REF_ANY.match(raw)
    if match is None:
        return ""
    letter = match.group(2)
    return match.group(1) + (f"({letter})" if letter else "")


def _content(item: Mapping[str, object]) -> str:
    raw = item.get("content")
    return re.sub(r"\s+", " ", raw).strip() if isinstance(raw, str) else ""


def _page_of(item: Mapping[str, object]) -> int | None:
    regions = item.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return None
    first = regions[0]
    if not isinstance(first, Mapping):
        return None
    page = first.get("page_number")
    return page if isinstance(page, int) else None


def _span_offset(item: Mapping[str, object]) -> int | None:
    spans = item.get("spans")
    if not isinstance(spans, list) or not spans:
        return None
    first = spans[0]
    if not isinstance(first, Mapping):
        return None
    offset = first.get("offset")
    return offset if isinstance(offset, int) else None


def table_rows(table: Mapping[str, object]) -> dict[int, dict[int, str]]:
    """Azure DI cells regrouped as {row_index: {column_index: text}}."""
    rows: dict[int, dict[int, str]] = {}
    cells = table.get("cells")
    if not isinstance(cells, list):
        return rows
    for cell in cells:
        if not isinstance(cell, Mapping):
            continue
        r, c = cell.get("row_index"), cell.get("column_index")
        if isinstance(r, int) and isinstance(c, int):
            rows.setdefault(r, {})[c] = _content(cell)
    return rows


def score_statement(table: Mapping[str, object], statement: str) -> int:
    """Distinct mandated line names present in this table's label column."""
    anchors = STATEMENT_ANCHORS.get(statement, ())
    rows = table_rows(table)
    labels = " | ".join(rows[r].get(0, "") for r in sorted(rows)).lower()
    return sum(1 for a in anchors if a in labels)


@dataclass(frozen=True)
class StatementTable:
    statement: str
    page: int
    score: int
    table: Mapping[str, object]


def find_statement_tables(
    tables: Sequence[object], statement: str, page_lo: int, page_hi: int
) -> list[StatementTable]:
    """Candidate face tables for `statement`, best first."""
    out: list[StatementTable] = []
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        page = _page_of(table)
        if page is None or not (page_lo <= page <= page_hi):
            continue
        score = score_statement(table, statement)
        if score >= MIN_ANCHORS:
            out.append(StatementTable(statement, page, score, table))
    out.sort(key=lambda t: (-t.score, t.page))
    return out


@dataclass(frozen=True)
class FaceRow:
    """One face row: what it says, where its detail lives, and what it is worth.

    `source_row` is the RAW `cell.row_index` this row was read from, kept
    because the position of a row within the returned (header- and
    caption-filtered) list cannot address a cell in the OCR grid. It defaults to
    -1 = unknown so a hand-built FaceRow in a caller's test stays valid."""

    label: str
    caption: str
    ref: str
    current: float | None
    prior: float | None
    source_row: int = -1
    section_path: tuple[str, ...] = ()


def _is_note_header(text: str) -> bool:
    """A column header naming the note column -- "Note", "Notes", "Note No.".

    Deliberately narrow. Matching the word "note" ANYWHERE cost an entire
    statement: RIL's cash flow ends with "Closing Balance of Cash and Cash
    Equivalents (Refer Note 9)" in row 43 of 43, which was taken as the header
    row, and every row at or before it was skipped -- all 44 of them. A header
    is a short label, not prose that mentions a note."""
    stripped = text.strip().rstrip(".:").strip()
    return len(stripped) <= 12 and "note" in stripped.lower()


# The Schedule III blocks that RESET the path rather than nesting inside it.
# A flat table carries no indentation, so depth is genuinely unknowable; what
# IS knowable is that a statement's top-level blocks are mutually exclusive.
# MEASURED: without this, `Non-current liabilities` and `Current liabilities`
# both collapse into the caption `Financial liabilities`, and the two lease
# rows become indistinguishable.
_TOP_SECTIONS: tuple[str, ...] = (
    "non-current assets", "current assets",
    "non-current liabilities", "current liabilities",
    "assets", "equity and liabilities", "equity", "liabilities",
    "income", "expenses", "expenditure",
)


def _section_reset(label: str) -> str:
    """The top-level block `label` opens, or "" if it opens none.

    Longest match wins: "non-current liabilities" must not be read as
    "liabilities", which would reset the path to the wrong block."""
    key = label.strip().lower().rstrip(":")
    best = ""
    for section in _TOP_SECTIONS:
        if key == section and len(section) > len(best):
            best = section
    return best


def parse_face_table(table: Mapping[str, object]) -> list[FaceRow]:
    """Face rows with amounts, note references and their section caption."""
    rows = table_rows(table)
    if not rows:
        return []
    # A header sits at the TOP of the table. Scanning the whole table lets a
    # late footnote row masquerade as one.
    header = next(
        (r for r in sorted(rows)[:_HEADER_SCAN]
         if any(_is_note_header(v) for v in rows[r].values())),
        None,
    )
    note_col: int | None = None
    if header is not None:
        note_col = next(
            (c for c, v in rows[header].items() if _is_note_header(v)), None
        )

    out: list[FaceRow] = []
    path: list[str] = []
    caption = ""
    for r in sorted(rows):
        if header is not None and r <= header:
            continue
        cols = rows[r]
        label = cols.get(0, "")
        if not label:
            continue
        amounts: list[float] = []
        for c in sorted(cols):
            if c == 0 or c == note_col:
                continue
            value = parse_amount(cols.get(c))
            if value is not None:
                amounts.append(value)
        if not amounts:
            # A row with no amount anywhere is a CAPTION.
            if _section_reset(label):
                path = [label]        # a top-level block starts a fresh path
            else:
                path.append(label)    # otherwise it nests under what is open
            caption = label
            continue
        ref = parse_note_ref(cols.get(note_col)) if note_col is not None else ""
        out.append(FaceRow(
            label=label, caption=caption, ref=ref,
            current=amounts[0],
            prior=amounts[1] if len(amounts) > 1 else None,
            source_row=r,
            section_path=tuple(path),
        ))
    return out


def verify_footing(
    rows: Sequence[FaceRow], plus: Sequence[str], minus: Sequence[str],
    target: str, tolerance: float = 1.0,
) -> bool | None:
    """Does the face foot? None when a needed line is absent.

    This is what proves the RIGHT table was chosen, whatever found it: TCS,
    RITES and RIL P&Ls all tie exactly."""
    def find(fragment: str) -> float | None:
        frag = fragment.lower()
        for row in rows:
            if frag in row.label.lower() and row.current is not None:
                return row.current
        return None

    total = find(target)
    if total is None:
        return None
    acc = 0.0
    for name in plus:
        value = find(name)
        if value is None:
            return None
        acc += value
    for name in minus:
        value = find(name)
        if value is None:
            return None
        acc -= value
    return abs(acc - total) <= tolerance


@dataclass(frozen=True)
class NoteWindow:
    """Where a note lives, and what it calls itself.

    The TITLE is not decoration: a note the face never cites is reachable only
    by its own heading ("11. Other income"), and that is how concepts the face
    summarises into a single line still get found."""

    ref: str
    title: str
    page: int
    start: int
    end: int


def note_windows(
    paragraphs: Sequence[object], page_lo: int, page_hi: int,
) -> dict[str, NoteWindow]:
    """ref -> the span covering that note's content.

    Page granularity is too coarse: TCS p301 carries TWO notes (13(a) at offset
    897,095 and 13(b) at 897,486), so a page-based fetch hands both to whichever
    concept asks first."""
    heads: list[tuple[str, str, int, int]] = []
    current = ""
    for para in paragraphs:
        if not isinstance(para, Mapping) or para.get("role") != "sectionHeading":
            continue
        page, offset = _page_of(para), _span_offset(para)
        if page is None or offset is None or not (page_lo <= page <= page_hi):
            continue
        text = _content(para)
        top = _HEAD_DOTTED.match(text) or _HEAD_FLAT.match(text)
        if top is not None:
            current = top.group(1)
            heads.append((current, (top.group(2) or "").strip(), page, offset))
            continue
        sub = _HEAD_SUB.match(text)
        if sub is not None and current:
            heads.append((f"{current}({sub.group(1)})", sub.group(2).strip(), page, offset))
    heads.sort(key=lambda h: h[3])
    out: dict[str, NoteWindow] = {}
    for i, (ref, title, page, offset) in enumerate(heads):
        end = heads[i + 1][3] if i + 1 < len(heads) else offset + NOTE_TAIL
        if ref not in out:
            out[ref] = NoteWindow(ref, title, page, offset, end)
    return out


def tables_in_window(
    tables: Sequence[object], window: NoteWindow | tuple[int, int]
) -> list[Mapping[str, object]]:
    """Tables whose span falls inside a note's offset window."""
    start, end = (
        (window.start, window.end) if isinstance(window, NoteWindow) else window
    )
    out: list[Mapping[str, object]] = []
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        offset = _span_offset(table)
        if offset is not None and start <= offset < end:
            out.append(table)
    return out


# The arithmetic each face must satisfy, as (plus, minus, targets) label
# fragments. `targets` is ORDERED and tried most-specific first: the first one
# present on the face is the rule that applies.
#
# WHY THE P&L NEEDS TWO TARGETS -- MEASURED ON TCS FY2025-26
# ----------------------------------------------------------
# `income - expenses = profit before tax` is only true for a filer with NO
# exceptional items. TCS FY26 reports three (restructuring 1,388, Labour Codes
# 2,128, legal claim 1,010), so its face runs::
#
#     TOTAL INCOME                             2,71,423
#     TOTAL EXPENSES                           2,01,410
#     PROFIT BEFORE EXCEPTIONAL ITEMS AND TAX     70,013   <- income - expenses
#     ... exceptional items                        4,526
#     PROFIT BEFORE TAX                           65,487   <- 4,526 lower
#
# Both bases scored `passed: False` against the single-target rule. The face is
# internally perfect (every subtotal ties); the RULE named the wrong row. Note
# that "profit before exceptional items and tax" does NOT contain the substring
# "profit before tax", so the two never shadow each other.
#
# Cash flow has NO rule: its anchors already miss REMC and RIL entirely (see the
# design's Phase 5 hole), so asserting one would manufacture failures on
# documents whose face is perfectly correct.
# WHY THE BALANCE SHEET NEEDS ALTERNATIVE EQUATIONS, NOT JUST ALTERNATIVE TARGETS
# ------------------------------------------------------------------------------
# MEASURED ON TCS FY2025-26: a Schedule III balance sheet commonly prints NO
# single "Total liabilities" line at all -- only "Total non-current liabilities"
# and "Total current liabilities" -- so an equation naming "total liabilities"
# can never resolve and every such balance sheet went permanently unchecked
# (TCS both bases: passed=None). The assets-side identity is a third route for
# a face that totals its two asset halves but not its liability ones.
#
# Substring matching is safe here: "total non-current liabilities" does NOT
# contain "total current liabilities" (the word "non-" intervenes), so the
# specific and general labels cannot shadow one another.
# The subtotal `total income - total expenses` actually produces, under the three
# names Ind AS filers give it. MOST SPECIFIC FIRST -- each is a real presentation
# variant measured on a real filing, not an issuer patch:
#   exceptional items      TCS FY2025-26  2,71,423 - 2,01,410 = 70,013
#   equity-method investees RIL FY2025-26  3,28,513 - 3,09,990 = 18,523
#   the plain form         everyone else
# None of the three contains another as a substring, so ordering cannot shadow.
_PL_TARGETS = (
    "profit before exceptional items and tax",
    "profit before share of profit",
    "profit before tax",
)

# REMC FY2024-25 heads its expense block "Total Expenditure", not "Total
# Expenses" -- 14,046.91 - 3,884.41 = 10,162.50. STATEMENT_ANCHORS already
# accepts both spellings; the footing rule did not, so that face went unchecked.
_PL_EXPENSES = ("total expenses", "total expenditure")

FOOTING_RULES: Mapping[
    str, tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...]
] = {
    # Expense wording outer, target inner: a face using the other spelling makes
    # every equation in the first group unresolvable (None), so the second group
    # is reached without either ever matching the wrong row.
    "profit_and_loss": tuple(
        (("total income",), (expense,), target)
        for expense in _PL_EXPENSES
        for target in _PL_TARGETS
    ),
    "balance_sheet": (
        (
            (
                "total equity",
                "total non-current liabilities",
                "total current liabilities",
            ),
            (),
            "total equity and liabilities",
        ),
        (("total equity", "total liabilities"), (), "total equity and liabilities"),
        (("total equity", "total liabilities"), (), "total assets"),
        (
            ("total non-current assets", "total current assets"),
            (),
            "total assets",
        ),
    ),
}

_FOOTING_TOLERANCE = 1.0


def describe_footing(rows: Sequence[FaceRow], statement: str) -> dict[str, object]:
    """Whether this face's arithmetic ties, as a recordable verdict.

    `passed is None` means COULD NOT CHECK -- no target line is present, or the
    statement has no rule. That is deliberately distinct from False: discarding
    a correct table because it labels one line unusually is worse than not
    checking it. `checked` makes the distinction explicit on the artifact.

    EQUATIONS are tried in order and the first one the face can actually resolve
    wins, so an issuer reporting exceptional items is checked against the subtotal
    the arithmetic really produces -- not against a lower line that legitimately
    differs by the exceptional items. `rule` records which target was used, so a
    verdict is always traceable to the equation behind it."""
    equations = FOOTING_RULES.get(statement)
    if not equations:
        return {"rule": "", "passed": None, "checked": False}

    def render(plus: Sequence[str], minus: Sequence[str], target: str) -> str:
        return (
            " + ".join(plus) + "".join(f" - {m}" for m in minus) + f" = {target}"
        )

    for plus, minus, target in equations:
        passed = verify_footing(
            rows, plus, minus, target, tolerance=_FOOTING_TOLERANCE
        )
        if passed is not None:
            return {
                "rule": render(plus, minus, target),
                "passed": passed,
                "checked": True,
            }
    return {"rule": render(*equations[0]), "passed": None, "checked": False}
