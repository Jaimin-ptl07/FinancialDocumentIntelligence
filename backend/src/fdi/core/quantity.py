"""What KIND of quantity a reported line carries.

WHY THIS IS NOT READ FROM THE TAXONOMY
--------------------------------------
Every one of v4.3's 453 typed fields declares `data_type: "decimal"`, so the
schema cannot distinguish a share count from a rupee amount. The distinction has
to be inferred from the line ITEM.

MEASURED: TCS's `EQWVR` (equity share capital, 362 crore) was resolved to the
weighted average share count 361,80,87,518, and `contested` reported a spread of
3.6 BILLION. Both claimants are perfectly valid numbers, so no amount of
arbitration tuning can separate them -- only knowing that one is a COUNT can.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

import re

KINDS = frozenset({"currency", "count", "per_share", "ratio", "unknown"})

# "number of ... shares", "no. of shares", "share count". Deliberately requires
# COUNTING language: "Sale of equity shares" is a rupee amount, not a count.
_COUNT = re.compile(
    r"\b(number|numbers|no\.?|count)\s+of\b|\bshare\s+count\b|\bweighted\s+average\s+number\b",
    re.IGNORECASE,
)
_PER_SHARE = re.compile(
    r"\bper\s+(equity\s+)?share\b|\bper\s+unit\b|\bEPS\b|\bface\s+value\b"
    r"|\b(basic|diluted)\b.*\(in\s*rs\.?\)",
    re.IGNORECASE,
)
_RATIO = re.compile(r"%|\bratio\b|\brate\b|\bpercentage\b|\byield\b", re.IGNORECASE)


def classify_quantity(label: str, value: float | None) -> str:
    """The kind of quantity `label` reports.

    The LABEL decides, never the magnitude: a large count and a large amount are
    both large, and letting size arbitrate would misclassify a genuinely big
    rupee figure. `value` is accepted for future refinement and deliberately
    unused today rather than used badly.

    An unlabelled row is "unknown", NOT "currency" -- defaulting to currency
    would let any stray number satisfy a monetary concept, which is the failure
    this function exists to prevent. Total: never raises."""
    if not isinstance(label, str) or not label.strip():
        return "unknown"
    if _COUNT.search(label):
        return "count"
    if _PER_SHARE.search(label):
        return "per_share"
    if _RATIO.search(label):
        return "ratio"
    return "currency"
