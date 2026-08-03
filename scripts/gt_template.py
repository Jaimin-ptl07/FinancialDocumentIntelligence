"""Emit a ground-truth skeleton so keying is CHECKING, not typing.

Every extractable tag in the module starts life under `not_reported`. The human
reads the statement and MOVES each disclosed line into `values` with the number
exactly as printed. Two properties fall out for free: nobody hand-types 402
tags, and `values`/`not_reported` are disjoint by construction -- which is what
`fdi.core.scoring.parse_groundtruth` hard-rejects a violation of.

Usage:
    uv run --directory backend python ../scripts/gt_template.py \
        --schema docs/architecture/field-extraction-schema.v4_3-pl.json \
        --issuer TCS --fy FY2025-26 --basis consolidated \
        --statement profit_and_loss \
        --out docs/groundtruth/TCS-FY2025-26-consolidated-profit_and_loss.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path


def _collect(node: object, out: set[str]) -> None:
    """Depth-first walk collecting every EXTRACTABLE tag.

    A node with a non-empty `fields` list is a group: recurse into it AND still
    consider its own tag, because v4.3 hoists a subtotal's tag onto the
    container it totals -- the parent carries the tag, so skipping groups would
    drop every reported total."""
    if not isinstance(node, Mapping):
        return
    tag = node.get("tag")
    origin = node.get("origin")
    if isinstance(tag, str) and tag and origin != "derived":
        out.add(tag)
    children = node.get("fields")
    if isinstance(children, list):
        for child in children:
            _collect(child, out)


def build_template(
    schema: Mapping[str, object],
    *,
    issuer: str,
    fiscal_year: str,
    basis: str,
    statement: str,
) -> dict[str, object]:
    """A ground-truth skeleton for one issuer/basis/statement. Never raises."""
    tags: set[str] = set()
    modules = schema.get("modules") if isinstance(schema, Mapping) else None
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, Mapping) or module.get("module_id") != statement:
            continue
        units = module.get("retrieval_units")
        for unit in units if isinstance(units, list) else []:
            if not isinstance(unit, Mapping):
                continue
            fields = unit.get("fields")
            for field in fields if isinstance(fields, list) else []:
                _collect(field, tags)

    return {
        "issuer": issuer,
        "fiscal_year": fiscal_year,
        "basis": basis,
        "statement": statement,
        "_instructions": (
            "Read the statement. For every line the issuer ACTUALLY reports, "
            "move its tag from not_reported into values with the number EXACTLY "
            "as printed (no rescaling). Leave the rest in not_reported."
        ),
        "values": {},
        "not_reported": sorted(tags),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--fy", required=True)
    parser.add_argument("--basis", required=True, choices=["consolidated", "standalone"])
    parser.add_argument("--statement", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    template = build_template(
        schema, issuer=args.issuer, fiscal_year=args.fy,
        basis=args.basis, statement=args.statement,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out} with {len(template['not_reported'])} tags to triage")


if __name__ == "__main__":
    main()
