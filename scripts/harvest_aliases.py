"""Propose report-side aliases for face captions the taxonomy cannot currently
match -- a REVIEW LIST for a human, never an edit to the taxonomy itself.

MEASURED ON TCS: `EQWVR` (equity share capital) expected 362, got 1.0. The
face row 'Share capital' is not a claimant at all -- the concept "Equity
share capital with voting rights" has no alias matching what TCS actually
prints, so an unrelated note row wins by default. No tier or arbitration
change can reach this; it is purely alias coverage.

An alias asserts that a printed caption MEANS a taxonomy concept. That is a
semantic judgement a script must not make unsupervised: inventing aliases
from string similarity is how a wrong mapping becomes permanent and
invisible to every later measurement -- the schema would then teach the
error to every future run. This module only PROPOSES: for each face label
the free lexical tier does not already resolve with CERTAINTY, it lists the
taxonomy concepts whose names are closest, as evidence for a human to
confirm or reject. It never writes to a schema file; a later, separate
step (not this one) applies what a human confirms.

WHY "CERTAINTY" AND NOT JUST "resolve_lexical says something": the real
mapping pipeline's `LEXICAL_FLOOR` (0.75) also accepts a token-overlap
guess, which `fdi.core.mapping.lexical_match` itself calls "a judgement"
(only an exact name or exact alias hit is "certain"). A judgement-tier hit
is exactly the shaky ground this harvester exists to firm up with a real,
curated alias -- treating it as settled would hide the coverage gap behind
a coincidental token overlap on the concept's own name. So a label is only
skipped here when the free tier resolved it with a score of 0.95 or above:
an exact name match (1.0) or an exact alias match (0.95).

Only FACE rows (`origin == "face"`) are candidates: a note's rows are
detail, and aliasing them would bind a concept to one issuer's schedule
wording rather than the caption an auditor actually recognises.

Pure and total, like `fdi.core`: `propose_aliases` does no I/O, malformed
input returns `[]`, and it never raises. `main()` is the only place that
touches a filesystem.

    uv run --directory backend python ../scripts/harvest_aliases.py \\
        --fog ../out/fog-run-llm-p6b/TCS/fog_objects.json \\
        --schema ../docs/architecture/field-extraction-schema.v4_3.json \\
        --basis consolidated --out ../out/alias-proposals-TCS.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from fdi.core.concepts import ConceptCandidate, all_concepts, build_concept_index
from fdi.core.fog_mapping import MapUnit, resolve_lexical, units_from_fog
from fdi.core.mapping import lexical_match

# How many ranked concepts to carry per proposal -- enough for a human to
# weigh the runner-up without wading through the whole taxonomy.
_MAX_RANKED = 5

# A resolution below this is "a judgement" in `lexical_match`'s own words,
# not certainty; see the module docstring for why that is not treated as
# alias coverage here.
_CERTAIN = 0.95


def _note_candidates(schema: Mapping[str, object]) -> tuple[ConceptCandidate, ...]:
    """Mirrors `fdi.pipeline.stages.fog_mapping._note_candidates`: every
    tagged concept, since note rows are DETAIL and match descendants.

    Kept as a local copy rather than an import: `scripts/` depends on
    `fdi.core` only, and this one small function is cheap to keep in sync
    by hand. It only matters here because `units_from_fog` needs a note
    candidate set to build note `MapUnit`s at all -- this harvester never
    proposes for a note row, so which concepts fill it does not otherwise
    affect the output."""
    index = build_concept_index(schema)
    seen: dict[str, ConceptCandidate] = {}
    for group in index.groups:
        for candidate in group.candidates:
            seen.setdefault(candidate.tag, candidate)
    return tuple(seen.values()) or all_concepts(schema)


def _rank_candidates(
    label: str, candidates: Sequence[ConceptCandidate]
) -> list[dict[str, object]]:
    """The concepts closest to `label`, best first, via repeated
    `lexical_match`: each round removes the winner and asks again, turning
    `lexical_match`'s single best-or-refuse answer into a ranking. A tie
    ends the ranking early -- the same "refuse rather than guess" rule
    `lexical_match` applies to one answer applies here to where it stops."""
    pool = list(candidates)
    out: list[dict[str, object]] = []
    while pool and len(out) < _MAX_RANKED:
        cand, _method, score = lexical_match(label, pool)
        if cand is None:
            break
        out.append({"tag": cand.tag, "name": cand.name, "score": round(float(score), 3)})
        pool = [c for c in pool if c.tag != cand.tag]
    return out


def _certainly_mapped_ids(units: Sequence[MapUnit]) -> set[str]:
    """Line item ids the free lexical tier resolves with CERTAINTY, over
    FACE units only -- a note unit resolving something tells us nothing
    about a face row's coverage. See the module docstring for the 0.95
    threshold."""
    out: set[str] = set()
    for unit in units:
        if unit.kind != "face":
            continue
        for row_id, resolution in resolve_lexical(unit).items():
            if resolution.score >= _CERTAIN:
                out.add(row_id)
    return out


def _statement_titles(region: Mapping[str, object]) -> dict[str, str]:
    """statement node id -> the name a proposal should show as `statement`:
    the statement's own name where present, its title otherwise -- the same
    fallback `fdi.core.fog_mapping.units_from_fog` uses for a unit's title."""
    out: dict[str, str] = {}
    raw = region.get("statements")
    for node in raw if isinstance(raw, list) else []:
        if not isinstance(node, Mapping):
            continue
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id:
            title = node.get("statement") or node.get("title") or ""
            out[node_id] = title if isinstance(title, str) else ""
    return out


