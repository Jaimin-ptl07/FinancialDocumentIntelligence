# RT-8 — Retrieval Agent (`POST /ask`)

- **Status:** Implemented 2026-07-18. The capstone of the retrieval layer
  (`docs/architecture/retrieval-roadmap.md`) — RT-1…RT-8 are now all complete.
  Registers every RT-2…RT-7 tool as an LLM function-calling tool and runs a
  bounded plan→call→synthesize loop that returns a cited answer. See
  `docs/superpowers/plans/2026-07-18-rt8-retrieval-agent.md` for the
  task-by-task execution record and the roadmap's "What RT-8 shipped" handoff
  for what a future sub-project needs to know.
- **Depends on (all done, 2026-07-18):**
  - **RT-1** — the `chunks` artifact + `{doc_id}:{chunk_id}` identity every
    text-returning tool resolves through.
  - **RT-2/RT-3/RT-4** — `fdi.pipeline.retrieval.vector_search`/
    `keyword_search`/`hybrid_search`. RT-8 registers **`hybrid_search`** only
    (see decision 1).
  - **RT-5** — `fdi.pipeline.graphquery.document_structure`/`neighbors`/
    `reading_order` (namespaced `{doc_id}:{bare_id}` node ids, citation shape
    `{doc_id, doc_key, version, heading, page, bounding_regions}`).
  - **RT-6** — `fdi.pipeline.tables.get_tables`/`concept_lookup`, reading the
    `structure` artifact on demand; `None` (no artifact yet) vs `[]` (checked,
    no match) is a real, distinct outcome RT-8 must preserve.
  - **RT-7** — `fdi.pipeline.entities.entity_timeline`/
    `compare_concept_across_versions`, `doc_key`-scoped, `status ∈ {matched,
    no_match, no_structure}` per version; `fdi.pipeline.crossdoc.
    resolve_entities` (SP-16), the corpus-wide discovery step RT-7's own
    handoff says RT-8 needs to turn a company name into candidate `doc_key`s.
  - **The mockable-seam precedent** (`fdi.platform.semantic.AzureOpenAIClient`
    + `fdi.pipeline.semantic.SemanticClient`/`FakeSemanticClient`): a
    module-level, monkeypatchable SDK import in `platform`; a `Protocol` +
    fake test double in `pipeline`; `from_settings` returns `None` when
    unconfigured. RT-8's agent seam mirrors this shape exactly, with one
    structural difference resolved in decision 2 below.
  - **`FDI_GPT_*`** (`fdi.platform.config.Settings`) — endpoint/key/
    deployment/model/api_version, already wired for the `semantic` stage.
    RT-8 reuses these unchanged; no new `FDI_` variable.

## 1. Purpose

RT-2…RT-7 built seven retrieval primitives, each independently mockable,
citation-carrying, and reachable as its own HTTP route. None of them plans —
a caller (human or UI) still has to know which tool answers which kind of
question, extract ids from one tool's output to feed the next, and stitch a
final answer together by hand. RT-8 is the one piece that makes the tool set
actually usable by an LLM: it (1) registers a curated subset of the RT tools
as OpenAI-style function-calling schemas, (2) runs a bounded loop that lets
an LLM decide which to call, executes them against the real injected
services, and feeds results back, and (3) synthesizes a final answer with an
aggregated, deduplicated citation list — exposed as `POST /ask`.

## 2. Scope decisions (autonomous; documented)

1. **The curated tool set is seven, not eleven.** `search` (→ RT-4's
   `hybrid_search`), `document_structure` (→ RT-5), `get_tables` and
   `concept_lookup` (→ RT-6), `resolve_entities` (→ SP-16/crossdoc), and
   `entity_timeline` and `compare_concept_across_versions` (→ RT-7).
   - **`vector_search`/`keyword_search` are NOT separately registered.**
     `hybrid_search` already fuses both (RRF) and gracefully degrades to
     keyword-only when the vector store is unconfigured (RT-4 decision 2) —
     giving an LLM three overlapping "search" tools invites it to guess which
     one is "better" or call more than one redundantly for the same intent;
     one decisive `search` tool is a strictly better default. A future slice
     MAY add `vector_search`/`keyword_search` back if a real corpus shows the
     agent needs to force pure-semantic or pure-lexical retrieval explicitly
     — not built here (YAGNI, §6).
   - **`neighbors`/`reading_order` (RT-5) are NOT registered.**
     `document_structure` already returns the whole section/paragraph tree
     in reading order with citations — it subsumes `reading_order`.
     `neighbors(node_id)` needs a specific **namespaced graph node id**
     (`{doc_id}:s3p1`) as input, which an LLM has no natural way to produce
     except by first reading it out of a `document_structure` result; as a
     standalone tool it mostly invites malformed ids. Deferred, not built
     (§6) — revisit if a real question needs single-node graph traversal
     `document_structure` can't answer (e.g. "what links to this footnote").
   - **`resolve_entities` IS registered**, even though the user's brief did
     not list it, because RT-7's own handoff (roadmap, "What RT-7 shipped")
     says explicitly: *"RT-8's agent that needs to answer 'how did ACME's
     revenue move' should call `POST /entities/resolve` first to discover
     candidate `doc_key`s, then loop RT-7's tools per `doc_key`."*
     `entity_timeline`/`compare_concept_across_versions` are `doc_key`-keyed,
     and no other registered tool ever surfaces a `doc_key` from a company
     *name* — without `resolve_entities` those two tools are reachable only
     if the human already knows the exact `doc_key`, which defeats the point
     of an agent. Included as tool 5 of 7.
