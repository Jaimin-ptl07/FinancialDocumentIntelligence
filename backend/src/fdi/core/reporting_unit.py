"""The unit a statement's amounts are printed in, read from the page.

An Indian statement prints its unit ONCE, above the table -- `(₹ crore)`,
`(₹ in lakhs)`, `(Rs. in Lakhs)`, `(All amounts in ₹ crore, unless otherwise
stated)` -- and never beside each figure. A value carried without it cannot be
normalised or compared across Companies: TCS's 2,67,021 is crore, REMC's
14,046.91 is lakh. Azure DI rarely attaches it as the table's caption, so it is
read from the page text immediately before the table, nearest first.

Per-share figures are printed in rupees whatever the statement's header says,
and counts and ratios carry no currency at all; `unit_for_kind` applies that.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

# How far before a table its unit line may sit. A unit line is within a few
# lines of the table (title, unit, column header); a page earlier is another
# statement's.
_LOOKBACK_CHARS: Final = 600
_LOOKAHEAD_CHARS: Final = 120

_SCALES: Final[tuple[tuple[str, str, Decimal], ...]] = (
    # (pattern, label word, rupees per printed unit). Longest first, so
    # "lakh crore" is never read as "lakh".
    (r"lakhs?\s+crores?", "lakh crore", Decimal("1000000000000")),
    (r"thousand\s+crores?", "thousand crore", Decimal("10000000000")),
    (r"crores?|cr\.?", "crore", Decimal("10000000")),
    (r"lakhs?|lacs?", "lakh", Decimal("100000")),
    (r"millions?|mn", "million", Decimal("1000000")),
    (r"thousands?|'000s?|000s", "thousand", Decimal("1000")),
)
_CURRENCY: Final = r"(?:₹|rs\.?|inr|rupees)"
# A unit declaration: a currency marker and a scale word, in either order, in
# a short span -- `(₹ crore)`, `₹ in crores`, `Rs. in Lakhs`, `INR million`,
# `amounts in crore of rupees`.
_SCALE_ALT: Final = "|".join(p for p, _, _ in _SCALES)
_UNIT_RE: Final = re.compile(
    rf"{_CURRENCY}[^\n()]{{0,24}}?\b(?P<s1>{_SCALE_ALT})\b"
    rf"|\b(?P<s2>{_SCALE_ALT})\b[^\n()]{{0,12}}?{_CURRENCY}",
    re.IGNORECASE,
)
# A bare `(₹)` -- amounts printed in whole rupees.
_RUPEES_RE: Final = re.compile(rf"\(\s*{_CURRENCY}\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class ReportingUnit:
    """What one printed figure is denominated in.

    `currency` is None for counts and ratios. `scale` multiplies a printed
    figure into absolute units (rupees for INR). `label` is how it is shown:
    "INR crore", "INR lakh", "INR", "number", "ratio". `printed` is the text it
    was read from, "" when it was inferred from the line's kind."""

    currency: str | None
    scale: Decimal
    label: str
    # The text it was read from. Not part of a unit's identity: `(₹ crore)`
    # and `(₹ crores)` are the same unit.
    printed: str = field(default="", compare=False)

    @property
    def known(self) -> bool:
        return self.label != UNKNOWN.label


UNKNOWN: Final = ReportingUnit(currency=None, scale=Decimal(1), label="unknown")
RUPEES: Final = ReportingUnit(currency="INR", scale=Decimal(1), label="INR")
NUMBER: Final = ReportingUnit(currency=None, scale=Decimal(1), label="number")
RATIO: Final = ReportingUnit(currency=None, scale=Decimal(1), label="ratio")


