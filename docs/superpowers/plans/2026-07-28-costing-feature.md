# Costing Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track OCR page charges and LLM token charges per document and corpus-wide, per-call, and report them converted to Indian Rupees, per `docs/superpowers/specs/2026-07-28-costing-feature-design.md`.

**Architecture:** Two new Postgres tables (`ocr_usage_event`, `llm_usage_event`) store raw usage units only; INR conversion happens at read time via four new `FDI_*` pricing settings and a pure `fdi.core.pricing` module. OCR is timed generically inside `run_stage`. LLM calls are captured at their three Azure OpenAI call sites (`platform/agent.py`, `platform/extractor.py`, `platform/semantic.py`), which now return their payload alongside an optional `LlmCallUsage`; a per-execution `UsageAccumulator` (new `fdi.pipeline.usage` module) collects `semantic` stage calls and `run_stage` persists them, while `/ask` and `/extract/document` (which run outside `run_stage` entirely) record directly through their own DB session.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 (async, asyncpg), Alembic, Celery, pytest (unit + testcontainers integration), Next.js/TypeScript frontend, React Query.

## Global Constraints

- Every new `FDI_*` Settings field MUST be mirrored in exactly three places or it is inert/crashes boot: `backend/src/fdi/platform/config.py` (the field), `docker/compose.yml`'s `x-backend-env` block, and `.env.example`. Missing any one is a known failure mode this codebase's own comments warn about repeatedly.
- Timestamps on new ORM columns are DB-clock (`server_default=func.now()`), never app-side `datetime.utcnow()` — this project's stated policy (`models.py:1-2`).
- Primary keys are `String(36)` UUIDs (`str(uuid.uuid4())`), matching every existing table.
- Alembic migrations are 100% handwritten (no autogenerate), sequential zero-padded revision ids (`"0008"`, filename `0008_<snake_case>.py`), a real working `downgrade()`, `CheckConstraint` for any bounded-string column, explicit `op.create_index` on every FK/filter column.
- `mypy --strict src`, `ruff`, `lint-imports`, and the full non-e2e test suite must stay green. Run from `backend/`: `uv run mypy --strict src`, `uv run ruff check .`, `uv run lint-imports`, `uv run pytest -m "not e2e"` (unit tests need no Docker; integration tests need `docker compose` running per `docker/compose.yml` — see Task 1 of this session for how to bring that stack up. Run `uv run pytest -m unit` alone if Docker is unavailable).
- Layering is `api|workers → pipeline → platform → core`; `platform` may never import `pipeline`. New pure data shapes (`LlmCallUsage`, `ClassifyResult`, `ExtractResult`) live in `fdi.core.*` so a `platform` client can construct them without importing `pipeline`, exactly how `AgentResponse` already works.
- No placeholders, no `TODO`, no bare `# handle errors` comments — every step below is the literal content to write.

---

### Task 1: Settings — four new pricing/FX fields

**Files:**
- Modify: `backend/src/fdi/platform/config.py:216` (after `extraction_schema_path`, before `settings_customise_sources`)
- Modify: `docker/compose.yml:50` (after `FDI_CORS_ORIGINS`, before `FDI_EXTRACTION_SCHEMA_PATH`, inside the `x-backend-env` anchor)
- Modify: `.env.example` (after `FDI_ENTITY_SIM_THRESHOLD`, before `FDI_CORS_ORIGINS`)
- Test: `backend/tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings.ocr_cost_per_page_inr: float`, `Settings.llm_input_cost_per_mtok_usd: float`, `Settings.llm_output_cost_per_mtok_usd: float`, `Settings.usd_to_inr_rate: float` — every later task that prices anything reads these off `get_settings()`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/unit/test_config.py`, after `test_entity_resolution_settings_default`:

```python
def test_costing_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.ocr_cost_per_page_inr == 1.0
    assert s.llm_input_cost_per_mtok_usd == 2.5
    assert s.llm_output_cost_per_mtok_usd == 10.0
    assert s.usd_to_inr_rate == 95.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_config.py::test_costing_settings_default -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'ocr_cost_per_page_inr'`

- [ ] **Step 3: Add the fields to `Settings`**

In `backend/src/fdi/platform/config.py`, insert after line 216 (`extraction_schema_path: str = str(_default_schema_path())`) and before the `@classmethod` on line 218:

```python

    # Costing (docs/superpowers/specs/2026-07-28-costing-feature-design.md).
    # OCR is billed 1 INR/page flat. LLM is billed per Azure OpenAI's own
    # $/M-token pricing, converted to INR at READ time (never persisted per
    # event -- a rate change here recomputes every historical total, by
    # design: spec decision 2). usd_to_inr_rate is a static configured
    # value, not fetched from any FX API (spec sec 6, explicitly deferred).
    ocr_cost_per_page_inr: float = 1.0
    llm_input_cost_per_mtok_usd: float = 2.5
    llm_output_cost_per_mtok_usd: float = 10.0
    usd_to_inr_rate: float = 95.0
```

- [ ] **Step 4: Mirror into `docker/compose.yml`**

In `docker/compose.yml`, insert after line 50 (`FDI_CORS_ORIGINS: '${FDI_CORS_ORIGINS:-["http://localhost:3000"]}'`) and before line 51 (`FDI_EXTRACTION_SCHEMA_PATH: ${FDI_EXTRACTION_SCHEMA_PATH}`):

```yaml
  FDI_OCR_COST_PER_PAGE_INR: ${FDI_OCR_COST_PER_PAGE_INR}
  FDI_LLM_INPUT_COST_PER_MTOK_USD: ${FDI_LLM_INPUT_COST_PER_MTOK_USD}
  FDI_LLM_OUTPUT_COST_PER_MTOK_USD: ${FDI_LLM_OUTPUT_COST_PER_MTOK_USD}
  FDI_USD_TO_INR_RATE: ${FDI_USD_TO_INR_RATE}
```

- [ ] **Step 5: Mirror into `.env.example`**

In `.env.example`, insert after the `FDI_ENTITY_SIM_THRESHOLD=0.85` line and before the `FDI_CORS_ORIGINS=...` block:

```
# Costing (docs/superpowers/specs/2026-07-28-costing-feature-design.md).
# OCR billed flat per page; LLM billed per Azure OpenAI's own $/M-token
# pricing, converted to INR at read time via this rate.
FDI_OCR_COST_PER_PAGE_INR=1.0
FDI_LLM_INPUT_COST_PER_MTOK_USD=2.5
FDI_LLM_OUTPUT_COST_PER_MTOK_USD=10.0
FDI_USD_TO_INR_RATE=95.0
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/unit/test_config.py -v`
Expected: PASS, all tests in the file green (including the new one).

- [ ] **Step 7: Commit**

```bash
git add backend/src/fdi/platform/config.py docker/compose.yml .env.example backend/tests/unit/test_config.py
git commit -m "feat(costing): add OCR/LLM pricing and FX-rate settings"
```

---

### Task 2: Pure pricing core — `fdi.core.pricing`

**Files:**
- Create: `backend/src/fdi/core/pricing.py`
- Test: `backend/tests/unit/test_pricing.py`

**Interfaces:**
- Produces: `LlmCallUsage(model: str, input_tokens: int, output_tokens: int, duration_ms: int)` — the shape every platform LLM client will return alongside its payload (Tasks 3-6). `ocr_cost_inr(page_count: int, price_per_page_inr: float) -> float`. `llm_cost_inr(input_tokens: int, output_tokens: int, *, input_usd_per_mtok: float, output_usd_per_mtok: float, usd_to_inr: float) -> float` — both used by the API layer (Task 16).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_pricing.py`:

```python
from fdi.core.pricing import LlmCallUsage, llm_cost_inr, ocr_cost_inr


def test_ocr_cost_inr_zero_pages() -> None:
    assert ocr_cost_inr(0, 1.0) == 0.0


def test_ocr_cost_inr_known_pages() -> None:
    assert ocr_cost_inr(42, 1.0) == 42.0
    assert ocr_cost_inr(10, 2.5) == 25.0


def test_llm_cost_inr_zero_tokens() -> None:
    assert llm_cost_inr(
        0, 0, input_usd_per_mtok=2.5, output_usd_per_mtok=10.0, usd_to_inr=95.0
    ) == 0.0


def test_llm_cost_inr_known_tokens() -> None:
    # 1_000_000 input tokens @ $2.5/M + 1_000_000 output tokens @ $10/M
    # = $12.5 USD * 95 INR/USD = 1187.5 INR
    cost = llm_cost_inr(
        1_000_000, 1_000_000,
        input_usd_per_mtok=2.5, output_usd_per_mtok=10.0, usd_to_inr=95.0,
    )
    assert cost == 1187.5


def test_llm_cost_inr_fractional_tokens() -> None:
    # 500 input tokens @ $2.5/M = $0.00125 -> * 95 = 0.11875 INR
    cost = llm_cost_inr(
        500, 0, input_usd_per_mtok=2.5, output_usd_per_mtok=10.0, usd_to_inr=95.0
    )
    assert cost == 0.11875


def test_llm_call_usage_is_frozen() -> None:
    usage = LlmCallUsage(model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=120)
    assert usage.model == "gpt-4o"
    assert usage.input_tokens == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_pricing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fdi.core.pricing'`

- [ ] **Step 3: Write the implementation**

Create `backend/src/fdi/core/pricing.py`:

```python
"""Pure OCR/LLM cost calculation, and the `LlmCallUsage` data shape every
platform LLM client (agent/extractor/semantic) returns alongside its
payload. No I/O, no config object, no clock -- costs are computed from
already-known counts and a caller-supplied price, per
docs/superpowers/specs/2026-07-28-costing-feature-design.md sec 2 decision
2: INR is always computed at READ time from the currently configured
price, never persisted per event."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LlmCallUsage:
    """One real chat.completions.create() call's cost-relevant facts."""

    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int


def ocr_cost_inr(page_count: int, price_per_page_inr: float) -> float:
    return page_count * price_per_page_inr


def llm_cost_inr(
    input_tokens: int,
    output_tokens: int,
    *,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    usd_to_inr: float,
) -> float:
    usd = (
        (input_tokens / 1_000_000) * input_usd_per_mtok
        + (output_tokens / 1_000_000) * output_usd_per_mtok
    )
    return usd * usd_to_inr
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/unit/test_pricing.py -v`
Expected: PASS, 6 tests green.

- [ ] **Step 5: Commit**

```bash
git add backend/src/fdi/core/pricing.py backend/tests/unit/test_pricing.py
git commit -m "feat(costing): add pure OCR/LLM INR cost calculation"
```

---

### Task 3: `AgentResponse` gains optional usage

**Files:**
- Modify: `backend/src/fdi/core/agent.py:24-37`
- Test: `backend/tests/unit/test_agent_core.py`

**Interfaces:**
- Consumes: `LlmCallUsage` from `fdi.core.pricing` (Task 2).
- Produces: `AgentResponse.usage: LlmCallUsage | None = None`. Defaulting to `None` means every existing `AgentResponse(...)` construction site (`tests/integration/test_ask.py`, `tests/integration/test_api.py`, `tests/unit/test_agent_client_protocol.py`) needs NO changes — this is a deliberately additive, non-breaking widening. `fdi.platform.agent.AzureOpenAIAgentClient.complete` (Task 6) will populate it for real; `FakeAgentClient` (unchanged) leaves it `None`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/unit/test_agent_core.py`, after `test_agent_response_requires_exactly_one_of_tool_calls_or_content`:

```python
def test_agent_response_usage_defaults_to_none() -> None:
    r = AgentResponse(content="ok")
    assert r.usage is None


