"""Schema-driven RAG field extraction -- pure algorithms (design spec sec
3.1). No I/O, no config, no clock/RNG, and no import of fdi.pipeline/
fdi.platform or any other fdi.core producer (fdi.core.retrieval/tables/
agent) -- the field-extraction schema and every candidate are read/built as
plain Mappings/dataclasses this module owns, the same "reads inputs as
plain Mappings" posture fdi.core.retrieval.build_chunks and fdi.core.agent's
extract_citations already establish.

parse_field_specs flattens the schema's modules into FieldSpecs, keeping
ONLY rag_extractable=true AND source_type="document_content" fields (design
spec sec 2 decision 2) -- every other field (metadata/computed/constant/
external_lookup/not_extracted) is pipeline-sourced and never reaches this
extractor. rerank_candidates is the model-free blend reranker (decision 3):
score = retrieval_score + keyword_weight * keyword_overlap(text, keywords),
stable-sorted descending, truncated to top_k. build_extraction_messages/
assemble_result are the two halves of the ExtractorClient contract: the
prompt IN, the LLM's raw dict OUT -> the schema's output_contract record,
with fallbacks so a malformed LLM response degrades to a well-formed record
rather than raising (decisions 8, 10). Every function here is total: no
raise, on any input shape, anywhere.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_NUMERIC_DATA_TYPES = frozenset({"decimal", "integer"})


@dataclass(frozen=True)
class FieldSpec:
    field_name: str
    data_type: str
    retrieval_query: str
    keywords: tuple[str, ...]
    extraction_prompt: str
    enum_values: tuple[str, ...] | None
    module_id: int
    module_name: str


@dataclass(frozen=True)
class Candidate:
    evidence_id: str
    text: str
    page: int | None
    score: float
    source_tool: str


def _module_matches(module: Mapping[str, object], selector: str) -> bool:
    return module.get("name") == selector or module.get("folder") == selector


def _field_spec_from(
    field: Mapping[str, object], *, module_id: int, module_name: str
) -> FieldSpec | None:
    """One schema field dict -> FieldSpec, or None if malformed (missing/
    wrong-typed field_name, data_type, retrieval_query, or
    extraction_prompt) -- parse_field_specs skips a None, never raises."""
    field_name = field.get("field_name")
    data_type = field.get("data_type")
    retrieval_query = field.get("retrieval_query")
    extraction_prompt = field.get("extraction_prompt")
    if not isinstance(field_name, str) or not field_name:
        return None
    if not isinstance(data_type, str) or not data_type:
        return None
    if not isinstance(retrieval_query, str) or not retrieval_query.strip():
        return None
    if not isinstance(extraction_prompt, str) or not extraction_prompt:
        return None

    keywords_raw = field.get("keywords")
    keywords = (
        tuple(k for k in keywords_raw if isinstance(k, str))
        if isinstance(keywords_raw, list)
        else ()
    )

    enum_values_raw = field.get("enum_values")
    enum_values = (
        tuple(v for v in enum_values_raw if isinstance(v, str))
        if isinstance(enum_values_raw, list)
        else None
    )

    return FieldSpec(
        field_name=field_name, data_type=data_type, retrieval_query=retrieval_query,
        keywords=keywords, extraction_prompt=extraction_prompt, enum_values=enum_values,
        module_id=module_id, module_name=module_name,
    )


def parse_field_specs(schema: Mapping[str, object], module: str | None = None) -> list[FieldSpec]:
    """Flatten schema["modules"][*]["fields"] into FieldSpecs, keeping only
    rag_extractable=true AND source_type="document_content" fields (decision
    2). `module`, if given, keeps only the module whose "name" or "folder"
    equals it (exact match). Malformed field/module entries are skipped,
    never raise; a malformed/missing "modules" list returns []."""
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

        fields_raw = m.get("fields")
        fields = fields_raw if isinstance(fields_raw, list) else []
        for f in fields:
            if not isinstance(f, Mapping):
                continue
            if f.get("rag_extractable") is not True:
                continue
            if f.get("source_type") != "document_content":
                continue
            spec = _field_spec_from(f, module_id=module_id, module_name=module_name)
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


def _source_key(candidate: Candidate) -> str:
    """The diversity-cap grouping key for a candidate's `evidence_id`:
    `table:{doc}:{i}` -> `t{i}` (a get_tables-sourced numeric-field
    candidate); `{doc}:t{i}r{j}` or `{doc}:t{i}` -> `t{i}` (a hybrid-search
    hit on a table row or whole-table chunk); `{doc}:s{i}` -> `s{i}` (a
    section chunk); anything else -> the whole id, unchanged."""
    evidence_id = candidate.evidence_id
    if evidence_id.startswith("table:"):
        parts = evidence_id.split(":", 2)
        if len(parts) == 3:
            return f"t{parts[2]}"
        return evidence_id

    if ":" in evidence_id:
        _doc, _, local = evidence_id.rpartition(":")
        if local.startswith("t"):
            rest = local[1:]
            i = rest.split("r", 1)[0]
            if i.isdigit():
                return f"t{i}"
        elif local.startswith("s") and local[1:].isdigit():
            return local

    return evidence_id


def rerank_candidates(
    candidates: Sequence[Candidate],
    keywords: Sequence[str],
    *,
    top_k: int = 8,
    keyword_weight: float = 1.0,
    max_per_source: int | None = None,
) -> list[Candidate]:
    """Stable-sort `candidates` descending by (score + keyword_weight *
    keyword_overlap(text, keywords)), take the first `top_k`. Python's sort
    is stable even under reverse=True (equal-key elements keep their
    original relative order, per the stdlib docs), so ties need no
    secondary tiebreak key.

    `max_per_source`, when set, caps how many of the top-scoring candidates
    may share a `_source_key` (Phase 7 diversity cap): after sorting, greedily
    take up to `top_k` candidates in score order, skipping any candidate whose
    source has already reached the cap, so a single table can't consume the
    whole context window. `None` (the default) is byte-identical to the prior,
    uncapped behavior."""

    def blended(c: Candidate) -> float:
        return c.score + keyword_weight * keyword_overlap(c.text, keywords)

    ranked = sorted(candidates, key=blended, reverse=True)
    if max_per_source is None:
        return ranked[:top_k]

    out: list[Candidate] = []
    per_source: dict[str, int] = {}
    for c in ranked:
        key = _source_key(c)
        if per_source.get(key, 0) >= max_per_source:
            continue
        out.append(c)
        per_source[key] = per_source.get(key, 0) + 1
        if len(out) >= top_k:
            break
    return out


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
