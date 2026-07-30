"""Note-reference index: bind a statement face row to the note that breaks it
down.

A statutory balance sheet prints its own navigation. Every face row that has a
schedule carries the note number in a dedicated column::

    Property, plant and equipment | 8(a) | 8,259
    Cash and cash equivalents     | 6(c) | 1,833

That reference is a DETERMINISTIC, high-precision edge to the page holding the
breakdown -- and it is the only reliable one. Similarity search cannot find the
PPE schedule from the word "Land": the label is generic and the schedule sits
~30 pages from the face. Measured on TCS FY2025-26, ~50 taxonomy leaves were
null purely because retrieval never reached the note; every one of them lives
behind such a reference.

TWO SIGNALS, AND WHY BOTH ARE NEEDED
------------------------------------
1. `sectionHeading` paragraphs give note headings: ``8) Property, plant and
   equipment`` (top level) and ``(a) Investments`` (sub-note, which inherits the
   open top-level number).
2. The face table gives ``label -> ref`` pairs.

Signal 1 alone is NOT sufficient, and failing quietly here is worse than
returning nothing. On TCS the ``8)`` heading was never emitted as a
sectionHeading, so its sub-notes ``(a)..(f)`` inherited the previously-open note
7 (Leases) -- PPE was filed as ``7(a)`` and a lookup of ``8(a)`` would have
confidently returned the WRONG page.

The face table is therefore treated as AUTHORITATIVE for the number: when a
ref does not resolve against the heading-derived index, the face row's LABEL is
matched to a sub-note heading's TITLE and rebound under the face's ref. On TCS
this lifted resolvable face references 75% -> 95% and note recovery 80% -> 100%.

PAGE BOUNDING is mandatory, not cosmetic: the AGM notice that follows the
financial statements restarts its own ``1)``, ``4)``, ``7)`` numbering and would
collide with the statement notes.

Pure (`fdi.core` law): takes already-parsed OCR structures, performs no I/O, is
deterministic, and never raises -- malformed input degrades to an empty index.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# "8) Property, plant and equipment"  /  "8. Leases"
_TOP = re.compile(r"^\s*(\d{1,2})\s*[\)\.]\s+([A-Z].{2,70})$")
# "(a) Investments"
_SUB = re.compile(r"^\s*\(([a-z])\)\s+([A-Z].{2,70})$")
# a face-table Note cell: "8(a)", "15", "6(c)"
_REF = re.compile(r"^\s*(\d{1,2})\s*(?:\(([a-z])\))?\s*$")

# How far past its heading a note may run when it is the last one indexed.
_TRAILING_SPAN = 8


@dataclass(frozen=True)
class NoteLocation:
    """Where a note lives. `end_page` is inclusive."""

    ref: str
    page: int
    end_page: int
    title: str
    bound_by: str  # "heading" | "title"


def normalize_label(text: object) -> str:
    """Lowercase alphanumerics only -- used to match a face label to a note
    title across punctuation/spacing noise."""
    if not isinstance(text, str):
        return ""
    # collapse AFTER stripping punctuation: "Property, plant" and "Property
    # plant" must normalize identically or the title rebinding misses.
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _page_of(item: Mapping[str, object]) -> int | None:
    regions = item.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return None
    first = regions[0]
    if not isinstance(first, Mapping):
        return None
    page = first.get("page_number")
    return page if isinstance(page, int) else None


def _content(item: Mapping[str, object]) -> str:
    raw = item.get("content")
    return re.sub(r"\s+", " ", raw).strip() if isinstance(raw, str) else ""


@dataclass(frozen=True)
class _Heading:
    ref: str
    letter: str
    page: int
    title: str


def _scan_headings(
    paragraphs: Sequence[object], page_lo: int, page_hi: int
) -> list[_Heading]:
    """sectionHeading paragraphs -> ordered note headings. A sub-note inherits
    the open top-level number; when none is open it is recorded with an empty
    ref so `bind_face_refs` can still rebind it by title."""
    out: list[_Heading] = []
    current = ""
    for para in paragraphs:
        if not isinstance(para, Mapping) or para.get("role") != "sectionHeading":
            continue
        page = _page_of(para)
        if page is None or not (page_lo <= page <= page_hi):
            continue
        text = _content(para)
        top = _TOP.match(text)
        if top is not None:
            current = top.group(1)
            out.append(_Heading(current, "", page, top.group(2).strip()))
            continue
        sub = _SUB.match(text)
        if sub is not None:
            letter = sub.group(1)
            ref = f"{current}({letter})" if current else ""
            out.append(_Heading(ref, letter, page, sub.group(2).strip()))
    return out


@dataclass(frozen=True)
class FaceRef:
    """A statement face row and the note it points at."""

    page: int
    label: str
    ref: str


def face_note_refs(
    tables: Sequence[object], face_pages: Sequence[int]
) -> list[FaceRef]:
    """Read `label -> note ref` pairs off the statement face tables. The note
    column is not at a fixed index across issuers, so the first cell right of
    the label that parses as a bare reference wins."""
    wanted = {p for p in face_pages if isinstance(p, int)}
    out: list[FaceRef] = []
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        page = _page_of(table)
        if page is None or page not in wanted:
            continue
        cells = table.get("cells")
        if not isinstance(cells, list):
            continue
        rows: dict[int, dict[int, str]] = {}
        for cell in cells:
            if not isinstance(cell, Mapping):
                continue
            row_i = cell.get("row_index")
            col_i = cell.get("column_index")
            if not isinstance(row_i, int) or not isinstance(col_i, int):
                continue
            rows.setdefault(row_i, {})[col_i] = _content(cell)
        for row_i in sorted(rows):
            cols = rows[row_i]
            label = cols.get(0, "")
            if not label:
                continue
            for col_i in sorted(c for c in cols if c > 0):
                match = _REF.match(cols.get(col_i, ""))
                if match is None:
                    continue
                letter = match.group(2)
                ref = match.group(1) + (f"({letter})" if letter else "")
                out.append(FaceRef(page, label, ref))
                break
    return out


_FACE_TITLES = ("balance sheet", "profit and loss", "statement of profit")
# The notice of AGM follows the statements and RESTARTS note numbering.
_AFTER_STATEMENTS = ("notice", "e-voting", "agm", "annual general meeting")


@dataclass(frozen=True)
class StatementPages:
    """Where the primary statements sit, and how far the notes run."""

    face_pages: list[int]
    page_lo: int
    page_hi: int


def find_statement_pages(
    paragraphs: Sequence[object], *, default_span: int = 90
) -> StatementPages:
    """Locate the statement face pages and bound the notes section.

    Bounding is not cosmetic: the AGM notice after the statements restarts its
    own ``1)``/``4)`` numbering, so an unbounded index collides. Degrades to an
    empty result rather than raising."""
    faces: list[int] = []
    stop = 0
    for para in paragraphs:
        if not isinstance(para, Mapping) or para.get("role") != "sectionHeading":
            continue
        page = _page_of(para)
        if page is None:
            continue
        low = _content(para).lower()
        if any(t in low for t in _FACE_TITLES) and page not in faces:
            faces.append(page)
        elif faces and page > min(faces) and any(t in low for t in _AFTER_STATEMENTS):
            if stop == 0 or page < stop:
                stop = page
    if not faces:
        return StatementPages([], 0, 0)
    lo = min(faces)
    hi = (stop - 1) if stop > lo else lo + default_span
    return StatementPages(sorted(faces), lo, hi)


def basis_bounds(
    basis_artifact: Mapping[str, object], preference: str
) -> tuple[int, int]:
    """Page span of the region whose basis is `preference`, else (0, 0).

    Note numbering is per-statement-set: a report carrying BOTH consolidated and
    standalone statements has two "note 8"s. The index must be built inside one
    basis region or references resolve to the wrong statement."""
    regions = basis_artifact.get("regions") if isinstance(basis_artifact, Mapping) else None
    if not isinstance(regions, list):
        return (0, 0)
    for region in regions:
        if not isinstance(region, Mapping) or region.get("basis") != preference:
            continue
        start = region.get("start_page")
        end = region.get("end_page")
        if isinstance(start, int) and isinstance(end, int) and start <= end:
            return (start, end)
    return (0, 0)


@dataclass(frozen=True)
class NoteMap:
    """Resolved notes, addressable by reference or by face label."""

    by_ref: dict[str, NoteLocation]
    by_label: dict[str, NoteLocation]
    unresolved: list[str]

    def pages_for_label(self, label: object) -> list[int]:
        """Inclusive page span of the note that breaks down `label`, or []."""
        loc = self.by_label.get(normalize_label(label))
        if loc is None:
            return []
        return list(range(loc.page, loc.end_page + 1))

    def pages_for_ref(self, ref: object) -> list[int]:
        loc = self.by_ref.get(ref) if isinstance(ref, str) else None
        if loc is None:
            return []
        return list(range(loc.page, loc.end_page + 1))


def build_note_map(
    paragraphs: Sequence[object],
    tables: Sequence[object],
    *,
    face_pages: Sequence[int],
    page_lo: int,
    page_hi: int,
) -> NoteMap:
    """Build the note index and bind it to the statement face rows.

    `page_lo`/`page_hi` MUST bound the financial-statements section: numbering
    restarts in the AGM notice that follows and would otherwise collide.
    """
    headings = _scan_headings(paragraphs, page_lo, page_hi)
    face = face_note_refs(tables, face_pages)

    # A note runs from its heading until the next heading starts.
    spans: list[tuple[_Heading, int]] = []
    for i, head in enumerate(headings):
        nxt = headings[i + 1].page - 1 if i + 1 < len(headings) else head.page + _TRAILING_SPAN
        spans.append((head, max(head.page, min(nxt, page_hi))))

    by_ref: dict[str, NoteLocation] = {}
    for head, end in spans:
        if head.ref and head.ref not in by_ref:
            by_ref[head.ref] = NoteLocation(
                head.ref, head.page, end, head.title, "heading"
            )

    # The face table is authoritative for the NUMBER: rebind by title match.
    by_title: dict[str, tuple[_Heading, int]] = {}
    for head, end in spans:
        by_title.setdefault(normalize_label(head.title), (head, end))

    unresolved: list[str] = []
    for entry in face:
        if entry.ref in by_ref:
            continue
        hit = by_title.get(normalize_label(entry.label))
        if hit is None:
            if entry.ref not in unresolved:
                unresolved.append(entry.ref)
            continue
        head, end = hit
        letter = ""
        if "(" in entry.ref:
            letter = entry.ref[entry.ref.find("(") + 1 : entry.ref.find(")")]
        # only rebind when the sub-note letter agrees (or the face ref is top level)
        if letter and head.letter and letter != head.letter:
            if entry.ref not in unresolved:
                unresolved.append(entry.ref)
            continue
        by_ref[entry.ref] = NoteLocation(entry.ref, head.page, end, head.title, "title")

    by_label: dict[str, NoteLocation] = {}
    for entry in face:
        loc = by_ref.get(entry.ref)
        if loc is not None:
            by_label.setdefault(normalize_label(entry.label), loc)
    # a note title is itself a usable label ("Other income" -> note 11)
    for loc in by_ref.values():
        by_label.setdefault(normalize_label(loc.title), loc)

    return NoteMap(by_ref=by_ref, by_label=by_label, unresolved=unresolved)
