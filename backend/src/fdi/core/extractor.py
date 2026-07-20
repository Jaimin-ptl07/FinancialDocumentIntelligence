"""Schema-driven RAG field extraction -- pure algorithms (design spec sec
3.1). No I/O, no config, no clock/RNG, and no import of fdi.pipeline/
fdi.platform or any other fdi.core producer (fdi.core.retrieval/tables/
agent) -- the field-extraction schema and every candidate are read/built as
plain Mappings/dataclasses this module owns, the same "reads inputs as
plain Mappings" posture fdi.core.retrieval.build_chunks and fdi.core.agent's
extract_citations already establish.

parse_field_specs flattens the schema's THREE-level v3.1 nesting --
modules -> retrieval_units -> fields -- into FieldSpecs. v3.1 replaced
v2.0's flat modules[*].fields, and with it dropped rag_extractable,
source_type, keywords, enum_values, and the per-field retrieval_query. So
the old "keep only rag_extractable=true AND source_type=document_content"
filter is GONE, not reimplemented: in v3.1 a field exists only inside a
retrieval unit, and a retrieval unit IS document context by construction
(the schema's own retrieval_model: "Retrieve-once, extract-many. Retrieval
is per Retrieval Unit, not per field"). Pipeline-sourced metadata fields
are simply absent from the file. retrieval_query is read off the UNIT
(falling back to a per-field one if a later revision adds it back), and
keywords are the field's `aliases` followed by the unit's
`locator_keywords`, order-preserving deduplicated -- keyword_overlap
divides by len(keywords), so a duplicate would silently dilute the boost.

rerank_candidates is the model-free blend reranker (decision 3):
score = retrieval_score + keyword_weight * keyword_overlap(text, keywords),
stable-sorted descending, truncated to top_k. build_extraction_messages/
assemble_result are the two halves of the ExtractorClient contract: the
prompt IN, the LLM's raw dict OUT -> the schema's output_contract record,
with fallbacks so a malformed LLM response degrades to a well-formed record
rather than raising (decisions 8, 10). Every function here is total: no
raise, on any input shape, anywhere.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_NUMERIC_DATA_TYPES = frozenset({"decimal", "integer"})


@dataclass(frozen=True)
class FieldSpec:
    """One extractable field, flattened out of its module and retrieval unit.

    The five unit/module members after `module_name` carry defaults so a
    hand-built FieldSpec (tests, and any future caller that synthesizes one
    outside the parser) still constructs with the v2.0-era argument set.
    """

    field_name: str
    data_type: str
    retrieval_query: str
    keywords: tuple[str, ...]
    extraction_prompt: str
    enum_values: tuple[str, ...] | None
    module_id: int
    module_name: str
    module_key: str = ""
    unit_id: str = ""
    unit_title: str = ""
    scope: str | None = None
    statement_type: str | None = None
    field_path: str = ""
    periods: tuple[str, ...] = ()
    locator_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    evidence_id: str
    text: str
    page: int | None
    score: float
    source_tool: str


_TABLE_NEEDING_DATA_TYPES = frozenset({"decimal", "integer", "list"})

_UNIT_TOP_K_MIN = 20
_UNIT_TOP_K_MAX = 60


@dataclass(frozen=True)
class UnitSpec:
    """One Retrieval Unit: the fields sharing a single retrieval + a single
    LLM call. Schema v3.1's own retrieval_model states "Retrieve-once,
    extract-many. Retrieval is per Retrieval Unit, not per field."."""

    unit_id: str
    unit_title: str
    retrieval_query: str
    locator_keywords: tuple[str, ...]
    fields: tuple[FieldSpec, ...]


def group_fields_by_unit(fields: Sequence[FieldSpec]) -> list[UnitSpec]:
    """Group `fields` by `unit_id`, order-preserving on first appearance so
    the caller can restore input order afterwards. A field with a blank
    `unit_id` (a hand-built FieldSpec) becomes its OWN singleton unit rather
    than being merged with every other blank-id field into one bogus group.
    Unit metadata is taken from the first field of each group."""
    order: list[str] = []
    grouped: dict[str, list[FieldSpec]] = {}
    for i, field in enumerate(fields):
        key = field.unit_id if field.unit_id else f"\x00singleton:{i}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(field)

    units: list[UnitSpec] = []
    for key in order:
        members = grouped[key]
        head = members[0]
        units.append(UnitSpec(
            unit_id=head.unit_id,
            unit_title=head.unit_title,
            retrieval_query=head.retrieval_query,
            locator_keywords=head.locator_keywords,
            fields=tuple(members),
        ))
    return units


