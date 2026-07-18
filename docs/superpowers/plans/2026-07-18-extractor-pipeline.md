# Extractor Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the schema-driven RAG field extractor — the layer ABOVE
retrieval that, given a document and the field-extraction schema, retrieves
candidate context per field via the retrieval layer (RT-1…RT-8, complete),
reranks it, prompts an LLM to extract the value, and returns JSON with its
source — exposed as `POST /extract/document`.

**Architecture:** `fdi.core.extractor` (pure) holds `FieldSpec`/`Candidate` +
schema parsing + the model-free blend reranker + prompt assembly + result
assembly. `fdi.pipeline.extractor` is the orchestrator + mockable LLM seam
(`ExtractorClient` Protocol + `FakeExtractorClient`), pooling context via
`fdi.pipeline.retrieval.hybrid_search` (+ `fdi.pipeline.tables.get_tables`
for numeric fields). `fdi.platform.extractor.AzureOpenAIExtractorClient` is
the real, monkeypatchable Azure OpenAI (JSON mode) client, reusing the
existing `FDI_GPT_*` settings — no new LLM config. `POST /extract/document`
wires it through the same deps `/ask`/`/retrieve` already use, plus a new
config-pathed schema file read.

**Tech Stack:** Python 3.13, `uv`, `pydantic`/FastAPI, `openai` SDK (already
a dependency), SQLAlchemy async, pytest + testcontainers.

**Design spec:** `docs/superpowers/specs/2026-07-18-extractor-pipeline-design.md`
— read it first; this plan implements its §3 architecture and §2 decisions
verbatim except where noted inline (two signature gaps and one deployment
gap the spec's illustrative code left unresolved — see the callouts in
Tasks 3 and 5, and "Risk flagged" in Self-Review). Decision numbers below
refer to that document.

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive),
  CI-enforced, stays "1 kept, 0 broken". `fdi.core.extractor` is PURE:
  stdlib only (`dataclasses`, `collections.abc`) — no I/O, no config, no
  clock/RNG, and no import of `fdi.pipeline`/`fdi.platform` or any other
  `fdi.core` producer (`fdi.core.retrieval`/`tables`/`agent`) — it reads the
  schema dict and `Candidate`s as plain `Mapping`s/dataclasses it defines
  itself.
- `ExtractorClient.extract(*, system, user) -> Mapping[str, object]` is
  SYNCHRONOUS — mirrors RT-8's `AgentClient.complete` and
  `SemanticClient.classify` exactly (the established convention for every
  external-LLM-API client protocol in this codebase). Do not make it
  `async def`.
- `Protocol` + `FakeExtractorClient` live in `fdi.pipeline.extractor`; the
  real `AzureOpenAIExtractorClient` lives in `fdi.platform.extractor` and
  structurally satisfies the protocol WITHOUT importing it (platform may
  not import pipeline).
- Mockable: the DEFAULT suite uses `FakeExtractorClient` + real
  testcontainers Postgres/MinIO for retrieval, with NO embedder/vector_store
  configured (hybrid_search degrades to keyword-only, RT-4) — no Azure,
  Milvus, or Neo4j touched anywhere in the default suite. Real Azure only
  behind `@pytest.mark.azure`, skipped without `FDI_GPT_ENDPOINT`/`FDI_GPT_KEY`.
- Never-raise (spec decision 10): `extract_field`/`extract_document` return
  a well-formed record per field even on an empty pool or LLM garbage —
  every I/O step inside `extract_field` (the document lookup, `hybrid_search`,
  `get_tables`, `client.extract`) is individually guarded so one tool's
  failure degrades that field's pool/answer, never the whole batch.
- Config trio (`config.py` / `docker/compose.yml` x-backend-env /
  `.env.example`) for `FDI_EXTRACTION_SCHEMA_PATH`. No new LLM config — the
  extractor reuses `FDI_GPT_*` (same settings `fdi.platform.semantic` and
  `fdi.platform.agent` already read).
- **Discovered constraint, not in the spec's illustrative code:** the
  schema-path default (`docs/architecture/field-extraction-schema.json`) is
  REPO-ROOT-relative, but neither the built Docker image's `WORKDIR`
  (`/app`, populated only from `COPY backend/ /app/` — `docs/` is a sibling
  of `backend/`, never copied) nor the test suite's cwd (`backend/`, via
  `uv run --directory backend pytest`) IS the repo root. Task 5 adds one
  `COPY` line to `backend/Dockerfile` (mirroring the existing
  `alembic.ini` COPY) so the bare default resolves inside the container;
  every test in this plan overrides the schema-path dependency/value
  explicitly rather than relying on the bare default resolving under
  pytest's cwd. See Task 5's docstring callout and Self-Review's "Risk
  flagged".
- Per-task gates before every commit: `uv run --directory backend ruff check .`
  · `uv run --directory backend mypy --strict src` ·
  `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter`
  · the task's pytest. Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) → commit.
- Integration tests: function-scoped `session_factory`/`store` fixtures over
  session-scoped shared Postgres/MinIO containers (`migrated_engine`,
  `minio_client` from `backend/tests/integration/conftest.py`); every
  document uses a fresh `uuid4`-based `doc_key`/marker to dodge cross-test
  collisions on the shared containers, the same posture RT-1/RT-4/RT-8's
  integration tests use.

---

### Task 1: `fdi.core.extractor` — pure schema parsing, rerank, prompt/result assembly

**Files:**
- Create: `backend/src/fdi/core/extractor.py`
- Test: `backend/tests/unit/test_extractor.py`

