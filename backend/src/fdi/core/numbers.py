"""Deterministic parsing of amounts as they are printed in Indian filings.

WHY THIS IS NOT THE MODEL'S JOB
-------------------------------
Number parsing is fully decidable, and delegating it was measurably wrong. When
the extraction prompt asked for values directly, TCS's `8,685` came back as `8`
and `1,833` as `1.833` -- the model read the digit-group separator as a decimal
point (European convention). Adding prompt guidance did not fix it. RITES, whose
figures carry BOTH separators (`14,372.80`), parsed correctly for the opposite
reason: there the comma is unambiguous.

So the rule is: the model may decide WHICH cell answers a concept; it never
decides what the characters in that cell mean.

WHAT REAL FILINGS PRINT
-----------------------
    2,20,938     Indian lakh/crore grouping   -> 220938
    14,372.80    grouping AND a decimal       -> 14372.8
    (468)        accounting negative          -> -468
    (2,681.75)   both                         -> -2681.75
    -            a nil marker, not a minus    -> None
    .*  *  NA    "less than half a unit" / not applicable -> None
    1,10,270 Cr  a trailing unit word         -> 110270

Pure (`fdi.core` law): no I/O, deterministic, total -- every input returns either
a number or None, and nothing raises.
"""

from __future__ import annotations

import re

# A nil/￮ marker. Filings use a bare dash for "nil" and a starred dash for
# "less than half the reporting unit" -- neither is a number, and reading either
# as 0 would silently invent a value.
_NIL = {"", "-", "--", "---", "–", "—", "*", ".*", "-*", "*-",
        "na", "n/a", "nil", "not applicable"}

# Trailing unit words that may follow a figure in a cell.
_UNIT_SUFFIX = re.compile(
    r"\s*(cr|crs|crore|crores|lakh|lakhs|lac|lacs|mn|million|millions|bn|billion|"
    r"thousand|thousands|000s)\.?\s*$",
    re.IGNORECASE,
)
_CURRENCY = str.maketrans("", "", "₹$£€")
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def parse_amount(text: object) -> float | None:
    """A printed amount as a number, or None when the cell holds no amount.

    Never raises. A cell that is not an amount -- a label, a nil marker, a date,
    a footnote reference -- returns None rather than a guess."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None

    raw = text.strip()
    if raw.lower() in _NIL:
        return None

    # Accounting negatives are parenthesised. Detect BEFORE stripping, so that a
    # cell like "(2,681.75)" is not silently read as positive.
    negative = raw.startswith("(") and raw.endswith(")")

    body = raw.strip("()").strip()
    body = body.translate(_CURRENCY)
    body = _UNIT_SUFFIX.sub("", body)
    body = body.replace(" ", "")

    # Commas are ALWAYS digit-group separators here, never decimal points:
    # Indian filings group as 2,20,938 and use "." for decimals. Removing them
    # is what makes 8,685 -> 8685 instead of 8.685 -> 8.
    body = body.replace(",", "")
    body = body.replace("−", "-")            # unicode minus

    if body.lower() in _NIL or not _NUMERIC.match(body):
        return None

    value = float(body)
    if negative:
        value = -abs(value)
    return value


def is_nil_marker(text: object) -> bool:
    """True when a cell EXPLICITLY prints nil -- `-`, `–`, `NIL`, `N/A`, `*`.

    An EMPTY cell is NOT a nil marker: it carries no assertion at all. That
    distinction is what separates a CAPTION from a nil row, and getting it wrong
    is not a small loss. A caption is structurally a heading -- it has no amount
    cells, so the label is the whole row -- and the caption is carried onto every
    row beneath it. A nil row (`Communication | - | -`) HAS amount cells; the
    issuer filled them in with an explicit nil. Classifying it as a caption
    re-labels the block for every following row, so `Deletions` gets recorded
    under "Communication" instead of "Gross block": an affirmatively false
    provenance claim, which is worse than the unknown it replaces.

    `parse_amount` cannot answer this: it returns None for a nil marker and for
    an empty cell alike, deliberately, because neither is a number."""
    if not isinstance(text, str):
        return False
    raw = text.strip().lower()
    return bool(raw) and raw in _NIL


def looks_numeric(text: object) -> bool:
    """True when a cell holds an amount. Used to tell a VALUE ROW from a CAPTION:
    a row with no amount in any column is a section header, not a concept, and
    mapping one produced several wrong assignments before this check existed."""
    return parse_amount(text) is not None


def row_amounts(cells: dict[int, str], label_col: int = 0) -> list[float]:
    """Every amount in a table row, left to right, skipping the label column.

    Column order is the period order in a statement: the first amount is the
    current year, the second the prior year."""
    out: list[float] = []
    for col in sorted(c for c in cells if c != label_col):
        value = parse_amount(cells.get(col))
        if value is not None:
            out.append(value)
    return out
