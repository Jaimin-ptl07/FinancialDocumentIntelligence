# RT-8 Retrieval Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register seven RT-2…RT-7 retrieval tools as LLM function-calling
schemas, build a bounded plan→call→synthesize loop over them, and expose it
as `POST /ask` — the retrieval layer's capstone.

**Architecture:** `fdi.core.agent` (pure) holds `ToolCall`/`AgentResponse`
dataclasses + citation reshaping/dedup. `fdi.pipeline.agent` is the
mockable seam (`AgentClient` Protocol + `FakeAgentClient`). `fdi.pipeline.
agent_tools` is the registry + dispatcher (`AgentContext`, `TOOL_SCHEMAS`,
`execute_tool`). `fdi.pipeline.ask.answer_question` is the loop.
`fdi.platform.agent.AzureOpenAIAgentClient` is the real, monkeypatchable
Azure OpenAI client, reusing existing `FDI_GPT_*` settings. `POST /ask`
wires it all through the same deps `/retrieve`/`/graph/*`/`/entities/*`
already use.

**Tech Stack:** Python 3.13, `uv`, `pydantic` (tool-argument validation),
`openai` SDK (already a dependency, via `fdi.platform.semantic`), SQLAlchemy
async, pytest + testcontainers.

**Design spec:** `docs/superpowers/specs/2026-07-18-rt8-retrieval-agent-design.md`
— read it first; this plan implements its §3 architecture and §2 decisions
verbatim. Decision numbers below refer to that document.

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive),
  CI-enforced, stays "1 kept, 0 broken". `fdi.core.agent` is PURE: no I/O,
  no import of `fdi.pipeline`/`fdi.platform`. `fdi.platform.agent` imports
  `fdi.core.agent` (types only) + `fdi.platform.config` — never
  `fdi.pipeline` (decision 2: this is WHY `AgentResponse`/`ToolCall` live in
  `core`, not `pipeline`).
- `AgentClient.complete` is SYNCHRONOUS (decision 4) — do not make it
  `async def`; `answer_question` is `async` because it awaits `execute_tool`.
- `execute_tool` NEVER raises for unknown tool name, bad arguments, or any
  exception inside a handler — always `{"error": "..."}` (decisions 5, 9).
- Handlers take `args: BaseModel` and immediately `cast()` to their concrete
  `*Args` subtype (not a narrower parameter type) — keeps `_DISPATCH`'s
  value type uniform and strict-mypy-clean with zero `type: ignore`
  comments, matching this codebase's `cast()`-over-ignore convention
  (`fdi.pipeline.graphquery`/`retrieval` do this throughout).
- Citation values are always `str | int | bool | None` — never a list/dict
  — required for `dedup_citations`' hashable-tuple-key dedup (decision 8).
- Per-task gates before every commit: `uv run --directory backend ruff check .`
  · `uv run --directory backend mypy --strict src` ·
  `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter`
  · the task's pytest. Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) → commit.
- No new `FDI_` environment variable; no new pytest marker (`@pytest.mark.azure`
  is reused for the opt-in real-Azure test, decision 14).

---

### Task 1: `fdi.core.agent` — `ToolCall`/`AgentResponse` + citation reshaping/dedup

**Files:**
- Create: `backend/src/fdi/core/agent.py`
- Test: `backend/tests/unit/test_agent_core.py`

**Interfaces:**
- Produces: `ToolCall`, `AgentResponse` (frozen dataclasses);
  `extract_citations(tool_name, arguments, result) -> list[dict[str, object]]`;
  `dedup_citations(citations) -> list[dict[str, object]]`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_agent_core.py
import pytest

from fdi.core.agent import AgentResponse, ToolCall, dedup_citations, extract_citations


def test_agent_response_requires_exactly_one_of_tool_calls_or_content() -> None:
    AgentResponse(content="ok")  # fine
    AgentResponse(tool_calls=[ToolCall(id="1", name="search", arguments={})])  # fine
    with pytest.raises(ValueError, match="exactly one"):
        AgentResponse()  # neither
    with pytest.raises(ValueError, match="exactly one"):
        AgentResponse(
            content="ok", tool_calls=[ToolCall(id="1", name="search", arguments={})]
        )  # both
    with pytest.raises(ValueError, match="exactly one"):
        AgentResponse(tool_calls=[])  # empty list counts as "not set"


def test_extract_citations_error_result_is_empty() -> None:
    assert extract_citations("search", {}, {"error": "boom"}) == []


def test_extract_citations_search() -> None:
    result = {"results": [
        {"doc_id": "d1", "doc_key": "k1", "version": 1, "chunk_id": "s0",
         "heading": "Balance Sheet", "page": 3, "section_type": "BALANCE_SHEET",
         "text": "...", "score": 0.9},
    ]}
    out = extract_citations("search", {"query": "q"}, result)
    assert out == [{
        "doc_id": "d1", "doc_key": "k1", "version": 1, "heading": "Balance Sheet",
        "page": 3, "chunk_id": "s0", "table_index": None, "row_label": None,
        "column_header": None, "node_id": None,
    }]


def test_extract_citations_document_structure() -> None:
    result = {
        "schema_version": 1, "doc_id": "d1",
        "document": {
            "id": "d1:doc", "type": "document", "label": "ACME",
            "citation": {"doc_id": "d1", "doc_key": "k1", "version": 1,
                         "heading": "ACME", "page": None, "bounding_regions": None},
        },
        "sections": [{
            "id": "d1:s0", "type": "section", "label": "Balance Sheet",
            "citation": {"doc_id": "d1", "doc_key": "k1", "version": 1,
                         "heading": "Balance Sheet", "page": 3, "bounding_regions": None},
            "paragraphs": [],
        }],
    }
    out = extract_citations("document_structure", {"doc_id": "d1"}, result)
    assert {"doc_id": "d1", "doc_key": "k1", "version": 1, "heading": "Balance Sheet",
            "page": 3, "chunk_id": None, "table_index": None, "row_label": None,
            "column_header": None, "node_id": "d1:s0"} in out
    assert len(out) == 2  # document node + one section, no paragraphs