def test_agent_response_carries_explicit_usage() -> None:
    from fdi.core.pricing import LlmCallUsage

    usage = LlmCallUsage(model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=42)
    r = AgentResponse(content="ok", usage=usage)
    assert r.usage == usage
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_agent_core.py::test_agent_response_carries_explicit_usage -v`
Expected: FAIL with `TypeError: AgentResponse.__init__() got an unexpected keyword argument 'usage'`

- [ ] **Step 3: Add the field**

In `backend/src/fdi/core/agent.py`, change the `AgentResponse` dataclass (lines 24-37):

```python
@dataclass(frozen=True)
class AgentResponse:
    tool_calls: list[ToolCall] | None = None
    content: str | None = None
    # Populated by fdi.platform.agent.AzureOpenAIAgentClient.complete from the
    # SDK response's own `usage` object -- costing feature (docs/superpowers/
    # specs/2026-07-28-costing-feature-design.md). None from every fake/test
    # double, which is the correct "no real Azure call happened" signal:
    # answer_question skips recording usage when this is None.
    usage: "LlmCallUsage | None" = None

    def __post_init__(self) -> None:
        has_calls = bool(self.tool_calls)
        has_content = self.content is not None
        if has_calls == has_content:  # both True, or both False
            raise ValueError(
                "AgentResponse must set exactly one of tool_calls (non-empty list) "
                "or content (a string) -- mirrors an OpenAI completion's "
                "tool_calls/stop exclusivity."
            )
```

And add the import at the top of the file, after the existing `from dataclasses import dataclass` line:

```python
from fdi.core.pricing import LlmCallUsage
```

(Remove the quotes around the type annotation once the import is real: `usage: LlmCallUsage | None = None`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/unit/test_agent_core.py -v`
Expected: PASS, all tests in the file green (including the two new ones) — confirms no existing `AgentResponse(...)` construction broke.

- [ ] **Step 5: Run the full unit suite to confirm zero ripple**

Run: `cd backend && uv run pytest -m unit -v`
Expected: PASS, no failures anywhere referencing `AgentResponse`.

- [ ] **Step 6: Commit**

```bash
git add backend/src/fdi/core/agent.py backend/tests/unit/test_agent_core.py
git commit -m "feat(costing): AgentResponse carries optional LLM usage"
```

---

### Task 4: `SemanticClient` widens to return `ClassifyResult`

**Files:**
- Modify: `backend/src/fdi/core/semantic.py` (add `ClassifyResult`)
- Modify: `backend/src/fdi/pipeline/semantic.py` (Protocol + `FakeSemanticClient`)
- Modify: `backend/src/fdi/platform/semantic.py` (`AzureOpenAIClient.classify`)
- Modify: `backend/tests/unit/test_semantic_client.py`
- Test: same file (updated assertions)

**Interfaces:**
- Consumes: `LlmCallUsage` from `fdi.core.pricing` (Task 2).
- Produces: `ClassifyResult(label: str, usage: LlmCallUsage | None = None)`. `SemanticClient.classify(...) -> ClassifyResult` (was `-> str`). Task 13 (`SemanticStage.run`) consumes `.label` and `.usage`.
- This is the ONLY test file that calls `.classify()` directly and inspects its return shape (confirmed by repo-wide grep); the ~24 other `FakeSemanticClient(...)` construction sites across the test suite never call `.classify()` themselves — they pass the fake into a stage runner — so they need no changes.

- [ ] **Step 1: Write the failing test**

Replace the full contents of `backend/tests/unit/test_semantic_client.py`:

```python
from fdi.core.semantic import SectionType
from fdi.pipeline.semantic import FakeSemanticClient, SemanticClient


def test_fake_records_and_returns_mapped_label() -> None:
    fake = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})
    client: SemanticClient = fake
    result = client.classify("Balance Sheet", "Assets 100")
    assert result.label == "BALANCE_SHEET"
    assert result.usage is None
    assert fake.calls == 1
    assert fake.seen == [("Balance Sheet", "Assets 100")]


def test_fake_defaults_to_other_for_unmapped_heading() -> None:
    fake = FakeSemanticClient({})
    assert fake.classify("Random Heading", "text").label == "OTHER"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_semantic_client.py -v`
Expected: FAIL with `AttributeError: 'str' object has no attribute 'label'`

- [ ] **Step 3: Add `ClassifyResult` to `fdi.core.semantic`**

In `backend/src/fdi/core/semantic.py`, add after the `SectionType` class (line 19) and before `build_snippet`:

```python
from dataclasses import dataclass

from fdi.core.pricing import LlmCallUsage


@dataclass(frozen=True)
class ClassifyResult:
    """One SemanticClient.classify() call's outcome: the coerced label plus
    the LLM usage that produced it (costing feature). `usage` is None from
    every fake/test double -- SemanticStage.run only records a usage event
    when it is present."""

    label: str
    usage: LlmCallUsage | None = None
```

(Place the two new imports at the top of the file, after the existing `from enum import StrEnum` line, not inline mid-file.)

- [ ] **Step 4: Widen the Protocol and `FakeSemanticClient`**

In `backend/src/fdi/pipeline/semantic.py`, replace the full file:

```python
"""The SemanticClient seam: a `classify(heading, text) -> ClassifyResult`
protocol.

`AzureOpenAIClient` (platform, does I/O) structurally satisfies this without
importing it -- `platform` may not import `pipeline`. `FakeSemanticClient`
lives here for tests: no live Azure OpenAI call anywhere in the suite.
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from fdi.core.semantic import ClassifyResult, SectionType


@runtime_checkable
class SemanticClient(Protocol):
    def classify(self, heading: str, text: str) -> ClassifyResult: ...


class FakeSemanticClient:
    def __init__(self, labels: Mapping[str, str]) -> None:
        self._labels = labels
        self.calls = 0
        self.seen: list[tuple[str, str]] = []

    def classify(self, heading: str, text: str) -> ClassifyResult:
        self.calls += 1
        self.seen.append((heading, text))
        return ClassifyResult(label=self._labels.get(heading, SectionType.OTHER.value))
```

- [ ] **Step 5: Populate real usage in `AzureOpenAIClient.classify`**

In `backend/src/fdi/platform/semantic.py`, replace the `classify` method and add a `time` import:

```python
import time

from openai import AzureOpenAI

from fdi.core.pricing import LlmCallUsage
from fdi.core.semantic import ClassifyResult
from fdi.platform.config import Settings
```

```python
    def classify(self, heading: str, text: str) -> ClassifyResult:
        start = time.perf_counter()
        resp = self._sdk.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Heading: {heading}\n\n{text}"},
            ],
            temperature=0,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        content = resp.choices[0].message.content
        label = (content or "").strip().upper()
        usage = resp.usage
        return ClassifyResult(
            label=label,
            usage=LlmCallUsage(
                model=self._model,
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                duration_ms=duration_ms,
            ),
        )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/unit/test_semantic_client.py -v`
Expected: PASS.

- [ ] **Step 7: Confirm the wider suite still collects (stage code not yet updated)**

Run: `cd backend && uv run pytest -m unit -v`
Expected: PASS for unit tests. `tests/integration/test_semantic_stage.py` and other integration tests that exercise `SemanticStage.run()` will fail at COLLECTION or RUN time until Task 13 updates `SemanticStage.run()` to unpack `ClassifyResult` — this is expected and fixed in Task 13, not here. Do not attempt to fix `SemanticStage.run()` in this task.

- [ ] **Step 8: Commit**

```bash
git add backend/src/fdi/core/semantic.py backend/src/fdi/pipeline/semantic.py backend/src/fdi/platform/semantic.py backend/tests/unit/test_semantic_client.py
git commit -m "feat(costing): SemanticClient.classify returns usage alongside label"
```

---

### Task 5: `ExtractorClient` widens to return `ExtractResult`

