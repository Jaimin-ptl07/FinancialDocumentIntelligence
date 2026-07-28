"""The agent's pure data shapes + citation reshaping/dedup. No I/O.

ToolCall/AgentResponse live here (not fdi.pipeline.agent) because
fdi.platform.agent.AzureOpenAIAgentClient must construct an AgentResponse to
satisfy fdi.pipeline.agent.AgentClient structurally without importing
pipeline (platform may not import pipeline) -- see design spec decision 2.
extract_citations/dedup_citations are the one place every RT tool's
differently-shaped result becomes ONE Citation shape
({doc_id, doc_key, version, heading, page, chunk_id, table_index, row_label,
column_header, node_id}, absent fields None), so answer_question and the API
view never branch on which tool produced a fact."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fdi.core.pricing import LlmCallUsage


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class AgentResponse:
    tool_calls: list[ToolCall] | None = None
    content: str | None = None
    # Populated by fdi.platform.agent.AzureOpenAIAgentClient.complete from the
    # SDK response's own `usage` object -- costing feature (docs/superpowers/
    # specs/2026-07-28-costing-feature-design.md). None from every fake/test
    # double, which is the correct "no real Azure call happened" signal:
    # answer_question skips recording usage when this is None.
    usage: LlmCallUsage | None = None

    def __post_init__(self) -> None:
        has_calls = bool(self.tool_calls)
        has_content = self.content is not None
        if has_calls == has_content:  # both True, or both False
            raise ValueError(
                "AgentResponse must set exactly one of tool_calls (non-empty list) "
                "or content (a string) -- mirrors an OpenAI completion's "
                "tool_calls/stop exclusivity."
            )


_EMPTY_CITATION: dict[str, object] = {
    "doc_id": None, "doc_key": None, "version": None, "heading": None,
    "page": None, "chunk_id": None, "table_index": None, "row_label": None,
    "column_header": None, "node_id": None,
}


def _cite(**overrides: object) -> dict[str, object]:
    return {**_EMPTY_CITATION, **overrides}


def _member_citations(members: object) -> list[dict[str, object]]:
    if not isinstance(members, list):
        return []
    out = []
    for m in members:
        if isinstance(m, Mapping):
            out.append(_cite(doc_id=m.get("doc_id"), doc_key=m.get("doc_key"),
                              version=m.get("version")))
    return out


def _node_citations(node: object) -> list[dict[str, object]]:
    """One document/section/paragraph node (fdi.pipeline.graphquery's
    citation-wrapped shape: {..., "citation": {doc_id, doc_key, version,
    heading, page, bounding_regions}}) -> one Citation, recursing into
    "sections"/"paragraphs" if present."""
    if not isinstance(node, Mapping):
        return []
    out: list[dict[str, object]] = []
    citation = node.get("citation")
    if isinstance(citation, Mapping):
        out.append(_cite(
            doc_id=citation.get("doc_id"), doc_key=citation.get("doc_key"),
            version=citation.get("version"), heading=citation.get("heading"),
            page=citation.get("page"), node_id=node.get("id"),
        ))
    for key in ("sections", "paragraphs"):
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                out.extend(_node_citations(child))
    return out


def extract_citations(
    tool_name: str, arguments: Mapping[str, object], result: object
) -> list[dict[str, object]]:
    """Reshape one registered tool's raw result into the common Citation
    shape. A result carrying "error" (execute_tool's structured-failure
    shape) always extracts to [] -- a failed call cites nothing."""
    if isinstance(result, Mapping) and "error" in result:
        return []
    if not isinstance(result, Mapping):
        return []

    if tool_name == "search":
        hits = result.get("results")
        if not isinstance(hits, list):
            return []
        return [
            _cite(doc_id=h.get("doc_id"), doc_key=h.get("doc_key"), version=h.get("version"),
                  heading=h.get("heading"), page=h.get("page"), chunk_id=h.get("chunk_id"))
            for h in hits if isinstance(h, Mapping)
        ]

    if tool_name == "document_structure":
        out: list[dict[str, object]] = []
        out.extend(_node_citations(result.get("document")))
        sections = result.get("sections")
        if isinstance(sections, list):
            for s in sections:
                out.extend(_node_citations(s))
        return out

    if tool_name == "get_tables":
        doc_id = arguments.get("doc_id")
        tables = result.get("tables")
        if not isinstance(tables, list):
            return []
        return [
            _cite(doc_id=doc_id, table_index=t.get("table_index"))
            for t in tables if isinstance(t, Mapping)
        ]

    if tool_name == "concept_lookup":
        doc_id = arguments.get("doc_id")
        rows = result.get("rows")
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            values = row.get("values")
            if isinstance(values, list):
                for v in values:
                    if isinstance(v, Mapping):
                        out.append(_cite(
                            doc_id=doc_id, table_index=row.get("table_index"),
                            row_label=row.get("row_label"), column_header=v.get("column_header"),
                        ))
        return out

    if tool_name == "resolve_entities":
        entities = result.get("entities")
        if not isinstance(entities, list):
            return []
        out = []
        for e in entities:
            if isinstance(e, Mapping):
                out.extend(_member_citations(e.get("members")))
        return out

    if tool_name == "entity_timeline":
        return _member_citations(result.get("timeline"))

    if tool_name == "compare_concept_across_versions":
        versions = result.get("versions")
        if not isinstance(versions, list):
            return []
        out = []
        for v in versions:
            if not isinstance(v, Mapping):
                continue
            rows = v.get("rows")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                values = row.get("values")
                if isinstance(values, list):
                    for val in values:
                        if isinstance(val, Mapping):
                            out.append(_cite(
                                doc_id=v.get("doc_id"), doc_key=v.get("doc_key"),
                                version=v.get("version"), table_index=row.get("table_index"),
                                row_label=row.get("row_label"),
                                column_header=val.get("column_header"),
                            ))
        return out

    return []  # unregistered tool name -- defensive, should not happen


def dedup_citations(citations: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """First-seen order, whole-dict equality. Every Citation value MUST be
    str | int | bool | None (never a list/dict) -- extract_citations is the
    sole producer and is unit-tested against this."""
    seen: set[tuple[tuple[str, object], ...]] = set()
    out: list[dict[str, object]] = []
    for c in citations:
        key = tuple(sorted(c.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(c))
    return out
