"""The Financial Object Graph's shapes -- nodes, edges, ids and serialization.

The FOG is PRE-ONTOLOGY: it is the issuer's own financial model, in the
issuer's own words, with values parsed deterministically. It is NOT the 402
taxonomy tags. `MAPS_TO` edges are the single seam where the issuer's model
meets the taxonomy, which is what lets document parsing be swapped without
touching financial reasoning.

This module owns shapes only; the algorithms live in `fdi.core.fog_build`.

Pure (`fdi.core` law): no I/O, deterministic, total -- except `edge_body`,
which raises on an unknown edge type ON PURPOSE (see its docstring).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# The closed edge vocabulary. GOVERNED_BY and the Policy node are RESERVED for
# domain 2 and never emitted here -- named now so the schema version that adds
# them is additive rather than breaking.
EDGE_TYPES: frozenset[str] = frozenset({
    "CONTAINS",
    "REFERENCES",
    "MAPS_TO",
    "COMPOSES",
    "RECONCILES_WITH",
    "SAME_AS",
    "GOVERNED_BY",
})


@dataclass(frozen=True)
class LineItem:
    """One row of a statement face or a note -- the FOG's atom.

    `origin` is "face" or "note". `ref` is the note reference printed in the
    face's note column, or "" -- an EMPTY ref on a face row means the issuer
    printed no pointer (RIL prints no note column at all), which is `absent`,
    not `missed`.

    TWO ROW NUMBERS, AND WHY BOTH ARE NEEDED. `row_index` is the ordinal within
    the VALUED rows of the table -- it is what `line_item_id` hashes on, so it
    must not move when a header or caption row is reclassified. `source_row` is
    the RAW `cell.row_index` in the OCR grid, and it is the only one that can
    address a cell: `core/provenance.py::_table_row_regions` indexes the raw
    grid, so handing it a valued-row ordinal returns the geometry of a
    different line (or `None`) and draws the highlight box on the wrong row."""

    id: str
    parent: str
    origin: str
    label: str
    caption: str
    ref: str
    current: float | None
    prior: float | None
    page: int
    table_index: int
    row_index: int
    source_row: int


@dataclass(frozen=True)
class Edge:
    type: str
    source: str
    target: str


def line_item_id(parent: str, row_index: int) -> str:
    """A stable, content-addressable id: parent node + row position.

    Stability is not cosmetic. Stage artifacts are content-addressed, so an id
    that moves between two identical runs changes the output hash and
    invalidates every downstream cache silently."""
    return f"li:{parent}:r{row_index}"


def line_item_body(item: LineItem) -> dict[str, object]:
    """A LineItem as a JSON-safe mapping. `None` amounts stay `None`:
    collapsing them to 0.0 would turn 'this row has no figure' into a fact."""
    return {
        "id": item.id,
        "parent": item.parent,
        "origin": item.origin,
        "label": item.label,
        "caption": item.caption,
        "ref": item.ref,
        "current": item.current,
        "prior": item.prior,
        "page": item.page,
        "table_index": item.table_index,
        "row_index": item.row_index,
        "source_row": item.source_row,
    }


def edge_body(edge: Edge) -> dict[str, object]:
    """An Edge as a JSON-safe mapping.

    Raises on an unknown type -- the ONE deliberate exception to the `fdi.core`
    totality law. An edge type is authored in code, never read from a document,
    so a bad one is a programming error; degrading it to a silent skip would
    produce a graph that nothing traverses and no test notices."""
    if edge.type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type {edge.type!r}")
    return {"type": edge.type, "source": edge.source, "target": edge.target}


def build_fog_artifact(
    regions: Mapping[
        str,
        tuple[
            Sequence[Mapping[str, object]],
            Sequence[Mapping[str, object]],
            Sequence[LineItem],
            Sequence[Edge],
        ],
    ],
    *,
    doc_id: str,
    diagnostics: Mapping[str, object],
) -> dict[str, object]:
    """Assemble the per-basis FOG artifact from `(statements, notes, items,
    edges)` tuples. Everything is sorted so the body hashes stably."""
    bases: dict[str, object] = {}
    for basis in sorted(regions):
        statements, notes, items, edges = regions[basis]
        bases[basis] = {
            "statements": list(statements),
            "notes": list(notes),
            "line_items": [line_item_body(i) for i in items],
            "edges": [
                edge_body(e)
                for e in sorted(edges, key=lambda e: (e.type, e.source, e.target))
            ],
        }
    return {
        # v3: a note row printing an explicit nil (`Communication | - | -`) is
        # now a LineItem with `current=None` instead of the block's caption, so
        # note `row_index` ordinals -- and therefore LineItem ids -- move.
        # `diagnostics` gains `refs_beyond_bound` and
        # `regions_without_statements`.
        # v2 added `source_row` to every LineItem body -- see LineItem's
        # docstring for why the valued-row ordinal could not address a cell.
        "schema_version": 3,
        "doc_id": doc_id,
        "bases": bases,
        "diagnostics": dict(diagnostics),
    }