**Files:**
- Modify: `backend/src/fdi/core/extractor.py` (add `ExtractResult`, near top after the module docstring's imports)
- Modify: `backend/src/fdi/pipeline/extractor.py` (Protocol, `FakeExtractorClient`, `extract_unit`, `extract_field`)
- Modify: `backend/src/fdi/platform/extractor.py` (`AzureOpenAIExtractorClient.extract`)

**Interfaces:**
- Consumes: `LlmCallUsage` from `fdi.core.pricing` (Task 2).
- Produces: `ExtractResult(data: Mapping[str, object], usage: LlmCallUsage | None = None)`. `ExtractorClient.extract(...) -> ExtractResult` (was `-> dict[str, object]`). `FakeExtractorClient`'s constructor is UNCHANGED (`list[Mapping[str, object]]`) — it wraps each scripted mapping into `ExtractResult(data=m, usage=None)` internally, so all ~10 existing `FakeExtractorClient([...])` construction sites across the test suite need no changes (confirmed: none of them call `.extract()` directly and inspect a bare-dict return; they always go through `extract_unit`/`extract_field`/`extract_document`).
- Task 15 (`extract_unit`/`extract_field`) consumes `.data` (renaming the old `raw` local) and `.usage`.

- [ ] **Step 1: Add `ExtractResult` to `fdi.core.extractor`**

In `backend/src/fdi/core/extractor.py`, find the import block near the top (after the module docstring, before line 60's `import re` — the file starts imports at line 60 per the existing read). Add, immediately after `import re` and before any subsequent imports:

```python
from collections.abc import Mapping
from dataclasses import dataclass

from fdi.core.pricing import LlmCallUsage


@dataclass(frozen=True)
class ExtractResult:
    """One ExtractorClient.extract() call's outcome: the raw parsed JSON
    dict plus the LLM usage that produced it (costing feature). `usage` is
    None from FakeExtractorClient -- extract_unit/extract_field only record
    a usage event when it is present."""

    data: Mapping[str, object]
    usage: LlmCallUsage | None = None
```

(If `Mapping` is already imported in this file from `collections.abc` elsewhere, do not duplicate the import — merge into the existing `from collections.abc import ...` line instead.)

- [ ] **Step 2: Widen the Protocol and `FakeExtractorClient` in `fdi.pipeline.extractor`**

In `backend/src/fdi/pipeline/extractor.py`, change the `ExtractorClient` Protocol and `FakeExtractorClient` class (currently lines 92-117):

```python
@runtime_checkable
class ExtractorClient(Protocol):
    def extract(self, *, system: str, user: str) -> ExtractResult: ...


class FakeExtractorClient:
    """Replays scripted {value, source, confidence}-shaped dicts, one per
    call, in order -- mirrors fdi.pipeline.agent.FakeAgentClient exactly
    (same AssertionError-on-overcall posture, a loud test-diagnosable
    failure rather than a confusing IndexError deep inside extract_field).
    Constructor is unchanged (still `list[Mapping[str, object]]`) -- each
    scripted mapping is wrapped into an ExtractResult with usage=None
    internally, so no existing FakeExtractorClient(...) construction site
    needs updating for the costing feature."""

    def __init__(self, results: list[Mapping[str, object]]) -> None:
        self._results = results
        self._next = 0
        self.calls: list[tuple[str, str]] = []

    def extract(self, *, system: str, user: str) -> ExtractResult:
        self.calls.append((system, user))
        if self._next >= len(self._results):
            raise AssertionError(
                f"FakeExtractorClient exhausted: {len(self._results)} scripted "
                f"result(s), call {len(self.calls)} requested"
            )
        result = self._results[self._next]
        self._next += 1
        return ExtractResult(data=result)
```

Add `ExtractResult` to this file's imports — it already has `from fdi.core.extractor import (...)` (lines 64-79); add `ExtractResult` alphabetically into that import list.

- [ ] **Step 3: Update `extract_unit` and `extract_field` to unpack `ExtractResult`**

In `backend/src/fdi/pipeline/extractor.py`, in `extract_unit` (around line 292-295), change:

```python
    prompt_candidates = [
        _render_table_candidate(c, structure=structure or {}) for c in ranked
    ]
    system, user = build_unit_messages(unit, prompt_candidates)
    raw: Mapping[str, object] = {}
    with contextlib.suppress(Exception):  # never raise (decision 10)
        raw = client.extract(system=system, user=user)
    return assemble_unit_results(unit, raw, ranked)
```

to:

```python
    prompt_candidates = [
        _render_table_candidate(c, structure=structure or {}) for c in ranked
    ]
    system, user = build_unit_messages(unit, prompt_candidates)
    raw: Mapping[str, object] = {}
    with contextlib.suppress(Exception):  # never raise (decision 10)
        result = client.extract(system=system, user=user)
        raw = result.data
        if result.usage is not None:
            await _record_llm_usage(
                ctx=ctx, source="extract_document", usage=result.usage,
            )
    return assemble_unit_results(unit, raw, ranked)
```

In `extract_field` (around line 310-314), apply the identical change:

```python
    system, user = build_extraction_messages(field, ranked)
    raw: Mapping[str, object] = {}
    with contextlib.suppress(Exception):  # never raise (decision 10)
        result = client.extract(system=system, user=user)
        raw = result.data
        if result.usage is not None:
            await _record_llm_usage(
                ctx=ctx, source="extract_document", usage=result.usage,
            )
    return assemble_result(field, raw, ranked)
```

Note: `_record_llm_usage` is defined in Task 15, not this task — this task's job is only the return-type widening. After this step, `extract_unit`/`extract_field` will fail to import (`_record_llm_usage` undefined) until Task 15. This is expected; proceed to Step 4 and note it in the commit, then finish the ripple in Task 15.

- [ ] **Step 4: Populate real usage in `AzureOpenAIExtractorClient.extract`**

In `backend/src/fdi/platform/extractor.py`, replace the `extract` method and update imports:

```python
import json
import time

from openai import AzureOpenAI

from fdi.core.extractor import ExtractResult
from fdi.core.pricing import LlmCallUsage
from fdi.platform.config import Settings
```

```python
    def extract(self, *, system: str, user: str) -> ExtractResult:
        start = time.perf_counter()
        resp = self._sdk.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        usage_obj = resp.usage
        usage = LlmCallUsage(
            model=self._model,
            input_tokens=usage_obj.prompt_tokens if usage_obj else 0,
            output_tokens=usage_obj.completion_tokens if usage_obj else 0,
            duration_ms=duration_ms,
        )
        content = resp.choices[0].message.content
        if not content:
            return ExtractResult(data={}, usage=usage)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return ExtractResult(data={}, usage=usage)
        return ExtractResult(
            data=parsed if isinstance(parsed, dict) else {}, usage=usage
        )
```

Note this deliberately records `usage` on EVERY branch, including the JSON-decode-failure branch — per spec decision 4 ("bill on any successful provider response"): the Azure call succeeded and consumed real tokens even though this code could not use the resulting content.

- [ ] **Step 5: Do not run the full suite yet**

`extract_unit`/`extract_field` reference `_record_llm_usage`, which does not exist until Task 15. Skip running tests for this task beyond a syntax sanity check; Task 15 completes the wiring and is where this whole chain gets test-verified together.

Run: `cd backend && uv run python -c "import ast; ast.parse(open('src/fdi/pipeline/extractor.py').read())"`
Expected: no `SyntaxError`.

- [ ] **Step 6: Commit**

```bash
git add backend/src/fdi/core/extractor.py backend/src/fdi/pipeline/extractor.py backend/src/fdi/platform/extractor.py
git commit -m "feat(costing): ExtractorClient.extract returns usage alongside parsed data"
```

---

### Task 6: `AzureOpenAIAgentClient` populates usage

**Files:**
- Modify: `backend/src/fdi/platform/agent.py`

**Interfaces:**
- Consumes: `LlmCallUsage` from `fdi.core.pricing` (Task 2), `AgentResponse.usage` field (Task 3).
- Produces: real `AgentResponse.usage` on every `complete()` call. No test changes needed here — `test_agent_client_protocol.py` never asserts `.usage` on a REAL client (it uses `FakeAgentClient`), and this class has no dedicated unit test file (confirmed by grep: no `test_agent_client.py`/`test_azure_agent_client.py` exists). Verified end-to-end in Task 14's integration test via `/ask`.

- [ ] **Step 1: Add timing + usage capture**

In `backend/src/fdi/platform/agent.py`, update imports and the `complete` method:

```python
import json
import time

from openai import AzureOpenAI

from fdi.core.agent import AgentResponse, ToolCall
from fdi.core.pricing import LlmCallUsage
from fdi.platform.config import Settings
```

```python
    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> AgentResponse:
        start = time.perf_counter()
        # The generated overloads declare `messages`/`tools` against the SDK's
        # own closed TypedDict param shapes, not the generic
        # list[dict[str, object]] this seam's Protocol (fdi.pipeline.agent.
        # AgentClient) uses so pipeline code never imports an openai type --
        # the same "SDK stub is narrower than what the wire protocol actually
        # accepts" mismatch fdi.platform.ocr.py's begin_analyze_document call
        # already carries a type: ignore[call-overload] for.
        resp = self._sdk.chat.completions.create(  # type: ignore[call-overload]
            model=self._deployment, messages=messages, tools=tools,
            tool_choice="auto", temperature=0,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        usage_obj = resp.usage
        usage = LlmCallUsage(
            model=self._model,
            input_tokens=usage_obj.prompt_tokens if usage_obj else 0,
            output_tokens=usage_obj.completion_tokens if usage_obj else 0,
            duration_ms=duration_ms,
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
            return AgentResponse(tool_calls=calls, usage=usage)
        return AgentResponse(content=msg.content or "", usage=usage)
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run: `cd backend && uv run python -c "import fdi.platform.agent"`
Expected: no error (requires `FDI_*` required env vars to be set for `Settings` construction only if the module does anything at import time beyond definitions — it does not, so this succeeds regardless of environment).

- [ ] **Step 3: Commit**

```bash
git add backend/src/fdi/platform/agent.py
git commit -m "feat(costing): AzureOpenAIAgentClient captures per-call token usage"
```

---

### Task 7: `UsageAccumulator` — the per-stage-execution LLM usage collector

**Files:**
- Create: `backend/src/fdi/pipeline/usage.py`
- Test: `backend/tests/unit/test_usage_accumulator.py`

**Interfaces:**
- Consumes: `LlmCallUsage` from `fdi.core.pricing` (Task 2).
- Produces: `RecordedLlmCall(source: str, usage: LlmCallUsage)`, `UsageAccumulator` with `.llm_calls: list[RecordedLlmCall]` and `.record_llm(source: str, usage: LlmCallUsage) -> None`. Task 12 (`run_stage`) constructs one per invocation and injects it into `StageContext.services["usage"]`; Task 13 (`SemanticStage.run`) calls `.record_llm(...)` on it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_usage_accumulator.py`:

```python
from fdi.core.pricing import LlmCallUsage
from fdi.pipeline.usage import UsageAccumulator


def test_accumulator_starts_empty() -> None:
    acc = UsageAccumulator()
    assert acc.llm_calls == []


def test_accumulator_records_calls_in_order() -> None:
    acc = UsageAccumulator()
    u1 = LlmCallUsage(model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=100)
    u2 = LlmCallUsage(model="gpt-4o", input_tokens=20, output_tokens=8, duration_ms=150)
    acc.record_llm("semantic_classify", u1)
    acc.record_llm("semantic_classify", u2)
    assert [c.usage for c in acc.llm_calls] == [u1, u2]
    assert all(c.source == "semantic_classify" for c in acc.llm_calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_usage_accumulator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fdi.pipeline.usage'`

- [ ] **Step 3: Write the implementation**

Create `backend/src/fdi/pipeline/usage.py`:

```python
"""The per-stage-execution LLM usage collector (costing feature, docs/
superpowers/specs/2026-07-28-costing-feature-design.md sec 3.3).

A stage's `run()` only ever returns its artifact body -- widening that
return contract to also carry cost data would break the "framework owns
all I/O and persistence" principle every Stage already follows (see
fdi.pipeline.stage's own docstring). Instead, `run_stage` constructs ONE
UsageAccumulator per invocation, injects it into StageContext.services
under the key "usage", and a stage implementation that makes real LLM
calls (currently only SemanticStage) reports each one to it as it happens
-- purely in-memory, no I/O, safe to call from inside a stage's near-pure
`run()`. `run_stage` reads the accumulator back AFTER `stage.run()`
returns (on success OR failure) and persists one llm_usage_event row per
entry, attributing them to that execution's stage_run_id."""

from dataclasses import dataclass, field

from fdi.core.pricing import LlmCallUsage


@dataclass(frozen=True)
class RecordedLlmCall:
    source: str
    usage: LlmCallUsage


@dataclass
class UsageAccumulator:
    llm_calls: list[RecordedLlmCall] = field(default_factory=list)

    def record_llm(self, source: str, usage: LlmCallUsage) -> None:
        self.llm_calls.append(RecordedLlmCall(source=source, usage=usage))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/unit/test_usage_accumulator.py -v`
Expected: PASS, 2 tests green.

- [ ] **Step 5: Commit**

```bash
git add backend/src/fdi/pipeline/usage.py backend/tests/unit/test_usage_accumulator.py
git commit -m "feat(costing): add per-stage-execution LLM usage accumulator"
```

---

### Task 8: DB models + migration 0008

**Files:**
- Modify: `backend/src/fdi/platform/db/models.py` (add `OcrUsageEvent`, `LlmUsageEvent`)
- Create: `backend/alembic/versions/0008_usage_events.py`

**Interfaces:**
- Produces: ORM classes `OcrUsageEvent`, `LlmUsageEvent` (tables `ocr_usage_event`, `llm_usage_event`). Task 9 (repository methods) constructs and queries these directly.

- [ ] **Step 1: Add the two ORM classes**

In `backend/src/fdi/platform/db/models.py`, add after `EvalCaseResult` (end of file, after line 113):

```python


class OcrUsageEvent(Base):
    """One real (cache-miss) Azure Document Intelligence `analyze()` call.
    Always doc- and stage_run-scoped: OCR only ever runs inside `run_stage`
    for one document. Costing feature (docs/superpowers/specs/2026-07-28-
    costing-feature-design.md); INR cost is computed at read time from
    `page_count`, never stored here."""

    __tablename__ = "ocr_usage_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("document.id"), index=True, nullable=False)
    stage_run_id: Mapped[str] = mapped_column(
        ForeignKey("stage_run.id"), index=True, nullable=False
    )
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_count: Mapped[int] = mapped_column(nullable=False)
    azure_model: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LlmUsageEvent(Base):
    """One real Azure OpenAI chat.completions.create() call, across all
    three call sites (semantic classification, schema-driven extraction,
    the /ask agent loop). `doc_id`/`stage_run_id` are nullable: `/ask` is
    corpus-wide (doc_id NULL), and both `/ask` and `/extract/document` run
    OUTSIDE run_stage entirely (stage_run_id NULL) -- only the `semantic`
    stage's calls carry a real stage_run_id. Costing feature (docs/
    superpowers/specs/2026-07-28-costing-feature-design.md); INR cost is
    computed at read time from input_tokens/output_tokens, never stored
    here."""

    __tablename__ = "llm_usage_event"
    __table_args__ = (
        CheckConstraint(
            "source IN ('semantic_classify','extract_document','ask_agent')",
            name="ck_llm_usage_event_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str | None] = mapped_column(
        ForeignKey("document.id"), index=True, nullable=True
    )
    stage_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage_run.id"), index=True, nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=False)
    output_tokens: Mapped[int] = mapped_column(nullable=False)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: Write the migration**

Create `backend/alembic/versions/0008_usage_events.py`:

```python
"""usage events

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_usage_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("doc_id", sa.String(36), sa.ForeignKey("document.id"), nullable=False),
        sa.Column(
            "stage_run_id", sa.String(36), sa.ForeignKey("stage_run.id"), nullable=False
        ),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("azure_model", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ocr_usage_event_doc_id", "ocr_usage_event", ["doc_id"])
    op.create_index(
        "ix_ocr_usage_event_stage_run_id", "ocr_usage_event", ["stage_run_id"]
    )

    op.create_table(
        "llm_usage_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("doc_id", sa.String(36), sa.ForeignKey("document.id"), nullable=True),
        sa.Column(
            "stage_run_id", sa.String(36), sa.ForeignKey("stage_run.id"), nullable=True
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # The ONLY thing enforcing the source domain -- see the stage_run
        # status constraint in 0002 for the same discipline.
        sa.CheckConstraint(
            "source IN ('semantic_classify','extract_document','ask_agent')",
            name="ck_llm_usage_event_source",
        ),
    )
    op.create_index("ix_llm_usage_event_doc_id", "llm_usage_event", ["doc_id"])
    op.create_index(
        "ix_llm_usage_event_stage_run_id", "llm_usage_event", ["stage_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_event_stage_run_id", table_name="llm_usage_event")
    op.drop_index("ix_llm_usage_event_doc_id", table_name="llm_usage_event")
    op.drop_table("llm_usage_event")
    op.drop_index("ix_ocr_usage_event_stage_run_id", table_name="ocr_usage_event")
    op.drop_index("ix_ocr_usage_event_doc_id", table_name="ocr_usage_event")
    op.drop_table("ocr_usage_event")
```

- [ ] **Step 3: Run the migration against the dev stack and verify**

Run (from `docker/`, stack must be up per the earlier session in this conversation): `sg docker -c "docker compose run --rm migrate"` (or `docker compose up -d --build migrate` then check its logs).
Expected: migration `0008` applies cleanly with no errors; `docker compose exec postgres psql -U fdi -d fdi -c '\d ocr_usage_event'` and `'\d llm_usage_event'` show the expected columns/constraints/indexes.

- [ ] **Step 4: Verify downgrade works**

Run: `sg docker -c "docker compose exec api alembic downgrade 0007"` then `sg docker -c "docker compose exec api alembic upgrade head"`.
Expected: both commands succeed with no errors; tables are dropped then recreated cleanly.

- [ ] **Step 5: Commit**

```bash
git add backend/src/fdi/platform/db/models.py backend/alembic/versions/0008_usage_events.py
git commit -m "feat(costing): add ocr_usage_event and llm_usage_event tables"
```

---

### Task 9: Repository — write methods (`record_ocr_usage`, `record_llm_usage`)

**Files:**
- Modify: `backend/src/fdi/platform/db/repository.py`
- Test: `backend/tests/integration/test_usage_repository.py`

**Interfaces:**
- Consumes: `OcrUsageEvent`, `LlmUsageEvent` (Task 8).
- Produces: `RegistryRepository.record_ocr_usage(id, *, doc_id, stage_run_id, correlation_id, page_count, azure_model, duration_ms) -> None`; `RegistryRepository.record_llm_usage(id, *, doc_id, stage_run_id, source, correlation_id, model, input_tokens, output_tokens, duration_ms) -> None`. Tasks 12, 13, 14, 15 all call these.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/integration/test_usage_repository.py`:

```python
"""RegistryRepository's OCR/LLM usage-event read/write methods, against
real Postgres. Costing feature."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


async def _make_document_and_stage_run(session_factory) -> tuple[str, str]:
    doc_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.create_document(doc_id)
        await repo.start_run(run_id, doc_id, "ocr", 5, "hash1", "corr1")
        await s.commit()
    return doc_id, run_id


async def test_record_ocr_usage_persists_a_row(session_factory) -> None:
    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_ocr_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            correlation_id="corr1", page_count=12,
            azure_model="prebuilt-layout", duration_ms=2500,
        )
        await s.commit()
    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc_id)
    ocr_row = next(r for r in rows if r["source"] == "ocr")
    assert ocr_row["page_count"] == 12
    assert ocr_row["call_count"] == 1
    assert ocr_row["duration_ms"] == 2500


