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
from fdi.core.fog import Edge, LineItem, build_fog_artifact, line_item_id
from fdi.core.numbers import parse_amount
from fdi.core.statements import (
    STATEMENT_ANCHORS,
    describe_footing,
    find_statement_tables,
    note_windows,
    parse_face_table,
    table_rows,
    tables_in_window,
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


def build_note_index(
    ocr: Mapping[str, object], basis_artifact: Mapping[str, object]
) -> dict[str, object]:
    """ref -> offset window, title and the tables inside it, per basis.

    Basis scoping is not optional: a report carrying both consolidated and
    standalone statements has two "note 8", and an unscoped index resolves a
    reference to whichever came first.

    Offsets, not pages: TCS p301 carries note 13(a) at offset 897,095 and 13(b)
    at 897,486, so a page-granular fetch hands both to whichever concept asks
    first. Total: never raises."""
    paragraphs_raw = ocr.get("paragraphs") if isinstance(ocr, Mapping) else None
    paragraphs = paragraphs_raw if isinstance(paragraphs_raw, list) else []
    tables = _tables_of(ocr)

    bases: dict[str, object] = {}
    for basis, lo, hi in _regions_of(basis_artifact):
        windows = note_windows(paragraphs, lo, hi)
        notes: list[dict[str, object]] = []
        for ref in sorted(windows):
            window = windows[ref]
            # Identity, NOT equality -- see build_statement_index. Two notes can
            # hold structurally identical tables, and `.index()` would bind both
            # to the first.
            in_window = tables_in_window(tables, window)
            indices = [
                i for i, t in enumerate(tables) if any(t is w for w in in_window)
            ]
            notes.append({
                "id": f"nt:{basis}:{ref}",
                "ref": ref,
                "title": window.title,
                "page": window.page,
                "start_offset": window.start,
                "end_offset": window.end,
                "table_indices": sorted(indices),
            })
        bases[basis] = {"notes": notes, "page_lo": lo, "page_hi": hi}

    return {"schema_version": 1, "bases": bases}


def _region_of(index: Mapping[str, object], basis: str) -> Mapping[str, object]:
    """One basis's slice of a `build_note_index` artifact, or an empty one."""
    bases = index.get("bases") if isinstance(index, Mapping) else None
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    return region if isinstance(region, Mapping) else {}


def _note_refs(note_index: Mapping[str, object], basis: str) -> set[str]:
    """The refs this basis actually resolved -- used to reject dangling
    pointers. An edge to a node that is not in the graph is worse than no edge:
    it looks like navigation and leads nowhere."""
    notes = _region_of(note_index, basis).get("notes")
    return {
        str(n["ref"])
        for n in (notes if isinstance(notes, list) else [])
        if isinstance(n, Mapping) and isinstance(n.get("ref"), str)
    }


def face_line_items(
    ocr: Mapping[str, object],
    statement_index: Mapping[str, object],
    note_index: Mapping[str, object],
    *,
    basis: str,
) -> tuple[list[LineItem], list[Edge]]:
    """Every valued face row of every statement in `basis`, as LineItems, plus
    CONTAINS and REFERENCES edges.

    This is the deterministic bootstrap: values, concepts and note pointers in
    ONE parse, with no model call. On TCS P&L, 22 of 31 facts came from here.

    A REFERENCES edge is emitted ONLY when the printed ref resolves to a note
    this basis actually has. Total: never raises."""
    tables = _tables_of(ocr)
    resolved = _note_refs(note_index, basis)
    raw_statements = (
        statement_index.get("statements") if isinstance(statement_index, Mapping) else None
    )

    items: list[LineItem] = []
    edges: list[Edge] = []
    for stmt in raw_statements if isinstance(raw_statements, list) else []:
        if not isinstance(stmt, Mapping) or stmt.get("basis") != basis:
            continue
        stmt_id = stmt.get("id")
        table_index = stmt.get("table_index")
        page = stmt.get("page")
        if not isinstance(stmt_id, str) or not isinstance(table_index, int):
            continue
        if not (0 <= table_index < len(tables)):
            continue
        table = tables[table_index]
        if not isinstance(table, Mapping):
            continue

        for row_index, row in enumerate(parse_face_table(table)):
            item = LineItem(
                id=line_item_id(stmt_id, row_index),
                parent=stmt_id,
                origin="face",
                label=row.label,
                caption=row.caption,
                ref=row.ref,
                current=row.current,
                prior=row.prior,
                page=page if isinstance(page, int) else 0,
                table_index=table_index,
                row_index=row_index,
            )
            items.append(item)
            edges.append(Edge("CONTAINS", stmt_id, item.id))
            if row.ref and row.ref in resolved:
                edges.append(Edge("REFERENCES", item.id, f"nt:{basis}:{row.ref}"))

    return items, edges


def _note_rows(table: Mapping[str, object]) -> list[tuple[str, float | None, float | None]]:
    """A note table's `(label, current, prior)` rows, header skipped.

    A note table has no note column to dodge, so every non-label cell is a
    candidate amount -- but a row with NO amount anywhere is a caption or a
    section break and is dropped, matching `parse_face_table`'s rule."""
    rows = table_rows(table)
    out: list[tuple[str, float | None, float | None]] = []
    for r in sorted(rows):
        cols = rows[r]
        label = cols.get(0, "").strip()
        if not label:
            continue
        amounts = [
            parsed
            for c in sorted(cols)
            if c != 0 and (parsed := parse_amount(cols.get(c))) is not None
        ]
        if not amounts:
            continue
        out.append((label, amounts[0], amounts[1] if len(amounts) > 1 else None))
    return out


def note_line_items(
    ocr: Mapping[str, object],
    note_index: Mapping[str, object],
    *,
    basis: str,
) -> tuple[list[LineItem], list[Edge]]:
    """Every valued row of every note in `basis`, as LineItems + CONTAINS edges.

    THE ACCURACY LEVER. Today a disclosed line with no taxonomy home -- TCS's
    "Facility expenses 2,966" -- vanishes silently. As a LineItem it is a
    visible node that Phase 3 can mark `unmapped` rather than lose, and the
    note's own total becomes available as the arbiter for its components.

    `origin` is "note", which is not cosmetic: face rows are summaries and match
    PARENT concepts, note rows are detail and match DESCENDANTS. Conflating the
    two candidate sets left 15 of TCS's 28 face rows unmapped. Total: never
    raises."""
    tables = _tables_of(ocr)
    notes = _region_of(note_index, basis).get("notes")

    items: list[LineItem] = []
    edges: list[Edge] = []
    for note in notes if isinstance(notes, list) else []:
        if not isinstance(note, Mapping):
            continue
        note_id = note.get("id")
        indices = note.get("table_indices")
        if not isinstance(note_id, str) or not isinstance(indices, list):
            continue

        row_index = 0
        for table_index in indices:
            if not isinstance(table_index, int) or not (0 <= table_index < len(tables)):
                continue
            table = tables[table_index]
            if not isinstance(table, Mapping):
                continue
            page = _page_of_table(table)
            for label, current, prior in _note_rows(table):
                item = LineItem(
                    id=line_item_id(note_id, row_index),
                    parent=note_id,
                    origin="note",
                    label=label,
                    caption="",
                    ref="",
                    current=current,
                    prior=prior,
                    page=page,
                    table_index=table_index,
                    row_index=row_index,
                )
                items.append(item)
                edges.append(Edge("CONTAINS", note_id, item.id))
                # Counter runs across ALL of a note's tables, deliberately: a
                # note split over two tables must not restart at r0 and collide.
                row_index += 1

    return items, edges


def _page_of_table(table: Mapping[str, object]) -> int:
    """A table's first page, or 0. Total."""
    regions = table.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return 0
    first = regions[0]
    if not isinstance(first, Mapping):
        return 0
    page = first.get("page_number")
    return page if isinstance(page, int) and not isinstance(page, bool) else 0


def build_fog(
    ocr: Mapping[str, object],
    statement_index: Mapping[str, object],
    note_index: Mapping[str, object],
    *,
    doc_id: str,
) -> dict[str, object]:
    """The complete deterministic FOG for one document -- every basis.

    Zero LLM calls. Diagnostics are per basis and deliberately name the
    quantities that distinguish ABSENT from MISSED: an issuer that prints no
    note column (`face_refs == 0`, RIL) looks nothing like one whose reference
    grammar failed to parse (`face_refs > 0, refs_resolved == 0`, the RITES
    bug). Both were previously invisible. Total: never raises."""
    raw_bases = note_index.get("bases") if isinstance(note_index, Mapping) else None
    bases = sorted(raw_bases) if isinstance(raw_bases, Mapping) else []

    regions: dict[
        str,
        tuple[
            list[Mapping[str, object]], list[Mapping[str, object]],
            list[LineItem], list[Edge],
        ],
    ] = {}
    diagnostics: dict[str, object] = {}

    raw_statements = (
        statement_index.get("statements") if isinstance(statement_index, Mapping) else None
    )
    all_statements = raw_statements if isinstance(raw_statements, list) else []

    for basis in bases:
        face_items, face_edges = face_line_items(
            ocr, statement_index, note_index, basis=basis
        )
        note_items, note_edges = note_line_items(ocr, note_index, basis=basis)

        statements = [
            s for s in all_statements
            if isinstance(s, Mapping) and s.get("basis") == basis
        ]
        raw_notes = _region_of(note_index, basis).get("notes")
        notes = [n for n in (raw_notes if isinstance(raw_notes, list) else [])
                 if isinstance(n, Mapping)]

        regions[basis] = (
            statements, notes, [*face_items, *note_items], [*face_edges, *note_edges],
        )
        face_refs = sum(1 for i in face_items if i.ref)
        diagnostics[basis] = {
            "statements": len(statements),
            "notes_indexed": len(notes),
            "face_refs": face_refs,
            "refs_resolved": sum(1 for e in face_edges if e.type == "REFERENCES"),
            "line_items": len(face_items) + len(note_items),
            "face_items": len(face_items),
            "note_items": len(note_items),
        }

    return build_fog_artifact(regions, doc_id=doc_id, diagnostics=diagnostics)
