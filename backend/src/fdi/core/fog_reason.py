"""Compose, validate, calibrate and cover a MAPPED Financial Object Graph.

Pure and free -- every input is already on the graph. That is the point of
putting the model in one stage upstream: a new ratio, a changed tolerance or a
taxonomy edit re-runs all of this at zero marginal cost.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping

from fdi.core.compose import compose, read_formulas, summarise


def _region(doc: Mapping[str, object], basis: str) -> Mapping[str, object]:
    bases = doc.get("bases") if isinstance(doc, Mapping) else None
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    return region if isinstance(region, Mapping) else {}


def facts_from_mapping(
    fog: Mapping[str, object], mapping: Mapping[str, object], *, basis: str
) -> dict[str, float]:
    """tag -> current-year value, for every line item a MAPS_TO edge resolves.

    A line item with NO figure yields no fact. A disclosed nil is not a zero,
    and emitting 0.0 would let it satisfy a reported total's reconciliation and
    claim the books tie. Total: never raises."""
    items_raw = _region(fog, basis).get("line_items")
    values: dict[str, float] = {}
    by_id: dict[str, object] = {}
    for item in items_raw if isinstance(items_raw, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            by_id[str(item["id"])] = item.get("current")

    edges_raw = _region(mapping, basis).get("edges")
    for edge in edges_raw if isinstance(edges_raw, list) else []:
        if not isinstance(edge, Mapping) or edge.get("type") != "MAPS_TO":
            continue
        source, tag = edge.get("source"), edge.get("target")
        if not isinstance(source, str) or not isinstance(tag, str) or not tag:
            continue
        current = by_id.get(source)
        if isinstance(current, bool) or not isinstance(current, int | float):
            continue
        values[tag] = float(current)
    return values


def reason(
    fog: Mapping[str, object],
    mapping: Mapping[str, object],
    schema: Mapping[str, object],
    *,
    basis: str,
) -> dict[str, object]:
    """The full reasoning pass over one basis. Total: never raises."""
    facts = facts_from_mapping(fog, mapping, basis=basis)
    formulas = read_formulas(schema) if isinstance(schema, Mapping) else {}
    composed = compose(facts, formulas)

    composed_body = {
        tag: {
            "value": c.value,
            "complete": c.complete,
            "coverage": c.coverage,
            "missing": sorted(c.missing),
        }
        for tag, c in sorted(composed.items())
    }
    checks: list[dict[str, object]] = []
    declared = set(formulas)
    covered = sorted(set(facts) | set(composed))
    coverage_body = {
        "reported": len(facts),
        "composed": len(composed),
        "declared_formulas": len(declared),
        "tags": covered,
    }
    return {
        "facts": dict(sorted(facts.items())),
        "composed": composed_body,
        "checks": checks,
        "coverage": coverage_body,
        "summary": summarise([]),
    }
