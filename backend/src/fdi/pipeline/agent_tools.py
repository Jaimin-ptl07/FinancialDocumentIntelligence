"""The RT-8 tool registry + dispatcher: TOOL_SCHEMAS (the seven OpenAI
function-calling schemas an LLM sees) and execute_tool (maps a tool NAME +
LLM-provided arguments to a real RT-2..RT-7/SP-16 pipeline call, injecting
session/store/embedder/vector_store/graph_store from an AgentContext).

Curated set (design spec decision 1): search (hybrid_search, RT-4, subsumes
vector_search/keyword_search), document_structure (RT-5, subsumes
neighbors/reading_order), get_tables + concept_lookup (RT-6),
resolve_entities (SP-16 -- the doc_key DISCOVERY step RT-7's own handoff
says an agent needs), entity_timeline + compare_concept_across_versions
(RT-7). Every handler degrades to {"error": ...} rather than raising:
unknown tool name, invalid arguments (pydantic ValidationError), a None
result from an underlying pipeline function (no artifact / unknown doc_key
yet), an unconfigured graph_store, or any other exception -- see design
spec section 4."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.ontology import CanonicalConcept
from fdi.core.semantic import SectionType
from fdi.pipeline.crossdoc import resolve_entities as resolve_entities_
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.entities import compare_concept_across_versions as compare_concept_
from fdi.pipeline.entities import entity_timeline as entity_timeline_
from fdi.pipeline.graphquery import document_structure as document_structure_
from fdi.pipeline.graphstore import GraphStore
from fdi.pipeline.retrieval import hybrid_search
from fdi.pipeline.tables import concept_lookup as concept_lookup_
from fdi.pipeline.tables import get_tables as get_tables_
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.storage.artifact_store import ArtifactStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AgentContext:
    session: AsyncSession
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    graph_store: GraphStore | None
    default_entity_threshold: float = 0.85


# --------------------------------------------------------------------------
# Argument models -- validation only (design spec decision 5). Kept
# field-name-adjacent to TOOL_SCHEMAS below, NOT derived from it (decision
# 6): the LLM-facing schema and the internal validation model serve
# different audiences and are hand-kept-in-sync, exercised by this file's
# integration test.
# --------------------------------------------------------------------------


class _SearchArgs(BaseModel):
    query: str
    k: int = 5
    section_type: SectionType | None = None
    doc_key: str | None = None
    version: int | None = None


class _DocIdArgs(BaseModel):
    doc_id: str


class _GetTablesArgs(BaseModel):
    doc_id: str
    keyword: str | None = None


class _ConceptLookupArgs(BaseModel):
    doc_id: str
    concept: CanonicalConcept


class _ResolveEntitiesArgs(BaseModel):
    threshold: float | None = None


class _DocKeyArgs(BaseModel):
    doc_key: str


class _CompareConceptArgs(BaseModel):
    doc_key: str
    concept: CanonicalConcept


# --------------------------------------------------------------------------
# Handlers. Each takes `args: BaseModel` (not its concrete subtype) and
# immediately casts -- keeps _DISPATCH's value type uniform and
# strict-mypy-clean with zero `type: ignore`, matching this codebase's
# cast()-over-ignore convention.
# --------------------------------------------------------------------------


async def _search(args: BaseModel, ctx: AgentContext) -> dict[str, object]:
    a = cast(_SearchArgs, args)
    if ctx.embedder is None or ctx.vector_store is None:
        return {"error": "search is unavailable: vector store not configured"}
    filters = {
        "section_type": a.section_type.value if a.section_type is not None else None,
        "doc_key": a.doc_key, "version": a.version,
    }
    results = await hybrid_search(
        session=ctx.session, embedder=ctx.embedder,
        vector_store=ctx.vector_store, query=a.query, k=a.k, filters=filters,
    )
    return {"results": results}


async def _document_structure(args: BaseModel, ctx: AgentContext) -> dict[str, object]:
    a = cast(_DocIdArgs, args)
    if ctx.graph_store is None:
        return {"error": "document_structure is not configured: missing graph_store"}
    return await document_structure_(
        session=ctx.session, store=ctx.store, graph_store=ctx.graph_store, doc_id=a.doc_id,
    )


async def _get_tables(args: BaseModel, ctx: AgentContext) -> dict[str, object]:
    a = cast(_GetTablesArgs, args)
    tables = await get_tables_(
        session=ctx.session, store=ctx.store, doc_id=a.doc_id, keyword=a.keyword,
    )
    if tables is None:
        return {"error": f"document {a.doc_id!r} has no structure artifact yet"}
    return {"tables": tables}


async def _concept_lookup(args: BaseModel, ctx: AgentContext) -> dict[str, object]:
    a = cast(_ConceptLookupArgs, args)
    rows = await concept_lookup_(
        session=ctx.session, store=ctx.store, doc_id=a.doc_id, concept=a.concept,
    )
    if rows is None:
        return {"error": f"document {a.doc_id!r} has no structure artifact yet"}
    return {"rows": rows}


async def _resolve_entities(args: BaseModel, ctx: AgentContext) -> dict[str, object]:
    a = cast(_ResolveEntitiesArgs, args)
    threshold = a.threshold if a.threshold is not None else ctx.default_entity_threshold
    return await resolve_entities_(session=ctx.session, store=ctx.store, threshold=threshold)


async def _entity_timeline(args: BaseModel, ctx: AgentContext) -> dict[str, object]:
    a = cast(_DocKeyArgs, args)
    result = await entity_timeline_(session=ctx.session, store=ctx.store, doc_key=a.doc_key)
    if result is None:
        return {"error": f"no documents found for doc_key {a.doc_key!r}"}
    return result


async def _compare_concept(args: BaseModel, ctx: AgentContext) -> dict[str, object]:
    a = cast(_CompareConceptArgs, args)
    result = await compare_concept_(
        session=ctx.session, store=ctx.store, doc_key=a.doc_key, concept=a.concept,
    )
    if result is None:
        return {"error": f"no documents found for doc_key {a.doc_key!r}"}
    return result


_Handler = Callable[[BaseModel, AgentContext], Awaitable[dict[str, object]]]

_DISPATCH: dict[str, tuple[type[BaseModel], _Handler]] = {
    "search": (_SearchArgs, _search),
    "document_structure": (_DocIdArgs, _document_structure),
    "get_tables": (_GetTablesArgs, _get_tables),
    "concept_lookup": (_ConceptLookupArgs, _concept_lookup),
    "resolve_entities": (_ResolveEntitiesArgs, _resolve_entities),
    "entity_timeline": (_DocKeyArgs, _entity_timeline),
    "compare_concept_across_versions": (_CompareConceptArgs, _compare_concept),
}


async def execute_tool(
    name: str, arguments: Mapping[str, object], *, ctx: AgentContext
) -> dict[str, object]:
    entry = _DISPATCH.get(name)
    if entry is None:
        return {"error": f"unknown tool {name!r}"}
    args_model, handler = entry
    try:
        args = args_model.model_validate(dict(arguments))
    except ValidationError as exc:
        return {"error": f"invalid arguments for {name!r}: {exc}"}
    try:
        return await handler(args, ctx)
    except Exception as exc:  # noqa: BLE001 -- deliberate, design spec decision 9
        log.warning("agent_tool.failed", tool=name, error=repr(exc))
        return {"error": f"tool {name!r} failed: {exc!r}"}


# UNKNOWN is CanonicalConcept's un-matched fallback bucket, not a concept an
# LLM should ever deliberately request -- excluded from both schemas below.
_CONCEPT_ENUM = [c.value for c in CanonicalConcept if c != CanonicalConcept.UNKNOWN]

# TOOL_SCHEMAS -- see design spec section 3.3 for the full, final text of
# all seven; names/descriptions/parameters MUST match the Args models above
# field-for-field -- the integration test below checks this by calling
# execute_tool with arguments shaped exactly like each schema declares.
TOOL_SCHEMAS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Semantic + keyword search across ALL ingested financial "
                "documents. Returns the best-matching passages with "
                "citations (doc_id, doc_key, version, heading, page). Use "
                "this first for any question about document CONTENT that "
                "is not a specific numeric line item."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language search text."},
                    "k": {"type": "integer", "description": "Max results.", "default": 5},
                    "section_type": {
                        "type": "string",
                        "enum": [t.value for t in SectionType],
                        "description": "Optional: restrict to one statement type.",
                    },
                    "doc_key": {
                        "type": "string",
                        "description": "Optional: restrict to one filing family.",
                    },
                    "version": {
                        "type": "integer",
                        "description": "Optional: restrict to one version (needs doc_key).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "document_structure",
            "description": (
                "Return the full section/paragraph outline of ONE document "
                "(by doc_id), each node cited with its page. Use this to "
                "see how a document is organized, or after `search` "
                "returns a doc_id and you need surrounding context."
            ),
            "parameters": {
                "type": "object",
                "properties": {"doc_id": {"type": "string"}},
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tables",
            "description": (
                "List a document's tables, optionally filtered by a "
                "keyword against column headers. Use this to find WHICH "
                "table has relevant data before extracting a specific "
                "concept with concept_lookup."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "keyword": {
                        "type": "string",
                        "description": "Optional case-insensitive column-header substring.",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "concept_lookup",
            "description": (
                "Find every table row in ONE document whose label matches "
                "a canonical financial concept, with every numeric "
                "column/period value. Use this for 'what was X in this "
                "filing' questions. Matching is synonym/substring based "
                "and can false-positive on related lines (e.g. 'Cost of "
                "revenue' may match REVENUE) -- check row_label before "
                "trusting a match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "concept": {"type": "string", "enum": _CONCEPT_ENUM},
                },
                "required": ["doc_id", "concept"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_entities",
            "description": (
                "Discover which documents in the corpus likely belong to "
                "the same company/filer, clustered by document-embedding "
                "similarity. Call this FIRST when a question names a "
                "company but you don't already have its doc_key, to find "
                "candidate doc_keys before calling entity_timeline or "
                "compare_concept_across_versions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "number",
                        "description": "Optional similarity threshold override, 0-1.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "entity_timeline",
            "description": (
                "Return the version-ordered filing timeline for one "
                "doc_key, with per-version section/concept counts and "
                "quality. Use this after resolve_entities gives you a "
                "doc_key, to answer 'what filings exist for X'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"doc_key": {"type": "string"}},
                "required": ["doc_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_concept_across_versions",
            "description": (
                "Compare a canonical financial concept across EVERY "
                "version of a doc_key, in filing order. Each version has "
                "status matched/no_match/no_structure -- no_structure "
                "means 'not yet processed', no_match means 'processed, "
                "concept not reported this way'; do not treat them as the "
                "same thing. Use this for 'how did X's revenue change "
                "over time' questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_key": {"type": "string"},
                    "concept": {"type": "string", "enum": _CONCEPT_ENUM},
                },
                "required": ["doc_key", "concept"],
            },
        },
    },
]
