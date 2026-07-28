"""Pure orchestration of the Fact graph for one document: derive subtotals,
compute ratios, and validate -- returning one self-explaining, self-validating
bundle. No I/O; this is what the API layer (Milestone 4) serialises.

`extracted` is the tag-keyed extraction view (fdi.core.extractor.index_by_tag),
one period at a time: {tag: {"value", "confidence"}}. The returned bundle merges
extracted + derived into `facts`, so every downstream consumer (ratios,
validation, XBRL export) reads one map where each entry knows its own origin.
"""

from collections.abc import Mapping, Sequence

from fdi.core.derivation import derive_facts
from fdi.core.ratios import compute_ratios
from fdi.core.validation import validate


def build_factsheet(
    statements: Sequence[Mapping[str, object]],
    extracted: Mapping[str, Mapping[str, object]],
    *,
    tolerance: float = 1.0,
) -> dict[str, object]:
    """Return {facts, derived, ratios, validations, summary}.

    * facts       -- merged {tag: {value, confidence, origin}} (extracted first,
                     derived layered on top for tags that resolved to a value)
    * derived     -- every DerivedFact incl. unresolved/missing (audit trail)
    * ratios      -- every RatioFact (ok / missing_inputs / undefined)
    * validations -- every ValidationResult
    * summary     -- counts a UI/audit report reads at a glance
    """
    derived = derive_facts(statements, extracted)

    facts: dict[str, dict[str, object]] = {}
    for tag, v in extracted.items():
        if isinstance(v, Mapping):
            facts[tag] = {**dict(v), "origin": "extracted"}
    for tag, d in derived.items():
        if d.get("status") == "ok" and d.get("value") is not None:
            facts[tag] = {"value": d["value"], "confidence": d["confidence"], "origin": "derived"}

    ratios = compute_ratios(facts)
    validations = validate(facts, tolerance=tolerance)

    summary = {
        "extracted": sum(1 for f in facts.values() if f["origin"] == "extracted"),
        "derived_ok": sum(1 for d in derived.values() if d["status"] == "ok"),
        "derived_unresolved": sum(1 for d in derived.values() if d["status"] != "ok"),
        "ratios_ok": sum(1 for r in ratios if r["status"] == "ok"),
        "checks_total": len(validations),
        "checks_failed": sum(1 for v in validations if not v["passed"]),
        "checks_failed_error": sum(
            1 for v in validations if not v["passed"] and v["severity"] == "error"
        ),
    }
    return {
        "facts": facts, "derived": derived, "ratios": ratios,
        "validations": validations, "summary": summary,
    }