def unit_top_k(n_fields: int) -> int:
    """How many reranked candidates a unit's single LLM call receives:
    `max(20, min(60, n_fields * 2 + 10))`."""
    return max(_UNIT_TOP_K_MIN, min(_UNIT_TOP_K_MAX, n_fields * 2 + 10))


def unit_needs_tables(unit: UnitSpec) -> bool:
    """True when any field wants a number or a list. `list` is deliberate:
    subsidiary/JV disclosures are AOC-1 style TABLES, and treating them as
    narrative strips the very chunks those fields need."""
    return any(f.data_type in _TABLE_NEEDING_DATA_TYPES for f in unit.fields)


def _module_matches(module: Mapping[str, object], selector: str) -> bool:
    """v3.1 modules carry `key` (stable id, e.g. "module_3_balance_sheet")
    and `name` (human label, e.g. "Balance Sheet") -- and no `folder`, which
    v2.0's selector also accepted. Either one matches, exactly: `key` is what
    a stored UI selection should send, `name` is what a person types."""
    return module.get("key") == selector or module.get("name") == selector


def _str_tuple(value: object) -> tuple[str, ...]:
    """`value` filtered to non-empty `str`s, or `()` for anything else."""
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def _dedup(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return tuple(out)


def _non_blank_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _field_spec_from(
    field: Mapping[str, object],
    *,
    unit: Mapping[str, object],
    module_id: int,
    module_name: str,
    module_key: str,
) -> FieldSpec | None:
    """One v3.1 schema field dict (plus its owning retrieval unit) ->
    FieldSpec, or None if malformed (missing/wrong-typed field_name,
    data_type, or extraction_prompt, or no non-blank retrieval_query on
    either the unit or the field) -- parse_field_specs skips a None, never
    raises."""
    field_name = field.get("field_name")
    data_type = field.get("data_type")
    extraction_prompt = field.get("extraction_prompt")
    if not isinstance(field_name, str) or not field_name:
        return None
    if not isinstance(data_type, str) or not data_type:
        return None
    if not isinstance(extraction_prompt, str) or not extraction_prompt:
        return None

    retrieval_query = _non_blank_str(unit.get("retrieval_query")) or _non_blank_str(
        field.get("retrieval_query")
    )
    if retrieval_query is None:
        return None

    keywords = _dedup(
        _str_tuple(field.get("aliases")) + _str_tuple(unit.get("locator_keywords"))
    )

    enum_values_raw = field.get("enum_values")
    enum_values = (
        tuple(v for v in enum_values_raw if isinstance(v, str))
        if isinstance(enum_values_raw, list)
        else None
    )

    unit_id = unit.get("unit_id")
    unit_title = unit.get("title")

    field_path = field.get("field_path")
    periods = _str_tuple(field.get("periods"))
    locator_keywords = _str_tuple(unit.get("locator_keywords"))

    return FieldSpec(
        field_name=field_name, data_type=data_type, retrieval_query=retrieval_query,
        keywords=keywords, extraction_prompt=extraction_prompt, enum_values=enum_values,
        module_id=module_id, module_name=module_name, module_key=module_key,
        unit_id=unit_id if isinstance(unit_id, str) else "",
        unit_title=unit_title if isinstance(unit_title, str) else "",
        scope=_non_blank_str(unit.get("scope")),
        statement_type=_non_blank_str(unit.get("statement_type")),
        field_path=field_path if isinstance(field_path, str) else "",
        periods=periods,
        locator_keywords=locator_keywords,
    )


def parse_field_specs(schema: Mapping[str, object], module: str | None = None) -> list[FieldSpec]:
    """Flatten schema["modules"][*]["retrieval_units"][*]["fields"] into
    FieldSpecs (v3.1 nesting; v2.0's flat modules[*]["fields"] is gone, and
    so are its rag_extractable/source_type filters -- see the module
    docstring). `module`, if given, keeps only the module whose "key" or
    "name" equals it (exact match). Malformed module/unit/field entries are
    skipped, never raise; a malformed/missing "modules", "retrieval_units",
    or "fields" list contributes nothing."""
    modules_raw = schema.get("modules")
    modules = modules_raw if isinstance(modules_raw, list) else []

    specs: list[FieldSpec] = []
    for m in modules:
        if not isinstance(m, Mapping):
            continue
        if module is not None and not _module_matches(m, module):
            continue
        module_id_raw = m.get("id")
        module_id = module_id_raw if isinstance(module_id_raw, int) else -1
        module_name_raw = m.get("name")
        module_name = module_name_raw if isinstance(module_name_raw, str) else ""
        module_key_raw = m.get("key")
        module_key = module_key_raw if isinstance(module_key_raw, str) else ""

        units_raw = m.get("retrieval_units")
        units = units_raw if isinstance(units_raw, list) else []
        for u in units:
            if not isinstance(u, Mapping):
                continue
            fields_raw = u.get("fields")
            fields = fields_raw if isinstance(fields_raw, list) else []
            for f in fields:
                if not isinstance(f, Mapping):
                    continue
                spec = _field_spec_from(
                    f, unit=u, module_id=module_id, module_name=module_name,
                    module_key=module_key,
                )
                if spec is not None:
                    specs.append(spec)
    return specs


def is_numeric(data_type: str) -> bool:
    return data_type in _NUMERIC_DATA_TYPES


def keyword_overlap(text: str, keywords: Sequence[str]) -> float:
    """Fraction of `keywords` (casefolded) present as substrings of `text`
    (casefolded). 0.0 when `keywords` is empty."""
    if not keywords:
        return 0.0
    text_cf = text.casefold()
    hits = sum(1 for kw in keywords if kw.casefold() in text_cf)
    return hits / len(keywords)


def rerank_candidates(
    candidates: Sequence[Candidate],
    keywords: Sequence[str],
    *,
    top_k: int = 8,
    keyword_weight: float = 1.0,
) -> list[Candidate]:
    """Stable-sort `candidates` descending by (score + keyword_weight *
    keyword_overlap(text, keywords)), take the first `top_k`. Python's sort
    is stable even under reverse=True (equal-key elements keep their
    original relative order, per the stdlib docs), so ties need no
    secondary tiebreak key."""

    def blended(c: Candidate) -> float:
        return c.score + keyword_weight * keyword_overlap(c.text, keywords)

    return sorted(candidates, key=blended, reverse=True)[:top_k]


def build_extraction_messages(field: FieldSpec, candidates: Sequence[Candidate]) -> tuple[str, str]:
    """(system, user) messages for ExtractorClient.extract. system states
    the JSON output_contract and the "only the given context" constraint,
    plus four field-agnostic extraction rules learned from real-document
    extraction misses (multi-year columns, statement-vs-notes duplication,
    label wording drift, unit fidelity -- see module docstring's design
    spec); user carries field.extraction_prompt plus every candidate
    labeled with its evidence_id/page/text, so the LLM can cite one back."""
    system = (
        "You extract ONE field's value from financial-document context. Use "
        "ONLY the context given below -- never prior knowledge of a specific "
        "company or filing. "
        "When a context row/line shows more than one period column (financial "
        "statements commonly print the current year then the prior year side "
        "by side), return the CURRENT reporting-year value -- the FIRST / "
        "most-recent period column -- never a prior-year comparative. "
        "When the same figure appears in more than one place, prefer the "
        "value from the PRIMARY financial statement (Balance Sheet, "
        "Statement of Profit and Loss, or Cash Flow Statement) over a note, "
        "schedule, or other disclosure that restates it. "
        "The field may be labeled differently than its name in the "
        "document -- recognize LABEL SYNONYMS and extract a value whose "
        "MEANING matches even when the wording differs (e.g. \"closing cash "
        "balance\" = \"Cash and cash equivalents\" at year-end; \"revenue\" "
        "= \"Revenue from operations\"). "
        "Return the number exactly as printed in the context -- do not "
        "rescale, convert, or reinterpret units. "
        "Respond with a single JSON object, nothing else, "
        "shaped exactly like this: "
        '{"value": <the extracted value, or null if not present in the '
        'context>, "source": {"page": <int or null>, "snippet": <verbatim '
        'supporting text or null>, "evidence_ids": [<the evidence_id(s) you '
        'used>]}, "confidence": <float 0.0-1.0>}. '
        "If the context does not contain the answer, set value to null and "
        "confidence to 0.0 -- never guess."
    )
    lines = [field.extraction_prompt, "", "Context:"]
    for c in candidates:
        lines.append(f"[{c.evidence_id}] (page {c.page}): {c.text}")
    return system, "\n".join(lines)


def _clamp_confidence(raw: object) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(1.0, value))


