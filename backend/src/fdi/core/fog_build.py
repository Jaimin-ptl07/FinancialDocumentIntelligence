"""Build the Financial Object Graph's indices and nodes from OCR artifacts.

The algorithms; `fdi.core.fog` owns the shapes. Everything here composes the
already-measured primitives in `fdi.core.statements` / `notes` / `numbers`
rather than reimplementing them -- those carry scars (the comma bug, the RITES
note grammar, the caption rule) that are expensive to re-earn.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fdi.core.basis import basis_regions
from fdi.core.statements import (
    STATEMENT_ANCHORS,
    describe_footing,
    find_statement_tables,
    parse_face_table,
)

# A document with no basis anchors is a real shape (see core/basis.py): it
# yields `regions: []`. Indexing nothing would silently lose the whole filing,
# so the fallback is ONE region of unknown basis spanning every page.
_UNKNOWN_REGION = ("unknown", 1, 100_000)


def _regions_of(basis_artifact: Mapping[str, object]) -> list[tuple[str, int, int]]:
    regions = basis_regions(basis_artifact) if isinstance(basis_artifact, Mapping) else []
    return regions or [_UNKNOWN_REGION]


def _tables_of(ocr: Mapping[str, object]) -> list[object]:
    tables = ocr.get("tables") if isinstance(ocr, Mapping) else None
    return list(tables) if isinstance(tables, list) else []


def build_statement_index(
    ocr: Mapping[str, object], basis_artifact: Mapping[str, object]
) -> dict[str, object]:
    """Locate every statement face, per basis region, by table CONTENT.

    One statement per (basis, statement_type): `find_statement_tables` already
    returns candidates best-first (by anchor score, then page), so the first is
    the face and the rest are restatements or segment tables that happen to
    share vocabulary. Keeping several would give the FOG two competing
    balance sheets and no rule for choosing.

    Total: never raises on any input shape."""
    tables = _tables_of(ocr)
    statements: list[dict[str, object]] = []

    for basis, lo, hi in _regions_of(basis_artifact):
        for statement in STATEMENT_ANCHORS:
            found = find_statement_tables(tables, statement, lo, hi)
            if not found:
                continue
            best = found[0]
            # Identity, NOT equality: `list.index` compares with `==`, and two
            # structurally identical tables on different pages would both
            # resolve to the first one's index -- silently pointing every
            # LineItem at the wrong table.
            table_index = next(
                (i for i, t in enumerate(tables) if t is best.table), -1
            )
            if table_index < 0:  # pragma: no cover - find_ returns our own tables
                continue
            rows = parse_face_table(best.table)
            statements.append({
                "id": f"st:{basis}:{statement}",
                "statement": statement,
                "basis": basis,
                "page": best.page,
                "table_index": table_index,
                "anchor_score": best.score,
                "footing": describe_footing(rows, statement),
            })

    statements.sort(key=lambda s: (str(s["basis"]), str(s["statement"])))
    return {"schema_version": 1, "statements": statements}
