"""Build the Financial Object Graph's indices and nodes from OCR artifacts.

The algorithms; `fdi.core.fog` owns the shapes. Everything here composes the
already-measured primitives in `fdi.core.statements` / `notes` / `numbers`
rather than reimplementing them -- those carry scars (the comma bug, the RITES
note grammar, the caption rule) that are expensive to re-earn.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from fdi.core.basis import basis_regions
from fdi.core.fog import Edge, LineItem, build_fog_artifact, line_item_id
from fdi.core.notes import find_statement_pages
from fdi.core.numbers import parse_amount
from fdi.core.statements import (
    STATEMENT_ANCHORS,
    FaceRow,
    StatementTable,
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


def _bounded_regions(
    basis_artifact: Mapping[str, object], paragraphs: Sequence[object]
) -> list[tuple[str, int, int]]:
    """Basis regions INTERSECTED with the span the primary statements occupy.

    The basis region alone is not a bound. `core/basis.py` gives a document that
    anchors only ONE basis (RIL, consolidated-only) the whole filing as its
    region, and `_UNKNOWN_REGION` does the same for a document with no anchors
    at all. Scanning that span picks up the Board's Report and the Notice of
    AGM, which restart their own `1.`/`2.` numbering -- and `_HEAD_FLAT` matches
    "1. FINANCIAL RESULTS" perfectly. `note_windows` keeps the FIRST occurrence
    by offset and front matter precedes the statements, so the front-matter
    heading wins the ref and the face's printed `10` binds to a dividend table.

    `find_statement_pages` returns 0 for "no opinion", which applies no bound --
    an unbounded region is strictly better than a wrong one. A region left with
    `hi < lo` carries no statements at all; it is kept (not dropped) so the
    artifact still records that the region existed."""
    pages = find_statement_pages(paragraphs)
    out: list[tuple[str, int, int]] = []
    for basis, lo, hi in _regions_of(basis_artifact):
        if pages.page_lo:
            lo = max(lo, pages.page_lo)
        if pages.page_hi:
            hi = min(hi, pages.page_hi)
        out.append((basis, lo, hi))
    return out


def _tables_of(ocr: Mapping[str, object]) -> list[object]:
    tables = ocr.get("tables") if isinstance(ocr, Mapping) else None
    return list(tables) if isinstance(tables, list) else []


def _paragraphs_of(ocr: Mapping[str, object]) -> list[object]:
    raw = ocr.get("paragraphs") if isinstance(ocr, Mapping) else None
    return list(raw) if isinstance(raw, list) else []


def _best_candidate(found: Sequence[StatementTable], statement: str) -> StatementTable:
    """Among the EQUALLY-anchored candidates, prefer one whose face foots.

    `find_statement_tables` breaks anchor ties on page, so a front-matter
    "Financial Highlights" table that ties the real face on anchor count wins
    purely by being earlier in the document. Footing is the evidence the design
    already names as "what proves the right table was chosen"; before this it
    was recorded on the artifact and read by nothing.

    `describe_footing` is THREE-valued: `passed is None` means COULD NOT CHECK
    (a needed line is absent, or the statement has no rule) and is deliberately
    NOT treated as a failure -- only an affirmative `True` promotes a candidate
    above the incumbent, so an unverifiable-but-correct face keeps its position
    in page order."""
    top = found[0].score
    for candidate in found:
        if candidate.score != top:
            break
        verdict = describe_footing(parse_face_table(candidate.table), statement)
        if verdict.get("passed") is True:
            return candidate
    return found[0]


def _statement_rank(entry: Mapping[str, object]) -> tuple[int, int, int]:
    """How good a face candidate is: anchors, then proven footing, then the
    earlier page. Used ONLY to choose between two regions of the SAME basis."""
    footing = entry.get("footing")
    passed = footing.get("passed") if isinstance(footing, Mapping) else None
    score = entry.get("anchor_score")
    page = entry.get("page")
    return (
        score if isinstance(score, int) and not isinstance(score, bool) else 0,
        1 if passed is True else 0,
        -page if isinstance(page, int) and not isinstance(page, bool) else 0,
    )


def _discard_key(entry: Mapping[str, object]) -> tuple[str, str, int]:
    """A total ordering for `discarded`; it reaches an artifact body."""
    table_index = entry.get("table_index")
    return (
        str(entry.get("basis")),
        str(entry.get("statement")),
        table_index if isinstance(table_index, int) and not isinstance(table_index, bool)
        else -1,
    )


def build_statement_index(
    ocr: Mapping[str, object], basis_artifact: Mapping[str, object]
) -> dict[str, object]:
    """Locate every statement face, per basis region, by table CONTENT.

    One statement per (basis, statement_type): `find_statement_tables` already
    returns candidates best-first (by anchor score, then page), so the first is
    the face and the rest are restatements or segment tables that happen to
    share vocabulary. Keeping several would give the FOG two competing
    balance sheets and no rule for choosing.

    ONE PER (basis, statement) ACROSS ALL REGIONS, not per region. `core/basis.py`
    collapses only CONSECUTIVE same-basis anchors, so
    `[standalone, consolidated, standalone]` is a supported document shape and
    used to emit `st:standalone:profit_and_loss` TWICE -- duplicate statement
    ids, duplicate `li:...:r0` LineItem ids and duplicate CONTAINS edges, all
    silent. The loser is recorded in `discarded`, never dropped without trace.

    Total: never raises on any input shape."""
    tables = _tables_of(ocr)
    paragraphs = _paragraphs_of(ocr)

    chosen: dict[tuple[str, str], dict[str, object]] = {}
    discarded: list[dict[str, object]] = []

    for basis, lo, hi in _bounded_regions(basis_artifact, paragraphs):
        for statement in STATEMENT_ANCHORS:
            found = find_statement_tables(tables, statement, lo, hi)
            if not found:
                continue
            best = _best_candidate(found, statement)
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
            entry: dict[str, object] = {
                "id": f"st:{basis}:{statement}",
                "statement": statement,
                "basis": basis,
                "page": best.page,
                "table_index": table_index,
                "anchor_score": best.score,
                "footing": describe_footing(rows, statement),
            }
            key = (basis, statement)
            incumbent = chosen.get(key)
            if incumbent is None:
                chosen[key] = entry
            elif _statement_rank(entry) > _statement_rank(incumbent):
                chosen[key] = entry
                discarded.append(incumbent)
            else:
                discarded.append(entry)

    statements = sorted(
        chosen.values(), key=lambda s: (str(s["basis"]), str(s["statement"]))
    )
    discarded.sort(key=_discard_key)
    # v2: `discarded` is new, and a face is now chosen once per (basis,
    # statement) across every region rather than once per region.
    return {"schema_version": 2, "statements": statements, "discarded": discarded}


def build_note_index(
    ocr: Mapping[str, object], basis_artifact: Mapping[str, object]
) -> dict[str, object]:
    """ref -> offset window, title and the tables inside it, per basis.

    Basis scoping is not optional: a report carrying both consolidated and
    standalone statements has two "note 8", and an unscoped index resolves a
    reference to whichever came first.

    Offsets, not pages: TCS p301 carries note 13(a) at offset 897,095 and 13(b)
    at 897,486, so a page-granular fetch hands both to whichever concept asks
    first.

    A BASIS MAY OWN SEVERAL REGIONS. `core/basis.py` collapses only CONSECUTIVE
    same-basis anchors, so `[standalone, consolidated, standalone]` is a
    supported shape. Assigning `bases[basis]` once per region made the LAST
    region overwrite the first -- notes silently vanished, and the resulting
    `face_refs > 0, refs_resolved == 0` is exactly the signature this artifact
    uses to report a RITES-style ref-grammar failure. Regions are therefore
    MERGED, and the merge is visible: `regions` lists every span that
    contributed, and `duplicate_refs` names every ref a later region re-used and
    could not add. Total: never raises."""
    paragraphs = _paragraphs_of(ocr)
    tables = _tables_of(ocr)

    notes_by_basis: dict[str, dict[str, dict[str, object]]] = {}
    spans: dict[str, list[tuple[int, int]]] = {}
    duplicates: dict[str, set[str]] = {}

    for basis, lo, hi in _bounded_regions(basis_artifact, paragraphs):
        seen = notes_by_basis.setdefault(basis, {})
        spans.setdefault(basis, []).append((lo, hi))
        collided = duplicates.setdefault(basis, set())
        windows = note_windows(paragraphs, lo, hi)
        for ref in sorted(windows):
            if ref in seen:
                # The first region in document order wins, matching
                # `note_windows`' own first-occurrence-by-offset rule. Recorded,
                # not silent: a dropped note must never look like an absent one.
                collided.add(ref)
                continue
            window = windows[ref]
            # Identity, NOT equality -- see build_statement_index. Two notes can
            # hold structurally identical tables, and `.index()` would bind both
            # to the first.
            in_window = tables_in_window(tables, window)
            indices = [
                i for i, t in enumerate(tables) if any(t is w for w in in_window)
            ]
            seen[ref] = {
                "id": f"nt:{basis}:{ref}",
                "ref": ref,
                "title": window.title,
                "page": window.page,
                "start_offset": window.start,
                "end_offset": window.end,
                "table_indices": sorted(indices),
            }

    bases: dict[str, object] = {}
    for basis in sorted(notes_by_basis):
        seen = notes_by_basis[basis]
        regions = spans[basis]
        bases[basis] = {
            "notes": [seen[ref] for ref in sorted(seen)],
            "page_lo": min(lo for lo, _ in regions),
            "page_hi": max(hi for _, hi in regions),
            "regions": [{"page_lo": lo, "page_hi": hi} for lo, hi in regions],
            "duplicate_refs": sorted(duplicates[basis]),
        }

    # v2: `regions` / `duplicate_refs` are new, and a basis that owns several
    # regions now merges them instead of keeping only the last.
    return {"schema_version": 2, "bases": bases}


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
                source_row=row.source_row,
            )
            items.append(item)
            edges.append(Edge("CONTAINS", stmt_id, item.id))
            if row.ref and row.ref in resolved:
                edges.append(Edge("REFERENCES", item.id, f"nt:{basis}:{row.ref}"))

    return items, edges


# The label a table prints over its own label column. A note table carries no
# "Note" column for `parse_face_table`'s header rule to find, so the header is
# recognised by this vocabulary instead. Without it, `Particulars | 2025 | 2024`
# yields a LineItem labelled "Particulars" worth 2025.0 -- a YEAR entering the
# graph as an amount, which is the class of bug the comma fix already paid for.
_NOTE_HEADER_LABELS: frozenset[str] = frozenset({
    "particulars", "particular", "description", "details", "item", "items",
    "nature", "category", "name", "head", "heads", "component", "components",
    "sr no", "sr", "s no", "sl no", "serial no", "no", "nos", "sno",
})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _is_note_header_label(text: object) -> bool:
    """Is this the label column's HEADING rather than a disclosed line?"""
    if not isinstance(text, str):
        return False
    return _NON_ALNUM.sub(" ", text.lower()).strip() in _NOTE_HEADER_LABELS