def test_extract_citations_get_tables() -> None:
    result = {"tables": [{"table_index": 0, "row_count": 2, "column_count": 2,
                           "stitched": False, "grid": {}}]}
    out = extract_citations("get_tables", {"doc_id": "d1"}, result)
    assert out == [{
        "doc_id": "d1", "doc_key": None, "version": None, "heading": None, "page": None,
        "chunk_id": None, "table_index": 0, "row_label": None, "column_header": None,
        "node_id": None,
    }]


def test_extract_citations_concept_lookup() -> None:
    result = {"rows": [{
        "table_index": 0, "row_label": "Revenue",
        "values": [{"column_header": "FY2023", "raw": "100", "value": 100.0}],
    }]}
    out = extract_citations("concept_lookup", {"doc_id": "d1", "concept": "REVENUE"}, result)
    assert out == [{
        "doc_id": "d1", "doc_key": None, "version": None, "heading": None, "page": None,
        "chunk_id": None, "table_index": 0, "row_label": "Revenue",
        "column_header": "FY2023", "node_id": None,
    }]


def test_extract_citations_resolve_entities() -> None:
    result = {"entities": [{"members": [
        {"doc_id": "d1", "doc_key": "k1", "version": 1},
        {"doc_id": "d2", "doc_key": "k1", "version": 2},
    ]}]}
    out = extract_citations("resolve_entities", {}, result)
    assert len(out) == 2
    assert out[0]["doc_id"] == "d1" and out[1]["doc_id"] == "d2"


def test_extract_citations_entity_timeline() -> None:
    result = {"timeline": [{"doc_id": "d1", "doc_key": "k1", "version": 1}]}
    out = extract_citations("entity_timeline", {"doc_key": "k1"}, result)
    assert out == [{
        "doc_id": "d1", "doc_key": "k1", "version": 1, "heading": None, "page": None,
        "chunk_id": None, "table_index": None, "row_label": None, "column_header": None,
        "node_id": None,
    }]


def test_extract_citations_compare_concept_only_matched_rows() -> None:
    result = {"versions": [
        {"doc_id": "d1", "doc_key": "k1", "version": 1, "status": "matched",
         "rows": [{"table_index": 0, "row_label": "Revenue",
                    "values": [{"column_header": "FY2023", "raw": "100", "value": 100.0}]}]},
        {"doc_id": "d2", "doc_key": "k1", "version": 2, "status": "no_structure", "rows": []},
    ]}
    out = extract_citations(
        "compare_concept_across_versions", {"doc_key": "k1", "concept": "REVENUE"}, result
    )
    assert len(out) == 1
    assert out[0]["doc_id"] == "d1" and out[0]["column_header"] == "FY2023"


def test_dedup_citations_collapses_exact_duplicates_preserves_order() -> None:
    a = {"doc_id": "d1", "chunk_id": "s0"}
    b = {"doc_id": "d1", "chunk_id": "s1"}
    out = dedup_citations([a, dict(a), b, dict(a)])
    assert out == [a, b]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_agent_core.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.agent'`.

- [ ] **Step 3: Write the implementation**

```python
# backend/src/fdi/core/agent.py
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


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class AgentResponse:
    tool_calls: list[ToolCall] | None = None
    content: str | None = None

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_agent_core.py -v`
Expected: PASS (all 10).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/core/agent.py backend/tests/unit/test_agent_core.py
git commit -m "feat(core): agent.py -- ToolCall/AgentResponse + citation reshape/dedup"
```

---

### Task 2: `fdi.pipeline.agent` — `AgentClient` Protocol + `FakeAgentClient`

**Files:**
- Create: `backend/src/fdi/pipeline/agent.py`
- Test: `backend/tests/unit/test_agent_client_protocol.py`

**Interfaces:**
- Consumes: `fdi.core.agent.AgentResponse` (Task 1).
- Produces: `AgentClient` Protocol (`complete(messages, tools) -> AgentResponse`,
  synchronous); `FakeAgentClient(responses: list[AgentResponse])`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_agent_client_protocol.py
import pytest

from fdi.core.agent import AgentResponse, ToolCall
from fdi.pipeline.agent import AgentClient, FakeAgentClient


def test_fake_agent_client_replays_scripted_responses_in_order() -> None:
    r1 = AgentResponse(tool_calls=[ToolCall(id="1", name="search", arguments={"query": "q"})])
    r2 = AgentResponse(content="final")
    fake = FakeAgentClient([r1, r2])
    assert isinstance(fake, AgentClient)  # runtime_checkable structural match
    assert fake.complete([], []) is r1
    assert fake.complete([], []) is r2
    assert len(fake.calls) == 2


def test_fake_agent_client_records_messages_and_tools_per_call() -> None:
    fake = FakeAgentClient([AgentResponse(content="ok")])
    messages = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "search"}}]
    fake.complete(messages, tools)
    assert fake.calls == [(messages, tools)]


def test_fake_agent_client_raises_a_diagnosable_error_on_overcall() -> None:
    fake = FakeAgentClient([AgentResponse(content="ok")])
    fake.complete([], [])
    with pytest.raises(AssertionError, match="exhausted"):
        fake.complete([], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_agent_client_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: fdi.pipeline.agent`.

- [ ] **Step 3: Implement**

