"""Score a pipeline's output against the hand-keyed ground-truth corpus.

Reads BOTH output shapes -- today's extraction record (`extractions/{doc_id}/
v2/{hash}.json`) and the Phase 2+ FOG artifact -- so the Phase 6 cutover
comparison is apples to apples on one harness.

    uv run --directory backend python ../scripts/score_pipeline.py \
        --gt-dir docs/groundtruth --extraction path/to/record.json --issuer TCS

Exit code is always 0: this is a measurement tool, not a gate. The number it
prints is the gate.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from fdi.core.scoring import GroundTruth, Score, parse_groundtruth, score_document


def facts_from_extraction(record: Mapping[str, object]) -> dict[str, object]:
    """tag -> value out of an extraction record, skipping nulls and untagged
    fields. A null value means ABSENT; collapsing it to 0.0 would turn every
    missed field into a wrong one and destroy the precision signal."""
    out: dict[str, object] = {}
    fields = record.get("fields") if isinstance(record, Mapping) else None
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, Mapping):
            continue
        tag = field.get("tag")
        value = field.get("value")
        if isinstance(tag, str) and tag and value is not None:
            out[tag] = value
    return out


def facts_from_fog(fog: Mapping[str, object], *, basis: str) -> dict[str, object]:
    """tag -> value out of a FOG artifact, via MAPS_TO edges.

    Phase 2 emits LineItems and CONTAINS/REFERENCES edges but NO MAPS_TO --
    mapping is Phase 3. Until then this correctly returns {}: the FOG has values
    but no opinion about which taxonomy concept they are, and inventing that
    opinion here would score the scorer instead of the pipeline."""
    bases = fog.get("bases") if isinstance(fog, Mapping) else None
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    if not isinstance(region, Mapping):
        return {}

    items: dict[str, Mapping[str, object]] = {}
    raw_items = region.get("line_items")
    for item in raw_items if isinstance(raw_items, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            items[str(item["id"])] = item

    out: dict[str, object] = {}
    edges = region.get("edges")
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, Mapping) or edge.get("type") != "MAPS_TO":
            continue
        item = items.get(str(edge.get("source", "")))
        tag = edge.get("target")
        if item is None or not isinstance(tag, str) or not tag:
            continue
        current, prior = item.get("current"), item.get("prior")
        if current is None:
            continue
        out[tag] = {"current": current, "prior": prior}
    return out


def report(rows: Sequence[tuple[GroundTruth, Score]]) -> str:
    """A fixed-width table, one row per ground-truth file, plus a total."""
    if not rows:
        return "no ground truth scored"
    lines = [
        f"{'issuer':<8} {'basis':<13} {'statement':<16} "
        f"{'recall':>8} {'prec':>8} {'ok':>4} {'wrong':>6} {'miss':>5} {'fp':>4}",
        "-" * 82,
    ]
    t_correct = t_reported = t_committed = t_wrong = t_missed = t_fp = 0
    for gt, s in rows:
        lines.append(
            f"{gt.issuer:<8} {gt.basis:<13} {gt.statement:<16} "
            f"{s.recall:>7.1%} {s.precision:>7.1%} {s.correct:>4} "
            f"{s.wrong:>6} {s.missed:>5} {s.false_positive:>4}"
        )
        t_correct += s.correct
        t_reported += s.reported
        t_committed += s.committed
        t_wrong += s.wrong
        t_missed += s.missed
        t_fp += s.false_positive
    lines.append("-" * 82)
    recall = t_correct / t_reported if t_reported else 0.0
    precision = t_correct / t_committed if t_committed else 0.0
    lines.append(
        f"{'TOTAL':<8} {'':<13} {'':<16} {recall:>7.1%} {precision:>7.1%} "
        f"{t_correct:>4} {t_wrong:>6} {t_missed:>5} {t_fp:>4}"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt-dir", required=True, type=Path)
    parser.add_argument("--extraction", type=Path, help="an extraction record JSON")
    parser.add_argument("--fog", type=Path, help="a FOG artifact JSON")
    parser.add_argument("--issuer", help="score only this issuer")
    args = parser.parse_args()

    rows: list[tuple[GroundTruth, Score]] = []
    for path in sorted(args.gt_dir.glob("*.json")):
        gt = parse_groundtruth(json.loads(path.read_text(encoding="utf-8")))
        if gt is None:
            print(f"SKIP malformed {path.name}")
            continue
        if args.issuer and gt.issuer != args.issuer:
            continue
        if args.extraction is not None:
            produced = facts_from_extraction(
                json.loads(args.extraction.read_text(encoding="utf-8"))
            )
        elif args.fog is not None:
            produced = facts_from_fog(
                json.loads(args.fog.read_text(encoding="utf-8")), basis=gt.basis
            )
        else:
            produced = {}
        rows.append((gt, score_document(gt, produced)))

    print(report(rows))


if __name__ == "__main__":
    main()