def assemble_result(
    field: FieldSpec, raw: Mapping[str, object], ranked: Sequence[Candidate]
) -> dict[str, object]:
    """The LLM's raw {value, source, confidence} dict -> the schema's
    output_contract record `{field_name, value, source: {page, snippet,
    evidence_ids}, confidence}`, filling fallbacks for a missing/malformed
    key from the top ranked candidate (decision 8) so a garbled LLM
    response still yields a well-formed record. Never raises."""
    value = raw.get("value")

    source_raw = raw.get("source")
    source = source_raw if isinstance(source_raw, Mapping) else {}

    page = source.get("page")
    if not isinstance(page, int) or isinstance(page, bool):
        page = ranked[0].page if ranked else None

    snippet = source.get("snippet")
    if not isinstance(snippet, str):
        snippet = ranked[0].text if ranked else None

    evidence_ids_raw = source.get("evidence_ids")
    if isinstance(evidence_ids_raw, list) and all(isinstance(e, str) for e in evidence_ids_raw):
        evidence_ids: list[str] = evidence_ids_raw
    else:
        evidence_ids = [c.evidence_id for c in ranked]

    return {
        "field_name": field.field_name,
        "value": value,
        "source": {"page": page, "snippet": snippet, "evidence_ids": evidence_ids},
        "confidence": _clamp_confidence(raw.get("confidence")),
    }