```python
# backend/src/fdi/pipeline/agent.py
"""The AgentClient seam: complete(messages, tools) -> AgentResponse.
AzureOpenAIAgentClient (fdi.platform.agent, does I/O) structurally satisfies
this without importing it -- platform may not import pipeline.
FakeAgentClient lives here for tests: no live Azure OpenAI call anywhere in
the default suite. complete() is SYNCHRONOUS, matching SemanticClient.
classify / EmbeddingClient.embed -- the established convention for external-
API client protocols in this codebase (design spec decision 4)."""

from typing import Protocol, runtime_checkable

from fdi.core.agent import AgentResponse


@runtime_checkable
class AgentClient(Protocol):
    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> AgentResponse: ...


class FakeAgentClient:
    """Replays a pre-scripted list of AgentResponses, one per call, in
    order. Raises AssertionError (not IndexError) if answer_question calls
    it more times than scripted -- a loud, test-diagnosable failure rather
    than a confusing exception deep in dict/list machinery."""

    def __init__(self, responses: list[AgentResponse]) -> None:
        self._responses = responses
        self._next = 0
        self.calls: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> AgentResponse:
        self.calls.append((messages, tools))
        if self._next >= len(self._responses):
            raise AssertionError(
                f"FakeAgentClient exhausted: {len(self._responses)} scripted "
                f"response(s), call {len(self.calls)} requested"
            )
        resp = self._responses[self._next]
        self._next += 1
        return resp
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_agent_client_protocol.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/agent.py backend/tests/unit/test_agent_client_protocol.py
git commit -m "feat(pipeline): agent.py -- AgentClient protocol + FakeAgentClient seam"
```

---

### Task 3: `fdi.pipeline.agent_tools` — `AgentContext` / `TOOL_SCHEMAS` / `execute_tool`

**Files:**
- Create: `backend/src/fdi/pipeline/agent_tools.py`
- Test: `backend/tests/unit/test_agent_tool_schemas.py` (pure schema-shape checks)
- Test: `backend/tests/integration/test_agent_tools.py` (real dispatch, testcontainers + fakes)

**Interfaces:**
- Consumes: `hybrid_search`, `document_structure` (as `document_structure_query`),
  `get_tables`, `concept_lookup`, `resolve_entities`, `entity_timeline`,
  `compare_concept_across_versions` (all existing, RT-2…RT-7 + SP-16).
- Produces: `AgentContext` (dataclass); `TOOL_SCHEMAS: list[dict[str, object]]`
  (seven entries); `execute_tool(name, arguments, *, ctx) -> dict[str, object]`.

This is the largest task. Do the pure schema-shape unit test first (Step 1a),
then the full module (Step 1b/3), then the integration dispatch test
(Step 1c) against real Postgres/MinIO — three sub-steps under one task
because `TOOL_SCHEMAS` and `execute_tool` are inseparable (a schema without
a dispatched handler, or vice versa, is a broken tool).

- [ ] **Step 1a: Write the failing schema-shape unit test**

```python
# backend/tests/unit/test_agent_tool_schemas.py
def test_tool_schemas_has_seven_entries_all_dispatched() -> None:
    from fdi.pipeline.agent_tools import _DISPATCH, TOOL_SCHEMAS

    assert len(TOOL_SCHEMAS) == 7
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert names == set(_DISPATCH.keys())


def test_every_schema_required_is_subset_of_properties() -> None:
    from fdi.pipeline.agent_tools import TOOL_SCHEMAS

    for schema in TOOL_SCHEMAS:
        params = schema["function"]["parameters"]
        required = set(params.get("required", []))
        properties = set(params["properties"].keys())
        assert required <= properties, schema["function"]["name"]


def test_every_schema_has_a_non_empty_description() -> None:
    from fdi.pipeline.agent_tools import TOOL_SCHEMAS

    for schema in TOOL_SCHEMAS:
        assert schema["function"]["description"].strip()
```