def propose_aliases(
    fog: Mapping[str, object], schema: Mapping[str, object], *, basis: str
) -> list[dict[str, object]]:
    """For each FACE label the free lexical tier does not certainly resolve,
    the taxonomy concepts whose names are closest -- a REVIEW LIST, sorted
    by label, for a human to confirm before anything is applied.

    Total: malformed `fog`/`schema` shapes, an unknown `basis`, or a schema
    with no tagged concepts all fall through to `[]` rather than raising."""
    if not isinstance(fog, Mapping) or not isinstance(schema, Mapping):
        return []
    bases = fog.get("bases")
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    if not isinstance(region, Mapping):
        return []

    face_candidates = all_concepts(schema)
    if not face_candidates:
        return []
    note_candidates = _note_candidates(schema)

    units = units_from_fog(
        region, face_candidates=face_candidates, note_candidates=note_candidates,
        basis=basis,
    )
    mapped_ids = _certainly_mapped_ids(units)
    statement_of = _statement_titles(region)

    items = region.get("line_items")
    proposals: list[dict[str, object]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping) or item.get("origin") != "face":
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label:
            continue
        item_id = item.get("id")
        key = item_id if isinstance(item_id, str) and item_id else label
        if key in mapped_ids:
            continue

        section_path = item.get("section_path")
        parent = item.get("parent")
        proposals.append({
            "label": label,
            "value": item.get("current"),
            "section_path": list(section_path) if isinstance(section_path, list) else [],
            "statement": statement_of.get(parent, "") if isinstance(parent, str) else "",
            "candidates": _rank_candidates(label, face_candidates),
        })

    proposals.sort(key=lambda p: str(p["label"]))
    return proposals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fog", required=True, type=Path, help="a FOG artifact JSON")
    parser.add_argument("--schema", required=True, type=Path, help="the taxonomy schema JSON")
    parser.add_argument("--basis", required=True, help="consolidated | standalone | unknown | ...")
    parser.add_argument("--out", required=True, type=Path, help="where to write the review list")
    args = parser.parse_args()

    fog = json.loads(args.fog.read_text(encoding="utf-8"))
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    proposals = propose_aliases(fog, schema, basis=args.basis)
    args.out.write_text(
        json.dumps(proposals, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(proposals)} proposal(s) written to {args.out}")


if __name__ == "__main__":
    main()
