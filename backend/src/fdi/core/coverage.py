"""Pure coverage / completeness report -- Milestone 4, Pillar 6.

Because the taxonomy is a FIXED list of ~500 tags, we can say exactly which were
found in the report, which were derived, and which are genuinely absent -- the
completeness assertion a bank auditor tests, inverted into something auditable.
A missing number is visible here, never silent. No I/O; total.
"""

from collections.abc import Collection, Mapping, Sequence


def _has_value(facts: Mapping[str, Mapping[str, object]], tag: str) -> bool:
    v = facts.get(tag)
    if not isinstance(v, Mapping):
        return False
    return v.get("value") is not None


def coverage_report(
    statements: Sequence[Mapping[str, object]],
    facts: Mapping[str, Mapping[str, object]],
    unanswered: Collection[str] = (),
) -> dict[str, object]:
    """Classify every taxonomy tag against the produced Facts.

    Returns per-statement and overall counts plus the explicit lists a bank
    audits: `missing_extracted` (should have been read, wasn't) and
    `unresolved_derived` (a subtotal we could not compute).

    `unanswered` splits `missing_extracted` in two, and the split is the whole
    point. A tag the model was asked about and reported as absent means the
    company does not disclose it -- a CORRECT null. A tag the model never
    returned a key for was dropped by batching (a 76-field unit shares the same
    60-candidate budget as a 25-field one), and reads afterwards as though the
    company disclosed nothing. Reporting both as one number lets our own recall
    loss masquerade as completeness. `not_answered` is therefore a defect
    count, not a coverage statistic.
    """
    per_statement: list[dict[str, object]] = []
    totals = {
        "expected": 0, "found_extracted": 0, "derived_ok": 0,
        "missing_extracted": 0, "unresolved_derived": 0, "not_answered": 0,
    }
    missing_extracted: list[str] = []
    not_answered: list[str] = []
    unanswered_set = {t for t in unanswered if isinstance(t, str)}
    unresolved_derived: list[str] = []

    for st in statements:
        rows_obj = st.get("rows")
        rows = [r for r in rows_obj if isinstance(r, Mapping)] if isinstance(rows_obj, list) else []
        counts = {
            "expected": 0, "found_extracted": 0, "derived_ok": 0,
            "missing_extracted": 0, "unresolved_derived": 0, "not_answered": 0,
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
                if tag in unanswered_set:
                    counts["not_answered"] += 1
                    not_answered.append(tag)

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
        # A defect list, not a coverage statistic: these were never asked-and-
        # answered, so they must not be read as "the company discloses nothing".
        "not_answered": not_answered,
    }