def unit_from_text(text: object) -> ReportingUnit | None:
    """The unit a piece of text declares, or None when it declares none. When
    a text declares several, the LAST wins: it is the one nearest the table
    that follows it."""
    if not isinstance(text, str) or not text:
        return None
    found: ReportingUnit | None = None
    for m in _UNIT_RE.finditer(text):
        word = (m.group("s1") or m.group("s2") or "").lower()
        for pattern, label, scale in _SCALES:
            if re.fullmatch(pattern, word, re.IGNORECASE):
                found = ReportingUnit("INR", scale, f"INR {label}", m.group(0).strip())
                break
    if found is None:
        bare = list(_RUPEES_RE.finditer(text))
        if bare:
            found = ReportingUnit("INR", Decimal(1), "INR", bare[-1].group(0))
    return found


def _offset(table: Mapping[str, object]) -> int | None:
    spans = table.get("spans")
    if isinstance(spans, list) and spans and isinstance(spans[0], Mapping):
        offset = spans[0].get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool):
            return offset
    return None


def table_unit(ocr: Mapping[str, object], table_index: int) -> ReportingUnit:
    """The unit the table at `table_index` is printed in: its caption first,
    then the page text just above it. UNKNOWN when neither declares one --
    never a guess, because a wrong scale is a wrong number. Total."""
    tables = ocr.get("tables") if isinstance(ocr, Mapping) else None
    if not isinstance(tables, list) or not (0 <= table_index < len(tables)):
        return UNKNOWN
    table = tables[table_index]
    if not isinstance(table, Mapping):
        return UNKNOWN
    caption = table.get("caption")
    if isinstance(caption, Mapping):
        got = unit_from_text(caption.get("content"))
        if got is not None:
            return got
    content = ocr.get("content")
    offset = _offset(table)
    if isinstance(content, str) and offset is not None:
        # The window runs a little PAST the table's start: Azure DI's table
        # span can begin mid-line, and MEASURED on TCS FY2024-25 it split
        # `(₹ c|rore)` across the boundary, losing every face unit. The header
        # row itself sometimes carries the unit too (`Particulars | ₹ crore`).
        window = content[max(0, offset - _LOOKBACK_CHARS):offset + _LOOKAHEAD_CHARS]
        got = unit_from_text(window)
        if got is not None:
            return got
    return UNKNOWN


def unit_for_kind(kind: str, statement_unit: ReportingUnit) -> ReportingUnit:
    """The unit a line of quantity `kind` (`fdi.core.quantity`) is in, given
    its statement's printed unit. Per-share figures are rupees whatever the
    header says; counts and ratios are not money."""
    if kind == "per_share":
        return RUPEES
    if kind == "count":
        return NUMBER
    if kind == "ratio":
        return RATIO
    if kind == "currency":
        return statement_unit
    return UNKNOWN


def normalise(value: Decimal, unit: ReportingUnit) -> Decimal | None:
    """`value` in absolute units (rupees for INR), or None when the unit is
    unknown -- an unknown scale must never pass for a scale of one."""
    if not unit.known:
        return None
    return value * unit.scale


_MONTHS: Final = {
    m: i + 1 for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    )
}
_DATE_RES: Final = (
    # March 31, 2026
    re.compile(r"(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?P<year>(?:19|20)\d{2})"),
    # 31 March 2026 / 31st March, 2026
    re.compile(r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>[A-Za-z]{3})[a-z]*\.?,?\s+"
               r"(?P<year>(?:19|20)\d{2})"),
)


def _date_in(text: str) -> date | None:
    for pattern in _DATE_RES:
        m = pattern.search(text)
        if m is None:
            continue
        month = _MONTHS.get(m.group("mon").lower()[:3])
        if month is None:
            continue
        try:
            return date(int(m.group("year")), month, int(m.group("day")))
        except ValueError:
            continue
    return None


def column_period_ends(header: Sequence[str]) -> tuple[date, ...]:
    """The period-end dates a statement's column header prints, left to right
    (`Year ended March 31, 2026 | Year ended March 31, 2025`). Cells that print
    no date are skipped. Total."""
    out: list[date] = []
    for cell in header:
        if isinstance(cell, str):
            got = _date_in(cell)
            if got is not None:
                out.append(got)
    return tuple(out)
