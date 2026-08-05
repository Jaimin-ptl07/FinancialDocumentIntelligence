"""Compose, validate, calibrate and cover a MAPPED Financial Object Graph.

Pure and free -- every input is already on the graph. That is the point of
putting the model in one stage upstream: a new ratio, a changed tolerance or a
taxonomy edit re-runs all of this at zero marginal cost.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fdi.core.compose import (
    Composed,
    certify,
    compose,
    read_formulas,
    reconcile,
    summarise,
)
from fdi.core.confidence import calibrate
from fdi.core.coverage import coverage_report
from fdi.core.factsheet import rows_from_schema
from fdi.core.validation import validate


def _region(doc: Mapping[str, object], basis: str) -> Mapping[str, object]:
    bases = doc.get("bases") if isinstance(doc, Mapping) else None
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    return region if isinstance(region, Mapping) else {}


def facts_from_mapping(
    fog: Mapping[str, object], mapping: Mapping[str, object], *, basis: str
) -> dict[str, float]:
    """tag -> current-year value, ARBITRATED when more than one line claims it.

    A line item with NO figure yields no fact. A disclosed nil is not a zero,
    and emitting 0.0 would let it satisfy a reported total's reconciliation and
    claim the books tie. Total: never raises."""
    return {tag: claim.value for tag, claim in _arbitrate(fog, mapping, basis).items()}


@dataclass(frozen=True)
class _Claim:
    """One line item's bid for a tag, and how strong the bid is."""

    tag: str
    value: float
    line_item: str
    label: str
    origin: str
    method: str
    score: float

    @property
    def rank(self) -> tuple[int, int, float, str]:
        """Higher is better. Ties break on the line-item id so the winner is
        stable across runs -- this reaches an artifact body.

        EVIDENCE OUTRANKS POSITION. Ordering origin first meant a face row's
        token-overlap GUESS (tier 1) beat a note row's EXACT hit (tier 3),
        resurrecting the very scar this arbitration was written to fix for
        every concept whose authoritative row lives in a note. Being on the
        face is a tie-breaker between equally-good matches, not a licence
        to override a better one."""
        return (_tier(self.method, self.score), _ORIGIN_RANK.get(self.origin, 0),
                self.score, self.line_item)


# The FACE is the issuer's own summary and outranks a note or a cash-flow line.
# MEASURED on TCS: "Right-of-use assets" 11,027 on the balance sheet lost to
# "Payment including advances for acquiring right-of-use assets" -109 in the
# cash flow, purely because the latter sorted later.
_ORIGIN_RANK = {"face": 2, "note": 1}


def _tier(method: str, score: float) -> int:
    """How much a resolution is worth, on evidence rather than on tier name.

    `lexical_match` returns method "name" for BOTH an exact hit (score 1.0) and
    a token-overlap guess (score <= 0.9), so the method string alone cannot tell
    them apart -- only the score can. Ranking on the string would let a fuzzy
    overlap outrank the model's considered answer.

    MEASURED on TCS: "Income tax assets (net)" token-overlapped onto NETDTA
    (Deferred Tax Assets) and overwrote the correct 4,465 with 1,259."""
    if method == "memory":
        return 3
    if method.startswith("lexical") and score >= _EXACT_SCORE:
        return 3          # an exact name or alias hit is certain
    if method == "llm":
        return 2          # a judgement, and a good one -- 69% -> 90% measured
    return 1              # token overlap: a guess, and the weakest evidence


_EXACT_SCORE = 0.95


