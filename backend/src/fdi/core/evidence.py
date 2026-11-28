"""Evidence: a located piece of one Report version, and its identity (S4).

Evidence (CONTEXT.md) is what every retrieval path returns and what an answer
points at. Its identity comes from WHERE it is in the Report -- never from how
it was found or how well it ranked -- so the same passage found by the dense
channel and by BM25 is ONE Evidence, and an Evidence id recorded today can be
inspected again tomorrow (ADR-0005).

The id grammar lives here and only here -- `evidence_id` builds, and
`parse_evidence_id` parses, every kind:

    passage      {doc_id}:s{i}                       an AST section (today's chunk id)
    table        {doc_id}:t{i}                       a table (today's table-card id)
    table_cell   {doc_id}:t{i}:r{row}:c{col}         grid coordinates of one cell
    page         {doc_id}:p{n}                       a whole page (1-based)
    note         {doc_id}:note:{basis}:{ref}         a note, as the FOG keys it
    line_item    {doc_id}:li:{st|nt}:{basis}:{name}:r{row}
                                                     a statement/note line: the FOG
                                                     line-item id behind the doc id
    fact         {doc_id}:fact:{basis}:{tag}         a reasoned fact (basis + tag)

`doc_id` is the Document version id (a UUID; never contains ':'), so every
Evidence id names exactly one Report version.

Everything else here is the pure half of the Retrieval service: building
Evidence from a vector-store hit, merging Evidence found by several paths,
grouping by Fiscal year, the matched/no_match/not_processed Report status, and
the ONE function that builds a vector-store filter expression -- from resolved
ids and closed vocabularies only, re-validated here as defence in depth.

Pure (`fdi.core` law): no I/O, deterministic. Total except
`scope_filter_expression`, which raises on an unsafe value ON PURPOSE: its
inputs are produced by code, never read from a document, so an unsafe one is a
programming error that must not become a filter that silently widens.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum


class EvidenceKind(StrEnum):
    PASSAGE = "passage"
    TABLE = "table"
    TABLE_CELL = "table_cell"
    PAGE = "page"
    NOTE = "note"
    LINE_ITEM = "line_item"
    FACT = "fact"


class Basis(StrEnum):
    """A statement basis. `unknown` is a real value: text outside every
    labelled basis region (front matter, the directors' report) carries it,
    so a basis-scoped search that should still see such text asks for it
    explicitly."""

    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"
    UNKNOWN = "unknown"


class RetrievalPath(StrEnum):
    STRUCTURED = "structured"
    HYBRID = "hybrid"
    STRUCTURAL = "structural"
    CROSS_DOCUMENT = "cross_document"


class ReportStatus(StrEnum):
    """Per-Report outcome of a cross-document request (RT-7's distinction,
    kept): `not_processed` means the Report has not reached the stage a path
    reads, `no_match` means it has and nothing matched. They must never be
    conflated -- one is "ask later", the other is an answer."""

    MATCHED = "matched"
    NO_MATCH = "no_match"
    NOT_PROCESSED = "not_processed"


# ---------------------------------------------------------------------------
# Id grammar
# ---------------------------------------------------------------------------

_DOC_ID = r"[A-Za-z0-9_-]{1,64}"
_BASIS = r"[a-z_]{1,32}"
# A printed note reference: "13", "2.1", "13(a)", "4A". Anything but ':' and
# whitespace -- the separator and the one character a ref never prints.
_REF = r"[^\s:]{1,64}"
_STATEMENT = r"[A-Za-z_]{1,32}"
_TAG = r"[A-Za-z0-9_.-]{1,64}"

DOC_ID_RE = re.compile(rf"^{_DOC_ID}$")
TAG_RE = re.compile(rf"^{_TAG}$")
_INDEX = r"(0|[1-9][0-9]{0,6})"

_FORMS: tuple[tuple[EvidenceKind, re.Pattern[str]], ...] = (
    (EvidenceKind.PASSAGE, re.compile(rf"^s{_INDEX}$")),
    (EvidenceKind.TABLE, re.compile(rf"^t{_INDEX}$")),
    (EvidenceKind.TABLE_CELL, re.compile(rf"^t{_INDEX}:r{_INDEX}:c{_INDEX}$")),
    (EvidenceKind.PAGE, re.compile(rf"^p{_INDEX}$")),
    (EvidenceKind.NOTE, re.compile(rf"^note:({_BASIS}):({_REF})$")),
    (
        EvidenceKind.LINE_ITEM,
        re.compile(rf"^li:(st|nt):({_BASIS}):({_REF}):r{_INDEX}$"),
    ),
    (EvidenceKind.FACT, re.compile(rf"^fact:({_BASIS}):({_TAG})$")),
)


@dataclass(frozen=True)
class EvidenceRef:
    """A parsed Evidence id. Which optional fields are set depends on `kind`:

    passage/table: `index`; table_cell: `index`, `row`, `col`; page: `index`
    (the 1-based page number); note: `basis`, `name` (the printed ref);
    line_item: `parent` ("st"/"nt"), `basis`, `name` (statement or ref),
    `row`; fact: `basis`, `name` (the taxonomy tag)."""

    doc_id: str
    kind: EvidenceKind
    index: int | None = None
    row: int | None = None
    col: int | None = None
    basis: str | None = None
    name: str | None = None
    parent: str | None = None

    @property
    def id(self) -> str:
        return evidence_id(self)

    @property
    def chunk_id(self) -> str | None:
        """The vector-index chunk id for a passage/table, else None."""
        if self.kind is EvidenceKind.PASSAGE:
            return f"s{self.index}"
        if self.kind is EvidenceKind.TABLE:
            return f"t{self.index}"
        return None

    @property
    def fog_id(self) -> str | None:
        """The FOG's own node id for a note or line item, else None."""
        if self.kind is EvidenceKind.NOTE:
            return f"nt:{self.basis}:{self.name}"
        if self.kind is EvidenceKind.LINE_ITEM:
            return f"li:{self.parent}:{self.basis}:{self.name}:r{self.row}"
        return None


def evidence_id(ref: EvidenceRef) -> str:
    """The one place an Evidence id is built. Round-trips with
    `parse_evidence_id` for every kind."""
    d = ref.doc_id
    match ref.kind:
        case EvidenceKind.PASSAGE:
            return f"{d}:s{ref.index}"
        case EvidenceKind.TABLE:
            return f"{d}:t{ref.index}"
        case EvidenceKind.TABLE_CELL:
            return f"{d}:t{ref.index}:r{ref.row}:c{ref.col}"
        case EvidenceKind.PAGE:
            return f"{d}:p{ref.index}"
        case EvidenceKind.NOTE:
            return f"{d}:note:{ref.basis}:{ref.name}"
        case EvidenceKind.LINE_ITEM:
            return f"{d}:li:{ref.parent}:{ref.basis}:{ref.name}:r{ref.row}"
        case EvidenceKind.FACT:
            return f"{d}:fact:{ref.basis}:{ref.name}"


def parse_evidence_id(value: object) -> EvidenceRef | None:
    """An Evidence id -> its EvidenceRef, or None when `value` is not a
    well-formed id of any kind. Total: never raises."""
    if not isinstance(value, str) or ":" not in value or len(value) > 512:
        return None
    doc_id, rest = value.split(":", 1)
    if not DOC_ID_RE.match(doc_id):
        return None
    for kind, pattern in _FORMS:
        m = pattern.match(rest)
        if m is None:
            continue
        g = m.groups()
        match kind:
            case EvidenceKind.PASSAGE | EvidenceKind.TABLE | EvidenceKind.PAGE:
                return EvidenceRef(doc_id, kind, index=int(g[0]))
            case EvidenceKind.TABLE_CELL:
                return EvidenceRef(doc_id, kind, index=int(g[0]), row=int(g[1]), col=int(g[2]))
            case EvidenceKind.NOTE | EvidenceKind.FACT:
                return EvidenceRef(doc_id, kind, basis=g[0], name=g[1])
            case EvidenceKind.LINE_ITEM:
                return EvidenceRef(
                    doc_id, kind, parent=g[0], basis=g[1], name=g[2], row=int(g[3])
                )
    return None


def passage_ref(doc_id: str, index: int) -> EvidenceRef:
    return EvidenceRef(doc_id, EvidenceKind.PASSAGE, index=index)


def table_ref(doc_id: str, index: int) -> EvidenceRef:
    return EvidenceRef(doc_id, EvidenceKind.TABLE, index=index)


def cell_ref(doc_id: str, table_index: int, row: int, col: int) -> EvidenceRef:
    return EvidenceRef(doc_id, EvidenceKind.TABLE_CELL, index=table_index, row=row, col=col)


def page_ref(doc_id: str, page: int) -> EvidenceRef:
    return EvidenceRef(doc_id, EvidenceKind.PAGE, index=page)


def fact_ref(doc_id: str, basis: str, tag: str) -> EvidenceRef:
    return EvidenceRef(doc_id, EvidenceKind.FACT, basis=basis, name=tag)


def ref_for_chunk(doc_id: str, chunk_id: object) -> EvidenceRef | None:
    """A vector-index chunk (`s{i}` / `t{i}`) -> its Evidence ref."""
    return parse_evidence_id(f"{doc_id}:{chunk_id}") if isinstance(chunk_id, str) else None


def ref_for_fog_node(doc_id: str, fog_id: object) -> EvidenceRef | None:
    """A FOG node id (`nt:{basis}:{ref}` or `li:...`) -> its Evidence ref, or
    None for any other node (a statement node is reached as its table)."""
    if not isinstance(fog_id, str):
        return None
    if fog_id.startswith("nt:"):
        return parse_evidence_id(f"{doc_id}:note:{fog_id[3:]}")
    if fog_id.startswith("li:"):
        return parse_evidence_id(f"{doc_id}:{fog_id}")
    return None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportMeta:
    """The Report version an Evidence belongs to, as Postgres knows it at
    query time (Company and Fiscal year are correctable, so they are never
    copied into the vector index -- ADR-0005)."""

    doc_id: str
    doc_key: str | None = None
    version: int | None = None
    company_id: str | None = None
    fiscal_year: str | None = None


@dataclass(frozen=True)
class Evidence:
    """One located piece of one Report version (design §11.3, minus
    `workspace_id`: Workspaces are not a boundary). `score` and
    `retrieval_paths` describe how it was FOUND and are never identity."""

    evidence_id: str
    kind: EvidenceKind
    report: ReportMeta
    content: str
    page: int | None = None
    section: str | None = None
    section_type: str | None = None
    basis: str | None = None
    table_index: int | None = None
    cell_refs: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    bounding_regions: tuple[Mapping[str, object], ...] = ()
    value: float | None = None
    retrieval_paths: tuple[str, ...] = ()
    score: float | None = None


def evidence_body(e: Evidence) -> dict[str, object]:
    """An Evidence as a JSON-safe mapping (the API and tool shape)."""
    return {
        "evidence_id": e.evidence_id,
        "kind": e.kind.value,
        "doc_id": e.report.doc_id,
        "doc_key": e.report.doc_key,
        "version": e.report.version,
        "company_id": e.report.company_id,
        "fiscal_year": e.report.fiscal_year,
        "page": e.page,
        "section": e.section,
        "section_type": e.section_type,
        "basis": e.basis,
        "content": e.content,
        "table_index": e.table_index,
        "cell_refs": list(e.cell_refs),
        "links": list(e.links),
        "bounding_regions": [dict(r) for r in e.bounding_regions],
        "value": e.value,
        "retrieval_path": e.retrieval_paths[0] if e.retrieval_paths else None,
        "retrieval_paths": list(e.retrieval_paths),
        "score": e.score,
    }


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def evidence_from_hit(
    hit: Mapping[str, object], report: ReportMeta, *, path: str
) -> Evidence | None:
    """A citation-shaped vector-store hit (`fdi.core.retrieval`'s shape:
    doc_id, chunk_id, heading, page, section_type, text, score, plus the
    index's `basis`/`kind`) -> Evidence, or None when its chunk id is not a
    passage or table. Total."""
    ref = ref_for_chunk(report.doc_id, hit.get("chunk_id"))
    if ref is None or ref.kind not in (EvidenceKind.PASSAGE, EvidenceKind.TABLE):
        return None
    score = hit.get("score")
    return Evidence(
        evidence_id=ref.id,
        kind=ref.kind,
        report=report,
        content=_str_or_none(hit.get("text")) or "",
        page=_int_or_none(hit.get("page")),
        section=_str_or_none(hit.get("heading")),
        section_type=_str_or_none(hit.get("section_type")),
        basis=_str_or_none(hit.get("basis")),
        table_index=ref.index if ref.kind is EvidenceKind.TABLE else None,
        retrieval_paths=(path,),
        score=float(score) if isinstance(score, int | float) else None,
    )


def legacy_hit(e: Evidence) -> dict[str, object]:
    """Evidence -> the pre-S4 citation-shaped hit dict that `/retrieve`,
    `/search` and the `/ask` `search` tool return, plus `evidence_id`,
    `basis` and `kind` (additive). Only passage/table Evidence has a chunk
    id; other kinds report `chunk_id` as their Evidence id's location part."""
    location = e.evidence_id.split(":", 1)[1]
    return {
        "doc_id": e.report.doc_id,
        "doc_key": e.report.doc_key,
        "version": e.report.version,
        "chunk_id": location,
        "heading": e.section,
        "page": e.page,
        "section_type": e.section_type,
        "text": e.content,
        "score": e.score if e.score is not None else 0.0,
        "evidence_id": e.evidence_id,
        "basis": e.basis,
        "kind": e.kind.value,
    }


def merge_evidence(*groups: Iterable[Evidence]) -> list[Evidence]:
    """Concatenate Evidence lists, collapsing repeats of the same Evidence id
    into ONE (identity is location). The first occurrence keeps its place and
    fields; later ones add their retrieval paths, fill any field the first
    left empty, and raise the score if theirs is higher. Deterministic."""
    order: list[str] = []
    merged: dict[str, Evidence] = {}
    for group in groups:
        for e in group:
            current = merged.get(e.evidence_id)
            if current is None:
                merged[e.evidence_id] = e
                order.append(e.evidence_id)
                continue
            paths = current.retrieval_paths + tuple(
                p for p in e.retrieval_paths if p not in current.retrieval_paths
            )
            score = current.score
            if e.score is not None and (score is None or e.score > score):
                score = e.score
            merged[e.evidence_id] = replace(
                current,
                retrieval_paths=paths,
                score=score,
                page=current.page if current.page is not None else e.page,
                section=current.section or e.section,
                section_type=current.section_type or e.section_type,
                basis=current.basis or e.basis,
                content=current.content if len(current.content) >= len(e.content)
                else e.content,
            )
    return [merged[i] for i in order]


# ---------------------------------------------------------------------------
# Cross-document assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportOutcome:
    report: ReportMeta
    status: ReportStatus
    evidence_count: int = 0


def report_status(*, processed: bool, evidence_count: int) -> ReportStatus:
    if not processed:
        return ReportStatus.NOT_PROCESSED
    return ReportStatus.MATCHED if evidence_count > 0 else ReportStatus.NO_MATCH


@dataclass(frozen=True)
class FiscalYearGroup:
    fiscal_year: str | None
    reports: tuple[ReportOutcome, ...]
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)


