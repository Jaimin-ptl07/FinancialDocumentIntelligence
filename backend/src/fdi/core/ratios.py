"""Pure declarative ratio / CARO-metric library over Facts.

A ratio is data: a numerator and denominator, each a list of tags summed with a
sign. `compute_ratios` evaluates every ratio whose inputs are all present,
skipping the rest with a reason -- it never divides by zero and never raises, so
a partial extraction yields the ratios it can and honestly omits the others.

Tag bindings are the customer taxonomy's own codes (verified present in
Q3VENI); they are the canonical "as reported" / derived totals. Confidence
propagates as the MIN over input Facts. Extending the catalogue is a data edit,
not a code change.

References: CARO 2020 clause (xix) and Schedule III require these ratios in the
auditor's report / notes.
"""

from collections.abc import Mapping, Sequence
from typing import Final

# key, label, CARO/Sch-III note, numerator tags, denominator tags, explanation.
RATIOS: Final[list[dict[str, object]]] = [
    {
        "key": "current_ratio",
        "label": "Current Ratio",
        "caro_clause": "xix / Schedule III",
        "numerator": ["TOTCAR"],
        "denominator": ["TOTCLR"],
        "explanation": "Total Current Assets / Total Current Liabilities",
    },
    {
        "key": "debt_equity",
        "label": "Debt-Equity Ratio",
        "caro_clause": "xix / Schedule III",
        "numerator": ["TOTBOR"],
        "denominator": ["TOTEQNR"],
        "explanation": "Total Borrowings / Total Equity",
    },
    {
        "key": "return_on_equity",
        "label": "Return on Equity",
        "caro_clause": "Schedule III",
        "numerator": ["PBMI"],
        "denominator": ["TOTEQNR"],
        "explanation": "Profit for the period / Total Equity",
    },
    {
        "key": "net_profit_margin",
        "label": "Net Profit Margin",
        "caro_clause": "Schedule III",
        "numerator": ["PBMI"],
        "denominator": ["RFONET"],
        "explanation": "Profit for the period / Net Revenue from Operations",
    },
]


def _str_list(v: object) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _sum_tags(
    tags: Sequence[str], values: Mapping[str, Mapping[str, object]]
) -> tuple[float, float] | None:
    """(sum of values, min confidence) over `tags`, or None if any is absent or
    non-numeric -- a ratio computed from a missing input would be a fiction."""
    total = 0.0
    conf = 1.0
    for tag in tags:
        v = values.get(tag)
        if not isinstance(v, Mapping):
            return None
        val = v.get("value")
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return None
        total += float(val)
        c = v.get("confidence")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            conf = min(conf, float(c))
    return total, conf


def compute_ratio(
    spec: Mapping[str, object], values: Mapping[str, Mapping[str, object]]
) -> dict[str, object]:
    """Evaluate one ratio spec against `values` (extracted + derived Facts)."""
    numerator = _str_list(spec["numerator"])
    denominator = _str_list(spec["denominator"])
    num = _sum_tags(numerator, values)
    den = _sum_tags(denominator, values)
    base: dict[str, object] = {
        "key": spec["key"], "label": spec["label"], "caro_clause": spec["caro_clause"],
        "origin": "derived",
        "inputs": {"numerator": numerator, "denominator": denominator},
        "explanation": spec["explanation"],
    }
    if num is None or den is None:
        return {**base, "value": None, "confidence": 0.0, "status": "missing_inputs"}
    if den[0] == 0:
        return {**base, "value": None, "confidence": 0.0, "status": "undefined"}
    return {
        **base,
        "value": num[0] / den[0],
        "confidence": min(num[1], den[1]),
        "status": "ok",
    }


def compute_ratios(values: Mapping[str, Mapping[str, object]]) -> list[dict[str, object]]:
    """Every ratio in the catalogue, evaluated. Pure, total."""
    return [compute_ratio(spec, values) for spec in RATIOS]