def _arbitrate(
    fog: Mapping[str, object], mapping: Mapping[str, object], basis: str
) -> dict[str, _Claim]:
    """The winning claim per tag.

    Several line items legitimately mention one concept -- a face row and the
    note that breaks it down are CORROBORATION, not a conflict. What must never
    happen is the arbitrary winner the old `values[tag] = ...` produced, where
    whichever edge sorted last silently replaced a correct face value."""
    items_raw = _region(fog, basis).get("line_items")
    by_id: dict[str, Mapping[str, object]] = {}
    for item in items_raw if isinstance(items_raw, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            by_id[str(item["id"])] = item

    region = _region(mapping, basis)
    proof_raw = region.get("proof")
    detail: dict[str, tuple[str, float]] = {}
    for record in proof_raw if isinstance(proof_raw, list) else []:
        if isinstance(record, Mapping) and isinstance(record.get("line_item"), str):
            raw_score = record.get("score")
            detail[str(record["line_item"])] = (
                str(record.get("method", "")),
                float(raw_score) if isinstance(raw_score, int | float) else 0.0,
            )

    best: dict[str, _Claim] = {}
    edges_raw = region.get("edges")
    for edge in edges_raw if isinstance(edges_raw, list) else []:
        if not isinstance(edge, Mapping) or edge.get("type") != "MAPS_TO":
            continue
        source, tag = edge.get("source"), edge.get("target")
        if not isinstance(source, str) or not isinstance(tag, str) or not tag:
            continue
        item = by_id.get(source)
        if item is None:
            continue
        current = item.get("current")
        if isinstance(current, bool) or not isinstance(current, int | float):
            continue
        method, score = detail.get(source, ("", 0.0))
        claim = _Claim(
            tag=tag, value=float(current), line_item=source,
            label=str(item.get("label", "")), origin=str(item.get("origin", "")),
            method=method, score=score,
        )
        incumbent = best.get(tag)
        if incumbent is None or claim.rank > incumbent.rank:
            best[tag] = claim
    return best


def contested_tags(
    fog: Mapping[str, object], mapping: Mapping[str, object], *, basis: str
) -> list[dict[str, object]]:
    """Tags more than one line item claimed, with the winner, the losers, and
    THE VALUES THEY DISAGREED BY.

    Corroboration and mis-mapping look identical from the fact map alone, so
    the competition is recorded rather than discarded -- `absent != missed`
    applies to how a value was CHOSEN, not only to whether one exists.

    Recording only the LABELS was not enough: a face row and its note agreeing
    on 11,027 and a face row losing 11,027 to an unrelated -109 produced the
    same record. `spread` is the gap between the highest and lowest claim --
    near zero means the sources corroborate, large means one of them is wrong
    and a human should look."""
    winners = _arbitrate(fog, mapping, basis)
    items_raw = _region(fog, basis).get("line_items")
    labels: dict[str, str] = {}
    values: dict[str, float] = {}
    for i in items_raw if isinstance(items_raw, list) else []:
        if not isinstance(i, Mapping) or not isinstance(i.get("id"), str):
            continue
        labels[str(i["id"])] = str(i.get("label", ""))
        current = i.get("current")
        if not isinstance(current, bool) and isinstance(current, int | float):
            values[str(i["id"])] = float(current)
    counts: dict[str, list[str]] = {}
    edges_raw = _region(mapping, basis).get("edges")
    for edge in edges_raw if isinstance(edges_raw, list) else []:
        if not isinstance(edge, Mapping) or edge.get("type") != "MAPS_TO":
            continue
        source, tag = edge.get("source"), edge.get("target")
        if isinstance(source, str) and isinstance(tag, str) and source in labels:
            counts.setdefault(tag, []).append(source)

    out: list[dict[str, object]] = []
    for tag in sorted(counts):
        claimants = sorted(counts[tag])
        if len(claimants) < 2:
            continue
        winner = winners.get(tag)
        claimed = [values[c] for c in claimants if c in values]
        out.append({
            "tag": tag,
            "claimants": len(claimants),
            "winner": winner.line_item if winner else "",
            "winner_label": winner.label if winner else "",
            "winner_value": winner.value if winner else None,
            # Dicts are not orderable -- sort by the line-item id, which is
            # already the stable identity everything else keys on.
            "losers": [
                {"label": labels[c], "value": values.get(c)}
                for c in claimants
                if winner is None or c != winner.line_item
            ],
            # 0 => the sources agree (corroboration). Large => one is wrong.
            "spread": round(max(claimed) - min(claimed), 4) if claimed else None,
        })
    return out


def _nested_facts(
    facts: Mapping[str, float], composed: Mapping[str, Composed]
) -> dict[str, dict[str, object]]:
    """Lift the FLAT `{tag: value}` fact map into the NESTED `{tag: {"value":
    ...}}` shape `validation`, `confidence` and `coverage` all read.

    `facts_from_mapping` returns a flat map; `validation._num` and
    `coverage._has_value` both read an inner "value" key specifically, so
    handing an engine the flat map directly would make it see zero usable
    facts and silently report nothing to check or cover -- exactly the
    failure class this wiring exists to eliminate.

    A composed (schema-derived) value is folded in ONLY when it is fully
    supported (`coverage >= 1.0`): a PARTIAL sum must never stand in for a
    fact a reported total's reconciliation gets checked against, or an
    incomplete number could silently satisfy validation and claim the books
    tie -- the same measured rule `compose.py` enforces for `composed`
    itself, re-applied here at the point a derived value re-enters a check."""
    out: dict[str, dict[str, object]] = {
        tag: {"value": value, "origin": "extracted"} for tag, value in facts.items()
    }
    for tag, c in composed.items():
        if tag not in out and c.coverage >= 1.0:
            out[tag] = {"value": c.value, "origin": "derived"}
    return out


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

    # Both engines read the NESTED shape (`_nested_facts`'s docstring says
    # why), merging in only fully-supported composed values so a partial sum
    # can never masquerade as a fact a check or a coverage count relies on.
    merged = _nested_facts(facts, composed)
    checks = sorted(validate(merged), key=lambda c: str(c.get("rule_id", "")))
    calibration = calibrate(merged, checks)
    confidence_body = {tag: calibration[tag] for tag in sorted(calibration)}

    statements: list[dict[str, object]] = (
        rows_from_schema(schema) if isinstance(schema, Mapping) else []
    )
    coverage_body = coverage_report(statements, merged)

    # `summary` grades each REPORTED total against its own formula -- a
    # separate question from the rule-based `checks` above. `certify` lets a
    # total that already ties prove its own missing children absent WITHOUT
    # ever upgrading a composed record's own coverage figure (that stays
    # honest in `composed_body`); `reconcile` reads that proof and `summarise`
    # counts it.
    certified_composed, certified_tags = certify(facts, formulas, composed)
    composed_checks = reconcile(facts, formulas, certified_composed, certified=certified_tags)

    return {
        "facts": dict(sorted(facts.items())),
        # Recorded, not discarded: a face row and its note are CORROBORATION,
        # a face row and an unrelated fuzzy match are a MIS-MAPPING, and the
        # fact map alone cannot tell them apart.
        "contested": contested_tags(fog, mapping, basis=basis),
        "composed": composed_body,
        "checks": checks,
        "coverage": coverage_body,
        "confidence": confidence_body,
        "summary": summarise(composed_checks),
    }