2. **`ToolCall`/`AgentResponse` live in `fdi.core.agent`, NOT
   `fdi.pipeline.agent`.** The semantic-stage precedent (`SemanticClient.
   classify(...) -> str`) never needed this: its return type is a bare `str`,
   so `platform.semantic.AzureOpenAIClient` never has to import anything from
   `pipeline` to satisfy the protocol structurally. RT-8's `complete(...) ->
   AgentResponse` returns a **custom type**. If `AgentResponse` lived in
   `fdi.pipeline.agent` (next to the `AgentClient` Protocol, as the naive
   reading of the seam precedent suggests), `fdi.platform.agent.
   AzureOpenAIAgentClient.complete` would have to import it from `pipeline`
   to construct its return value — `platform -> pipeline` is exactly the
   layering violation the whole seam pattern exists to prevent. Putting
   `AgentResponse`/`ToolCall` in `fdi.core.agent` (pure dataclasses, no I/O)
   fixes this: both `pipeline.agent` (`platform -> core` is legal) and
   `platform.agent` (`platform -> core` is legal) import the SAME type from
   below both of them, and `platform.agent` still never imports
   `pipeline.agent` — the Protocol is satisfied structurally, exactly like
   `semantic`, just with the shared type parked one layer lower because it
   isn't a builtin. **Every future agent-return-type addition follows this
   rule**: if a `platform` client's return type is anything other than a
   builtin, it belongs in `core`, not `pipeline`.
3. **`AgentResponse` enforces "exactly one of `tool_calls`/`content`" at
   construction**, mirroring OpenAI's own `finish_reason ∈ {"tool_calls",
   "stop"}` exclusivity. A frozen dataclass with a `__post_init__` that
   raises `ValueError` on both-set or neither-set. This is a pure invariant
   check (no I/O/config/clock/RNG), the same category of thing
   `Settings._reject_unknown_fdi_env_vars` is for config — "fail loudly at
   construction" rather than let a malformed `AgentResponse` silently corrupt
   the loop. Both `FakeAgentClient` (scripted, so a test author's own bug is
   caught immediately) and `AzureOpenAIAgentClient` (must map the SDK's
   `finish_reason`/`tool_calls` cleanly into this shape) benefit equally.
4. **`AgentClient.complete` is synchronous**, matching `SemanticClient.
   classify` and `EmbeddingClient.embed` (`vector_search` calls `embedder.
   embed([query])` with no `await` — the established convention for every
   external-API client protocol in this codebase, platform clients block,
   pipeline orchestrators are `async def` around them). `answer_question`
   (§3.4) is `async` because it `await`s `execute_tool`, not because
   `complete` itself needs to be.
5. **`execute_tool` is the ONLY place LLM-provided arguments become real
   calls.** Argument validation is per-tool `pydantic.BaseModel`s
   (`fdi.pipeline.agent_tools`, private, one per tool) — reusing `pydantic`
   (already the codebase's schema-validation library, `fdi.api.schemas`) for
   its exact job here: coerce/validate an untyped `dict[str, object]` from
   an LLM into typed fields, including enum-checking `section_type` against
   `fdi.core.semantic.SectionType` and `concept` against `fdi.core.ontology.
   CanonicalConcept` for free. A `pydantic.ValidationError` becomes a
   structured `{"error": ...}` result, never a crash — same posture as an
   unknown tool name.
6. **`TOOL_SCHEMAS` (the LLM-facing JSON) is hand-authored, not derived from
   the pydantic Args models.** Deriving via `Model.model_json_schema()` was
   considered and rejected: pydantic v2 emits `$ref`/`$defs` for enum fields
   (`section_type`, `concept`), which needs flattening before it is a valid
   standalone OpenAI tool-schema `parameters` object, and the LLM-facing
   `description` strings need to be written FOR the model (task framing, not
   field documentation) — two different audiences for two different
   purposes. The seven schemas and their matching Args models are kept in
   the SAME file (`fdi.pipeline.agent_tools`), field-name-adjacent, and the
   integration test (§5) exercises `execute_tool` with arguments shaped
   exactly like the schemas declare — the drift risk is bounded and caught
   by that test, not eliminated by code generation. Revisit if the tool
   count grows enough that hand-sync becomes the bottleneck (not the case at
   seven).
7. **Citation aggregation uses one flat, superset `Citation` shape** —
   `{doc_id, doc_key, version, heading, page, chunk_id, table_index,
   row_label, column_header, node_id}`, absent fields `None` — rather than a
   union type per tool. Every tool's raw result already carries citation
   data in a DIFFERENT shape (flat hit dicts for `search`; nested `citation`
   sub-objects for `document_structure`; row/value pairs for
   `concept_lookup`/`compare_concept_across_versions`; member lists for
   `resolve_entities`/`entity_timeline`). `fdi.core.agent.extract_citations`
   is the ONE place that reshapes each tool's result into this common shape;
   downstream code (`dedup_citations`, the API's `AskCitationView`) never
   needs to know which tool a citation came from. `error` results extract to
   `[]` (a failed tool call cites nothing).
8. **Dedup is whole-dict equality**, not a partial key. `dedup_citations`
   builds a `sorted(items())` tuple per citation and drops exact repeats,
   first-seen order preserved. This is deliberately simpler than picking a
   "natural key" per citation kind (RT-1/RT-6/RT-7 each have a DIFFERENT
   natural key — `chunk_id` vs. `table_index+row_label` vs. `version` — so a
   single partial key would either be wrong for some tools or need a
   per-shape branch). Two citations that agree on every field ARE the same
   citation; two that differ in even one field (e.g. a different
   `column_header` for the same row) are legitimately different facts and
   both survive. Requires every citation value to stay hashable (`str | int
   | bool | None` — never a list/dict); `extract_citations` is the sole
   producer and is unit-tested against this invariant.
9. **`execute_tool` catches ALL exceptions from a tool handler**, not only
   validation errors — `{"error": f"tool {name!r} failed: {exc!r}"}`, logged.
   The task brief's floor is "unknown tool / bad args never crash"; this
   extends the same posture to a transient failure inside a handler (e.g. a
   DB hiccup mid-loop), matching `routes._probe`'s "a check that can raise
   cannot report" philosophy — one tool call failing must not abort the
   whole `/ask` request when the agent might route around it or ask the user
   to rephrase.
10. **`/ask` 503s ONLY on a missing `agent_client`.** `embedder`/
    `vector_store`/`graph_store` being `None` is NOT a route-level 503 —
    those are handled tool-by-tool (`search` degrades via `hybrid_search`
    itself; `document_structure` returns a structured tool-visible error if
    `graph_store` is `None`, letting the agent see it and reason around it,
    e.g. by relying on `search` instead). An LLM with no vector index but a
    working keyword index and a working Postgres should still be able to
    answer many questions — gating the whole endpoint on every possible
    tool's config would throw that away. The ONLY thing `/ask` structurally
    cannot do without is something to plan tool calls at all.
11. **`max_rounds` defaults to 5** (per the task brief) and is
    **request-overridable** (`AskRequest.max_rounds: int | None = None`,
    falls back to `answer_question`'s default when unset) — a thin,
    optional knob, not a new required concept. **Flagged as a human fork**:
    5 is a guess, not measured against a real corpus/question set; a fixed
    server-side constant (no per-request override) is the simpler
    alternative if configurability turns out to be unused.
12. **On exhaustion, `answer_question` returns a LOCALLY-built best-effort
    answer — no extra LLM call.** The alternative (one more `complete()`
    call with `tools=[]` to force a final synthesis) was considered and
    rejected for this slice: it adds an unbounded-cost round outside
    `max_rounds`'s own accounting, and it requires `FakeAgentClient`'s
    scripts to always provision an (N+1)-th response even for tests that
    only care about the bound itself. Exhaustion instead returns `{"answer":
    "Could not fully answer within {max_rounds} tool-call rounds.",
    "citations": <everything gathered so far, deduped>, "tool_trace": <every
    round>}` — deterministic, and the citations/tool_trace still let a human
    finish the job by hand. Flagged as a fork: a forced-synthesis final call
    is a legitimate product improvement, deferred (§6).
13. **No streaming.** Already decided at the roadmap level ("Explicitly
    deferred (beyond RT-8): Streaming `/ask` responses") — `POST /ask`
    returns one JSON body after the loop completes, like every other
    synchronous route in this API (`/search`, `/retrieve`, `/entities/
    resolve`). Not reopened here.
14. **The real-Azure opt-in test reuses the EXISTING `@pytest.mark.azure`
    marker** — not a new one. `tests/azure/test_real_semantic.py` already
    uses `azure` for a GPT-backed (not just Document-Intelligence-backed)
    real call gated on `FDI_GPT_ENDPOINT`/`FDI_GPT_KEY`; RT-8's real agent
    test is the same credential family and the same "opt-in, skipped without
    creds" posture. No `pyproject.toml` marker registration change needed.

## 3. Architecture

```
fdi.core.agent        (pure)      ToolCall, AgentResponse, extract_citations, dedup_citations
fdi.pipeline.agent     (pipeline)  AgentClient Protocol, FakeAgentClient
fdi.pipeline.agent_tools (pipeline) AgentContext, TOOL_SCHEMAS, execute_tool
fdi.pipeline.ask       (pipeline)  answer_question (the loop)
fdi.platform.agent     (platform)  AzureOpenAIAgentClient, from_settings
fdi.api.deps           (api)       get_agent_client / AgentClientDep
fdi.api.app            (api)       lifespan: app.state.agent_client = AzureOpenAIAgentClient.from_settings(...)
fdi.api.schemas        (api)       AskRequest / AskCitationView / AskToolTraceEntryView / AskResponse
fdi.api.routes         (api)       POST /ask
```

Layering: `fdi.pipeline.agent`/`agent_tools`/`ask` import `fdi.core.agent` +
platform stores/repos, same shape as every other `pipeline` module.
`fdi.platform.agent` imports `fdi.core.agent` (types) + `fdi.platform.config`
only — never `fdi.pipeline`. `api` imports `pipeline` as usual. Stays
1-kept/0-broken against `.importlinter`'s `api | workers → pipeline →
platform → core`.

### 3.1 `fdi.core.agent` (pure)

```python
"""The agent's pure data shapes + citation reshaping/dedup. No I/O.

ToolCall/AgentResponse live here (not fdi.pipeline.agent) because
fdi.platform.agent.AzureOpenAIAgentClient must construct an AgentResponse to
satisfy fdi.pipeline.agent.AgentClient structurally without importing
pipeline -- see design spec decision 2. extract_citations/dedup_citations are
the one place every tool's differently-shaped result becomes one Citation
shape, so answer_question and the API view never branch on tool identity."""

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
        if has_calls == has_content:  # both True or both False
            raise ValueError(
                "AgentResponse must set exactly one of tool_calls (non-empty) or content"
            )