def group_by_fiscal_year(
    outcomes: Sequence[ReportOutcome], evidence: Sequence[Evidence]
) -> list[FiscalYearGroup]:
    """Evidence grouped by its Report's Fiscal year, in the order the
    Reports were given (the registry's newest-Fiscal-year-first order). Each
    Fiscal year appears once; a Report without a Fiscal year groups under
    None, after every dated one. Every Report appears with its status even
    when it contributed no Evidence -- an absent year is itself an answer."""
    years: list[str | None] = []
    for o in outcomes:
        if o.report.fiscal_year not in years:
            years.append(o.report.fiscal_year)
    years.sort(key=lambda y: y is None)  # stable: dated years keep their order
    groups: list[FiscalYearGroup] = []
    for year in years:
        reports = tuple(o for o in outcomes if o.report.fiscal_year == year)
        doc_ids = {o.report.doc_id for o in reports}
        groups.append(FiscalYearGroup(
            fiscal_year=year,
            reports=reports,
            evidence=tuple(e for e in evidence if e.report.doc_id in doc_ids),
        ))
    return groups


# ---------------------------------------------------------------------------
# Vector-store filter
# ---------------------------------------------------------------------------

_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _in_clause(field_name: str, values: Sequence[str]) -> str:
    for v in values:
        if not _SAFE_LITERAL.match(v):
            raise ValueError(f"unsafe filter literal for {field_name}: {v!r}")
    joined = ", ".join(f'"{v}"' for v in values)
    return f"{field_name} in [{joined}]"