async def test_record_llm_usage_persists_a_row(session_factory) -> None:
    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="semantic_classify", correlation_id="corr1",
            model="gpt-4o", input_tokens=100, output_tokens=20, duration_ms=800,
        )
        await s.commit()
    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc_id)
    llm_row = next(r for r in rows if r["source"] == "semantic_classify")
    assert llm_row["input_tokens"] == 100
    assert llm_row["output_tokens"] == 20
    assert llm_row["call_count"] == 1


async def test_llm_usage_with_no_doc_or_stage_run_is_allowed(session_factory) -> None:
    """/ask's untethered rows: doc_id=None, stage_run_id=None."""
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=None, stage_run_id=None,
            source="ask_agent", correlation_id="corr2",
            model="gpt-4o", input_tokens=50, output_tokens=10, duration_ms=400,
        )
        await s.commit()
    async with session_factory() as s:
        totals = await RegistryRepository(s).corpus_cost_totals()
    assert totals["llm_input_tokens"] >= 50
    assert totals["llm_output_tokens"] >= 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_usage_repository.py -v`
Expected: FAIL with `AttributeError: 'RegistryRepository' object has no attribute 'record_ocr_usage'`

- [ ] **Step 3: Add the write methods**

In `backend/src/fdi/platform/db/repository.py`, add to the imports:

```python
from fdi.platform.db.models import Document, EvalCaseResult, EvalRun, LlmUsageEvent, OcrUsageEvent, StageRun
```

Add two new methods to `RegistryRepository`, after `fail_run` (after line 121, before `runs_for_document`):

```python
    async def record_ocr_usage(
        self,
        id: str,
        *,
        doc_id: str,
        stage_run_id: str,
        correlation_id: str | None,
        page_count: int,
        azure_model: str,
        duration_ms: int,
    ) -> None:
        self._s.add(
            OcrUsageEvent(
                id=id, doc_id=doc_id, stage_run_id=stage_run_id,
                correlation_id=correlation_id, page_count=page_count,
                azure_model=azure_model, duration_ms=duration_ms,
            )
        )
        await self._s.flush()

    async def record_llm_usage(
        self,
        id: str,
        *,
        doc_id: str | None,
        stage_run_id: str | None,
        source: str,
        correlation_id: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
    ) -> None:
        self._s.add(
            LlmUsageEvent(
                id=id, doc_id=doc_id, stage_run_id=stage_run_id, source=source,
                correlation_id=correlation_id, model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                duration_ms=duration_ms,
            )
        )
        await self._s.flush()
```

(This step depends on Task 10's `document_cost_by_source`/`corpus_cost_totals` existing for the test to pass — implement both tasks' repository changes together in one edit pass to this file, then run the test once at the end of Task 10. Do not attempt to run this task's test standalone until Task 10 is also done.)

- [ ] **Step 4: Commit**

```bash
git add backend/src/fdi/platform/db/repository.py backend/tests/integration/test_usage_repository.py
git commit -m "feat(costing): add RegistryRepository usage-event write methods"
```

---

### Task 10: Repository — read/aggregation methods

**Files:**
- Modify: `backend/src/fdi/platform/db/repository.py`

**Interfaces:**
- Produces: `CostBySource` (TypedDict: `source, call_count, page_count, input_tokens, output_tokens, duration_ms`), `RegistryRepository.document_cost_by_source(doc_id: str) -> list[CostBySource]`; `CorpusCostTotals` (TypedDict: `ocr_page_count, llm_input_tokens, llm_output_tokens`), `RegistryRepository.corpus_cost_totals() -> CorpusCostTotals`, `RegistryRepository.stage_cost_totals() -> dict[str, CorpusCostTotals]`. Task 16 (API routes) consumes all three.

- [ ] **Step 1: Add the TypedDicts and read methods**

In `backend/src/fdi/platform/db/repository.py`, add the two new TypedDicts after `StageThroughput` (after line 43, before `_latest_stage_runs`):

```python
class CostBySource(TypedDict):
    """One usage source's raw totals for one document (costing feature).
    `source` is "ocr" or one of the llm_usage_event.source values.
    page_count is 0 for every LLM source; input_tokens/output_tokens are 0
    for "ocr". INR conversion happens at the API layer via fdi.core.pricing
    -- this stays in raw units so a pricing change recomputes without
    touching this query."""

    source: str
    call_count: int
    page_count: int
    input_tokens: int
    output_tokens: int
    duration_ms: int


class CorpusCostTotals(TypedDict):
    """Raw usage totals, corpus-wide or per-stage (costing feature). Every
    real billable event counts -- unlike stage_throughput/document_outcome_
    counts this is NOT latest-per-stage: a document reprocessed for real
    (checksum unchanged, stage version bumped) contributes both charges,
    matching actual money spent."""

    ocr_page_count: int
    llm_input_tokens: int
    llm_output_tokens: int
```

Add three new methods to `RegistryRepository`, after `document_outcome_counts` (end of that class, before `class EvalRepository`):

```python
    async def document_cost_by_source(self, doc_id: str) -> list[CostBySource]:
        """Every source of billable usage for one document: "ocr" (if any
        ocr_usage_event rows exist) plus each distinct llm_usage_event
        `source` that has at least one row for this doc_id."""
        rows: list[CostBySource] = []
        ocr_row = (
            await self._s.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(OcrUsageEvent.page_count), 0),
                    func.coalesce(func.sum(OcrUsageEvent.duration_ms), 0),
                ).where(OcrUsageEvent.doc_id == doc_id)
            )
        ).one()
        if ocr_row[0]:
            rows.append(
                CostBySource(
                    source="ocr", call_count=int(ocr_row[0]),
                    page_count=int(ocr_row[1]), input_tokens=0, output_tokens=0,
                    duration_ms=int(ocr_row[2]),
                )
            )
        llm_rows = (
            await self._s.execute(
                select(
                    LlmUsageEvent.source,
                    func.count(),
                    func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.duration_ms), 0),
                )
                .where(LlmUsageEvent.doc_id == doc_id)
                .group_by(LlmUsageEvent.source)
                .order_by(LlmUsageEvent.source)
            )
        ).all()
        rows.extend(
            CostBySource(
                source=str(source), call_count=int(count), page_count=0,
                input_tokens=int(in_tok), output_tokens=int(out_tok),
                duration_ms=int(dur),
            )
            for source, count, in_tok, out_tok, dur in llm_rows
        )
        return rows

    async def corpus_cost_totals(self) -> CorpusCostTotals:
        """Every billable event corpus-wide, including /ask's doc_id=NULL
        rows -- the "grand total" figure for GET /analytics."""
        ocr_total = (
            await self._s.execute(
                select(func.coalesce(func.sum(OcrUsageEvent.page_count), 0))
            )
        ).scalar_one()
        llm_totals = (
            await self._s.execute(
                select(
                    func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
                )
            )
        ).one()
        return CorpusCostTotals(
            ocr_page_count=int(ocr_total),
            llm_input_tokens=int(llm_totals[0]),
            llm_output_tokens=int(llm_totals[1]),
        )

    async def stage_cost_totals(self) -> dict[str, CorpusCostTotals]:
        """{stage_name: totals} for the two stages that ever cost anything:
        "ocr" (page charges) and "semantic" (its LLM calls, source=
        'semantic_classify'). /ask and extract_document have no
        stage_run_id and so no stage entry here -- they only ever appear in
        corpus_cost_totals. A stage with zero billable events is simply
        absent from the returned dict."""
        result: dict[str, CorpusCostTotals] = {}
        ocr_total = (
            await self._s.execute(
                select(func.coalesce(func.sum(OcrUsageEvent.page_count), 0))
            )
        ).scalar_one()
        if ocr_total:
            result["ocr"] = CorpusCostTotals(
                ocr_page_count=int(ocr_total), llm_input_tokens=0, llm_output_tokens=0
            )
        semantic_totals = (
            await self._s.execute(
                select(
                    func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
                ).where(LlmUsageEvent.source == "semantic_classify")
            )
        ).one()
        if semantic_totals[0] or semantic_totals[1]:
            result["semantic"] = CorpusCostTotals(
                ocr_page_count=0, llm_input_tokens=int(semantic_totals[0]),
                llm_output_tokens=int(semantic_totals[1]),
            )
        return result
