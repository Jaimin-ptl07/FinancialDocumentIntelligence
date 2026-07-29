"""Pure financial validation: assertions over the Fact value map.

Three families, each result fully traceable (rule, tags, expected vs actual,
delta, tolerance, severity, passed):

* reconciliation -- an "as reported" total must equal the sum-of-parts the
  derivation engine computed for the same concept (footing / tie-out);
* identity       -- cross-statement accounting identities (Assets = Liabilities
  + Equity), seeded conservatively and marked `warn` until verified on a report;
* domain         -- a reported TOTAL that is negative is worth a human's eyes.

Tolerance is absolute in INR Lakhs (rounding). Total: a missing input skips its
rule rather than firing a false failure; never raises.
"""

from collections.abc import Mapping, Sequence
from typing import Final

# reported "as reported" total  vs  derived sum-of-parts of the SAME concept.
RECONCILIATIONS: Final[list[dict[str, object]]] = [
    {"rule_id": "recon_current_assets", "reported": "TOTCAR", "computed": "TOTA",
     "label": "Total Current Assets: reported vs sum of parts"},
    {"rule_id": "recon_retained_earnings", "reported": "TOTREREP", "computed": "TOTRE/SOPL",
     "label": "Retained Earnings: reported vs computed"},
]

# Seeded conservatively at `warn` -- tag bindings need report verification.
IDENTITIES: Final[list[dict[str, object]]] = [
    {"rule_id": "identity_balance_sheet", "left": ["TOTNCNCA"], "right": ["TOTL", "TOTEQNR"],
     "label": "Total Assets = Total Liabilities + Equity", "severity": "warn"},
]


def _str_list(v: object) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _num(values: Mapping[str, Mapping[str, object]], tag: str) -> float | None:
    v = values.get(tag)
    if not isinstance(v, Mapping):
        return None
    val = v.get("value")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return None


def _sum(values: Mapping[str, Mapping[str, object]], tags: Sequence[str]) -> float | None:
    total = 0.0
    for t in tags:
        n = _num(values, t)
        if n is None:
            return None
        total += n
    return total


def _result(
    rule_id: str, kind: str, tags: Sequence[str], expected: float, actual: float,
    tolerance: float, severity: str,
) -> dict[str, object]:
    delta = expected - actual
    return {
        "rule_id": rule_id, "kind": kind, "tags": list(tags),
        "expected": expected, "actual": actual, "delta": delta,
        "tolerance": tolerance, "severity": severity, "passed": abs(delta) <= tolerance,
    }


def validate(
    values: Mapping[str, Mapping[str, object]], *, tolerance: float = 1.0
) -> list[dict[str, object]]:
    """Every applicable check over the merged (extracted + derived) Fact map."""
    results: list[dict[str, object]] = []

    for rec in RECONCILIATIONS:
        reported_tag, computed_tag = str(rec["reported"]), str(rec["computed"])
        reported = _num(values, reported_tag)
        computed = _num(values, computed_tag)
        if reported is None or computed is None:
            continue
        results.append(_result(
            str(rec["rule_id"]), "reconciliation", [reported_tag, computed_tag],
            reported, computed, tolerance, "error",
        ))

    for idy in IDENTITIES:
        left_tags, right_tags = _str_list(idy["left"]), _str_list(idy["right"])
        left = _sum(values, left_tags)
        right = _sum(values, right_tags)
        if left is None or right is None:
            continue
        results.append(_result(
            str(idy["rule_id"]), "identity", [*left_tags, *right_tags],
            left, right, tolerance, str(idy.get("severity", "warn")),
        ))

    for tag in values:
        if not tag.startswith("TOT"):
            continue
        n = _num(values, tag)
        if n is not None and n < 0:
            results.append({
                "rule_id": f"domain_negative_total:{tag}", "kind": "domain", "tags": [tag],
                "expected": ">= 0", "actual": n, "delta": n, "tolerance": tolerance,
                "severity": "warn", "passed": False,
            })

    return results