**Interfaces:**
- Produces: `FieldSpec`, `Candidate` (frozen dataclasses); `parse_field_specs(schema, module=None) -> list[FieldSpec]`; `is_numeric(data_type: str) -> bool`; `keyword_overlap(text, keywords) -> float`; `rerank_candidates(candidates, keywords, *, top_k=8, keyword_weight=1.0) -> list[Candidate]`; `build_extraction_messages(field, candidates) -> tuple[str, str]`; `assemble_result(field, raw, ranked) -> dict[str, object]` (the FULL per-field output record: `{field_name, value, source: {page, snippet, evidence_ids}, confidence}`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_extractor.py
from fdi.core.extractor import (
    Candidate,
    FieldSpec,
    assemble_result,
    build_extraction_messages,
    is_numeric,
    keyword_overlap,
    parse_field_specs,
    rerank_candidates,
)


def _schema() -> dict[str, object]:
    return {
        "modules": [
            {
                "id": 0, "name": "PREPROCESS", "folder": "0-preprocess",
                "fields": [
                    {  # metadata, not rag_extractable -- EXCLUDED
                        "field_name": "Company name", "source_type": "metadata",
                        "rag_extractable": False, "data_type": "string",
                        "retrieval_query": None, "extraction_prompt": "x", "keywords": [],
                    },
                    {  # rag_extractable + document_content -- KEPT
                        "field_name": "Total page count", "source_type": "document_content",
                        "rag_extractable": True, "data_type": "integer",
                        "retrieval_query": "Number of pages", "extraction_prompt": "Extract it.",
                        "keywords": ["pages", "total"],
                    },
                    {  # rag_extractable + document_content, but MALFORMED
                       # (retrieval_query null) -- EXCLUDED, never raises
                        "field_name": "Broken field", "source_type": "document_content",
                        "rag_extractable": True, "data_type": "string",
                        "retrieval_query": None, "extraction_prompt": "x", "keywords": [],
                    },
                ],
            },
            {
                "id": 1, "name": "BALANCE_SHEET", "folder": "1-balance-sheet",
                "fields": [
                    {
                        "field_name": "Total assets", "source_type": "document_content",
                        "rag_extractable": True, "data_type": "decimal",
                        "retrieval_query": "Total assets value",
                        "extraction_prompt": "Extract the total assets value.",
                        "keywords": ["total assets"], "enum_values": None,
                    },
                ],
            },
        ]
    }


def test_parse_field_specs_filters_rag_extractable_and_document_content() -> None:
    specs = parse_field_specs(_schema())
    names = [s.field_name for s in specs]
    assert names == ["Total page count", "Total assets"]


def test_parse_field_specs_skips_malformed_field() -> None:
    specs = parse_field_specs(_schema())
    assert "Broken field" not in [s.field_name for s in specs]


def test_parse_field_specs_module_filter_by_name() -> None:
    specs = parse_field_specs(_schema(), module="BALANCE_SHEET")
    assert [s.field_name for s in specs] == ["Total assets"]
    assert specs[0].module_id == 1 and specs[0].module_name == "BALANCE_SHEET"


def test_parse_field_specs_module_filter_by_folder() -> None:
    specs = parse_field_specs(_schema(), module="0-preprocess")
    assert [s.field_name for s in specs] == ["Total page count"]


def test_parse_field_specs_module_filter_no_match_is_empty() -> None:
    assert parse_field_specs(_schema(), module="no-such-module") == []


def test_parse_field_specs_empty_or_malformed_schema_never_raises() -> None:
    assert parse_field_specs({}) == []
    assert parse_field_specs({"modules": "not-a-list"}) == []
    assert parse_field_specs({"modules": [{"fields": "not-a-list"}]}) == []
    assert parse_field_specs({"modules": [{"fields": [1, "x", None]}]}) == []


def test_parse_field_specs_field_shape() -> None:
    spec = parse_field_specs(_schema())[0]
    assert spec == FieldSpec(
        field_name="Total page count", data_type="integer",
        retrieval_query="Number of pages", keywords=("pages", "total"),
        extraction_prompt="Extract it.", enum_values=None,
        module_id=0, module_name="PREPROCESS",
    )


def test_is_numeric() -> None:
    assert is_numeric("decimal") is True
    assert is_numeric("integer") is True
    for other in ("string", "date", "enum", "text", "link", "list", "boolean"):
        assert is_numeric(other) is False


def test_keyword_overlap_zero_partial_full_and_casefold() -> None:
    assert keyword_overlap("Total Assets 100", ["revenue"]) == 0.0
    assert keyword_overlap("Total Assets 100", ["total", "liabilities"]) == 0.5
    assert keyword_overlap("Total Assets 100", ["total", "assets"]) == 1.0
    assert keyword_overlap("TOTAL ASSETS", ["total"]) == 1.0  # casefold


def test_keyword_overlap_empty_keywords_is_zero() -> None:
    assert keyword_overlap("anything", []) == 0.0


def _cand(evidence_id: str, text: str, score: float) -> Candidate:
    return Candidate(evidence_id=evidence_id, text=text, page=1, score=score, source_tool="hybrid")


def test_rerank_candidates_keyword_boost_changes_order() -> None:
    low_score_relevant = _cand("a", "Total Assets 100", 0.4)
    high_score_irrelevant = _cand("b", "nothing relevant here", 0.5)
    ranked = rerank_candidates(
        [high_score_irrelevant, low_score_relevant], ["total", "assets"], keyword_weight=1.0
    )
    assert [c.evidence_id for c in ranked] == ["a", "b"]  # boost flips the order


def test_rerank_candidates_stable_on_ties() -> None:
    a, b, c = _cand("a", "x", 0.5), _cand("b", "x", 0.5), _cand("c", "x", 0.5)
    ranked = rerank_candidates([a, b, c], [])
    assert [x.evidence_id for x in ranked] == ["a", "b", "c"]  # original order preserved


def test_rerank_candidates_top_k_truncation() -> None:
    cands = [_cand(str(i), "x", float(i)) for i in range(5)]
    ranked = rerank_candidates(cands, [], top_k=2)
    assert [c.evidence_id for c in ranked] == ["4", "3"]


def test_rerank_candidates_empty_input() -> None:
    assert rerank_candidates([], ["k"]) == []


def _field() -> FieldSpec:
    return FieldSpec(
        field_name="Total assets", data_type="decimal", retrieval_query="Total assets value",
        keywords=("total", "assets"), extraction_prompt="Extract the total assets value.",
        enum_values=None, module_id=1, module_name="BALANCE_SHEET",
    )


def test_build_extraction_messages_system_states_json_contract() -> None:
    system, _ = build_extraction_messages(_field(), [])
    assert "JSON" in system
    assert "only" in system.lower()
    assert "value" in system and "source" in system and "confidence" in system


def test_build_extraction_messages_user_carries_prompt_and_candidates() -> None:
    cands = [_cand("d1:s0", "Total assets were 100.", 0.9)]
    _, user = build_extraction_messages(_field(), cands)
    assert _field().extraction_prompt in user
    assert "d1:s0" in user
    assert "Total assets were 100." in user
    assert "1" in user  # the candidate's page


def test_assemble_result_full_llm_dict_passthrough() -> None:
    raw = {
        "value": 145, "confidence": 0.8,
        "source": {"page": 3, "snippet": "Total assets 145", "evidence_ids": ["d1:s0"]},
    }
    out = assemble_result(_field(), raw, [_cand("d1:s0", "Total assets 145", 0.9)])
    assert out == {
        "field_name": "Total assets", "value": 145,
        "source": {"page": 3, "snippet": "Total assets 145", "evidence_ids": ["d1:s0"]},
        "confidence": 0.8,
    }


def test_assemble_result_missing_keys_fall_back_to_candidates() -> None:
    ranked = [_cand("d1:s0", "Total assets were 100.", 0.9)]
    out = assemble_result(_field(), {}, ranked)
    assert out["value"] is None
    assert out["confidence"] == 0.0
    assert out["source"] == {
        "page": 1, "snippet": "Total assets were 100.", "evidence_ids": ["d1:s0"],
    }


def test_assemble_result_empty_pool_and_empty_raw_is_fully_none() -> None:
    out = assemble_result(_field(), {}, [])
    assert out["value"] is None
    assert out["confidence"] == 0.0
    assert out["source"] == {"page": None, "snippet": None, "evidence_ids": []}


def test_assemble_result_confidence_clamp() -> None:
    assert assemble_result(_field(), {"confidence": 1.7}, [])["confidence"] == 1.0
    assert assemble_result(_field(), {"confidence": -3}, [])["confidence"] == 0.0
    assert assemble_result(_field(), {"confidence": "x"}, [])["confidence"] == 0.0
    assert assemble_result(_field(), {}, [])["confidence"] == 0.0
    assert assemble_result(_field(), {"confidence": 0.42}, [])["confidence"] == 0.42


def test_assemble_result_malformed_source_falls_back() -> None:
    ranked = [_cand("d1:s0", "candidate text", 0.9)]
    out = assemble_result(_field(), {"source": "not-a-dict"}, ranked)
    assert out["source"] == {"page": 1, "snippet": "candidate text", "evidence_ids": ["d1:s0"]}


def test_assemble_result_malformed_evidence_ids_falls_back() -> None:
    ranked = [_cand("d1:s0", "candidate text", 0.9), _cand("d1:s1", "more text", 0.8)]
    out = assemble_result(_field(), {"source": {"evidence_ids": "not-a-list"}}, ranked)
    assert out["source"]["evidence_ids"] == ["d1:s0", "d1:s1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.extractor'`.

- [ ] **Step 3: Write the implementation**

```python
# backend/src/fdi/core/extractor.py
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
    the JSON output_contract and the "only the given context" constraint;
    user carries field.extraction_prompt plus every candidate labeled with
    its evidence_id/page/text, so the LLM can cite one back."""
    system = (
        "You extract ONE field's value from financial-document context. Use "
        "ONLY the context given below -- never prior knowledge of a specific "
        "company or filing. Respond with a single JSON object, nothing else, "
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_extractor.py -v`
Expected: PASS (all 21).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/core/extractor.py backend/tests/unit/test_extractor.py
git commit -m "feat(core): extractor.py -- schema parsing, blend rerank, prompt/result assembly"
```

---

### Task 2: Config — `FDI_EXTRACTION_SCHEMA_PATH`

**Files:**
- Modify: `backend/src/fdi/platform/config.py`
- Modify: `docker/compose.yml`
- Modify: `.env.example`
- Test: `backend/tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings.extraction_schema_path: str` (default
  `"docs/architecture/field-extraction-schema.json"`), env var
  `FDI_EXTRACTION_SCHEMA_PATH`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/unit/test_config.py`:

```python
def test_extraction_schema_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.extraction_schema_path == "docs/architecture/field-extraction-schema.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_config.py -k extraction_schema_path -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'extraction_schema_path'`.

- [ ] **Step 3: Add the field**

In `backend/src/fdi/platform/config.py`, insert immediately after `entity_sim_threshold`
(before the `@model_validator`):

```python
    # Extractor pipeline (schema-driven RAG field extraction, POST
    # /extract/document). Always has a working default -- the schema ships
    # in the repo -- so, like azure_di_model/embed_model, this is inert
    # config: present in the trio for discoverability/override, not because
    # the stack needs it set to boot. No new LLM config: the extractor
    # reuses FDI_GPT_* (fdi.platform.extractor.AzureOpenAIExtractorClient).
    # The default is REPO-ROOT-relative -- see backend/Dockerfile's COPY of
    # this exact file into the image's WORKDIR, which is what makes the
    # bare default resolve inside a running container.
    extraction_schema_path: str = "docs/architecture/field-extraction-schema.json"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_config.py -v`
Expected: PASS (all, including the new one).

- [ ] **Step 5: Mirror into the compose/`.env.example` trio**

In `docker/compose.yml`'s `x-backend-env` block, add after `FDI_ENTITY_SIM_THRESHOLD`:

```yaml
  FDI_EXTRACTION_SCHEMA_PATH: ${FDI_EXTRACTION_SCHEMA_PATH}
```

In `.env.example`, add after the `FDI_ENTITY_SIM_THRESHOLD=0.85` line:

```bash
# Extractor pipeline: schema-driven RAG field extraction over
# POST /extract/document. Always has a working default (the schema ships in
# the repo, and backend/Dockerfile copies it to the same relative path
# inside the image) -- override only to point at a different schema
# file/version.
FDI_EXTRACTION_SCHEMA_PATH=docs/architecture/field-extraction-schema.json
```

- [ ] **Step 6: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/platform/config.py backend/tests/unit/test_config.py docker/compose.yml .env.example
git commit -m "feat(config): FDI_EXTRACTION_SCHEMA_PATH -- extractor pipeline schema path (trio)"
```

---

### Task 3: `fdi.pipeline.extractor` — orchestrator + `ExtractorClient` seam

**Files:**
- Create: `backend/src/fdi/pipeline/extractor.py`
- Test: `backend/tests/integration/test_extract_document.py`

**Interfaces:**
- Consumes: `fdi.core.extractor.{FieldSpec, Candidate, is_numeric, rerank_candidates, build_extraction_messages, assemble_result}` (Task 1); `fdi.pipeline.retrieval.hybrid_search`; `fdi.pipeline.tables.get_tables`; `fdi.platform.db.repository.DocumentRepository`.
- Produces: `ExtractorClient` Protocol; `FakeExtractorClient(results: list[Mapping[str, object]])`; `ExtractContext` dataclass; `extract_field(*, ctx, field, client) -> dict[str, object]`; `extract_document(*, ctx, fields, client) -> dict[str, object]` (returns `{schema_version, doc_id, field_count, fields}` — see the callout below for why `doc_key`/`module` are NOT in this return).

**Spec-gap callout (read before writing code):** design spec §3.2 shows
`ExtractContext` carrying only `doc_id: str`, yet `hybrid_search`'s
`filters` are keyed by `doc_key`/`version`, not `doc_id` — without
resolving `doc_id` back to its `Document` row, `hybrid_search` cannot be
scoped to THIS document and would silently search the whole corpus.
Resolution (kept `ExtractContext` exactly as spec-shown, no new fields):
`extract_field` calls `DocumentRepository(ctx.session).get(ctx.doc_id)`
once per field to get `doc_key`/`version` for the `hybrid_search` filter.
This is O(fields) extra single-row PK lookups per document (cheap; a
module is a handful of fields), not O(corpus). The alternative — adding
`doc_key`/`version` to `ExtractContext` so the route resolves them ONCE —
is more efficient and is the fork flagged in this plan's Self-Review; this
plan implements the spec-literal dataclass shape. Separately, §3.2's
prose describes `extract_document`'s return as including `doc_key`/`module`,
but its shown signature takes only `ctx`/`fields`/`client` (no `doc_key`
param). This plan has `extract_document` return `{schema_version, doc_id,
field_count, fields}` only; Task 5's route (which already holds
`body.doc_key`/`body.module`) merges them into the final response — no
deviation from the shown signature, and no extra query needed.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_extract_document.py
"""extract_document end-to-end against real Postgres/MinIO with FakeOcr +
FakeSemantic + FakeExtractorClient -- NO embedder/vector_store configured
(hybrid_search degrades to keyword-only, RT-4) and NO Azure OpenAI call
anywhere in this file. Proves: a text field's pool comes from
keyword_search over the chunks FTS index; a numeric field's pool
ADDITIONALLY carries a `table:` candidate built from get_tables'
canonicalized grid (decision 4) even though the table's cell text never
reaches keyword_search (tables are not chunk text, they live in the
`structure` artifact); an unmatchable, non-numeric field's pool is empty
and still yields a well-formed {value: None, confidence: 0.0} record
(decision 10)."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.extractor import parse_field_specs
from fdi.pipeline.extractor import ExtractContext, FakeExtractorClient, extract_document
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.retrieval import index_chunks_fts
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.chunks import ChunksStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.pipeline.stages.structure import StructureStage
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


def _schema(marker: str) -> dict[str, object]:
    return {
        "modules": [
            {
                "id": 1, "name": "OVERVIEW", "folder": "1-overview",
                "fields": [
                    {
                        "field_name": "Company overview text",
                        "source_type": "document_content", "rag_extractable": True,
                        "data_type": "text",
                        "retrieval_query": f"manufacturing company {marker}",
                        "extraction_prompt": "Extract the company overview.",
                        "keywords": ["manufacturing"],
                    },
                ],
            },
            {
                "id": 2, "name": "BALANCE_SHEET", "folder": "2-balance-sheet",
                "fields": [
                    {
                        "field_name": "Total assets",
                        "source_type": "document_content", "rag_extractable": True,
                        "data_type": "decimal", "retrieval_query": "Total assets value",
                        "extraction_prompt": "Extract the total assets value.",
                        "keywords": ["total", "assets"],
                    },
                    {
                        "field_name": "Unmatchable field",
                        "source_type": "document_content", "rag_extractable": True,
                        "data_type": "string",
                        "retrieval_query": "zzzznonexistenttoken9999 impossible query",
                        "extraction_prompt": "Extract nothing findable.", "keywords": [],
                    },
                ],
            },
        ],
    }


@pytest.fixture
async def processed_doc(session_factory, store):
    """One fully-processed document: a text section (matched by keyword
    search) + a numeric table (matched only via get_tables, never indexed
    into chunk FTS). Returns (doc, marker)."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage(), StructureStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    fake_ocr = FakeOcrClient({
        "content": "ACME LTD",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Overview"},
            {"content": f"ACME Ltd is a manufacturing company {marker}."},
        ],
        "tables": [
            {
                "rowCount": 2, "columnCount": 2,
                "spans": [{"offset": 20, "length": 60}],
                "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                     "content": "Particulars"},
                    {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "FY24"},
                    {"rowIndex": 1, "columnIndex": 0, "content": "Total assets"},
                    {"rowIndex": 1, "columnIndex": 1, "content": "12,345"},
                ],
            },
        ],
    })
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
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
        services={"semantic": FakeSemanticClient({"Overview": "MDNA"})}, **common,
    )
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()
    return doc, marker


async def test_extract_document_pools_keyword_and_table_candidates(
    session_factory, store, processed_doc
) -> None:
    doc, marker = processed_doc
    fields = parse_field_specs(_schema(marker))
    assert len(fields) == 3  # both modules' fields, no module filter

    fake = FakeExtractorClient([
        {"value": "ACME is a manufacturer.",
         "source": {"page": 1, "snippet": "ACME Ltd is a manufacturing company",
                     "evidence_ids": [f"{doc.id}:s0"]},
         "confidence": 0.9},
        {"value": 12345.0,
         "source": {"page": None, "snippet": "Total assets 12,345",
                     "evidence_ids": [f"table:{doc.id}:0"]},
         "confidence": 0.95},
        # The third field's pool is empty -- extract_field short-circuits
        # BEFORE calling the client, so only 2 scripted results are consumed.
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=None, vector_store=None, doc_id=doc.id
        )
        result = await extract_document(ctx=ctx, fields=fields, client=fake)

    assert result["doc_id"] == doc.id
    assert result["field_count"] == 3
    f0, f1, f2 = result["fields"]

    assert f0["field_name"] == "Company overview text"
    assert f0["value"] == "ACME is a manufacturer."
    assert f0["confidence"] == 0.9

    assert f1["field_name"] == "Total assets"
    assert f1["value"] == 12345.0
    assert f1["source"]["evidence_ids"] == [f"table:{doc.id}:0"]

    assert f2["field_name"] == "Unmatchable field"
    assert f2["value"] is None
    assert f2["confidence"] == 0.0
    assert f2["source"] == {"page": None, "snippet": None, "evidence_ids": []}

    # Exactly 2 real LLM calls (the third field never reaches the client);
    # the numeric field's prompt carries the table's rendered grid text and
    # its `table:` evidence_id -- proving get_tables' output entered the pool.
    assert len(fake.calls) == 2
    assert "table:" not in fake.calls[0][1]
    assert f"table:{doc.id}:0" in fake.calls[1][1]
    assert "12,345" in fake.calls[1][1]


async def test_extract_field_blank_query_pool_is_empty_and_well_formed(
    session_factory, store, processed_doc
) -> None:
    """spec sec 4: an empty/whitespace retrieval_query -> empty pool ->
    value None. parse_field_specs never produces such a FieldSpec (it
    rejects blank retrieval_query at parse time, see Task 1) -- this
    exercises extract_field's OWN defensive guard directly, for a
    hand-built FieldSpec bypassing the parser."""
    from fdi.core.extractor import FieldSpec
    from fdi.pipeline.extractor import extract_field

    doc, _ = processed_doc
    blank = FieldSpec(
        field_name="Blank query field", data_type="string", retrieval_query="   ",
        keywords=(), extraction_prompt="x", enum_values=None, module_id=9, module_name="X",
    )
    async with session_factory() as s:
        ctx = ExtractContext(session=s, store=store, embedder=None, vector_store=None, doc_id=doc.id)
        out = await extract_field(ctx=ctx, field=blank, client=FakeExtractorClient([]))
    assert out == {
        "field_name": "Blank query field", "value": None,
        "source": {"page": None, "snippet": None, "evidence_ids": []}, "confidence": 0.0,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_extract_document.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.pipeline.extractor'`.

- [ ] **Step 3: Write the implementation**

```python
# backend/src/fdi/pipeline/extractor.py
"""The extractor's orchestrator + ExtractorClient seam (design spec sec
3.2): extract_field pools context per field (hybrid_search + get_tables for
numeric fields), reranks via fdi.core.extractor (pure), prompts the client,
and assembles the schema's output_contract record. extract_document loops
every field. ExtractorClient.extract(*, system, user) -> Mapping is
SYNCHRONOUS, mirroring fdi.pipeline.agent.AgentClient.complete exactly.
AzureOpenAIExtractorClient (fdi.platform.extractor, does I/O) structurally
satisfies this protocol without importing it -- platform may not import
pipeline. FakeExtractorClient lives here for tests: no live Azure OpenAI
call anywhere in the default suite.

Never raises (decision 10): extract_field guards every I/O step (the
Document lookup, hybrid_search, get_tables, client.extract) individually,
so one tool's failure degrades this field's pool/answer, never the whole
document's batch. See this plan's Task 3 callout for why extract_field
resolves ctx.doc_id -> Document row itself (hybrid_search's filters are
doc_key/version-keyed, not doc_id-keyed, and ExtractContext only carries
doc_id per design spec sec 3.2) rather than ExtractContext carrying
doc_key/version directly."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.extractor import (
    Candidate,
    FieldSpec,
    assemble_result,
    build_extraction_messages,
    is_numeric,
    rerank_candidates,
)
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.retrieval import hybrid_search
from fdi.pipeline.tables import get_tables
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.repository import DocumentRepository
from fdi.platform.storage.artifact_store import ArtifactStore


@runtime_checkable
class ExtractorClient(Protocol):
    def extract(self, *, system: str, user: str) -> Mapping[str, object]: ...


class FakeExtractorClient:
    """Replays scripted {value, source, confidence}-shaped dicts, one per
    call, in order -- mirrors fdi.pipeline.agent.FakeAgentClient exactly
    (same AssertionError-on-overcall posture, a loud test-diagnosable
    failure rather than a confusing IndexError deep inside extract_field)."""

    def __init__(self, results: list[Mapping[str, object]]) -> None:
        self._results = results
        self._next = 0
        self.calls: list[tuple[str, str]] = []

    def extract(self, *, system: str, user: str) -> Mapping[str, object]:
        self.calls.append((system, user))
        if self._next >= len(self._results):
            raise AssertionError(
                f"FakeExtractorClient exhausted: {len(self._results)} scripted "
                f"result(s), call {len(self.calls)} requested"
            )
        result = self._results[self._next]
        self._next += 1
        return result


@dataclass
class ExtractContext:
    session: AsyncSession
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    doc_id: str


_POOL_K = 12
_TABLE_BASE_SCORE = 0.5


def _render_table(table: Mapping[str, object]) -> str:
    """A canonicalized table grid (fdi.pipeline.tables.get_tables' output
    shape) -> plain text an LLM can read: header row, then one line per
    data row, cells pipe-separated. Empty/malformed input -> "" (extract_
    field adds no candidate for an empty render)."""
    grid_body = table.get("grid")
    if not isinstance(grid_body, Mapping):
        return ""
    lines: list[str] = []
    headers = grid_body.get("column_headers")
    if isinstance(headers, list):
        lines.append(" | ".join(str(h) for h in headers))
    rows = grid_body.get("grid")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                lines.append(" | ".join(str(c) for c in row))
    return "\n".join(lines)


async def extract_field(
    *, ctx: ExtractContext, field: FieldSpec, client: ExtractorClient
) -> dict[str, object]:
    """pool (hybrid_search, scoped to ctx.doc_id's document, + get_tables if
    numeric) -> Candidate list -> rerank_candidates -> build_extraction_
    messages -> client.extract -> assemble_result. Never raises."""
    candidates: list[Candidate] = []

    query = field.retrieval_query.strip()
    if query:
        doc = await DocumentRepository(ctx.session).get(ctx.doc_id)
        if doc is not None:
            hits: list[dict[str, object]] = []
            try:
                hits = await hybrid_search(
                    session=ctx.session, store=ctx.store, embedder=ctx.embedder,
                    vector_store=ctx.vector_store, query=query, k=_POOL_K,
                    filters={"doc_key": doc.doc_key, "version": doc.version},
                )
            except Exception:  # noqa: BLE001 -- never raise (decision 10)
                pass
            for h in hits:
                candidates.append(Candidate(
                    evidence_id=f"{h['doc_id']}:{h['chunk_id']}",
                    text=str(h.get("text", "")),
                    page=cast(int | None, h.get("page")),
                    score=float(cast(float, h.get("score", 0.0))),
                    source_tool="hybrid",
                ))

    if is_numeric(field.data_type):
        tables: list[dict[str, object]] | None = None
        try:
            tables = await get_tables(session=ctx.session, store=ctx.store, doc_id=ctx.doc_id)
        except Exception:  # noqa: BLE001 -- never raise (decision 10)
            pass
        for t in tables or []:
            text = _render_table(t)
            if text:
                candidates.append(Candidate(
                    evidence_id=f"table:{ctx.doc_id}:{t.get('table_index')}",
                    text=text, page=None, score=_TABLE_BASE_SCORE, source_tool="table",
                ))

    ranked = rerank_candidates(candidates, field.keywords)
    if not ranked:
        return assemble_result(field, {}, ranked)

    system, user = build_extraction_messages(field, ranked)
    raw: Mapping[str, object] = {}
    try:
        raw = client.extract(system=system, user=user)
    except Exception:  # noqa: BLE001 -- never raise (decision 10)
        pass
    return assemble_result(field, raw, ranked)


async def extract_document(
    *, ctx: ExtractContext, fields: Sequence[FieldSpec], client: ExtractorClient
) -> dict[str, object]:
    """Loop `fields`, calling extract_field for each; always returns one
    record per field, never raises (decision 10). Empty `fields` ->
    field_count 0, fields []. `doc_key`/`module` are NOT part of this
    return -- the route (which already holds both from the request body)
    attaches them; see fdi.api.routes.extract_document_route (Task 5)."""
    results = [await extract_field(ctx=ctx, field=field, client=client) for field in fields]
    return {
        "schema_version": 1, "doc_id": ctx.doc_id,
        "field_count": len(results), "fields": results,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_extract_document.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/extractor.py backend/tests/integration/test_extract_document.py
git commit -m "feat(pipeline): extractor.py -- extract_field/extract_document over hybrid_search+get_tables"
```

---

### Task 4: `fdi.platform.extractor.AzureOpenAIExtractorClient`

**Files:**
- Create: `backend/src/fdi/platform/extractor.py`
- Test: `backend/tests/unit/test_azure_extractor_client.py`
- Test: `backend/tests/azure/test_real_extraction.py` (opt-in, `@pytest.mark.azure`)

**Interfaces:**
- Consumes: `openai.AzureOpenAI` (module-level, monkeypatchable); `fdi.platform.config.Settings`.
- Produces: `AzureOpenAIExtractorClient` (structurally satisfies `fdi.pipeline.extractor.ExtractorClient`); `from_settings(settings) -> AzureOpenAIExtractorClient | None`.

- [ ] **Step 1: Write the failing unit test**

```python
# backend/tests/unit/test_azure_extractor_client.py
"""AzureOpenAIExtractorClient wraps the openai SDK in JSON mode. The SDK is
monkeypatched -- no live Azure OpenAI call. Mirrors
test_azure_agent_client.py's _client_returning idiom exactly."""

import json

import pytest


def _client_returning(monkeypatch: pytest.MonkeyPatch, content: str | None) -> dict[str, object]:
    from fdi.platform import extractor as ex

    calls: dict[str, object] = {}

    class _Msg:
        content = content

    class _Choice:
        message = _Msg()

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

    monkeypatch.setattr(ex, "AzureOpenAI", _Client)
    return calls


def test_extract_parses_json_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    payload = {
        "value": 42, "source": {"page": 3, "snippet": "s", "evidence_ids": ["e1"]},
        "confidence": 0.8,
    }
    calls = _client_returning(monkeypatch, json.dumps(payload))
    c = AzureOpenAIExtractorClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    result = c.extract(system="sys", user="usr")
    assert result == payload
    assert calls["kwargs"]["response_format"] == {"type": "json_object"}
    assert calls["kwargs"]["temperature"] == 0


def test_extract_malformed_json_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    _client_returning(monkeypatch, "{not valid json")
    c = AzureOpenAIExtractorClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    assert c.extract(system="sys", user="usr") == {}


def test_extract_empty_content_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    _client_returning(monkeypatch, None)
    c = AzureOpenAIExtractorClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    assert c.extract(system="sys", user="usr") == {}


def test_extract_non_dict_json_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    _client_returning(monkeypatch, json.dumps([1, 2, 3]))
    c = AzureOpenAIExtractorClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    assert c.extract(system="sys", user="usr") == {}


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    assert AzureOpenAIExtractorClient.from_settings(s) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_azure_extractor_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.platform.extractor'`.

- [ ] **Step 3: Implement**

```python
# backend/src/fdi/platform/extractor.py
"""AzureOpenAIExtractorClient -- ExtractorClient backed by Azure OpenAI
(GPT), JSON mode. Lives in platform (does I/O), not pipeline: the
ExtractorClient protocol and FakeExtractorClient live in
fdi.pipeline.extractor. This module structurally satisfies that protocol
without importing it -- platform may not import pipeline per the layered
architecture contract. AzureOpenAI is imported at module level so tests
can monkeypatch fdi.platform.extractor.AzureOpenAI -- no live Azure OpenAI
call in the default suite. Reuses the SAME FDI_GPT_* settings the semantic
stage (fdi.platform.semantic) and the agent (fdi.platform.agent) already
use -- no new config."""

import json

from openai import AzureOpenAI

from fdi.platform.config import Settings


class AzureOpenAIExtractorClient:
    """ExtractorClient backed by Azure OpenAI, JSON mode. Lives in platform
    (does I/O)."""

    def __init__(
        self, *, endpoint: str, key: str, deployment: str, model: str, api_version: str
    ) -> None:
        self._sdk = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
        self._deployment = deployment
        self._model = model

    def extract(self, *, system: str, user: str) -> dict[str, object]:
        resp = self._sdk.chat.completions.create(  # type: ignore[call-overload]
            model=self._deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = resp.choices[0].message.content
        if not content:
            return {}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOpenAIExtractorClient | None":
        if not settings.gpt_endpoint or not settings.gpt_key:
            return None
        return AzureOpenAIExtractorClient(
            endpoint=settings.gpt_endpoint, key=settings.gpt_key,
            deployment=settings.gpt_deployment, model=settings.gpt_model,
            api_version=settings.gpt_api_version,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_azure_extractor_client.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Write + run the opt-in real-Azure test**

```python
# backend/tests/azure/test_real_extraction.py
"""One real Azure OpenAI extraction round trip. Mirrors
test_real_semantic.py / test_real_agent.py exactly: reuses @pytest.mark.azure
(no new marker), skipped without FDI_GPT_ENDPOINT/FDI_GPT_KEY."""

import os

import pytest

from fdi.core.extractor import Candidate, FieldSpec, build_extraction_messages
from fdi.platform.config import get_settings
from fdi.platform.extractor import AzureOpenAIExtractorClient

pytestmark = [
    pytest.mark.azure,
    pytest.mark.skipif(
        not (os.getenv("FDI_GPT_ENDPOINT") and os.getenv("FDI_GPT_KEY")),
        reason="Azure OpenAI not configured",
    ),
]


def test_real_extractor_client_returns_json_shaped_result() -> None:
    client = AzureOpenAIExtractorClient.from_settings(get_settings())
    assert client is not None
    field = FieldSpec(
        field_name="Total revenue", data_type="decimal",
        retrieval_query="Total revenue from operations", keywords=("revenue",),
        extraction_prompt="Extract the total revenue value.", enum_values=None,
        module_id=1, module_name="INCOME_STATEMENT",
    )
    candidate = Candidate(
        evidence_id="s0", text="Total revenue from operations was 1,234.", page=5,
        score=1.0, source_tool="hybrid",
    )
    system, user = build_extraction_messages(field, [candidate])
    result = client.extract(system=system, user=user)
    assert isinstance(result, dict)
    # JSON-mode + this plan's assemble_result already tolerate a garbled or
    # empty dict -- this only needs to confirm the SDK round trip didn't raise.
```

Run: `uv run --directory backend pytest tests/azure -k test_real_extraction -v -m azure`
Expected (creds set): PASS. Expected (no creds): `1 skipped`.

- [ ] **Step 6: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/platform/extractor.py backend/tests/unit/test_azure_extractor_client.py backend/tests/azure/test_real_extraction.py
git commit -m "feat(platform): extractor.py -- AzureOpenAIExtractorClient (JSON mode), reuses FDI_GPT_*"
```

---

### Task 5: API — `POST /extract/document`

**Files:**
- Modify: `backend/src/fdi/api/deps.py` (add `get_extractor_client`/`ExtractorClientDep`, `get_extraction_schema_path`/`ExtractionSchemaPathDep`)
- Modify: `backend/src/fdi/api/app.py` (lifespan: `app.state.extractor_client`, `app.state.extraction_schema_path`)
- Modify: `backend/src/fdi/api/schemas.py` (add `ExtractRequest`/`ExtractSourceView`/`ExtractFieldView`/`ExtractDocumentView`)
- Modify: `backend/src/fdi/api/routes.py` (add `POST /extract/document`)
- Modify: `backend/Dockerfile` (copy the schema file into the image)
- Modify: `backend/tests/integration/test_api.py` (append a new section, following every prior sub-project's own convention)

**Interfaces:**
- Consumes: `extract_document`/`ExtractContext` (Task 3), `AzureOpenAIExtractorClient` (Task 4), `parse_field_specs` (Task 1), `Settings.extraction_schema_path` (Task 2), `SessionDep`/`StoreDep`/`EmbedderDep`/`VectorStoreDep` (existing).
- Produces: `POST /extract/document {doc_key, module?} -> {doc_key, doc_id, module, field_count, fields: [{field_name, value, source: {page, snippet, evidence_ids}, confidence}, ...]}`; 404 unknown `doc_key`; 503 when `extractor_client` is unconfigured.

**Deployment callout (read before Step 5):** `FDI_EXTRACTION_SCHEMA_PATH`'s
default is repo-root-relative (`docs/architecture/field-extraction-schema.json`),
but `backend/Dockerfile`'s `COPY backend/ /app/` never brings `docs/` (a
sibling of `backend/`) into the image, and the image's `WORKDIR` is `/app`.
Without Step 5 below, a deployed container's default-configured route would
`FileNotFoundError` on every request. `docker/compose.yml`'s
`x-backend-build.context` is `..` (the repo root, confirmed: `backend/` and
`docs/` are both immediate children of that context), so a plain `COPY`
(not `--from=builder`, since the builder stage never received `docs/`
either) of just the one schema file is enough — mirrors the existing
`alembic.ini` COPY precedent exactly.

- [ ] **Step 1: Write the failing tests** (append to `test_api.py`, after the RT-8 `/ask` section)

```python
# --------------------------------------------------------------------------
# SP-17: POST /extract/document -- schema-driven RAG field extraction.
# --------------------------------------------------------------------------


async def test_extract_document_503_when_extractor_client_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/extract/document", json={"doc_key": "no-such-key"})
    assert r.status_code == 503
    assert "extractor_client" in r.json()["detail"]


async def test_extract_document_404_unknown_doc_key(make_client, _alembic_applied) -> None:
    from fdi.api.deps import get_extractor_client
    from fdi.pipeline.extractor import FakeExtractorClient

    async with make_client(
        dependency_overrides={get_extractor_client: lambda: FakeExtractorClient([])}
    ) as client:
        r = await client.post("/extract/document", json={"doc_key": "no-such-key"})
    assert r.status_code == 404


async def test_extract_document_happy_path_with_fake_extractor_client_override(
    make_client, session_factory, store, tmp_path, _alembic_applied
) -> None:
    import json
    import uuid

    from fdi.api.deps import get_extraction_schema_path, get_extractor_client
    from fdi.pipeline.extractor import FakeExtractorClient
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.retrieval import index_chunks_fts
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.semantic import SemanticStage

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
            {"role": "sectionHeading", "content": "Overview"},
            {"content": f"ACME is a manufacturer {marker}."},
        ],
        "tables": [],
    })
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
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
        services={"semantic": FakeSemanticClient({"Overview": "MDNA"})}, **common,
    )
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()

    schema = {
        "modules": [{
            "id": 1, "name": "OVERVIEW", "folder": "1-overview",
            "fields": [{
                "field_name": "Company overview text",
                "source_type": "document_content", "rag_extractable": True,
                "data_type": "text", "retrieval_query": f"manufacturer {marker}",
                "extraction_prompt": "Extract the overview.", "keywords": ["manufacturer"],
            }],
        }],
    }
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    fake = FakeExtractorClient([
        {"value": "ACME is a manufacturer.",
         "source": {"page": 1, "snippet": "ACME is a manufacturer",
                     "evidence_ids": [f"{doc.id}:s0"]},
         "confidence": 0.9},
    ])

    async with make_client(
        dependency_overrides={
            get_extractor_client: lambda: fake,
            get_extraction_schema_path: lambda: str(schema_path),
        }
    ) as client:
        r = await client.post("/extract/document", json={"doc_key": f"acme-{marker}"})
    assert r.status_code == 200
    body = r.json()
    assert body["doc_key"] == f"acme-{marker}"
    assert body["doc_id"] == doc.id
    assert body["module"] is None
    assert body["field_count"] == 1
    assert body["fields"][0]["field_name"] == "Company overview text"
    assert body["fields"][0]["value"] == "ACME is a manufacturer."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k test_extract_document -v`
Expected: FAIL — `404` (no `/extract/document` route yet) / `ImportError: get_extractor_client`.

- [ ] **Step 3: Implement**

In `backend/src/fdi/api/deps.py`, add near the other `*Dep` pairs:

```python
from fdi.pipeline.extractor import ExtractorClient


def get_extractor_client(request: Request) -> ExtractorClient | None:
    extractor_client: ExtractorClient | None = request.app.state.extractor_client
    return extractor_client


def get_extraction_schema_path(request: Request) -> str:
    path: str = request.app.state.extraction_schema_path
    return path


ExtractorClientDep = Annotated[ExtractorClient | None, Depends(get_extractor_client)]
ExtractionSchemaPathDep = Annotated[str, Depends(get_extraction_schema_path)]
```

In `backend/src/fdi/api/app.py`'s `lifespan`, alongside the existing
`agent_client` line:

```python
from fdi.platform.extractor import AzureOpenAIExtractorClient
# ...
app.state.extractor_client = AzureOpenAIExtractorClient.from_settings(settings)
app.state.extraction_schema_path = settings.extraction_schema_path
```

In `backend/src/fdi/api/schemas.py`, append:

```python
class ExtractRequest(BaseModel):
    doc_key: str
    module: str | None = None


class ExtractSourceView(BaseModel):
    page: int | None
    snippet: str | None
    evidence_ids: list[str]


class ExtractFieldView(BaseModel):
    field_name: str
    value: object
    source: ExtractSourceView
    confidence: float


class ExtractDocumentView(BaseModel):
    doc_key: str
    doc_id: str
    module: str | None
    field_count: int
    fields: list[ExtractFieldView]
```

In `backend/src/fdi/api/routes.py`: add `import json` to the top-level
imports (alongside `import contextlib`/`import uuid`); add
`ExtractionSchemaPathDep`, `ExtractorClientDep` to the `fdi.api.deps` import
block; add `ExtractDocumentView`, `ExtractRequest` to the `fdi.api.schemas`
import block; add `from fdi.pipeline.extractor import ExtractContext,
extract_document` and `from fdi.core.extractor import parse_field_specs`;
then add the route in a new section after the RT-8 `/ask` block:

```python
# --------------------------------------------------------------------------
# SP-17: POST /extract/document -- schema-driven RAG field extraction,
# built on the retrieval layer (RT-1..RT-8). Reads FDI_EXTRACTION_SCHEMA_
# PATH, parses it (fdi.core.extractor.parse_field_specs, pure), resolves
# doc_key to its latest document version, and answers every rag_extractable
# document_content field of the given module (or the whole schema when
# module is omitted) via fdi.pipeline.extractor.extract_document. 503s
# ONLY when extractor_client is unconfigured -- an unconfigured embedder/
# vector_store degrades each field's pool (fewer candidates), not the
# route (spec decision 10, same posture as /ask's tool-level degradation).
# --------------------------------------------------------------------------


@router.post("/extract/document", response_model=ExtractDocumentView)
async def extract_document_route(
    body: ExtractRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
    extractor_client: ExtractorClientDep,
    schema_path: ExtractionSchemaPathDep,
) -> ExtractDocumentView:
    if extractor_client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "extract/document is not configured: missing extractor_client "
                "(set FDI_GPT_ENDPOINT/FDI_GPT_KEY)"
            ),
        )
    versions = await DocumentRepository(session).list_by_doc_key(body.doc_key)
    if not versions:
        raise HTTPException(status_code=404, detail=f"no documents for doc_key {body.doc_key!r}")
    doc = versions[-1]  # list_by_doc_key orders by version ascending -> last is latest

    with open(schema_path, encoding="utf-8") as f:
        schema_raw = json.load(f)
    schema = schema_raw if isinstance(schema_raw, dict) else {}
    fields = parse_field_specs(schema, body.module)

    ctx = ExtractContext(
        session=session, store=store, embedder=embedder, vector_store=vector_store, doc_id=doc.id,
    )
    result = await extract_document(ctx=ctx, fields=fields, client=extractor_client)
    return ExtractDocumentView.model_validate(
        {"doc_key": body.doc_key, "module": body.module, **result}
    )
```

In `backend/Dockerfile`'s runtime stage, add one line after the
`alembic.ini` COPY:

```dockerfile
COPY --chown=fdi:fdi docs/architecture/field-extraction-schema.json /app/docs/architecture/field-extraction-schema.json
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k test_extract_document -v`
Expected: PASS (all 3).

Run the whole file to confirm nothing else regressed:
`uv run --directory backend pytest tests/integration/test_api.py -v`

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/deps.py backend/src/fdi/api/app.py backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/Dockerfile backend/tests/integration/test_api.py
git commit -m "feat(api): POST /extract/document -- the schema-driven extractor route"
```

---

### Task 6: Full gates, spec status, roadmap note

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-extractor-pipeline-design.md` (Status → Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (append a short downstream note)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`,
  `PYTHONUTF8=1 lint-imports` (1 kept/0 broken). Grep-confirm
  `fdi.core.extractor` imports neither `fdi.pipeline`/`fdi.platform` nor
  `fdi.core.retrieval`/`fdi.core.tables`/`fdi.core.agent`; confirm
  `fdi.platform.extractor` imports neither `fdi.pipeline.extractor` nor
  `fdi.pipeline.agent`.

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`
  (default: fast loop, no integration/e2e/azure) then the `integration`
  marker set with containers up; `azure` stays opt-in and skips without
  creds. Note the passed count against the prior total.

- [ ] **Step 3: Audit done criteria** (spec §7) — map each numbered item to
  its task:
  1. `fdi.core.extractor` pure, unit-tested, never-raise → Task 1.
  2. `fdi.pipeline.extractor` (`ExtractorClient`/`FakeExtractorClient`,
     `extract_field`/`extract_document`, never-raise, pool via
     `hybrid_search`+`get_tables`) → Task 3.
  3. `fdi.platform.extractor.AzureOpenAIExtractorClient` (JSON mode,
     monkeypatchable, `FDI_GPT_*`, `from_settings` None-gated) → Task 4.
  4. `POST /extract/document` shape, 404, 503, `FDI_EXTRACTION_SCHEMA_PATH`
     trio → Tasks 2, 5.
  5. All four gates green; default suite makes NO Azure call and needs no
     Milvus/Neo4j; real Azure only behind `@pytest.mark.azure`; §6 deferred
     items out of scope → Steps 1–2 of this task.
  Confirm the numeric-field `table:` candidate assertion (Task 3) and the
  empty-pool well-formed-record assertion (Task 3's second test) both pass;
  confirm `tests/azure/test_real_extraction.py` skips cleanly without creds.

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps 1–3 green).

- [ ] **Step 5: Roadmap note** — append a short "## Downstream: the
  extractor pipeline" section to `docs/architecture/retrieval-roadmap.md`
  pointing at `docs/superpowers/specs/2026-07-18-extractor-pipeline-design.md`,
  stating that `POST /extract/document` is built entirely on RT-4's
  `hybrid_search` and RT-6's `get_tables` — no new retrieval primitive was
  needed, confirming the retrieval layer's tool set was sufficient for the
  next layer up.

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: extractor pipeline done-criteria audit, spec status, roadmap note"
```

---

## Self-Review

**Spec coverage:** §3.1 `fdi.core.extractor` (`FieldSpec`/`Candidate`,
`parse_field_specs`, `is_numeric`, `keyword_overlap`, `rerank_candidates`,
`build_extraction_messages`, `assemble_result`) → Task 1. §3.2
`ExtractorClient`/`FakeExtractorClient`/`ExtractContext`/`extract_field`/
`extract_document` → Task 3. §3.3 `AzureOpenAIExtractorClient` → Task 4.
§3.4 `POST /extract/document` → Task 5. §3.5 config → Task 2. §2 decisions:
schema-driven batch + module filter (d1) → Task 3's `extract_document` loop
+ Task 5's `parse_field_specs(schema, body.module)`; rag_extractable+
document_content filter (d2) → Task 1's `parse_field_specs`; model-free
blend rerank (d3) → Task 1's `rerank_candidates`; hybrid+tables pool (d4) →
Task 3's `extract_field`; concept_lookup best-effort/deferred (d5) → not
built, noted in Task 3's docstring and §6 audit; unified candidate shape
(d6) → Task 1's `Candidate` dataclass, consumed identically by every pool
source in Task 3; ExtractorClient mirrors RT-8's agent seam (d7) → Task 3's
Protocol + Task 4's `AzureOpenAIExtractorClient`, both sync `extract`; output
contract + fallbacks (d8) → Task 1's `assemble_result` + its 6 dedicated
tests; schema loading config-pathed, parsed in core, read in route (d9) →
Task 2 (config) + Task 5 (route's `open`/`json.load` + `parse_field_specs`
call); never-raise well-formed record (d10) → Task 3's `extract_field`
guards + both of its integration tests. §4 error handling → Task 1's
`parse_field_specs`/`assemble_result` tests, Task 3's blank-query and
unmatchable-field tests. §5 testing → unit (Task 1), integration (Task 3),
API (Task 5), opt-in Azure (Task 4), config (Task 2) — one file each,
matching the spec's file list exactly. §6 deferred → not built anywhere
(Task 6 audit: no `normalization`/`data_type` coercion, no
`CanonicalConcept` field map, no whole-document/parallel/cached batch, no
LLM rerank). §7 done criteria → Task 6.

**Placeholder scan:** every task (1, 3, 4, 5) carries complete, pasteable
code — no "TBD"/"add error handling"/"similar to Task N" placeholders.
Task 5's Dockerfile/route steps spell out the exact line to add rather than
describing it. Task 3's integration test builds its own local
`session_factory`/`store` fixtures (matching `test_tables_lookup.py`'s
established pattern) rather than assuming a shared conftest fixture that
doesn't exist at this path.

**Type consistency:** `FieldSpec`/`Candidate` (Task 1, `fdi.core.extractor`)
are constructed and consumed unchanged by `fdi.pipeline.extractor` (Task 3)
— `extract_field` builds `Candidate`s from `hybrid_search` hits and
`get_tables` grids, `rerank_candidates`/`build_extraction_messages`/
`assemble_result` consume them by the exact field names Task 1 defined.
`ExtractorClient.extract(*, system: str, user: str) -> Mapping[str, object]`
(Task 3) is the exact signature both `FakeExtractorClient` (Task 3) and
`AzureOpenAIExtractorClient` (Task 4) implement — neither is `async`.
`ExtractContext`'s five fields (Task 3) are constructed identically by
Task 3's own tests and Task 5's route. `extract_document`'s return
(`{schema_version, doc_id, field_count, fields}`, Task 3) is merged with
`{doc_key, module}` by exactly one caller — Task 5's route — into the
`ExtractDocumentView` shape declared in the same task's `schemas.py`
addition; no other task re-derives or duplicates that merge.

**Risk flagged (for a human to confirm, not auto-resolved by this plan):**

1. **`hybrid_search` scoping.** The design spec's `ExtractContext` (§3.2)
   carries only `doc_id`, but `hybrid_search`'s filters are `doc_key`/
   `version`-keyed. This plan's Task 3 resolves `ctx.doc_id` → `Document`
   row via one extra `DocumentRepository.get` call PER FIELD inside
   `extract_field`, rather than widening `ExtractContext` with `doc_key`/
   `version` fields the route could resolve ONCE. The chosen approach keeps
   the dataclass exactly as spec-shown at the cost of O(fields) redundant
   single-row lookups per document (cheap at module scale, a real but small
   cost if a future "whole-document" batch — explicitly deferred, §6 —
   ever extracts hundreds of fields in one call). If that follow-on
   happens, revisit this trade-off then.
2. **`extract_document`'s return shape vs. its shown signature.** §3.2
   describes the return as including `doc_key`/`module`, but the shown
   signature takes only `ctx`/`fields`/`client`. This plan has
   `extract_document` return `{schema_version, doc_id, field_count,
   fields}` and lets Task 5's route attach `doc_key`/`module` (which it
   already holds from the request body) — a direct, low-risk gap-fill, not
   a design fork; flagged here only so a reviewer isn't surprised the
   function's return differs from the spec prose's literal shape.
3. **`FDI_EXTRACTION_SCHEMA_PATH`'s repo-root-relative default resolves in
   NEITHER of the project's two real execution contexts** (the built
   Docker image's `WORKDIR /app`, and the test suite's `uv run --directory
   backend` cwd) without help — this is a genuine, previously-undocumented
   operational gap, not an artifact of this plan's choices. This plan's
   fix (Task 5: one `COPY` line copying just the schema file into the
   image at the same relative path, mirroring the existing `alembic.ini`
   COPY) is the minimal option. A human should confirm this is the
   preferred fix over the alternatives: mounting `docs/` as a live volume
   (trades a rebuild-to-update schema change for a slightly more complex
   compose file, but matches this project's dev-oriented Compose style
   elsewhere — e.g. `rabbitmq.conf`'s bind mount), or relocating the
   canonical schema file under `backend/` entirely (removes the cross-
   directory reference for good, but moves the file away from
   `docs/architecture/` where the design spec and its sibling research
   docs currently point at it).