def _note_rows(table: Mapping[str, object]) -> list[FaceRow]:
    """A note table's valued rows, with the header skipped and captions kept.

    A note table has no note column to dodge, so every non-label cell is a
    candidate amount -- but a row with NO amount anywhere is a caption or a
    section break, and it is carried onto the rows beneath it exactly as
    `parse_face_table` does. That is not cosmetic: a PPE note prints
    `Additions` once under "Gross block" and once under "Accumulated
    depreciation", and without the caption the two are indistinguishable nodes.

    Only the FIRST row can be the header -- scanning the whole table lets a late
    row that happens to say "Particulars" swallow real disclosures."""
    rows = table_rows(table)
    if not rows:
        return []
    ordered = sorted(rows)
    header = ordered[0] if _is_note_header_label(rows[ordered[0]].get(0, "")) else None

    out: list[FaceRow] = []
    caption = ""
    for r in ordered:
        if r == header:
            continue
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
            caption = label          # a row with no amount anywhere is a CAPTION
            continue
        out.append(FaceRow(
            label=label, caption=caption, ref="",
            current=amounts[0],
            prior=amounts[1] if len(amounts) > 1 else None,
            source_row=r,
        ))
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
            for row in _note_rows(table):
                item = LineItem(
                    id=line_item_id(note_id, row_index),
                    parent=note_id,
                    origin="note",
                    label=row.label,
                    caption=row.caption,
                    ref="",
                    current=row.current,
                    prior=row.prior,
                    page=page,
                    table_index=table_index,
                    row_index=row_index,
                    source_row=row.source_row,
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
    bug). Both were previously invisible.

    `regions`, `duplicate_refs` and `statements_discarded` are part of that
    split, not bookkeeping: a basis assembled from more than one region is a
    THIRD cause of an unresolved ref, and without them a merged document is
    indistinguishable from a broken one. Total: never raises."""
    raw_bases = note_index.get("bases") if isinstance(note_index, Mapping) else None
    bases = sorted(raw_bases, key=str) if isinstance(raw_bases, Mapping) else []

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

    raw_discarded = (
        statement_index.get("discarded") if isinstance(statement_index, Mapping) else None
    )
    all_discarded = raw_discarded if isinstance(raw_discarded, list) else []

    for basis in bases:
        face_items, face_edges = face_line_items(
            ocr, statement_index, note_index, basis=basis
        )
        note_items, note_edges = note_line_items(ocr, note_index, basis=basis)

        statements = [
            s for s in all_statements
            if isinstance(s, Mapping) and s.get("basis") == basis
        ]
        region = _region_of(note_index, basis)
        raw_notes = region.get("notes")
        notes = [n for n in (raw_notes if isinstance(raw_notes, list) else [])
                 if isinstance(n, Mapping)]
        raw_regions = region.get("regions")
        raw_duplicates = region.get("duplicate_refs")

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
            # > 1 means this basis was assembled from several page regions.
            "regions": len(raw_regions) if isinstance(raw_regions, list) else 0,
            # Notes and faces a later region of this basis could not add.
            "duplicate_refs": (
                len(raw_duplicates) if isinstance(raw_duplicates, list) else 0
            ),
            "statements_discarded": sum(
                1 for s in all_discarded
                if isinstance(s, Mapping) and s.get("basis") == basis
            ),
        }

    return build_fog_artifact(regions, doc_id=doc_id, diagnostics=diagnostics)