# Markers are deliberately SPECIFIC. A bare "consolidated financial
# statements" also occurs inside standalone-section narrative (policies,
# related-party notes), and matching it would set the boundary too early and
# silently drop standalone content -- the exact failure this filter exists to
# prevent.
_CONSOLIDATED_MARKERS = (
    "independent auditor's report on consolidated financial statements",
    "independent auditors' report on consolidated financial statements",
    "consolidated balance sheet",
    "consolidated statement of profit and loss",
    "consolidated statement of cash flow",
)

_TABLE_CHUNK_RE = re.compile(r"^t\d+(r\d+)?$")


def find_consolidated_start_page(chunks: Sequence[Mapping[str, object]]) -> int | None:
    """First page of the document's Consolidated section, or None when it has
    none (making the filter a no-op).

    Schema v3.1 never requests consolidated data -- the string "consolidat"
    appears ZERO times in it -- while a consolidated section restates every
    primary statement with DIFFERENT values. Returning the minimum page across
    all marker matches lets the caller drop that whole region.

    Never raises: a malformed chunk, a non-str text/heading, or a non-int page
    is skipped."""
    pages: list[int] = []
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        page = chunk.get("page")
        if not isinstance(page, int) or isinstance(page, bool):
            continue
        parts = [chunk.get("heading"), chunk.get("text")]
        haystack = " ".join(p for p in parts if isinstance(p, str)).casefold()
        if not haystack:
            continue
        if any(marker in haystack for marker in _CONSOLIDATED_MARKERS):
            pages.append(page)
    return min(pages) if pages else None


def filter_candidates(
    candidates: Sequence[Candidate],
    *,
    consolidated_start_page: int | None,
    needs_tables: bool,
) -> list[Candidate]:
    """Scope a unit's pool before reranking.

    Drops (a) candidates at or after `consolidated_start_page` -- skipped
    entirely when it is None -- and (b) table chunks (`t{i}` / `t{i}r{j}`)
    when the unit has no numeric/list field, so short narrative paragraphs are
    not crowded out by unrelated numeric schedules sharing a keyword. A
    candidate with `page is None` is always kept: absence of a page is not
    evidence it is consolidated. Never raises."""
    out: list[Candidate] = []
    for c in candidates:
        if (
            consolidated_start_page is not None
            and isinstance(c.page, int)
            and c.page >= consolidated_start_page
        ):
            continue
        if not needs_tables:
            chunk_id = c.evidence_id.split(":", 1)[1] if ":" in c.evidence_id else c.evidence_id
            if _TABLE_CHUNK_RE.match(chunk_id):
                continue
        out.append(c)
    return out