```

- [ ] **Step 2: Run Task 9's + this task's test together**

Run: `cd backend && uv run pytest tests/integration/test_usage_repository.py -v`
Expected: PASS, all 3 tests green (requires the Docker stack's Postgres to be reachable — `docker compose up -d postgres` from `docker/` if not already running, and `DATABASE_URL`/testcontainers config per the existing integration test setup in `backend/tests/integration/conftest.py`).

- [ ] **Step 3: Run static gates**

Run: `cd backend && uv run mypy --strict src && uv run ruff check .`
Expected: PASS, no new errors.

- [ ] **Step 4: Commit**

```bash
git add backend/src/fdi/platform/db/repository.py
git commit -m "feat(costing): add RegistryRepository usage-event aggregation queries"
```

---

### Task 11: `run_stage` — OCR generic timing + page-count billing

**Files:**
- Modify: `backend/src/fdi/pipeline/run.py`
- Modify: `backend/tests/integration/test_ocr_stage.py`

**Interfaces:**
- Consumes: `RegistryRepository.record_ocr_usage` (Task 9).
- Produces: one `ocr_usage_event` row per real (cache-miss) `ocr` stage execution, `duration_ms` measured around the whole `stage.run()` call, `page_count` derived from `len(body["pages"])`.

- [ ] **Step 1: Write the failing test**

In `backend/tests/integration/test_ocr_stage.py`, add imports and a new test function after the existing `test_ocr_stage_runs_and_caches`:

```python
from fdi.platform.db.repository import RegistryRepository


async def test_ocr_stage_records_one_usage_event_per_real_call(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=b"%PDF-1.7\n%fake-pdf-body-2\n",
        filename="b.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    fake = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [
                {"pageNumber": 1, "lines": [], "words": []},
                {"pageNumber": 2, "lines": [], "words": []},
                {"pageNumber": 3, "lines": [], "words": []},
            ],
            "paragraphs": [],
            "tables": [],
        }
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id, "stage_name": "ocr", "correlation_id": "c1",
        "session_factory": session_factory, "store": store,
        "registry": reg, "resolver": resolver, "services": {"ocr": fake},
    }
    await run_stage(run_id=str(uuid.uuid4()), **common)
    await run_stage(run_id=str(uuid.uuid4()), **common)  # cache hit -- must NOT bill again

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
    ocr_row = next(r for r in rows if r["source"] == "ocr")
    assert ocr_row["call_count"] == 1, "cache hit must not create a second usage event"
    assert ocr_row["page_count"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_ocr_stage.py::test_ocr_stage_records_one_usage_event_per_real_call -v`
Expected: FAIL with `StopIteration` (no `"ocr"` row exists in `document_cost_by_source` yet).

- [ ] **Step 3: Instrument `run_stage`**

In `backend/src/fdi/pipeline/run.py`, add `import time` and `import uuid` at the top, and change the cache-miss branch (currently lines 82-99):

```python
import time
import uuid
from collections.abc import Mapping
```

```python
    # 3. cache miss: run, validate, store, record.
    start = time.perf_counter()
    try:
        body = await stage.run(
            StageContext(
                doc_id=doc_id, read_artifact=read_artifact,
                read_source=read_source, services=svcs,
            )
        )
        validate_finite(body)
        await store.put_json(key, body)
    except Exception as exc:
        async with session_factory() as s:
            await RegistryRepository(s).fail_run(run_id, repr(exc))
            await s.commit()
        raise
    duration_ms = int((time.perf_counter() - start) * 1000)
    # OCR bills 1 real Azure DI call per stage execution -- page count is
    # already present in the artifact body (fdi.core.ocr_schema.normalize_
    # ocr's "pages" list), so no change to AzureOcrClient/OcrStage is
    # needed. Costing feature (docs/superpowers/specs/2026-07-28-costing-
    # feature-design.md sec 3.3).
    if stage_name == "ocr" and isinstance(body, Mapping):
        pages = body.get("pages")
        page_count = len(pages) if isinstance(pages, list) else 0
        async with session_factory() as s:
            await RegistryRepository(s).record_ocr_usage(
                str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
                correlation_id=correlation_id, page_count=page_count,
                azure_model=get_settings().azure_di_model, duration_ms=duration_ms,
            )
            await s.commit()
    async with session_factory() as s:
        await RegistryRepository(s).finish_run(run_id, output_hash(body), key)
        await s.commit()
    return resolver.successors(stage_name)
```

Add the `get_settings` import to the top of the file:

```python
from fdi.platform.config import get_settings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/integration/test_ocr_stage.py -v`
Expected: PASS, both tests in the file green (the pre-existing cache-hit test and the new usage-event test).

- [ ] **Step 5: Commit**

```bash
git add backend/src/fdi/pipeline/run.py backend/tests/integration/test_ocr_stage.py
git commit -m "feat(costing): run_stage records one ocr_usage_event per real OCR call"
```

---

### Task 12: `run_stage` — wire `UsageAccumulator` for LLM stages

**Files:**
- Modify: `backend/src/fdi/pipeline/run.py`

**Interfaces:**
- Consumes: `UsageAccumulator` (Task 7), `RegistryRepository.record_llm_usage` (Task 9).
- Produces: `StageContext.services["usage"]` is now ALWAYS populated with a fresh `UsageAccumulator` for every `run_stage` invocation; any `RecordedLlmCall`s a stage appended to it are persisted as `llm_usage_event` rows (keyed to that execution's `run_id`) whether the stage ultimately succeeds or raises. Task 13 (`SemanticStage.run`) is the first and only current consumer of `ctx.services["usage"]`.

- [ ] **Step 1: Add the accumulator injection and flush logic**

In `backend/src/fdi/pipeline/run.py`, add the import:

```python
from fdi.pipeline.usage import UsageAccumulator
```

Change the top of `run_stage` (currently lines 30-31) and the cache-miss `try`/`except` block from Task 11 to also construct and flush the accumulator. The function becomes (showing the parts that change; everything else from Task 11 stays as written):

```python
async def run_stage(
    *, doc_id: str, stage_name: str, run_id: str, correlation_id: str | None,
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore,
    registry: StageRegistry, resolver: DagResolver,
    services: Mapping[str, object] | None = None,
) -> list[str]:
    stage = registry.get(stage_name)
    usage = UsageAccumulator()
    svcs: dict[str, object] = {**(services or {}), "usage": usage}
```

(The remainder of the function through the cache-hit branch is unchanged from the existing code and from Task 11.)

Change the cache-miss `except` clause to ALSO flush any usage recorded before the failure (spec decision 4/9: a partial loop that made real calls before erroring still billed for those calls), and change the post-success block to flush too:

```python
    # 3. cache miss: run, validate, store, record.
    start = time.perf_counter()
    try:
        body = await stage.run(
            StageContext(
                doc_id=doc_id, read_artifact=read_artifact,
                read_source=read_source, services=svcs,
            )
        )
        validate_finite(body)
        await store.put_json(key, body)
    except Exception as exc:
        await _flush_llm_usage(
            usage, session_factory=session_factory, doc_id=doc_id,
            run_id=run_id, correlation_id=correlation_id,
        )
        async with session_factory() as s:
            await RegistryRepository(s).fail_run(run_id, repr(exc))
            await s.commit()
        raise
    duration_ms = int((time.perf_counter() - start) * 1000)
    if stage_name == "ocr" and isinstance(body, Mapping):
        pages = body.get("pages")
        page_count = len(pages) if isinstance(pages, list) else 0
        async with session_factory() as s:
            await RegistryRepository(s).record_ocr_usage(
                str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
                correlation_id=correlation_id, page_count=page_count,
                azure_model=get_settings().azure_di_model, duration_ms=duration_ms,
            )
            await s.commit()
    await _flush_llm_usage(
        usage, session_factory=session_factory, doc_id=doc_id,
        run_id=run_id, correlation_id=correlation_id,
    )
    async with session_factory() as s:
        await RegistryRepository(s).finish_run(run_id, output_hash(body), key)
        await s.commit()
    return resolver.successors(stage_name)


async def _flush_llm_usage(
    usage: UsageAccumulator,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    doc_id: str,
    run_id: str,
    correlation_id: str | None,
) -> None:
    """Persist every LLM call a stage reported to its UsageAccumulator, as
    one llm_usage_event row each, attributed to this stage execution's
    run_id. Called on BOTH the success and the failure path -- a stage
    that made 3 real calls before erroring on a 4th still billed for those
    3 (spec decision 4/9)."""
    if not usage.llm_calls:
        return
    async with session_factory() as s:
        repo = RegistryRepository(s)
        for call in usage.llm_calls:
            await repo.record_llm_usage(
                str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
                source=call.source, correlation_id=correlation_id,
                model=call.usage.model, input_tokens=call.usage.input_tokens,
                output_tokens=call.usage.output_tokens,
                duration_ms=call.usage.duration_ms,
            )
        await s.commit()
```

- [ ] **Step 2: Run the existing OCR + semantic integration tests to confirm no regression**

Run: `cd backend && uv run pytest tests/integration/test_ocr_stage.py -v`
Expected: PASS (unaffected by this change — OCR path is unchanged from Task 11, `usage.llm_calls` is always empty for `OcrStage`, so `_flush_llm_usage` is a no-op).

`test_semantic_stage.py` will still fail at this point (Task 13 has not yet updated `SemanticStage.run` to unpack `ClassifyResult`) — this is expected, not a regression from this task.

- [ ] **Step 3: Commit**

```bash
git add backend/src/fdi/pipeline/run.py
git commit -m "feat(costing): run_stage injects and flushes a per-execution LLM usage accumulator"
```

---

### Task 13: `SemanticStage.run` records usage per AST section

**Files:**
- Modify: `backend/src/fdi/pipeline/stages/semantic.py`
- Modify: `backend/tests/integration/test_semantic_stage.py`

**Interfaces:**
- Consumes: `ClassifyResult` (Task 4), `UsageAccumulator` via `ctx.services["usage"]` (Task 12).
- Produces: one `RecordedLlmCall(source="semantic_classify", ...)` appended per AST section processed; `run_stage` (Task 12) persists them as `llm_usage_event` rows.

- [ ] **Step 1: Write the failing test**

In `backend/tests/integration/test_semantic_stage.py`, add an import and a new test after `test_semantic_stage_runs_and_caches`:

```python
from fdi.platform.db.repository import RegistryRepository


async def test_semantic_stage_records_one_llm_usage_event_per_section(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="c.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Assets 100"},
                {"role": "sectionHeading", "content": "Income Statement"},
                {"content": "Revenue 50"},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient({
        "Balance Sheet": SectionType.BALANCE_SHEET.value,
        "Income Statement": SectionType.INCOME_STATEMENT.value,
    })

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()),
                     services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="semantic", run_id=str(uuid.uuid4()),
                     services={"semantic": fake_semantic}, **common)

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
    semantic_row = next(r for r in rows if r["source"] == "semantic_classify")
    assert semantic_row["call_count"] == 2  # two sections classified
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_semantic_stage.py -v`
Expected: FAIL — either an `AttributeError` inside `SemanticStage.run` (it still does `label = client.classify(...)` expecting a bare string) or a `StopIteration` on `semantic_row` (no usage events recorded yet). Both existing and new tests in this file fail until Step 3.

- [ ] **Step 3: Update `SemanticStage.run`**

In `backend/src/fdi/pipeline/stages/semantic.py`, replace the full file:

```python
"""SemanticStage: a DERIVED stage that classifies each AST section into a
SectionType via an injected SemanticClient. Auto-chains off AstStage via
`deps=("ast",)`.

Content-addressed on `derived_input_hash([ast.output_hash])` +
`SemanticStage.version` -- deliberate, since the LLM is non-deterministic: a
second run against the same `ast` artifact is a store cache hit that does NOT
call the client again (proven in tests/integration/test_semantic_stage.py with
a call-counting fake). Each real classify() call reports its LlmCallUsage to
ctx.services["usage"] (costing feature, docs/superpowers/specs/2026-07-28-
costing-feature-design.md) -- run_stage persists these as llm_usage_event
rows after this stage returns, so a cache hit (this method never runs) does
not bill again either."""

from collections.abc import Mapping
from typing import cast

from fdi.core.semantic import SectionType, build_snippet
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.semantic import SemanticClient
from fdi.pipeline.stage import StageContext
from fdi.pipeline.usage import UsageAccumulator


