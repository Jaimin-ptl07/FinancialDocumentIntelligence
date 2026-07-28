"""Pure coverage / completeness report -- Milestone 4, Pillar 6.

Because the taxonomy is a FIXED list of ~500 tags, we can say exactly which were
found in the report, which were derived, and which are genuinely absent -- the
completeness assertion a bank auditor tests, inverted into something auditable.
A missing number is visible here, never silent. No I/O; total.
"""

from collections.abc import Mapping, Sequence


def _has_value(facts: Mapping[str, Mapping[str, object]], tag: str) -> bool:
    v = facts.get(tag)
    if not isinstance(v, Mapping):
        return False
    return v.get("value") is not None


def coverage_report(
    statements: Sequence[Mapping[str, object]],
    facts: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Classify every taxonomy tag against the produced Facts.

    Returns per-statement and overall counts plus the explicit lists a bank
    audits: `missing_extracted` (should have been read, wasn't) and
    `unresolved_derived` (a subtotal we could not compute).
    """
    per_statement: list[dict[str, object]] = []
    totals = {
        "expected": 0, "found_extracted": 0, "derived_ok": 0,
        "missing_extracted": 0, "unresolved_derived": 0,
    }
    missing_extracted: list[str] = []
    unresolved_derived: list[str] = []

    for st in statements:
        rows_obj = st.get("rows")
        rows = [r for r in rows_obj if isinstance(r, Mapping)] if isinstance(rows_obj, list) else []
        counts = {
            "expected": 0, "found_extracted": 0, "derived_ok": 0,
            "missing_extracted": 0, "unresolved_derived": 0,
        }
        for r in rows:
            tag = r.get("tag")
            if not isinstance(tag, str) or not tag:
                continue
            if r.get("excluded") or r.get("is_section_header"):
                continue
            origin = r.get("origin")
            counts["expected"] += 1
            present = _has_value(facts, tag)
            if origin == "derived":
                if present:
                    counts["derived_ok"] += 1
                else:
                    counts["unresolved_derived"] += 1
                    unresolved_derived.append(tag)
            elif present:
                counts["found_extracted"] += 1
            else:
                counts["missing_extracted"] += 1
                missing_extracted.append(tag)

        for k in totals:
            totals[k] += counts[k]
        per_statement.append({"statement": st.get("statement"), **counts})

    denom = totals["expected"] or 1
    totals_out: dict[str, object] = dict(totals)
    totals_out["completeness_pct"] = round(
        100.0 * (totals["found_extracted"] + totals["derived_ok"]) / denom, 1
    )
    return {
        "per_statement": per_statement,
        "totals": totals_out,
        "missing_extracted": missing_extracted,
        "unresolved_derived": unresolved_derived,
    }