def scope_filter_expression(
    *,
    doc_ids: Sequence[str] = (),
    bases: Sequence[str] = (),
    kinds: Sequence[str] = (),
    section_types: Sequence[str] = (),
) -> str:
    """The vector-store filter for a resolved scope: `field in [...]`
    clauses joined by ` and `, the only shape `FakeVectorStore` and Milvus
    are both asked to understand. Empty sequences add no clause; all empty
    -> "" (unscoped). Built ONLY from resolved document ids and closed
    vocabularies -- every literal is re-checked against a strict charset and
    an unsafe one raises rather than reaching the store."""
    clauses: list[str] = []
    if doc_ids:
        clauses.append(_in_clause("doc_id", sorted(set(doc_ids))))
    if bases:
        clauses.append(_in_clause("basis", sorted(set(bases))))
    if kinds:
        clauses.append(_in_clause("kind", sorted(set(kinds))))
    if section_types:
        clauses.append(_in_clause("section_type", sorted(set(section_types))))
    return " and ".join(clauses)


def chunk_kind(chunk_id: object) -> str:
    """The Evidence kind a vector-index chunk is indexed under."""
    if isinstance(chunk_id, str) and chunk_id.startswith("t"):
        return EvidenceKind.TABLE.value
    return EvidenceKind.PASSAGE.value