class SemanticStage:
    name = "semantic"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        client = cast(SemanticClient, ctx.services["semantic"])
        usage = cast(UsageAccumulator, ctx.services["usage"])

        sections_out: list[dict[str, object]] = []
        sections_raw = ast.get("sections")
        if isinstance(sections_raw, list):
            for section in sections_raw:
                if not isinstance(section, dict):
                    continue
                heading_raw = section.get("heading")
                heading = heading_raw if isinstance(heading_raw, str) else ""
                paragraphs_raw = section.get("paragraphs")
                paragraphs = (
                    [p for p in paragraphs_raw if isinstance(p, str)]
                    if isinstance(paragraphs_raw, list)
                    else []
                )
                snippet = build_snippet(paragraphs)
                result = client.classify(heading, snippet)
                if result.usage is not None:
                    usage.record_llm("semantic_classify", result.usage)
                section_type = SectionType(result.label)
                sections_out.append(
                    {
                        "heading": heading_raw,
                        "type": section_type.value,
                        "paragraph_count": len(paragraphs),
                    }
                )

        return {"schema_version": 1, "sections": sections_out}


REGISTRY.register(SemanticStage())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/integration/test_semantic_stage.py -v`
Expected: PASS, both the pre-existing cache-hit test and the new usage-event test green.

- [ ] **Step 5: Run the broader integration suite for anything depending on `SemanticStage`**

Run: `cd backend && uv run pytest -m integration -k "semantic or index or knowledge or graphquery or resolve_entities or entity_timeline or chunks or hybrid_search or vector_search or ask or extract_document or agent_tools" -v`
Expected: PASS across the board — all of these tests construct `FakeSemanticClient(...)` and pass it through `services={"semantic": ...}`, never calling `.classify()` themselves, so they are unaffected by the internal label/usage unpacking change in `SemanticStage.run`.

- [ ] **Step 6: Prove a mid-loop failure still bills the calls made before it (spec decision 4/9)**

This is the one behavior Task 12's `_flush_llm_usage` was built for and needs its own direct proof: a stage that makes 2 real, successfully-billed calls and then raises on its 3rd must still leave 2 `llm_usage_event` rows behind, with the `StageRun` itself ending up `failed`. `FakeSemanticClient` always returns a label from its dict and never raises, so this needs a small local test double. Add to `backend/tests/integration/test_semantic_stage.py`:

```python
from fdi.core.semantic import ClassifyResult
from fdi.core.pricing import LlmCallUsage


class _FlakySemanticClient:
    """Classifies the first `succeed_count` calls for real (with usage),
    then raises on the next one. Proves partial-failure billing."""

    def __init__(self, succeed_count: int) -> None:
        self._succeed_count = succeed_count
        self.calls = 0

    def classify(self, heading: str, text: str) -> ClassifyResult:
        self.calls += 1
        if self.calls > self._succeed_count:
            raise RuntimeError("simulated Azure OpenAI failure")
        return ClassifyResult(
            label=SectionType.OTHER.value,
            usage=LlmCallUsage(
                model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=50
            ),
        )


async def test_semantic_stage_bills_calls_made_before_a_mid_loop_failure(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="d.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "A"},
                {"content": "text a"},
                {"role": "sectionHeading", "content": "B"},
                {"content": "text b"},
                {"role": "sectionHeading", "content": "C"},
                {"content": "text c"},
            ],
            "tables": [],
        }
    )
    flaky = _FlakySemanticClient(succeed_count=2)

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()),
                     services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)

    with pytest.raises(RuntimeError, match="simulated Azure OpenAI failure"):
        await run_stage(stage_name="semantic", run_id=str(uuid.uuid4()),
                         services={"semantic": flaky}, **common)

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
        facts = await RegistryRepository(s).latest_runs(doc.id)
    semantic_row = next(r for r in rows if r["source"] == "semantic_classify")
    assert semantic_row["call_count"] == 2, "the 2 successful calls before the raise must bill"
    assert facts["semantic"].status == "failed"
```

Run: `cd backend && uv run pytest tests/integration/test_semantic_stage.py::test_semantic_stage_bills_calls_made_before_a_mid_loop_failure -v`
Expected: PASS. If it fails with 0 rows recorded, the bug is in Task 12's `_flush_llm_usage` not being called from the `except` branch, or being called with an accumulator that isn't populated until the exception propagates up from `SemanticStage.run` — verify the accumulator instance passed into `StageContext.services["usage"]` is the SAME one `run_stage` reads back in both its `try` success path and its `except` failure path (it must be constructed once, before the `try`, not inside it).

- [ ] **Step 7: Commit**

```bash
git add backend/src/fdi/pipeline/stages/semantic.py backend/tests/integration/test_semantic_stage.py
git commit -m "feat(costing): SemanticStage records one llm_usage_event per section classified"
```

---

### Task 14: `/ask` records usage per agent-loop round

**Files:**
- Modify: `backend/src/fdi/pipeline/agent_tools.py` (`AgentContext` gains `correlation_id`)
- Modify: `backend/src/fdi/pipeline/ask.py` (`answer_question` records usage)
- Modify: `backend/src/fdi/api/routes.py` (`/ask` passes `correlation_id`, commits the session)
- Test: `backend/tests/integration/test_ask.py`

**Interfaces:**
- Consumes: `RegistryRepository.record_llm_usage` (Task 9), `AgentResponse.usage` (Task 3).
- Produces: one `llm_usage_event` row (`source="ask_agent"`, `doc_id=NULL`, `stage_run_id=NULL`) per real `agent_client.complete(...)` call made during one `/ask` request.

- [ ] **Step 1: Write the failing test**

In `backend/tests/integration/test_ask.py`, add an import and a new test near the existing agent-loop tests:

```python
from fdi.platform.db.repository import RegistryRepository


async def test_ask_records_one_llm_usage_event_per_round(
    session_factory, store, ...  # match this file's existing fixture list exactly
) -> None:
    """Adapt the fixture setup from this file's existing test_ask_* tests
    (same doc/embedder/vector_store wiring) -- the only new assertions are
    the ones on llm_usage_event below. Script the FakeAgentClient with
    explicit usage this time (every other test in this file leaves usage
    unset, which is deliberately fine -- None means "not recorded")."""
    from fdi.core.agent import AgentResponse
    from fdi.core.pricing import LlmCallUsage

    usage_a = LlmCallUsage(model="gpt-4o", input_tokens=100, output_tokens=20, duration_ms=300)
    usage_b = LlmCallUsage(model="gpt-4o", input_tokens=150, output_tokens=40, duration_ms=250)
    fake = FakeAgentClient([
        AgentResponse(
            tool_calls=[ToolCall(id="c1", name="search", arguments={"query": "assets"})],
            usage=usage_a,
        ),
        AgentResponse(content="Total Assets were 100.", usage=usage_b),
    ])

    # ... call POST /ask via this file's existing test client / app fixture,
    # with dependency_overrides={get_agent_client: lambda: fake}, exactly as
    # this file's pre-existing tests already do.

    async with session_factory() as s:
        totals = await RegistryRepository(s).corpus_cost_totals()
    assert totals["llm_input_tokens"] >= 250  # 100 + 150
    assert totals["llm_output_tokens"] >= 60   # 20 + 40
```

(Because this file's exact HTTP-client/app-fixture plumbing for `/ask` was not captured verbatim in this plan's research pass, the implementer must copy the surrounding fixture/request boilerplate from this file's existing `test_ask_*` tests rather than inventing new plumbing — the only genuinely new lines are the `LlmCallUsage`/usage-carrying `AgentResponse` construction and the `corpus_cost_totals` assertion shown above.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_ask.py::test_ask_records_one_llm_usage_event_per_round -v`
Expected: FAIL — totals assertion fails (no usage recorded yet).

- [ ] **Step 3: Add `correlation_id` to `AgentContext`**

In `backend/src/fdi/pipeline/agent_tools.py`, change the `AgentContext` dataclass (lines 42-49):

```python
@dataclass(frozen=True)
class AgentContext:
    session: AsyncSession
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    graph_store: GraphStore | None
    default_entity_threshold: float = 0.85
    # Costing feature (docs/superpowers/specs/2026-07-28-costing-feature-
    # design.md): threaded through to answer_question's llm_usage_event
    # rows so every call in one /ask request is traceable to it. Optional,
    # appended last -- every existing keyword-only AgentContext(...)
    # construction site is unaffected.
    correlation_id: str | None = None
```

- [ ] **Step 4: Record usage in `answer_question`**

In `backend/src/fdi/pipeline/ask.py`, add imports and record usage after each `agent_client.complete(...)` call:

```python
import uuid

from fdi.platform.db.repository import RegistryRepository
```

Change the loop body (currently starting at line 40):

```python
    for round_num in range(max_rounds):
        resp = agent_client.complete(messages, TOOL_SCHEMAS)
        if resp.usage is not None:
            await RegistryRepository(ctx.session).record_llm_usage(
                str(uuid.uuid4()), doc_id=None, stage_run_id=None,
                source="ask_agent", correlation_id=ctx.correlation_id,
                model=resp.usage.model, input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                duration_ms=resp.usage.duration_ms,
            )
        if resp.tool_calls is None:
            return {
                "answer": resp.content or "",
                "citations": dedup_citations(raw_citations),
                "tool_trace": tool_trace,
            }
        ...  # rest of the loop body unchanged
```

- [ ] **Step 5: Wire `correlation_id` through the route and commit the session**

In `backend/src/fdi/api/routes.py`, change the `ask` route handler (lines 910-934):

```python
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
    correlation_id: CorrelationIdDep,
) -> AskResponse:
    if agent_client is None:
        raise HTTPException(
            status_code=503,
            detail="ask is not configured: missing agent_client (set FDI_GPT_ENDPOINT/FDI_GPT_KEY)",
        )
    ctx = AgentContext(
        session=session, store=store, embedder=embedder, vector_store=vector_store,
        graph_store=graph_store, default_entity_threshold=entity_threshold,
        correlation_id=correlation_id,
    )
    max_rounds = body.max_rounds if body.max_rounds is not None else 5
    result = await answer_question(
        question=body.question, ctx=ctx, agent_client=agent_client, max_rounds=max_rounds,
    )
    # answer_question may have written llm_usage_event rows via ctx.session
    # (costing feature) -- this route previously never wrote anything, so
    # it never needed to commit. RegistryRepository.record_llm_usage only
    # flushes; the caller owns the commit, same convention as every other
    # write path in this codebase.
    await session.commit()
    return AskResponse.model_validate(result)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/integration/test_ask.py -v`
Expected: PASS, all tests in the file green, including the new one.

- [ ] **Step 7: Commit**

```bash
git add backend/src/fdi/pipeline/agent_tools.py backend/src/fdi/pipeline/ask.py backend/src/fdi/api/routes.py backend/tests/integration/test_ask.py
git commit -m "feat(costing): /ask records llm_usage_event per real agent-loop call"
```

---

### Task 15: `/extract/document` records usage per unit call

