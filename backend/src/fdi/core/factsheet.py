"""Pure orchestration of the Fact graph for one document: derive subtotals,
compute ratios, and validate -- returning one self-explaining, self-validating
bundle. No I/O; this is what the API layer (Milestone 4) serialises.

`extracted` is the tag-keyed extraction view (fdi.core.extractor.index_by_tag),
one period at a time: {tag: {"value", "confidence"}}. The returned bundle merges
extracted + derived into `facts`, so every downstream consumer (ratios,
validation, XBRL export) reads one map where each entry knows its own origin.
"""

from collections.abc import Mapping, Sequence

from fdi.core.confidence import calibrate
from fdi.core.coverage import coverage_report
from fdi.core.derivation import derive_facts
from fdi.core.ratios import compute_ratios
from fdi.core.validation import validate


def rows_from_schema(schema: Mapping[str, object]) -> list[dict[str, object]]:
    """Reconstruct the derivation/coverage row view from a v4 field schema, so a
    factsheet can be built from (schema, extraction records) alone -- no separate
    normalized-taxonomy file at runtime. Walks modules -> units -> nested fields,
    emitting one row per tagged leaf (extracted or derived) with the block_id
    (unit_id), sr_no, particulars and origin the engines read."""
    statements: list[dict[str, object]] = []
    modules = schema.get("modules")
    if not isinstance(modules, list):
        return statements
    for m in modules:
        if not isinstance(m, Mapping):
            continue
        rows: list[dict[str, object]] = []
        units = m.get("retrieval_units")
        if isinstance(units, list):
            for u in units:
                if not isinstance(u, Mapping):
                    continue
                block = u.get("unit_id")
                fields = u.get("fields")
                if isinstance(fields, list):
                    _walk_schema_fields(fields, block if isinstance(block, str) else "", rows)
        statements.append({"statement": m.get("statement") or m.get("key"), "rows": rows})
    return statements


def _walk_schema_fields(
    fields: Sequence[object], block: str, out: list[dict[str, object]]
) -> None:
    for f in fields:
        if not isinstance(f, Mapping):
            continue
        children = f.get("fields")
        tag = f.get("tag")
        # Emit a row for EVERY tagged node, parents included -- a derived parent
        # (e.g. RM = its Current/Non-Current children) must still be computed.
        if isinstance(tag, str) and tag:
            out.append(_schema_row(f, block, tag))
        if isinstance(children, list) and children:
            _walk_schema_fields(children, block, out)


def _schema_row(f: Mapping[str, object], block: str, tag: str) -> dict[str, object]:
    row: dict[str, object] = {
        "block_id": block,
        "tag": tag,
        "sr_no": f.get("sr_no"),
        "particulars": f.get("field_name"),
        "origin": f.get("origin", "extracted"),
        "excluded": False,
        "is_section_header": False,
    }
    # Carry a v4.1 node's pre-resolved formula so derivation uses it directly
    # instead of re-parsing sr_no ranges.
    deriv = f.get("derivation")
    if isinstance(deriv, Mapping) and deriv.get("inputs"):
        inputs = deriv.get("inputs")
        row["inputs"] = list(inputs) if isinstance(inputs, list) else []
        row["method"] = deriv.get("method", "sum")
    return row


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
    calibration = calibrate(facts, validations)

    # Fold the calibrated confidence + band back onto each Fact so a consumer
    # reads one object per number.
    for tag, cal in calibration.items():
        if tag in facts:
            facts[tag]["confidence"] = cal["confidence"]
            facts[tag]["band"] = cal["band"]

    bands = {"high": 0, "needs_review": 0, "low": 0, "failed": 0}
    for cal in calibration.values():
        band = cal["band"]
        if isinstance(band, str) and band in bands:
            bands[band] += 1

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
        "bands": bands,
    }
    return {
        "facts": facts, "derived": derived, "ratios": ratios,
        "validations": validations, "calibration": calibration, "summary": summary,
    }


def document_factsheet(
    schema: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    *,
    tolerance: float = 1.0,
) -> dict[str, object]:
    """End-to-end factsheet from a v4 schema + extraction records (the API entry
    point). `records` are extraction output_contract records ({tag, value,
    confidence, ...}); only extracted, tagged, valued records seed the Fact map,
    and the derivation engine produces the rest. Adds a coverage report."""
    statements = rows_from_schema(schema)
    extracted: dict[str, dict[str, object]] = {}
    for rec in records:
        tag = rec.get("tag")
        if not isinstance(tag, str) or not tag or rec.get("origin") == "derived":
            continue
        if rec.get("value") is None:
            continue
        extracted[tag] = {"value": rec.get("value"), "confidence": rec.get("confidence", 0.0)}

    bundle = build_factsheet(statements, extracted, tolerance=tolerance)
    facts = bundle["facts"]
    if isinstance(facts, dict):
        bundle["coverage"] = coverage_report(statements, facts)
    return bundle