_EMPTY_CITATION: dict[str, object] = {
    "doc_id": None, "doc_key": None, "version": None, "heading": None,
    "page": None, "chunk_id": None, "table_index": None, "row_label": None,
    "column_header": None, "node_id": None,
}


def _cite(**overrides: object) -> dict[str, object]:
    return {**_EMPTY_CITATION, **overrides}


def extract_citations(
    tool_name: str, arguments: Mapping[str, object], result: object
) -> list[dict[str, object]]:
    """Reshape one tool's raw result into the common Citation shape. A
    result carrying "error" (execute_tool's structured-failure shape)
    always extracts to [] -- a failed call cites nothing. Unknown
    tool_name (shouldn't happen -- execute_tool already rejected it before
    a result could exist) also extracts to []."""
    if isinstance(result, Mapping) and "error" in result:
        return []
    # ... one branch per of the seven registered tool names, detailed in
    # the implementation plan's Task 1 (per-tool reshaping using the exact
    # field names in fdi.api.schemas's *View models, since the pipeline
    # functions return plain dicts of the same shape those Views validate).


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

### 3.2 `fdi.pipeline.agent` (the seam)

```python
"""The AgentClient seam: complete(messages, tools) -> AgentResponse.
AzureOpenAIAgentClient (platform, does I/O) structurally satisfies this
without importing it -- platform may not import pipeline. FakeAgentClient
lives here for tests: no live Azure OpenAI call anywhere in the default
suite. complete() is SYNCHRONOUS, matching SemanticClient.classify /
EmbeddingClient.embed -- see design spec decision 4."""

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
    than a confusing KeyError deep in a fixture."""

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

### 3.3 `fdi.pipeline.agent_tools` (the registry + dispatcher)

`AgentContext` carries every injectable service `execute_tool` might need,
built by the route from the SAME deps `/retrieve`, `/graph/*`, `/entities/*`
already use:

```python
@dataclass(frozen=True)
class AgentContext:
    session: AsyncSession
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    graph_store: GraphStore | None
    default_entity_threshold: float = 0.85
```

The seven schemas (final text, written for the model — not derived, see
decision 6). `_CONCEPT_ENUM` is computed once and reused by both
concept-taking schemas, rather than repeating the filtered list
comprehension inline (and risking the two copies drifting):

```python
# UNKNOWN is CanonicalConcept's un-matched fallback bucket, not a concept an
# LLM should ever deliberately request -- excluded from both schemas below.
_CONCEPT_ENUM = [c.value for c in CanonicalConcept if c != CanonicalConcept.UNKNOWN]

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
                    "doc_key": {"type": "string", "description": "Optional: restrict to one filing family."},
                    "version": {"type": "integer", "description": "Optional: restrict to one version (needs doc_key)."},
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
                    "keyword": {"type": "string", "description": "Optional case-insensitive column-header substring."},
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
                    "threshold": {"type": "number", "description": "Optional similarity threshold override, 0-1."},
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
```

`execute_tool` — one private pydantic `*Args` model per tool (validation
only, §2 decision 5/6), a name→(model, handler) dispatch table, uniform
error handling:

```python
async def execute_tool(
    name: str, arguments: Mapping[str, object], *, ctx: AgentContext
) -> dict[str, object]:
    entry = _DISPATCH.get(name)
    if entry is None:
        return {"error": f"unknown tool {name!r}"}
    args_model, handler = entry
    try:
        args = args_model.model_validate(arguments)
    except ValidationError as exc:
        return {"error": f"invalid arguments for {name!r}: {exc}"}
    try:
        return await handler(args, ctx)
    except Exception as exc:  # noqa: BLE001 -- deliberate: see decision 9
        log.warning("agent_tool.failed", tool=name, error=repr(exc))
        return {"error": f"tool {name!r} failed: {exc!r}"}
```

Each handler wraps its RT tool 1:1 (illustrative two of seven — full seven
in the plan). Every handler takes `args: BaseModel` (NOT its concrete
`*Args` subtype) and immediately `cast()`s to it — a `_Handler = Callable[
[BaseModel, AgentContext], Awaitable[dict[str, object]]]` alias then types
EVERY entry in `_DISPATCH` identically, so `execute_tool` needs no cast at
its own call site. (A handler typed with its narrower subtype directly,
e.g. `_search(args: _SearchArgs, ...)`, is NOT assignable to that shared
alias under `mypy --strict` — parameter types are contravariant, so a
function accepting the narrower type is not substitutable where the wider
one is expected. Internal-cast-per-handler is the fix, not a workaround.):

```python
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
    return await document_structure_query(
        session=ctx.session, store=ctx.store, graph_store=ctx.graph_store, doc_id=a.doc_id,
    )
```

`get_tables`/`concept_lookup`/`entity_timeline`/`compare_concept_across_
versions` handlers each convert their pipeline function's `None` ("no
artifact"/"no such `doc_key`") into `{"error": "..."}` (never pass a bare
`None` back as a tool message body — it isn't useful JSON for an LLM to
reason over). `resolve_entities`'s handler defaults `args.threshold` to
`ctx.default_entity_threshold` when unset, mirroring
`resolve_entities_route`.

### 3.4 `fdi.pipeline.ask` (the loop)

```python
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

    return {
        "answer": f"Could not fully answer within {max_rounds} tool-call round(s).",
        "citations": dedup_citations(raw_citations),
        "tool_trace": tool_trace,
    }
```

`json.dumps(result, default=str)` — a tool result may embed a `float`
already handled natively, but the safety net matters if a future tool ever
returns something json can't natively serialize (e.g. a `Decimal`); never
crashes the loop over a serialization edge case.

### 3.5 `fdi.platform.agent`

```python
"""AzureOpenAIAgentClient -- AgentClient backed by Azure OpenAI (GPT).
Structurally satisfies fdi.pipeline.agent.AgentClient without importing it
(platform may not import pipeline). AzureOpenAI imported at module level so
tests can monkeypatch fdi.platform.agent.AzureOpenAI -- no live call in the
default suite. Reuses the SAME FDI_GPT_* settings the semantic stage does."""

from openai import AzureOpenAI

from fdi.core.agent import AgentResponse, ToolCall
from fdi.platform.config import Settings


class AzureOpenAIAgentClient:
    def __init__(self, *, endpoint, key, deployment, model, api_version) -> None:
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
                    # produce a ToolCall execute_tool's own pydantic
                    # validation will reject with a normal structured
                    # error (the expected fields will simply be absent).
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

### 3.6 API wiring

- `fdi.api.deps`: `get_agent_client(request) -> AgentClient | None` (reads
  `request.app.state.agent_client`); `AgentClientDep = Annotated[AgentClient |
  None, Depends(get_agent_client)]`.
- `fdi.api.app` lifespan: `app.state.agent_client =
  AzureOpenAIAgentClient.from_settings(settings)`, alongside the existing
  `embedder`/`vector_store`/`graph_store` lines.
- `fdi.api.schemas`:
  ```python
  class AskRequest(BaseModel):
      question: str
      max_rounds: int | None = None

  class AskCitationView(BaseModel):
      doc_id: str | None = None
      doc_key: str | None = None
      version: int | None = None
      heading: str | None = None
      page: int | None = None
      chunk_id: str | None = None
      table_index: int | None = None
      row_label: str | None = None
      column_header: str | None = None
      node_id: str | None = None

  class AskToolTraceEntryView(BaseModel):
      round: int
      tool: str
      arguments: dict[str, object]
      result: object
      error: str | None

  class AskResponse(BaseModel):
      answer: str
      citations: list[AskCitationView]
      tool_trace: list[AskToolTraceEntryView]
  ```
- `fdi.api.routes`:
  ```python
  @router.post("/ask", response_model=AskResponse)
  async def ask(
      body: AskRequest, session: SessionDep, store: StoreDep, embedder: EmbedderDep,
      vector_store: VectorStoreDep, graph_store: GraphStoreDep,
      entity_threshold: EntityThresholdDep, agent_client: AgentClientDep,
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
  (`max_rounds` resolved to a concrete `int` before the call — deliberately
  NOT a conditional `**kwargs` unpack, which would ask `mypy --strict` to
  verify a plain `dict[str, int]` against `answer_question`'s three
  differently-typed keyword-only parameters for no real benefit; a plain
  ternary is simpler and unambiguously typed.)

## 4. Error handling

- **Unknown tool name** → `{"error": "unknown tool '...'"}`, never raises;
  `extract_citations` treats it (any `"error"` key) as `[]` citations.
- **Malformed/missing required arguments** (pydantic `ValidationError`) →
  `{"error": "invalid arguments for '...': ..."}`; includes a malformed-JSON
  tool call from a real LLM (§3.5 — the raw string surfaces as an
  unrecognized field, which the Args model rejects the normal way).
- **Any other exception inside a tool handler** (DB error, artifact-store
  error, etc.) → caught, logged, `{"error": "tool '...' failed: ..."}`
  (decision 9) — the loop continues to the next tool call / round rather
  than aborting the whole `/ask` request.
- **A tool's underlying pipeline function returns `None`** (`get_tables`/
  `concept_lookup`: no `structure` artifact yet; `entity_timeline`/
  `compare_concept_across_versions`: unknown `doc_key`) → the handler
  converts this to `{"error": "..."}` before it reaches the agent — `None`
  is never passed through as a tool message body.
- **`graph_store`/`vector_store` unconfigured** → `document_structure`'s
  handler returns a structured error immediately (checked before calling
  `document_structure_query`); `search`'s handler never needs this branch —
  `hybrid_search` itself degrades to keyword-only.
- **`max_rounds` exhausted without a final `content`** → `answer_question`
  returns the best-effort answer text (decision 12), never raises, never
  hangs.
- **`AgentResponse` invariant violation** (both/neither of `tool_calls`/
  `content` set) → `ValueError` at CONSTRUCTION time, inside
  `AzureOpenAIAgentClient.complete` or a test's `FakeAgentClient` script —
  fails loudly and immediately, not silently inside the loop.
- **`FakeAgentClient` called more times than scripted** → `AssertionError`
  naming the scripted count and the call count actually reached — a
  diagnosable test failure, not an `IndexError` pointing at internal list
  machinery.
- **`/ask` with no `agent_client` configured** → 503, `detail` names the
  missing service + the env vars to set (same shape as `/search`'s and
  `/graph/*`'s existing 503s).

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_agent_core.py`:
  `AgentResponse` rejects both-set and neither-set; `ToolCall`/
  `AgentResponse` are plain, comparable dataclasses; `extract_citations`
  covers all seven tool names against a small representative result for
  each (asserting the exact `Citation` fields populated/`None`) plus the
  uniform `{"error": ...}` → `[]` case; `dedup_citations` collapses exact
  duplicates, preserves first-seen order, and leaves citations differing in
  one field (e.g. `column_header`) both present.
- **Unit**, `backend/tests/unit/test_agent_tool_schemas.py`: `TOOL_SCHEMAS`
  has exactly seven entries; every `function.name` is a key `execute_tool`'s
  dispatch table recognizes (no orphaned schema, no undispatched name);
  every schema's `parameters.required` names are a subset of its
  `properties` keys (a hand-authored-schema sanity check, given decision 6
  forgoes derivation).
- **Unit**, `backend/tests/unit/test_azure_agent_client.py`: mirrors
  `test_azure_openai_client.py` exactly — monkeypatch
  `fdi.platform.agent.AzureOpenAI` with a fake SDK returning (a) a
  `tool_calls`-bearing message → `AgentResponse(tool_calls=[...])` with
  correctly parsed `json.loads`'d arguments, (b) a `content`-bearing message
  → `AgentResponse(content=...)`, (c) a malformed-JSON `arguments` string →
  a `ToolCall` carrying `{"_malformed_json": ...}`, never a raised
  exception; `from_settings` returns `None` on an unconfigured `Settings`
  (same construction as `test_from_settings_none_when_unconfigured`).
- **Integration** (testcontainers + fakes), `backend/tests/integration/
  test_agent_tools.py`: `execute_tool` against real Postgres/MinIO with
  `FakeEmbeddingClient`/`FakeVectorStore`/`FakeGraphStore` in an
  `AgentContext` — one happy-path case per of the seven tools (reusing the
  fixture patterns from `test_hybrid_search.py`/`test_tables_lookup.py`/
  `test_compare_concept.py`/`test_graphquery.py`/`test_resolve_entities.py`),
  plus: unknown tool name, a `ValidationError`-triggering bad-arg call,
  `document_structure` with `graph_store=None`, `get_tables`/
  `concept_lookup` against a document with no `structure` artifact yet
  (`None` → `{"error": ...}`, not a crash), `entity_timeline`/
  `compare_concept_across_versions` against an unknown `doc_key`.
- **Integration**, `backend/tests/integration/test_ask.py` — the
  end-to-end, default-suite, NO-Azure test the task requires: build a real
  document through `chunks`+`embedding`+`index` (reusing
  `test_hybrid_search.py`'s `hybrid_corpus`-style fixture, `FakeOcrClient`+
  `FakeSemanticClient`+`FakeEmbeddingClient`+`FakeVectorStore`, a fresh
  `uuid4` marker in the OCR text to dodge the shared-DB collision class
  every other RT integration test already guards against), script a
  `FakeAgentClient` with exactly two `AgentResponse`s — round 1:
  `tool_calls=[ToolCall(id="c1", name="search", arguments={"query": f"...
  {marker}"})]`; round 2: `content="<a synthesized, cited answer text>"` —
  call `answer_question(question=..., ctx=AgentContext(... real fakes
  ...), agent_client=fake, max_rounds=5)`, and assert: `result["answer"]`
  equals the scripted content; `result["citations"]` contains exactly one
  entry matching the marked document/chunk (real citation data produced by
  a REAL `execute_tool` → `hybrid_search` call, not a stub); `result[
  "tool_trace"]` has one entry, `round=0`, `tool="search"`, `error is None`;
  `fake.calls` has length 2 (both rounds actually reached the agent
  client). A second test proves the `max_rounds` exhaustion path: script
  `FakeAgentClient` with `max_rounds` responses that are ALL `tool_calls`
  (e.g. all "search" with a query that matches nothing) — assert the
  returned `answer` names the round bound, `citations == []`, and
  `len(tool_trace) == max_rounds`.
- **Integration**, `test_api.py` additions (appended, following every prior
  RT sub-project's own section-header convention — "RT-8: `POST /ask`"):
  - `test_ask_503_when_agent_client_unconfigured` — `make_client()` with no
    `FDI_GPT_*` set (the default in every fixture), `client.post("/ask",
    json={"question": "..."})` → 503, `"agent_client"` in `detail`.
  - `test_ask_returns_cited_answer_with_fake_agent_client_override` —
    `make_client(dependency_overrides={get_agent_client: lambda:
    FakeAgentClient([...])})`, mirroring `test_graph_neighbors_returns_
    cited_hits`'s `get_graph_store` override exactly; seed one document via
    `session_factory`/`store` through `chunks`+`index_fts` (keyword-only is
    enough — no need to also stand up a real vector store for an HTTP-layer
    smoke test), script the same two-response plan as `test_ask.py`, assert
    `200`, `body["answer"]`, `body["citations"]` non-empty, `body[
    "tool_trace"][0]["tool"] == "search"`.
- **Opt-in real Azure**, `backend/tests/azure/test_real_agent.py`
  (`@pytest.mark.azure`, `skipif` on `FDI_GPT_ENDPOINT`/`FDI_GPT_KEY`
  absent — mirrors `test_real_semantic.py` verbatim): build
  `AzureOpenAIAgentClient.from_settings(get_settings())`, call `.complete()`
  once with a minimal user message and `TOOL_SCHEMAS`, assert the SDK round
  trip produced a well-formed `AgentResponse` (the `AgentResponse.
  __post_init__` invariant already guarantees exactly one branch was hit —
  the test only needs to assert it didn't raise and, if `tool_calls` is
  set, that at least one call's `name` is one of the seven registered
  tools). Deliberately scoped to ONE real call, like `test_real_semantic.
  py`'s single `classify` — a full live `/ask` round-trip (multiple real
  LLM calls chained through real tools) is a heavier, separate smoke test,
  noted but not built (§6).

## 6. Explicitly deferred (YAGNI)

- **`vector_search`/`keyword_search`/`neighbors`/`reading_order` as
  separately registered tools** — `hybrid_search`/`document_structure`
  already cover their use cases for an LLM planner (decision 1); add back
  only if a real question set shows the agent needs to force one retrieval
  mode or do single-node graph traversal `document_structure` can't answer.
- **Schema derivation from the pydantic Args models** (decision 6) —
  hand-sync is fine at seven tools; revisit if the tool count grows.
- **A forced final-synthesis call on `max_rounds` exhaustion** (decision
  12) — a legitimate improvement, adds cost/complexity this slice avoids.
- **Streaming `/ask` responses; multi-turn conversational memory** — already
  deferred at the roadmap level, not reopened here.
- **A full live `/ask` smoke test chaining multiple real Azure OpenAI calls
  through real tools** — the opt-in `azure` test (§5) covers the
  `AzureOpenAIAgentClient` wiring in isolation, matching
  `test_real_semantic.py`'s scope; a full live loop is costlier and not
  needed to prove the seam works.
- **Truncating large tool results in `tool_trace`** — `tool_trace` currently
  stores each tool's full raw result for auditability; if a real
  `document_structure` result on a huge document makes `/ask` responses
  unwieldy, truncation/summarization is a follow-on, not solved here.
- **Per-request tool-set customization** (e.g. letting a caller disable
  `resolve_entities` for a cheaper/faster answer) — `TOOL_SCHEMAS` is fixed
  for every `/ask` call in this slice.

## 7. Done criteria

1. `fdi.core.agent.ToolCall`/`AgentResponse` (§3.1) — pure, `AgentResponse`
   enforces its either/or invariant at construction; `extract_citations`
   covers all seven registered tool names + the uniform error case;
   `dedup_citations` is order-preserving, whole-dict-equality dedup; `core`
   stays pure (no I/O, no import of `fdi.pipeline`/`fdi.platform`).
2. `fdi.pipeline.agent.AgentClient` Protocol + `FakeAgentClient` (§3.2) —
   `FakeAgentClient` replays a scripted list deterministically and raises a
   diagnosable error on over-call.
3. `fdi.pipeline.agent_tools.AgentContext`/`TOOL_SCHEMAS`/`execute_tool`
   (§3.3) — seven schemas, each dispatched; unknown tool / bad args / any
   handler exception all degrade to `{"error": ...}`, never raise; every
   handler injects `ctx`'s services rather than reaching for a global.
4. `fdi.pipeline.ask.answer_question` (§3.4) — loops up to `max_rounds`,
   calls `execute_tool` for every `ToolCall`, records `tool_trace`,
   aggregates+dedups citations, returns a best-effort answer + partial
   results on exhaustion, never raises, never hangs.
5. `fdi.platform.agent.AzureOpenAIAgentClient` (§3.5) — module-level
   monkeypatchable `AzureOpenAI` import; `from_settings` returns `None`
   when `FDI_GPT_*` unconfigured; structurally satisfies `AgentClient`
   without importing `fdi.pipeline`.
6. `POST /ask` (§3.6) — 503 only on a missing `agent_client`; builds
   `AgentContext` from the same deps `/retrieve`/`/graph/*`/`/entities/*`
   already use; returns `{answer, citations, tool_trace}`.
7. The default suite's `test_ask.py` end-to-end test (§5) passes with NO
   live Azure call — `FakeAgentClient` scripts one real `search` tool call
   against testcontainers-backed Postgres/MinIO + fakes, producing a real,
   correctly-cited final answer. The opt-in `tests/azure/test_real_agent.py`
   is skipped without `FDI_GPT_*` creds and passes with them.
8. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/
   0-broken, full suite with `-m "not integration and not e2e and not
   azure"` for the fast loop and the full marker set for CI); the retrieval
   roadmap's RT-8 row flips to done with a "what RT-8 shipped" handoff.