**Files:**
- Modify: `backend/src/fdi/pipeline/extractor.py` (`ExtractContext` gains `correlation_id`; add `_record_llm_usage`; wire it into `extract_unit`/`extract_field` — completing Task 5's deferred wiring)
- Modify: `backend/src/fdi/api/routes.py` (`/extract/document` passes `correlation_id`, commits the session)
- Test: `backend/tests/integration/test_extract_document.py`

**Interfaces:**
- Consumes: `RegistryRepository.record_llm_usage` (Task 9), `ExtractResult.usage` (Task 5).
- Produces: one `llm_usage_event` row (`source="extract_document"`, `doc_id=<resolved doc>`, `stage_run_id=NULL`) per real `client.extract(...)` call made during one `/extract/document` request (one per Retrieval Unit that had a non-empty candidate pool).

- [ ] **Step 1: Write the failing test**

In `backend/tests/integration/test_extract_document.py`, add an import and a new test modeled on this file's existing `test_extract_document_*` fixture setup (copy the doc/schema/embedder/vector_store wiring from an existing passing test in this file rather than inventing new plumbing):

```python
from fdi.platform.db.repository import RegistryRepository


async def test_extract_document_records_llm_usage_events(
    session_factory, store, ...  # match this file's existing fixture list
) -> None:
    """Adapt this file's existing extract_document integration setup. The
    FakeExtractorClient's constructor is UNCHANGED (list[Mapping]) --
    .extract() now wraps each scripted result into ExtractResult(usage=
    None) internally, so no usage event is recorded for THIS fake by
    default (usage=None is the correct 'no real Azure call' signal). This
    test instead verifies the plumbing end-to-end against the repository
    directly by checking zero events post-run with the plain Fake, proving
    usage=None is correctly treated as 'nothing to record' rather than
    crashing."""
    # ... run extract_document via this file's existing helpers with a
    # FakeExtractorClient scripted as today ...
    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
    assert not any(r["source"] == "extract_document" for r in rows)
```

Additionally, add a focused unit-style check directly against `extract_unit` (not through the full route) that DOES exercise a real usage-carrying `ExtractResult`, since `FakeExtractorClient` never produces one by construction:

```python
from fdi.core.extractor import ExtractResult
from fdi.core.pricing import LlmCallUsage


class _UsageCarryingExtractorClient:
    """Local test double -- unlike FakeExtractorClient, returns a real
    usage on every call, to prove extract_unit records it."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def extract(self, *, system: str, user: str) -> ExtractResult:
        return ExtractResult(
            data=self._data,
            usage=LlmCallUsage(model="gpt-4o", input_tokens=80, output_tokens=15, duration_ms=200),
        )


async def test_extract_unit_records_usage_when_client_returns_it(
    session_factory, store, ...  # match this file's existing ExtractContext setup
) -> None:
    # ... build an ExtractContext (ctx) and a UnitSpec (unit) exactly as
    # this file's existing extract_unit-level tests already do ...
    client = _UsageCarryingExtractorClient({"value": "100", "confidence": 0.9})
    await extract_unit(ctx=ctx, unit=unit, client=client)
    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(ctx.doc_id)
    row = next(r for r in rows if r["source"] == "extract_document")
    assert row["input_tokens"] == 80
    assert row["output_tokens"] == 15
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_extract_document.py::test_extract_unit_records_usage_when_client_returns_it -v`
Expected: FAIL with `NameError: name '_record_llm_usage' is not defined` (matches Task 5 Step 3's deferred reference).

- [ ] **Step 3: Add `correlation_id` to `ExtractContext` and the `_record_llm_usage` helper**

In `backend/src/fdi/pipeline/extractor.py`, change the `ExtractContext` dataclass (lines 120-128):

```python
@dataclass
class ExtractContext:
    session: AsyncSession
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    doc_id: str
    doc_key: str
    version: int
    # Costing feature (docs/superpowers/specs/2026-07-28-costing-feature-
    # design.md): threaded through to _record_llm_usage's llm_usage_event
    # rows. Optional, appended last -- every existing keyword-only
    # ExtractContext(...) construction site is unaffected.
    correlation_id: str | None = None
```

Add the `_record_llm_usage` helper near the top of the module, after the `ExtractContext` dataclass and before `_POOL_K`/`_EXTRACT_TOP_K`:

```python
async def _record_llm_usage(
    *, ctx: ExtractContext, source: str, usage: LlmCallUsage
) -> None:
    """Persist one real ExtractorClient.extract() call's usage. stage_run_id
    is always None here -- extract_document runs outside run_stage entirely
    (it is a synchronous route, not a Celery-dispatched stage)."""
    await RegistryRepository(ctx.session).record_llm_usage(
        str(uuid.uuid4()), doc_id=ctx.doc_id, stage_run_id=None,
        source=source, correlation_id=ctx.correlation_id,
        model=usage.model, input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens, duration_ms=usage.duration_ms,
    )
```

Add `import uuid` and `from fdi.core.pricing import LlmCallUsage` to this module's existing import block (the `RegistryRepository` import already exists per the file's current contents — confirm it is imported at module scope, not just inside a function, since `_record_llm_usage` is a top-level function).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/integration/test_extract_document.py -v`
Expected: PASS, all tests in the file green (this also finally exercises Task 5 Step 3's `extract_unit`/`extract_field` changes end-to-end, since those referenced `_record_llm_usage` before it existed).

- [ ] **Step 5: Wire `correlation_id` through the route and commit the session**

In `backend/src/fdi/api/routes.py`, change the `extract_document_route` handler's signature and `ExtractContext` construction (lines 953-1017):

```python
@router.post("/extract/document", response_model=ExtractDocumentView)
async def extract_document_route(
    body: ExtractRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
    extractor_client: ExtractorClientDep,
    schema_path: ExtractionSchemaPathDep,
    correlation_id: CorrelationIdDep,
) -> ExtractDocumentView:
```

(keep the body through the `index_run` check and `schema, fields` resolution unchanged), then:

```python
    ctx = ExtractContext(
        session=session, store=store, embedder=embedder, vector_store=vector_store,
        doc_id=doc.id, doc_key=body.doc_key, version=version,
        correlation_id=correlation_id,
    )
    result = await extract_document(
        ctx=ctx, fields=fields, client=extractor_client, force=body.force
    )
    # extract_document may have written llm_usage_event rows via ctx.session
    # (costing feature) -- the extraction RECORD itself persists to the
    # artifact store (_persist_extraction), not Postgres, so this route
    # never needed to commit before. RegistryRepository.record_llm_usage
    # only flushes; the caller owns the commit.
    await session.commit()
    return ExtractDocumentView.model_validate(
        {"doc_key": body.doc_key, "module": body.module, **result}
    )
```

- [ ] **Step 6: Run the full extract_document + api integration tests**

Run: `cd backend && uv run pytest tests/integration/test_extract_document.py tests/integration/test_api.py -v -k "extract"`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add backend/src/fdi/pipeline/extractor.py backend/src/fdi/api/routes.py backend/tests/integration/test_extract_document.py
git commit -m "feat(costing): /extract/document records llm_usage_event per real unit call"
```

---

### Task 16: API — `GET /documents/{doc_id}/cost` + `GET /analytics` cost fields

**Files:**
- Modify: `backend/src/fdi/api/schemas.py`
- Modify: `backend/src/fdi/api/routes.py`
- Test: `backend/tests/integration/test_api.py`

**Interfaces:**
- Consumes: `RegistryRepository.document_cost_by_source`/`corpus_cost_totals`/`stage_cost_totals` (Task 10), `fdi.core.pricing.ocr_cost_inr`/`llm_cost_inr` (Task 2).
- Produces: `CostBySourceView`, `DocumentCostView`; `GET /documents/{doc_id}/cost -> DocumentCostView`; `AnalyticsView.total_cost_inr: float` (new field); `StageThroughputView.cost_inr: float` (new field).

- [ ] **Step 1: Write the failing test**

In `backend/tests/integration/test_api.py`, add a new test near the existing OCR/semantic-stage API tests (copy this file's existing app-fixture/client setup):

```python
async def test_document_cost_endpoint_returns_priced_breakdown(
    ...  # this file's standard client/session/store fixtures
) -> None:
    # Upload a document, run ocr with a FakeOcrClient producing 4 pages
    # (mirroring this file's existing OCR-stage API test setup), then:
    response = await client.get(f"/documents/{doc.id}/cost")
    assert response.status_code == 200
    body = response.json()
    assert body["doc_id"] == doc.id
    ocr_entry = next(r for r in body["by_source"] if r["source"] == "ocr")
    assert ocr_entry["page_count"] == 4
    assert ocr_entry["cost_inr"] == 4.0  # 4 pages * default FDI_OCR_COST_PER_PAGE_INR=1.0
    assert body["total_cost_inr"] == ocr_entry["cost_inr"]


async def test_document_cost_endpoint_404s_for_unknown_document(
    ...  # this file's standard client fixture
) -> None:
    response = await client.get("/documents/does-not-exist/cost")
    assert response.status_code == 404


async def test_analytics_reports_total_cost_inr(
    ...  # this file's standard client/session/store fixtures
) -> None:
    # Reuse this file's existing /analytics test setup, run an ocr stage
    # for at least one document first.
    response = await client.get("/analytics")
    body = response.json()
    assert "total_cost_inr" in body
    assert body["total_cost_inr"] >= 0.0
    ocr_stage_entry = next(s for s in body["stages"] if s["name"] == "ocr")
    assert "cost_inr" in ocr_stage_entry
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_api.py -k "cost" -v`
Expected: FAIL with 404 on `/documents/{doc_id}/cost` (route doesn't exist) and `KeyError: 'total_cost_inr'` on the analytics test.

- [ ] **Step 3: Add the schemas**

In `backend/src/fdi/api/schemas.py`, add after `StageThroughputView` and before `AnalyticsView`:

```python
class CostBySourceView(BaseModel):
    """One usage source's totals for a document, priced into INR at read
    time (costing feature, docs/superpowers/specs/2026-07-28-costing-
    feature-design.md). `page_count` is 0 for every LLM source;
    `input_tokens`/`output_tokens` are 0 for "ocr"."""

    source: str
    call_count: int
    page_count: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    cost_inr: float


class DocumentCostView(BaseModel):
    doc_id: str
    total_cost_inr: float
    by_source: list[CostBySourceView]
```

Change `StageThroughputView` to add `cost_inr`:

```python
class StageThroughputView(BaseModel):
    """One pipeline stage, counted over the LATEST run per (document, stage).
    `median_seconds` covers the `done` runs only, and is null when there are
    none -- a stage that has never succeeded has no throughput, which is not
    the same as a throughput of zero. `cost_inr` (costing feature) is the
    corpus-wide total for this stage's billable calls, priced at read time
    -- 0.0 for every stage except "ocr" and "semantic", the only two that
    ever make a real Azure call."""

    name: str
    done: int
    running: int
    queued: int
    failed: int
    median_seconds: float | None
    cost_inr: float
```

Change `AnalyticsView` to add `total_cost_inr`:

```python
class AnalyticsView(BaseModel):
    """`GET /analytics`. Corpus-wide and never paginated: the screen that
    reads it shows totals, and a page of totals is not a total."""

    corpus: CorpusTotalsView
    stages: list[StageThroughputView]
    documents_complete: int
    documents_failed: int
    chunk_count: int
    total_cost_inr: float
```

- [ ] **Step 4: Add the route and pricing helpers**

In `backend/src/fdi/api/routes.py`, add imports:

```python
from fdi.core.pricing import llm_cost_inr, ocr_cost_inr
```

Add `CostBySourceView`, `DocumentCostView` to the existing `from fdi.api.schemas import (...)` block (alphabetically), and add `Settings` to the `from fdi.platform.config import get_settings` line -> `from fdi.platform.config import Settings, get_settings`.

Add a pricing helper and the new route, placed after the `document_artifact` route (after line 529) and before `_content_disposition`:

```python
def _priced_source(row: Mapping[str, object], settings: Settings) -> CostBySourceView:
    source = str(row["source"])
    if source == "ocr":
        cost = ocr_cost_inr(int(row["page_count"]), settings.ocr_cost_per_page_inr)  # type: ignore[arg-type]
    else:
        cost = llm_cost_inr(
            int(row["input_tokens"]), int(row["output_tokens"]),  # type: ignore[arg-type]
            input_usd_per_mtok=settings.llm_input_cost_per_mtok_usd,
            output_usd_per_mtok=settings.llm_output_cost_per_mtok_usd,
            usd_to_inr=settings.usd_to_inr_rate,
        )
    return CostBySourceView(
        source=source, call_count=int(row["call_count"]),  # type: ignore[arg-type]
        page_count=int(row["page_count"]), input_tokens=int(row["input_tokens"]),  # type: ignore[arg-type]
        output_tokens=int(row["output_tokens"]), duration_ms=int(row["duration_ms"]),  # type: ignore[arg-type]
        cost_inr=cost,
    )


@router.get("/documents/{doc_id}/cost", response_model=DocumentCostView)
async def document_cost(doc_id: str, session: SessionDep) -> DocumentCostView:
    repo = RegistryRepository(session)
    if await repo.get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    settings = get_settings()
    by_source = [_priced_source(r, settings) for r in await repo.document_cost_by_source(doc_id)]
    return DocumentCostView(
        doc_id=doc_id,
        total_cost_inr=sum(r.cost_inr for r in by_source),
        by_source=by_source,
    )
```

Change the `analytics` route (lines 1145-1192) to also compute cost:

```python
@router.get("/analytics", response_model=AnalyticsView)
async def analytics(session: SessionDep, vector_store: VectorStoreDep) -> AnalyticsView:
    totals = await DocumentRepository(session).corpus_totals()
    registry = RegistryRepository(session)
    stages = await registry.stage_throughput()
    outcomes = await registry.document_outcome_counts()
    chunk_count = vector_store.count() if vector_store is not None else 0

    settings = get_settings()
    corpus_costs = await registry.corpus_cost_totals()
    stage_costs = await registry.stage_cost_totals()
    total_cost_inr = ocr_cost_inr(
        corpus_costs["ocr_page_count"], settings.ocr_cost_per_page_inr
    ) + llm_cost_inr(
        corpus_costs["llm_input_tokens"], corpus_costs["llm_output_tokens"],
        input_usd_per_mtok=settings.llm_input_cost_per_mtok_usd,
        output_usd_per_mtok=settings.llm_output_cost_per_mtok_usd,
        usd_to_inr=settings.usd_to_inr_rate,
    )

    def _stage_cost_inr(name: str) -> float:
        t = stage_costs.get(name)
        if t is None:
            return 0.0
        return ocr_cost_inr(
            t["ocr_page_count"], settings.ocr_cost_per_page_inr
        ) + llm_cost_inr(
            t["llm_input_tokens"], t["llm_output_tokens"],
            input_usd_per_mtok=settings.llm_input_cost_per_mtok_usd,
            output_usd_per_mtok=settings.llm_output_cost_per_mtok_usd,
            usd_to_inr=settings.usd_to_inr_rate,
        )

    return AnalyticsView(
        corpus=CorpusTotalsView(
            document_count=totals["document_count"],
            doc_key_count=totals["doc_key_count"],
            total_bytes=totals["total_bytes"],
            latest_ingested_at=totals["latest_ingested_at"],
        ),
        stages=[
            StageThroughputView(
                name=s["name"],
                done=s["done"],
                running=s["running"],
                queued=s["queued"],
                failed=s["failed"],
                median_seconds=s["median_seconds"],
                cost_inr=_stage_cost_inr(s["name"]),
            )
            for s in stages
        ],
        documents_complete=outcomes["complete"],
        documents_failed=outcomes["failed"],
        chunk_count=chunk_count,
        total_cost_inr=total_cost_inr,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/integration/test_api.py -k "cost or analytics" -v`
Expected: PASS.

- [ ] **Step 6: Run the full backend test suite and static gates**

Run: `cd backend && uv run pytest -m "not e2e" -v && uv run mypy --strict src && uv run ruff check . && uv run lint-imports`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(costing): add GET /documents/{id}/cost and cost_inr on /analytics"
```

---

### Task 17: Frontend — types, API client, currency formatter

**Files:**
- Modify: `frontend/lib/types.ts`
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/lib/format.ts`

**Interfaces:**
- Produces: TS types `CostBySourceView`, `DocumentCostView`; extended `StageThroughputView`/`AnalyticsView`; `getDocumentCost(docId: string): Promise<DocumentCostView>`; `formatInr(amount: number): string`. Tasks 18-19 consume all of these.

- [ ] **Step 1: Add the TypeScript types**

In `frontend/lib/types.ts`, add after `StageThroughputView` and before `AnalyticsView`:

```typescript
/** One usage source's totals for a document, priced into INR at read time. */
export interface CostBySourceView {
  source: string;
  call_count: number;
  page_count: number;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
  cost_inr: number;
}

export interface DocumentCostView {
  doc_id: string;
  total_cost_inr: number;
  by_source: CostBySourceView[];
}
```

Change `StageThroughputView` to add `cost_inr`:

```typescript
export interface StageThroughputView {
  name: string;
  done: number;
  running: number;
  queued: number;
  failed: number;
  median_seconds: number | null;
  cost_inr: number;
}
```

Change `AnalyticsView` to add `total_cost_inr`:

```typescript
export interface AnalyticsView {
  corpus: {
    document_count: number;
    doc_key_count: number;
    total_bytes: number;
    latest_ingested_at: string | null;
  };
  stages: StageThroughputView[];
  documents_complete: number;
  documents_failed: number;
  chunk_count: number;
  total_cost_inr: number;
}
```

- [ ] **Step 2: Add the API client function**

In `frontend/lib/api.ts`, add `DocumentCostView` to the `import type { ... } from "./types"` block (alphabetically), and add near `getStages`:

```typescript
export const getDocumentCost = (docId: string) =>
  request<DocumentCostView>(`/documents/${docId}/cost`);
```

- [ ] **Step 3: Add a currency formatter**

In `frontend/lib/format.ts`, add after `formatBytes`:

```typescript
const INR_CURRENCY = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

/** Cost figures (costing feature) are always shown in Rupees, grouped
 *  lakh/crore style like formatAmount -- Intl's "en-IN" locale does this
 *  natively for currency too. */
export function formatInr(amount: number): string {
  return INR_CURRENCY.format(amount);
}
```

- [ ] **Step 4: Type-check the frontend**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new type errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/types.ts frontend/lib/api.ts frontend/lib/format.ts
git commit -m "feat(costing): add frontend types, API client, and INR formatter"
```

---

### Task 18: Frontend — ingest page cost readout

**Files:**
- Modify: `frontend/app/ingest/page.tsx`

**Interfaces:**
- Consumes: `getDocumentCost` (Task 17), `formatInr` (Task 17).

- [ ] **Step 1: Add a slow-polled cost query and render it**

In `frontend/app/ingest/page.tsx`, add to the imports:

```typescript
import { getDocumentCost, getStages, startStage, uploadDocument } from "@/lib/api";
import { formatBytes, formatInr } from "@/lib/format";
```

Add a new query after the existing `stages` query (after the `useQuery` block ending around line 42), before `const stages = data?.stages ?? [];`:

```typescript
  const { data: cost } = useQuery({
    queryKey: ["cost", document?.id],
    queryFn: () => getDocumentCost(document!.id),
    enabled: document !== null,
    // Cost only changes when a billable stage (ocr/semantic) actually ran,
    // not every second like stage status -- a slower interval avoids
    // adding load to the same hot path SP-18's stages poll already owns.
    // Stops once the pipeline reaches its terminal stage or fails, same
    // rule the stages query above uses.
    refetchInterval: (query) => {
      const stagesData = query.state.data;
      return stagesData === undefined ? 5_000 : false;
    },
  });
```

Wait — this refetchInterval logic needs the SAME `finished`/`failed` signal the stages query uses, not its own data. Replace it with an interval keyed off the existing `stages`/`finished`/`failed` variables instead (defined below), by moving this query below their computation:

Move the `cost` query to AFTER this existing block:

```typescript
  const stages = data?.stages ?? [];
  const done = stages.filter((s) => s.status === "done").length;
  const failed = stages.find((s) => s.status === "failed");
  const finished = stages.find((s) => s.name === TERMINAL_STAGE)?.status === "done";
```

and there define it as:

```typescript
  const { data: cost } = useQuery({
    queryKey: ["cost", document?.id],
    queryFn: () => getDocumentCost(document!.id),
    enabled: document !== null,
    // Cost only changes when a billable stage (ocr/semantic) actually ran,
    // not every second like stage status -- a slower interval avoids
    // adding load to the same hot path SP-18's stages poll already owns.
    refetchInterval: finished || failed ? false : 5_000,
  });
```

Render it next to the existing stage counter (inside the `<div className="flex items-baseline justify-between" ...>` block, after the `{done}/{STAGES.length} stages` `<div>`, around line 186-189):

```typescript
              <div className="data" style={{ fontSize: "var(--text-sm)" }}>
                {done}/{STAGES.length} stages
                {cost && cost.total_cost_inr > 0 ? (
                  <span style={{ marginLeft: "var(--sp-3)", color: "var(--ink-soft)" }}>
                    {formatInr(cost.total_cost_inr)}
                  </span>
                ) : null}
              </div>
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new type errors.

- [ ] **Step 3: Manually verify in the browser**

Start the dev server (`cd frontend && npm run dev`, backend stack up per `docker/compose.yml`), upload a PDF on `/ingest`, and confirm a Rupee figure appears next to the stage counter once the `ocr` stage completes (it should read at least ₹1.00 for a 1-page document at the default `FDI_OCR_COST_PER_PAGE_INR=1.0`).

- [ ] **Step 4: Commit**

```bash
git add frontend/app/ingest/page.tsx
git commit -m "feat(costing): show running document cost on the ingest page"
```

---

### Task 19: Frontend — analytics page total-cost tile + per-stage cost column

**Files:**
- Modify: `frontend/app/analytics/page.tsx`
- Modify: `frontend/components/analytics/StageThroughput.tsx`

**Interfaces:**
- Consumes: `AnalyticsView.total_cost_inr`/`StageThroughputView.cost_inr` (Task 16/17), `formatInr` (Task 17).

- [ ] **Step 1: Add a total-cost tile to the analytics page**

In `frontend/app/analytics/page.tsx`, add `formatInr` to the `format` import:

```typescript
import { formatBytes, formatInr, formatPercent } from "@/lib/format";
```

Add a new item to the existing `<LedgerTotals items={[...]} />` array (after the `"Chunks indexed"` item, around line 84-88):

```typescript
                {
                  label: "Total cost",
                  value: formatInr(data.total_cost_inr),
                  note: "OCR pages + LLM tokens, priced in INR",
                },
```

- [ ] **Step 2: Add a cost column to the per-stage throughput table**

In `frontend/components/analytics/StageThroughput.tsx`, add a new `<th>` after the existing "Median" header (after line 26-28):

```typescript
          <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) 0", width: "6rem" }}>
            Cost
          </th>
```

Add `formatInr` to the imports and a matching `<td>` after the existing median-seconds `<td>` (after line 112, before the closing `</tr>`):

```typescript
import { formatInr } from "@/lib/format";
```

```typescript
              <td
                className="data"
                style={{
                  padding: "var(--sp-3) 0",
                  fontSize: "var(--text-xs)",
                  color: "var(--ink-soft)",
                  textAlign: "right",
                }}
              >
                {stage.cost_inr > 0 ? formatInr(stage.cost_inr) : "—"}
              </td>
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new type errors.

- [ ] **Step 4: Manually verify in the browser**

Visit `/analytics` with the dev stack up and at least one document that has run `ocr`. Confirm the "Total cost" tile shows a Rupee figure and the "ocr" row in the pipeline throughput table shows a matching per-stage cost (or the two roughly agree once `semantic` has also run against at least one document).

- [ ] **Step 5: Commit**

```bash
git add frontend/app/analytics/page.tsx frontend/components/analytics/StageThroughput.tsx
git commit -m "feat(costing): show total and per-stage cost on the analytics page"
```

---

## Final verification (after all 19 tasks)

- [ ] Run the full backend gate: `cd backend && uv run pytest -m "not e2e" -v && uv run mypy --strict src && uv run ruff check . && uv run lint-imports`
- [ ] Run the frontend type-check: `cd frontend && npx tsc --noEmit`
- [ ] Rebuild and restart the Docker stack (`cd docker && sg docker -c "docker compose up -d --build"`), confirm migration `0008` applies, and manually exercise: upload a PDF → run `ocr` → run `semantic` → check `GET /documents/{id}/cost` and `GET /analytics` both show non-zero, correctly-priced totals → check the ingest and analytics pages render them.
- [ ] Re-read `docs/superpowers/specs/2026-07-28-costing-feature-design.md` section 7 (Done criteria) and confirm all 11 items are met.