- [ ] **Step 2a: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_agent_tool_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: fdi.pipeline.agent_tools`.

- [ ] **Step 3: Write the implementation**

```python
# backend/src/fdi/pipeline/agent_tools.py
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
    filters = {
        "section_type": a.section_type.value if a.section_type is not None else None,
        "doc_key": a.doc_key, "version": a.version,
    }
    results = await hybrid_search(
        session=ctx.session, store=ctx.store, embedder=ctx.embedder,
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


# TOOL_SCHEMAS -- see design spec section 3.3 for the full, final text of
# all seven; paste verbatim here (names/descriptions/parameters MUST match
# the Args models above field-for-field -- the integration test below
# checks this by calling execute_tool with arguments shaped exactly like
# each schema declares).
TOOL_SCHEMAS: list[dict[str, object]] = [
    # ... (7 entries, exact text from design spec section 3.3)
]
```

> Every handler in `_DISPATCH` has the SAME signature — `(args: BaseModel,
> ctx: AgentContext) -> Awaitable[dict[str, object]]` — because each handler
> takes `args: BaseModel` and casts internally (this task's Global
> Constraints note) rather than a narrower per-tool parameter type. That
> uniformity is what lets `_DISPATCH`'s value type be the single precise
> alias `_Handler` above, with no `object`-typed fallback and no `cast()` at
> the `execute_tool` call site — only inside each handler, narrowing `args`
> to its own concrete `*Args` subtype.

- [ ] **Step 4a: Run schema test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_agent_tool_schemas.py -v`
Expected: PASS (all 3).

- [ ] **Step 1c: Write the failing integration dispatch test**

```python
# backend/tests/integration/test_agent_tools.py
"""execute_tool against real Postgres/MinIO with Fake embedder/vector_store/
graph_store -- no live Azure/Milvus/Neo4j. Reuses fixture patterns from
test_hybrid_search.py (search), test_tables_lookup.py (get_tables/
concept_lookup), test_graphquery.py (document_structure), test_resolve_
entities.py (resolve_entities), test_compare_concept.py/test_entity_
timeline.py (entity_timeline/compare_concept_across_versions)."""

import pytest

pytestmark = pytest.mark.integration

# ... session_factory/store fixtures identical to the sibling RT integration
# files (migrated_engine, minio_client -- see any of the files above).


async def test_unknown_tool_returns_structured_error(session_factory, store) -> None:
    from fdi.pipeline.agent_tools import AgentContext, execute_tool

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("not_a_real_tool", {}, ctx=ctx)
    assert out == {"error": "unknown tool 'not_a_real_tool'"}


async def test_bad_arguments_returns_structured_error(session_factory, store) -> None:
    from fdi.pipeline.agent_tools import AgentContext, execute_tool

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("search", {}, ctx=ctx)  # missing required "query"
    assert "error" in out and "search" in out["error"]


async def test_document_structure_without_graph_store_returns_structured_error(
    session_factory, store
) -> None:
    from fdi.pipeline.agent_tools import AgentContext, execute_tool

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("document_structure", {"doc_id": "d1"}, ctx=ctx)
    assert out == {"error": "document_structure is not configured: missing graph_store"}


async def test_get_tables_no_structure_artifact_yet_returns_structured_error(
    session_factory, store
) -> None:
    from fdi.pipeline.agent_tools import AgentContext, execute_tool
    from fdi.pipeline.intake import intake_document

    doc = await intake_document(
        pdf=b"%PDF-1.7\n%fake\n", filename="a.pdf", doc_key="k1",
        content_type="application/pdf", session_factory=session_factory, store=store,
    )
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("get_tables", {"doc_id": doc.id}, ctx=ctx)
    assert "error" in out


async def test_entity_timeline_unknown_doc_key_returns_structured_error(
    session_factory, store
) -> None:
    from fdi.pipeline.agent_tools import AgentContext, execute_tool

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("entity_timeline", {"doc_key": "no-such-key"}, ctx=ctx)
    assert "error" in out


# Happy-path coverage: one test per tool, each building the minimal fixture
# its underlying RT test file already builds (reuse those fixtures/helpers
# rather than re-deriving OCR payloads) -- search (chunks+index via
# FakeEmbeddingClient/FakeVectorStore), document_structure (FakeGraphStore.
# load), get_tables/concept_lookup (numeric-table OCR fixture from
# test_tables_lookup.py), resolve_entities (two documents, embedding
# similarity), entity_timeline/compare_concept_across_versions (same
# doc_key, multiple versions). Assert each returns the tool's real result
# shape (not an error), e.g. `"results" in out` for search,
# `"tables" in out` for get_tables, etc.
```

- [ ] **Step 2c: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_agent_tools.py -v`
Expected: FAIL initially on collection/import if `TOOL_SCHEMAS`/`execute_tool`
aren't complete yet; once Step 3 lands, the five error-path tests above pass
immediately (they only need `execute_tool` + `AgentContext`) — write and
green the seven happy-path tests next, one at a time, each pointed at the
named sibling fixture file for its exact OCR/stage-run pattern.

- [ ] **Step 4c: Run full file to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_agent_tools.py -v`
Expected: PASS (5 error-path + 7 happy-path = 12).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/agent_tools.py backend/tests/unit/test_agent_tool_schemas.py backend/tests/integration/test_agent_tools.py
git commit -m "feat(pipeline): agent_tools.py -- TOOL_SCHEMAS + execute_tool dispatch over RT-2..RT-7/SP-16"
```

---

### Task 4: `fdi.pipeline.ask.answer_question` (the loop)

**Files:**
- Create: `backend/src/fdi/pipeline/ask.py`
- Test: `backend/tests/integration/test_ask.py`

**Interfaces:**
- Consumes: `AgentClient`/`FakeAgentClient` (Task 2), `AgentContext`/
  `TOOL_SCHEMAS`/`execute_tool` (Task 3), `extract_citations`/
  `dedup_citations` (Task 1).
- Produces: `answer_question(*, question, ctx, agent_client, max_rounds=5)
  -> dict[str, object]` → `{answer, citations, tool_trace}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_ask.py
"""answer_question end-to-end: a scripted FakeAgentClient calling ONE real
tool (search -> hybrid_search) against real Postgres/MinIO with
FakeOcr/FakeSemantic/FakeEmbedding/FakeVectorStore -- NO Azure OpenAI call
anywhere in this file. Reuses test_hybrid_search.py's hybrid_corpus-style
build (fresh uuid4 marker in OCR text to dodge the session-scoped-DB
collision class every other RT integration test already guards against)."""

import uuid

import pytest

from fdi.core.agent import AgentResponse, ToolCall
from fdi.pipeline.agent import FakeAgentClient
from fdi.pipeline.agent_tools import AgentContext
from fdi.pipeline.ask import answer_question
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.chunks import ChunksStage
from fdi.pipeline.stages.embedding import EmbeddingStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.index import IndexStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.pipeline.vectorstore import FakeVectorStore

pytestmark = pytest.mark.integration

# session_factory/store fixtures: identical to test_hybrid_search.py's.


@pytest.fixture
async def marked_doc(session_factory, store):
    """One fully-processed, doubly-indexed document with a fresh uuid4
    marker embedded in its text (Balance Sheet / Total Assets), mirroring
    test_hybrid_search.py's hybrid_corpus fixture but for ONE document."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage(), EmbeddingStage(), IndexStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()

    fake_ocr = FakeOcrClient({
        "content": "ACME",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": f"Total Assets 100 {marker}"},
        ],
        "tables": [],
    })
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="bs.pdf",
        doc_key=f"acme-{marker}", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": FakeSemanticClient({"Balance Sheet": "BALANCE_SHEET"})}, **common,
    )
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services={"embedder": embedder}, **common,
    )
    await run_stage(
        stage_name="index", run_id=str(uuid.uuid4()), services={"vector_store": vector_store}, **common,
    )
    return doc, marker, embedder, vector_store


async def test_answer_question_calls_one_real_tool_then_synthesizes_cited_answer(
    session_factory, store, marked_doc
) -> None:
    doc, marker, embedder, vector_store = marked_doc
    query = f"Total Assets 100 {marker}"
    fake = FakeAgentClient([
        AgentResponse(tool_calls=[ToolCall(id="c1", name="search", arguments={"query": query})]),
        AgentResponse(content=f"Total Assets were 100 (see {doc.id}:s0)."),
    ])
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=embedder, vector_store=vector_store,
                            graph_store=None)
        result = await answer_question(
            question=f"What were total assets, marker {marker}?",
            ctx=ctx, agent_client=fake, max_rounds=5,
        )

    assert result["answer"] == f"Total Assets were 100 (see {doc.id}:s0)."
    assert len(fake.calls) == 2
    assert len(result["tool_trace"]) == 1
    assert result["tool_trace"][0]["tool"] == "search"
    assert result["tool_trace"][0]["error"] is None
    citations = result["citations"]
    assert any(c["doc_id"] == doc.id and c["chunk_id"] == "s0" for c in citations)


async def test_answer_question_exhausts_max_rounds_gracefully(session_factory, store) -> None:
    always_tool_calls = [
        AgentResponse(tool_calls=[ToolCall(id=f"c{i}", name="search",
                                            arguments={"query": "zzzznonexistenttoken9999"})])
        for i in range(3)
    ]
    fake = FakeAgentClient(always_tool_calls)
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        result = await answer_question(
            question="anything", ctx=ctx, agent_client=fake, max_rounds=3,
        )
    assert "3 tool-call round" in result["answer"]
    assert result["citations"] == []
    assert len(result["tool_trace"]) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_ask.py -v`
Expected: FAIL — `ModuleNotFoundError: fdi.pipeline.ask`.

- [ ] **Step 3: Implement**

```python
# backend/src/fdi/pipeline/ask.py
"""RT-8's agent loop: answer_question(question, ctx, agent_client,
max_rounds=5) -> {answer, citations, tool_trace}. Plans (agent_client.
complete), executes (execute_tool against ctx's real injected services),
feeds results back, repeats up to max_rounds, then either returns the
LLM's final content or a locally-built best-effort answer on exhaustion
(design spec decision 12 -- no extra "force a final answer" LLM call)."""

import json
from collections.abc import Mapping

import structlog

from fdi.core.agent import dedup_citations, extract_citations
from fdi.pipeline.agent import AgentClient
from fdi.pipeline.agent_tools import TOOL_SCHEMAS, AgentContext, execute_tool

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are a financial-document analyst. Answer the user's question using "
    "ONLY facts returned by your tools -- never rely on prior knowledge of a "
    "specific company or filing. Call tools as needed; when you have enough "
    "information, give a final answer that (1) directly answers the "
    "question, (2) cites the specific document/section/table/period each "
    "fact came from, and (3) says plainly if the tools could not find an "
    "answer rather than guessing."
)


async def answer_question(
    *, question: str, ctx: AgentContext, agent_client: AgentClient, max_rounds: int = 5
) -> dict[str, object]:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_trace: list[dict[str, object]] = []
    raw_citations: list[dict[str, object]] = []

    for round_num in range(max_rounds):
        resp = agent_client.complete(messages, TOOL_SCHEMAS)
        if resp.tool_calls is None:
            return {
                "answer": resp.content or "",
                "citations": dedup_citations(raw_citations),
                "tool_trace": tool_trace,
            }

        messages.append({
            "role": "assistant",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in resp.tool_calls
            ],
        })
        for tc in resp.tool_calls:
            result = await execute_tool(tc.name, tc.arguments, ctx=ctx)
            error = result.get("error") if isinstance(result, Mapping) else None
            tool_trace.append({
                "round": round_num, "tool": tc.name, "arguments": tc.arguments,
                "result": result, "error": error,
            })
            raw_citations.extend(extract_citations(tc.name, tc.arguments, result))
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str),
            })

    log.warning("ask.max_rounds_exhausted", max_rounds=max_rounds)
    return {
        "answer": f"Could not fully answer within {max_rounds} tool-call round(s).",
        "citations": dedup_citations(raw_citations),
        "tool_trace": tool_trace,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_ask.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/ask.py backend/tests/integration/test_ask.py
git commit -m "feat(pipeline): ask.answer_question -- bounded plan/call/synthesize loop"
```

---

### Task 5: `fdi.platform.agent.AzureOpenAIAgentClient`

**Files:**
- Create: `backend/src/fdi/platform/agent.py`
- Test: `backend/tests/unit/test_azure_agent_client.py`
- Test: `backend/tests/azure/test_real_agent.py` (opt-in, `@pytest.mark.azure`)

**Interfaces:**
- Consumes: `fdi.core.agent.AgentResponse`/`ToolCall` (Task 1); `openai.
  AzureOpenAI` (module-level, monkeypatchable); `fdi.platform.config.Settings`.
- Produces: `AzureOpenAIAgentClient` (structurally satisfies `fdi.pipeline.
  agent.AgentClient`); `from_settings(settings) -> AzureOpenAIAgentClient | None`.

- [ ] **Step 1: Write the failing unit test**

```python
# backend/tests/unit/test_azure_agent_client.py
"""AzureOpenAIAgentClient wraps the openai SDK. The SDK is monkeypatched --
no live Azure OpenAI call. Mirrors test_azure_openai_client.py exactly."""

import json

import pytest


def _client_returning(monkeypatch: pytest.MonkeyPatch, msg: object) -> dict[str, object]:
    from fdi.platform import agent as ag

    calls: dict[str, object] = {}

    class _Choice:
        message = msg

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs: object) -> _Resp:
            calls["kwargs"] = kwargs
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            calls["init"] = kwargs

        chat = _Chat()

    monkeypatch.setattr(ag, "AzureOpenAI", _Client)
    return calls


def test_agent_client_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.agent import AzureOpenAIAgentClient

    class _Fn:
        name = "search"
        arguments = json.dumps({"query": "revenue"})

    class _TC:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = None
        tool_calls = [_TC()]

    calls = _client_returning(monkeypatch, _Msg())
    c = AzureOpenAIAgentClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    resp = c.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert resp.tool_calls == [
        __import__("fdi.core.agent", fromlist=["ToolCall"]).ToolCall(
            id="call_1", name="search", arguments={"query": "revenue"}
        )
    ]
    assert calls["kwargs"]["tool_choice"] == "auto"


def test_agent_client_parses_final_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.agent import AzureOpenAIAgentClient

    class _Msg:
        content = "the answer"
        tool_calls = None

    _client_returning(monkeypatch, _Msg())
    c = AzureOpenAIAgentClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    resp = c.complete([], [])
    assert resp.content == "the answer" and resp.tool_calls is None


def test_agent_client_malformed_tool_json_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.agent import AzureOpenAIAgentClient

    class _Fn:
        name = "search"
        arguments = "{not valid json"

    class _TC:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = None
        tool_calls = [_TC()]

    _client_returning(monkeypatch, _Msg())
    c = AzureOpenAIAgentClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    resp = c.complete([], [])
    assert resp.tool_calls is not None
    assert "_malformed_json" in resp.tool_calls[0].arguments


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.agent import AzureOpenAIAgentClient
    from fdi.platform.config import Settings

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    assert AzureOpenAIAgentClient.from_settings(s) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_azure_agent_client.py -v`
Expected: FAIL — `ModuleNotFoundError: fdi.platform.agent`.

- [ ] **Step 3: Implement**

```python
# backend/src/fdi/platform/agent.py
"""AzureOpenAIAgentClient -- AgentClient backed by Azure OpenAI (GPT).
Lives in platform (does I/O), not pipeline: the AgentClient protocol and
FakeAgentClient live in fdi.pipeline.agent. This module structurally
satisfies that protocol without importing it -- platform may not import
pipeline per the layered architecture contract. AzureOpenAI is imported at
module level so tests can monkeypatch fdi.platform.agent.AzureOpenAI -- no
live Azure OpenAI call in the default suite. Reuses the SAME FDI_GPT_*
settings the semantic stage (fdi.platform.semantic) already uses -- no new
config."""

import json

from openai import AzureOpenAI

from fdi.core.agent import AgentResponse, ToolCall
from fdi.platform.config import Settings


class AzureOpenAIAgentClient:
    """AgentClient backed by Azure OpenAI. Lives in platform (does I/O)."""

    def __init__(
        self, *, endpoint: str, key: str, deployment: str, model: str, api_version: str
    ) -> None:
        self._sdk = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
        self._deployment = deployment
        self._model = model

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> AgentResponse:
        resp = self._sdk.chat.completions.create(
            model=self._deployment, messages=messages, tools=tools,
            tool_choice="auto", temperature=0,
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            calls = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    # Malformed model output. Do NOT crash the loop --
                    # produce a ToolCall whose args execute_tool's own
                    # pydantic validation will reject the normal way (the
                    # expected fields are simply absent from this dict).
                    args = {"_malformed_json": tc.function.arguments}
                calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
            return AgentResponse(tool_calls=calls)
        return AgentResponse(content=msg.content or "")

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOpenAIAgentClient | None":
        if not settings.gpt_endpoint or not settings.gpt_key:
            return None
        return AzureOpenAIAgentClient(
            endpoint=settings.gpt_endpoint, key=settings.gpt_key,
            deployment=settings.gpt_deployment, model=settings.gpt_model,
            api_version=settings.gpt_api_version,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_azure_agent_client.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Write + run the opt-in real-Azure test**

```python
# backend/tests/azure/test_real_agent.py
"""One real Azure OpenAI tool-calling round trip. Mirrors
test_real_semantic.py exactly: reuses @pytest.mark.azure (no new marker,
design spec decision 14), skipped without FDI_GPT_ENDPOINT/FDI_GPT_KEY."""

import os

import pytest

from fdi.platform.agent import AzureOpenAIAgentClient
from fdi.platform.config import get_settings

pytestmark = [
    pytest.mark.azure,
    pytest.mark.skipif(
        not (os.getenv("FDI_GPT_ENDPOINT") and os.getenv("FDI_GPT_KEY")),
        reason="Azure OpenAI not configured",
    ),
]


def test_real_agent_client_produces_a_well_formed_response() -> None:
    from fdi.pipeline.agent_tools import TOOL_SCHEMAS

    client = AzureOpenAIAgentClient.from_settings(get_settings())
    assert client is not None
    resp = client.complete(
        [{"role": "user", "content": "What tools can you use? Answer in one sentence, "
                                      "do not call a tool for this question."}],
        TOOL_SCHEMAS,
    )
    # AgentResponse.__post_init__ already guarantees exactly one of
    # tool_calls/content is set -- this only needs to confirm the SDK round
    # trip didn't raise, and that a tool_calls branch (if hit) names a real
    # registered tool.
    if resp.tool_calls is not None:
        names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        assert all(tc.name in names for tc in resp.tool_calls)
```

Run: `uv run --directory backend pytest tests/azure -k test_real_agent -v -m azure`
Expected (creds set): PASS. Expected (no creds): `1 skipped`.

- [ ] **Step 6: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/platform/agent.py backend/tests/unit/test_azure_agent_client.py backend/tests/azure/test_real_agent.py
git commit -m "feat(platform): agent.py -- AzureOpenAIAgentClient, reuses FDI_GPT_* settings"
```

---

### Task 6: API wiring — `POST /ask`

**Files:**
- Modify: `backend/src/fdi/api/deps.py` (add `get_agent_client`/`AgentClientDep`)
- Modify: `backend/src/fdi/api/app.py` (lifespan: `app.state.agent_client`)
- Modify: `backend/src/fdi/api/schemas.py` (add `AskRequest`/`AskCitationView`/
  `AskToolTraceEntryView`/`AskResponse`)
- Modify: `backend/src/fdi/api/routes.py` (add `POST /ask`)
- Modify: `backend/tests/integration/test_api.py` (append `# RT-8: POST /ask`
  section, following every prior RT sub-project's own convention)

**Interfaces:**
- Consumes: `answer_question` (Task 4), `AgentContext` (Task 3),
  `AzureOpenAIAgentClient` (Task 5), `SessionDep`/`StoreDep`/`EmbedderDep`/
  `VectorStoreDep`/`GraphStoreDep`/`EntityThresholdDep` (existing).
- Produces: `POST /ask {question, max_rounds?} -> {answer, citations, tool_trace}`,
  503 when `agent_client` is unconfigured.

- [ ] **Step 1: Write the failing tests** (append to `test_api.py`, after the
  RT-5 graph-retrieval section)

```python
# --------------------------------------------------------------------------
# RT-8: POST /ask -- the retrieval agent.
# --------------------------------------------------------------------------


async def test_ask_503_when_agent_client_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/ask", json={"question": "What was revenue?"})
    assert r.status_code == 503
    assert "agent_client" in r.json()["detail"]


async def test_ask_returns_cited_answer_with_fake_agent_client_override(
    make_client, session_factory, store, _alembic_applied
) -> None:
    from fdi.api.deps import get_agent_client
    from fdi.core.agent import AgentResponse, ToolCall
    from fdi.pipeline.agent import FakeAgentClient
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.retrieval import index_chunks_fts
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.semantic import SemanticStage
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    import uuid

    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    fake_ocr = FakeOcrClient({
        "content": "ACME",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": f"Total Assets 100 {marker}"},
        ],
        "tables": [],
    })
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="bs.pdf",
        doc_key=f"acme-{marker}", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": FakeSemanticClient({"Balance Sheet": "BALANCE_SHEET"})}, **common,
    )
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()

    fake = FakeAgentClient([
        AgentResponse(tool_calls=[ToolCall(
            id="c1", name="search", arguments={"query": f"Total Assets 100 {marker}"}
        )]),
        AgentResponse(content=f"Total Assets were 100 (see {doc.id}:s0)."),
    ])

    async with make_client(
        dependency_overrides={get_agent_client: lambda: fake}
    ) as client:
        r = await client.post("/ask", json={"question": f"total assets, marker {marker}?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == f"Total Assets were 100 (see {doc.id}:s0)."
    assert body["citations"]
    assert body["tool_trace"][0]["tool"] == "search"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k test_ask -v`
Expected: FAIL — `404` (no `/ask` route yet) / `ImportError: get_agent_client`.

- [ ] **Step 3: Implement**

In `backend/src/fdi/api/deps.py`, add near the other `*Dep` pairs:

```python
from fdi.pipeline.agent import AgentClient

def get_agent_client(request: Request) -> AgentClient | None:
    agent_client: AgentClient | None = request.app.state.agent_client
    return agent_client

AgentClientDep = Annotated[AgentClient | None, Depends(get_agent_client)]
```

In `backend/src/fdi/api/app.py`'s `lifespan`, alongside the existing
`embedder`/`vector_store`/`graph_store` lines:

```python
from fdi.platform.agent import AzureOpenAIAgentClient
# ...
app.state.agent_client = AzureOpenAIAgentClient.from_settings(settings)
```

In `backend/src/fdi/api/schemas.py`, append the four view models from
design spec §3.6 verbatim.

In `backend/src/fdi/api/routes.py`, add the imports (`AgentClientDep`,
`AskRequest`/`AskResponse`, `AgentContext`, `answer_question`) and the route
from design spec §3.6 verbatim, in a new section comment block after the
RT-5 graph section:

```python
# --------------------------------------------------------------------------
# RT-8: POST /ask -- the retrieval agent. Plans over TOOL_SCHEMAS (every
# RT-2..RT-7/SP-16 tool registered via fdi.pipeline.agent_tools), executes
# against the SAME injected services /retrieve, /graph/*, /entities/* use,
# and returns a cited answer. 503s ONLY when agent_client is unconfigured --
# an unconfigured embedder/vector_store/graph_store degrades tool-by-tool
# (design spec decision 10), not at the route.
# --------------------------------------------------------------------------


@router.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
    graph_store: GraphStoreDep,
    entity_threshold: EntityThresholdDep,
    agent_client: AgentClientDep,
) -> AskResponse:
    if agent_client is None:
        raise HTTPException(
            status_code=503,
            detail="ask is not configured: missing agent_client (set FDI_GPT_ENDPOINT/FDI_GPT_KEY)",
        )
    ctx = AgentContext(
        session=session, store=store, embedder=embedder, vector_store=vector_store,
        graph_store=graph_store, default_entity_threshold=entity_threshold,
    )
    max_rounds = body.max_rounds if body.max_rounds is not None else 5
    result = await answer_question(
        question=body.question, ctx=ctx, agent_client=agent_client, max_rounds=max_rounds,
    )
    return AskResponse.model_validate(result)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k test_ask -v`
Expected: PASS (both).

Run the whole file to confirm nothing else regressed:
`uv run --directory backend pytest tests/integration/test_api.py -v`

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/deps.py backend/src/fdi/api/app.py backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(api): POST /ask -- the RT-8 retrieval agent route"
```

---

### Task 7: Full gates, roadmap + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-rt8-retrieval-agent-design.md` (Status → Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (RT-8 row → done + "what RT-8 shipped" handoff)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`,
  `PYTHONUTF8=1 lint-imports` (1 kept/0 broken). Grep-confirm `fdi.core.agent`
  imports neither `fdi.pipeline` nor `fdi.platform`; confirm `fdi.platform.
  agent` imports neither `fdi.pipeline.agent` nor `fdi.pipeline.agent_tools`.

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`
  (default: `-m "not integration and not e2e and not azure"` per
  `tests/azure/README.md`'s documented fast loop, or whatever the project's
  CI-equivalent full run is) then the `integration` marker set with
  containers up; azure/neo4j/milvus/bge stay opt-in and skip without creds/
  env. Note the passed count against the prior total.

- [ ] **Step 3: Audit RT-8 done criteria** (§7 of the design spec) — map
  each numbered item to its task; confirm the default-suite `test_ask.py`
  end-to-end test passes with zero live Azure calls; confirm the opt-in
  `tests/azure/test_real_agent.py` skips cleanly without creds.

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps 1–3 green).

- [ ] **Step 5: Roadmap** — flip the RT-8 row to `✅ done`; add a "What RT-8
  shipped" handoff covering: the seven-tool curated set + why
  `vector_search`/`keyword_search`/`neighbors`/`reading_order` were left
  out; the `fdi.core.agent`-not-`fdi.pipeline.agent` placement of
  `AgentResponse`/`ToolCall` and why (the layering puzzle decision 2
  solves); the flat superset `Citation` shape + whole-dict dedup; the
  `max_rounds=5` default and the "no forced final-synthesis call" choice on
  exhaustion — both explicitly flagged as human forks in the design spec,
  worth restating here for anyone revisiting the tuning later; that `/ask`
  is the retrieval roadmap's last row (RT-1…RT-8 complete).

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: RT-8 done-criteria audit, spec status, roadmap handoff -- retrieval layer complete"
```

---

## Self-Review

**Spec coverage:** §3.1 `fdi.core.agent` → Task 1. §3.2 `fdi.pipeline.agent`
→ Task 2. §3.3 `AgentContext`/`TOOL_SCHEMAS`/`execute_tool` → Task 3. §3.4
`answer_question` → Task 4. §3.5 `AzureOpenAIAgentClient` → Task 5. §3.6 API
wiring → Task 6. §2 decisions: curated tool set (d1) → Task 3's `TOOL_
SCHEMAS`/`_DISPATCH`; `AgentResponse` placement in `core` (d2) → Task 1 +
Task 5's import list (never imports `fdi.pipeline`); either/or invariant
(d3) → Task 1's `__post_init__` + its test; sync `complete` (d4) → Task 2/5
signatures; pydantic Args validation (d5) → Task 3; hand-authored schemas
(d6) → Task 3's `TOOL_SCHEMAS` literal + its schema-shape unit test; flat
Citation shape (d7) → Task 1's `_cite`/`extract_citations`; whole-dict dedup
(d8) → Task 1's `dedup_citations`; catch-all in `execute_tool` (d9) → Task
3's `execute_tool`; 503-only-on-agent_client (d10) → Task 6's route; `max_
rounds` default+override (d11) → Task 4's signature + Task 6's `AskRequest`;
no-forced-synthesis exhaustion (d12) → Task 4's final return + its
exhaustion test; no streaming (d13) → not built anywhere, confirmed at Task
7; reused `azure` marker (d14) → Task 5's `test_real_agent.py`. §4 error
handling → Tasks 1, 3, 4 tests. §5 testing → Tasks 1–6, one file each,
matching the design spec's file list exactly. §6 deferred → not built
(Task 7 audit). §7 done criteria → Task 7.

**Placeholder scan:** Tasks 1, 2, 4, 5 carry complete, pasteable code. Task
3's `TOOL_SCHEMAS` literal is EXPLICITLY marked "paste verbatim from design
spec section 3.3" rather than re-typed here, to avoid two documents drifting
on hand-authored LLM-facing text (decision 6's own risk, made worse by a
THIRD copy) — the executor must copy it from the design spec, not
improvise; Task 3's integration test happy-path bodies are intentionally
delegated to NAMED existing fixture files (`test_hybrid_search.py`,
`test_tables_lookup.py`, `test_graphquery.py`, `test_resolve_entities.py`,
`test_compare_concept.py`, `test_entity_timeline.py`) — the executor reuses
their exact OCR/stage-run patterns rather than inventing new ones, same
posture as RT-1 Task 2/3's delegation to `test_knowledge_stage.py`/
`test_index_stage.py`.

**Type consistency:** `ToolCall`/`AgentResponse` (Task 1, `fdi.core.agent`)
constructed unchanged by `FakeAgentClient` (Task 2) and
`AzureOpenAIAgentClient` (Task 5) — both import them from `core`, neither
imports the other's layer. `AgentContext` (Task 3) constructed by `answer_
question`'s caller (Task 4's test, Task 6's route) with the SAME five
service fields both places. `execute_tool(name, arguments, *, ctx) -> dict`
(Task 3) is the exact signature `answer_question` (Task 4) calls. `TOOL_
SCHEMAS` (Task 3) is the exact list `answer_question` passes to `agent_
client.complete` (Task 4) and the exact list the opt-in real-Azure test
(Task 5) exercises. `AskRequest.max_rounds: int | None` (Task 6) maps to
`answer_question`'s `max_rounds: int = 5` (Task 4) via the route's
conditional-kwargs dict, never silently coercing `None` into a call.

**Risk flagged:** Task 3's `_DISPATCH` originally risked needing a
`type: ignore` for its handler dispatch (a heterogeneous callable table is a
known strict-mypy rough edge) — resolved during self-review by giving every
handler the SAME signature (`args: BaseModel, ctx: AgentContext ->
Awaitable[dict[str, object]]`, each casting `args` to its own concrete
`*Args` subtype internally) and typing `_DISPATCH`'s values with one precise
`Callable` alias (`_Handler`) instead of `object` + a call-site cast. No
`type: ignore` should be needed at all now; if `mypy --strict` still
objects, the fallback is the same one-line `# type: ignore[arg-type]` noted
in Task 3's Global-Constraints-adjacent comment, not a redesign. Task 6's
`AskRequest.max_rounds` optionality (`int | None`) resolves to a concrete
`int` via a plain ternary before calling `answer_question` — an earlier
draft of this plan used a conditional `**kwargs` unpack instead, which was
replaced during self-review: it forced `mypy --strict` to reason about a
`dict[str, int]` unpacked against three differently-typed keyword-only
parameters for no real benefit over a one-line ternary.
