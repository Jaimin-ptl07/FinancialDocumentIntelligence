# SP-10 — Semantic Intelligence via Azure OpenAI (GPT)

- **Status:** Implemented 2026-07-17 on `feature/sp-10-semantic` (worktree `D:\fdi-wt\sp10`). All 9 plan
  tasks done via strict TDD; full non-e2e/non-azure suite green (198 tests); all four static gates green;
  no live Azure OpenAI call anywhere in the suite (`FakeSemanticClient` proves the whole path; the
  monkeypatched-SDK unit test proves `AzureOpenAIClient`; the opt-in `@pytest.mark.azure` smoke test
  skips cleanly unconfigured).
- **Depends on:** SP-2 (stage framework — `StageContext.services`, content-addressed derived-stage
  caching), SP-8 (Document AST — provides `{schema_version, title, sections:[{heading, paragraphs}]}`).
- **Decision carried from roadmap memory (`external-services-azure`):** OCR = Azure Document Intelligence
  (SP-5); **LLM = Azure OpenAI (GPT)**. No separate deep-research pass — SP-10 is structurally IDENTICAL
  to SP-5 (mockable client protocol, fake for tests, a platform client for the real service, config,
  a derived stage, an opt-in real smoke test), substituting the LLM for OCR. Real creds live in the
  sibling `D:\FinancialDocIntelligence\.env` as `GPT_ENDPOINT` / `GPT_DEPLOYMENT` / `GPT_MODEL_NAME` /
  `GPT_SUBSCRIPTION_KEY` / `GPT_API_VERSION` — copied by the operator into this repo's gitignored `.env`
  under the `FDI_GPT_*` names (never committed).

## 1. Purpose

Classify each AST section into a financial-statement type — balance sheet, income statement, cash flow,
notes, MD&A, auditor's/director's report, or other — by running **Azure OpenAI (GPT)** as a pipeline
**stage** over the SP-8 AST artifact. This is the first semantic (meaning-bearing) layer: everything
before it (OCR, normalize, AST) is structural. Every later layer (SP-11 ontology mapping, SP-9 graph)
consumes section *type*, not just section *shape*.

Thin vertical slice: the semantic stage + a mockable Azure OpenAI client + config wiring, proven with a
**fake LLM client** in the test suite (no live Azure, no cost, no token spend) plus an **opt-in real
Azure OpenAI smoke test**. Classification *accuracy* and prompt-variant comparison are measurement work
for SP-3's harness, deliberately deferred (§6).

## 2. Scope decisions (made autonomously, mirroring SP-5's grounded pattern)

1. **A closed, canonical label set**, not free-text classification. `fdi.core.semantic.SectionType` is a
   `StrEnum` of 8 values: `BALANCE_SHEET`, `INCOME_STATEMENT`, `CASH_FLOW`, `NOTES`, `MDNA`,
   `AUDITORS_REPORT`, `DIRECTORS_REPORT`, `OTHER`. A closed set is what makes classification *checkable*
   (SP-3 `exact_match` against ground truth) and what makes downstream ontology mapping (SP-11) tractable
   — an LLM that could answer anything would need its own normalization layer, which is exactly the
   problem this enum avoids.
2. **The LLM is an injected `SemanticClient` protocol**, so the suite runs against a `FakeSemanticClient`
   (canned heading→label mapping, no network) and a real `AzureOpenAIClient` (SDK) is used in production
   plus an opt-in smoke test. Same SP-3 lesson as SP-5: the engine is a swappable candidate — a future
   prompt variant or even a non-LLM classifier (keyword rules) is just another `SemanticClient`.
3. **SDK = `openai` (official Python SDK), `openai.AzureOpenAI` sync client**, `chat.completions.create`.
   Distinct from the AOAI *Assistants*/*Responses* API — plain chat completion is the simplest interface
   sufficient for a single-turn classification prompt; no dependency on `azure-ai-*` packages here (the
   `openai` package's `AzureOpenAI` class talks to Azure OpenAI's REST surface directly via
   endpoint+api_version+deployment, no separate SDK needed).
4. **`SemanticStage` is a DERIVED stage** (`name="semantic"`, `version=1`, `deps=("ast",)`) — the same
   shape as SP-8's `AstStage` (`deps=("normalize",)`), not a root stage like SP-5's `OcrStage`. It reads
   the AST artifact via `ctx.read("ast")`, classifies each section via the injected client, and returns
   a new artifact.
5. **Determinism exception (Global Constraint 6), decided explicitly — and simpler than SP-5's:** GPT
   output is NOT byte-stable (temperature, model updates, prompt drift). But because `SemanticStage` is a
   **derived** stage, SP-2's existing framework ALREADY content-addresses it on
   `derived_input_hash([ast_run.output_hash])` + `SemanticStage.version` (see `fdi/pipeline/run.py`
   `run_stage`) — no root-stage special-casing is needed (unlike SP-5, which had to fold the DI
   model/api_version into a root `input_hash`). The framework caches the FIRST successful classification
   at that key and does not re-run; a deliberate reprocess or a `version` bump (bump when the prompt,
   `FDI_GPT_DEPLOYMENT`, or the label set changes) regenerates. "Run once, cache" is correct here for the
   same reason as SP-5: re-classifying is a paid, non-deterministic external call.
6. **The stored artifact is derived, not the raw chat completion**: a stable JSON
   `{schema_version:1, sections:[{heading, type, paragraph_count}]}` — one entry per AST section, in
   order. The raw `ChatCompletion` response (id, usage, finish_reason, …) is NOT persisted; only the
   parsed label matters downstream. This mirrors SP-5's "normalized subset, not the SDK object" call.
7. **The client returns a raw string; the STAGE is the authority on validity.** `SemanticClient.classify`
   returns `str` (whatever the LLM said, prompted to be one of the 8 labels). `SemanticStage` does
   `SectionType(label)` to coerce/validate — an LLM reply outside the enum is a hard `ValueError`, not a
   silent `OTHER` fallback (a silently-wrong label is worse than a loud failure; see SP-2's "no partial
   artifact on error" precedent from SP-5 §4). This keeps `fdi.core` (the enum) unaware of the LLM, and
   keeps prompt-parsing best-effort in `platform` while validation is centralized in `pipeline`.
8. **Config, not secrets in repo:** `FDI_GPT_ENDPOINT` / `FDI_GPT_KEY` are `str | None = None` (the stack
   boots without Azure OpenAI configured; semantic classification raises a clear error if unconfigured,
   same as SP-5's OCR). Operator copies real values from the sibling `D:\FinancialDocIntelligence\.env`
   (there named `GPT_ENDPOINT` / `GPT_SUBSCRIPTION_KEY` / `GPT_DEPLOYMENT` / `GPT_MODEL_NAME` /
   `GPT_API_VERSION`) into this repo's gitignored `.env` under the `FDI_GPT_*` names below.
9. **A bounded text snippet per section, not the whole section.** Sections (esp. Notes) can be very long;
   sending the full paragraph list on every case burns tokens for no classification benefit (the label is
   usually obvious from the heading + first paragraph or two). `fdi.core.semantic.build_snippet(paragraphs,
   max_chars=2000)` — pure, joins paragraphs with `"\n"` and truncates to `max_chars` — bounds the prompt.
   Token-budget-aware chunking / adaptive sampling is deferred (§6); 2000 chars is a documented, tunable
   constant, not a magic number smuggled into the prompt string.

## 3. Architecture

### 3.1 No stage-framework change needed (unlike SP-5)

SP-5 had to extend `StageContext` with `read_source()` + `services` because OCR is a root stage needing
the raw PDF and an external client. SP-10 needs neither extension: `StageContext.services` already exists
(added by SP-5) and `SemanticStage` is a **derived** stage reading an upstream artifact via
`ctx.read("ast")` — exactly `AstStage`'s shape. `run_stage`'s existing `services: Mapping[str, object]`
param already threads a `services["semantic"]` entry through, and its existing derived-stage
`input_hash` computation (`derived_input_hash([facts["ast"].output_hash])`) already gives content-address
caching for free (§2.5). Zero changes to `fdi/pipeline/stage.py` or `fdi/pipeline/run.py`.

### 3.2 `SectionType` (core, pure)

```python
# fdi.core.semantic
from enum import StrEnum

class SectionType(StrEnum):
    BALANCE_SHEET = "BALANCE_SHEET"
    INCOME_STATEMENT = "INCOME_STATEMENT"
    CASH_FLOW = "CASH_FLOW"
    NOTES = "NOTES"
    MDNA = "MDNA"
    AUDITORS_REPORT = "AUDITORS_REPORT"
    DIRECTORS_REPORT = "DIRECTORS_REPORT"
    OTHER = "OTHER"

def build_snippet(paragraphs: list[str], max_chars: int = 2000) -> str:
    return "\n".join(paragraphs)[:max_chars]
```

### 3.3 `SemanticClient` (protocol + two implementations)

- **`fdi.pipeline.semantic.SemanticClient`** (Protocol): `def classify(self, heading: str, text: str) ->
  str` — returns a raw label string (the client tries to make it one of `SectionType`'s values; the stage
  is the final validator). Pure interface, no I/O import.
- **`fdi.platform.semantic.AzureOpenAIClient`** (I/O — lives in `platform`): wraps `openai.AzureOpenAI`;
  constructed with endpoint/key/deployment/model/api_version from Settings; `classify` sends a
  single-turn chat prompt (system message lists the 8 allowed labels + instructions to answer with
  exactly one; user message carries `heading` + `text`), parses `response.choices[0].message.content`
  (`.strip().upper()`).
- **`FakeSemanticClient`** (test helper, in `pipeline`): `__init__(self, labels: Mapping[str, str])` —
  canned heading→label mapping (default `SectionType.OTHER.value` for an unmapped heading); records
  `self.calls` (int) and `self.seen: list[tuple[str, str]]` (every `(heading, text)` it was called with).

### 3.4 `AzureOpenAIClient` (platform)

```python
# fdi.platform.semantic
from openai import AzureOpenAI
from fdi.platform.config import Settings

_LABELS = ["BALANCE_SHEET", "INCOME_STATEMENT", "CASH_FLOW", "NOTES", "MDNA",
           "AUDITORS_REPORT", "DIRECTORS_REPORT", "OTHER"]

class AzureOpenAIClient:
    def __init__(self, *, endpoint: str, key: str, deployment: str, model: str,
                 api_version: str) -> None:
        self._deployment = deployment
        self._model = model
        self._sdk = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)

    def classify(self, heading: str, text: str) -> str:
        resp = self._sdk.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": (
                    "Classify a financial-statement document section into exactly one of: "
                    + ", ".join(_LABELS) + ". Reply with ONLY the label, nothing else.")},
                {"role": "user", "content": f"Heading: {heading}\n\n{text}"},
            ],
            temperature=0,
        )
        content = resp.choices[0].message.content or ""
        return content.strip().upper()

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOpenAIClient | None":
        if not settings.gpt_endpoint or not settings.gpt_key:
            return None
        return AzureOpenAIClient(
            endpoint=settings.gpt_endpoint, key=settings.gpt_key,
            deployment=settings.gpt_deployment, model=settings.gpt_model,
            api_version=settings.gpt_api_version,
        )
```
`self._model` is threaded through for parity with `AzureOcrClient` (which folds model into its cache
key discipline) even though Azure OpenAI's `create(model=...)` call actually takes the **deployment**
name; `_model` is retained on the instance for logging/future prompt-variant bookkeeping, not passed to
the SDK call (Azure OpenAI resolves the underlying model from the deployment).

### 3.5 `SemanticStage` (`fdi.pipeline.stages.semantic`)

```python
class SemanticStage:
    name = "semantic"
    version = 1   # BUMP when the prompt, FDI_GPT_DEPLOYMENT, or the label set changes
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        client = cast(SemanticClient, ctx.services["semantic"])
        sections_raw = ast.get("sections")
        sections: list[dict[str, object]] = []
        for section in (sections_raw if isinstance(sections_raw, list) else []):
            heading = section.get("heading") or ""
            paragraphs = section.get("paragraphs") or []
            text = build_snippet(paragraphs if isinstance(paragraphs, list) else [])
            label = await asyncio.to_thread(client.classify, heading, text)   # SDK is sync
            sections.append({
                "heading": section.get("heading"),
                "type": SectionType(label).value,   # hard-fails on an invalid label
                "paragraph_count": len(paragraphs) if isinstance(paragraphs, list) else 0,
            })
        return {"schema_version": 1, "sections": sections}
```
Self-registers into `REGISTRY` (`fdi.pipeline.stages.__init__` gains `from fdi.pipeline.stages import
semantic as semantic  # noqa: F401`).

### 3.6 Config (Settings + x-backend-env + .env.example)

New `FDI_` fields: `FDI_GPT_ENDPOINT: str | None = None`, `FDI_GPT_KEY: str | None = None`,
`FDI_GPT_DEPLOYMENT: str = "gpt-4o"`, `FDI_GPT_MODEL: str = "gpt-4o"`, `FDI_GPT_API_VERSION: str =
"2024-10-21"` (a current stable AOAI chat-completions API version; operator overrides via `.env` to
match the deployment actually provisioned). All five added to Settings AND compose `x-backend-env` AND
`.env.example` (the inert-config lesson, carried from SP-5). Endpoint/key `str | None = None` so
empty→None boots cleanly.

Note on naming: the sibling repo's `.env` uses bare `GPT_*` names (`GPT_SUBSCRIPTION_KEY`, not
`GPT_KEY`); this project's convention (every setting is `FDI_`-prefixed, enforced by
`_reject_unknown_fdi_env_vars`) renames on the way in: `GPT_SUBSCRIPTION_KEY` → `FDI_GPT_KEY`,
`GPT_ENDPOINT` → `FDI_GPT_ENDPOINT`, `GPT_DEPLOYMENT` → `FDI_GPT_DEPLOYMENT`, `GPT_MODEL_NAME` →
`FDI_GPT_MODEL`, `GPT_API_VERSION` → `FDI_GPT_API_VERSION`. The operator does this rename by hand when
copying values into this repo's `.env` (never committed).

### 3.7 Trigger

`POST /documents/{id}/semantic` → 202: dispatch `run_stage_task(doc_id, "semantic", correlation_id)`
(reuses `StageDispatcher`, mirrors SP-5's `POST /documents/{id}/ocr`). 404 if the document is unknown.
The generic `POST /docs/{doc_id}/reprocess?from=semantic` (SP-2/18) already covers ad-hoc re-dispatch; the
dedicated route exists for parity/discoverability with the OCR trigger, same rationale as SP-5.

### 3.8 Dependency

Add `openai` (the official Python SDK, current stable) to `backend/pyproject.toml`.

## 4. Error handling

- Azure OpenAI unconfigured (`endpoint`/`key` None) when `semantic` runs → the stage fails with a clear
  message (`ctx.services["semantic"]` raises `KeyError`, or the wiring omits the key entirely — see Task
  7); `run_stage`'s fail-path records `stage_run(failed, error=...)`. No partial artifact.
- Azure OpenAI API error / timeout → the SDK raises; `run_stage` records the failure; retryable per
  Celery (same as SP-5).
- An LLM reply outside the 8-label enum → `SectionType(label)` raises `ValueError` — a hard, recorded
  failure, NOT a silent `OTHER` (§2.7). A section with zero paragraphs and an empty heading still gets
  classified (the client decides, typically `OTHER`) — no special-casing of degenerate sections.
- An AST artifact with zero sections → `SemanticStage` returns `{schema_version:1, sections:[]}` with
  **no LLM calls at all** — a trivially valid, cheap result, not an error.
- `semantic` dispatched before `ast` has run → `run_stage`'s existing derived-stage machinery already
  requires the dep's `stage_run` to exist (`facts["ast"].output_hash` — SP-2 behavior, unchanged).

## 5. Testing

- **Unit** (no Docker, no Azure): `build_snippet` truncates at `max_chars` and joins with `\n`;
  `SectionType` enum values round-trip as strings; `FakeSemanticClient` records `(heading, text)` calls
  and returns the canned label (default `OTHER` for an unmapped heading); `AzureOpenAIClient` (SDK
  monkeypatched, no live call) builds the chat messages correctly and parses/strips/uppercases the
  response; `from_settings` returns `None` when endpoint/key unset; config defaults
  (`gpt_endpoint is None`, `gpt_deployment == "gpt-4o"`, …) present in Settings.
- **Integration** (testcontainers, `FakeSemanticClient` — NO live Azure OpenAI): run `ast` then `semantic`
  via `run_stage` with `services={"semantic": FakeSemanticClient(...)}`; assert a `done` stage_run + a
  stored artifact at the content-addressed key + `sections[].type` matches the canned mapping; a second
  `semantic` run is a cache hit (LLM NOT called twice — proven by a call counter on the fake, same
  pattern as SP-5's OCR cache-hit test).
- **Opt-in real-Azure-OpenAI smoke test** (`@pytest.mark.azure`, skipped unless `FDI_GPT_ENDPOINT`/
  `FDI_GPT_KEY` set): constructs the real `AzureOpenAIClient`, classifies one canned heading+text pair
  against a real deployment, asserts the response coerces into `SectionType`. NEVER in CI (no creds, no
  cost). Reuses the existing `azure` pytest marker (broadened, §Task 8, to cover both Document
  Intelligence and OpenAI live-service tests) so the fully-offline command
  (`-m "not integration and not e2e and not azure"`) still excludes it without a new flag.
- **API** (integration): `POST /documents/{id}/semantic` → 202; unknown doc id → 404.
- Config consistency: `FDI_GPT_*` present in Settings + compose + `.env.example`.

## 6. Explicitly deferred (YAGNI)

- **Classification accuracy as an SP-3 metric + prompt-variant candidates.** SP-3's harness (Candidate +
  Metric + leaderboard) is built for exactly this: once a ground-truth dataset of (heading, text) →
  label pairs exists, wrap `AzureOpenAIClient.classify` (and a cheap keyword-rule baseline) as
  `Candidate`s, score with an `exact_match`-style metric, and let the leaderboard pick the winning
  prompt/model. Not built here — SP-10 ships the classifier the harness will later measure, mirroring
  how SP-5 shipped `OcrClient` for SP-3 to measure `read` vs `layout` later.
- **Ontology mapping** (`Revenue` → `CANONICAL_REVENUE`, line-item canonicalization) → SP-11, which
  consumes `SectionType` labels as its input signal.
- **Batch/async classification** for corpus-scale (parallel section classification, request batching) →
  when the 5000-doc run is real (SP-13/14), same deferral as SP-5's batch-analysis note.
- **Confidence scores / multi-label sections** (a section that is genuinely both, e.g. a combined
  statement) → add when real annual reports show the need; the 8-label closed set assumes one label per
  AST section for the thin slice.
- **Retry/backoff policy tuned to Azure OpenAI rate limits** (429s) → generic Celery retry covers the
  thin slice; provider-specific backoff tuning arrives with corpus-scale ingestion.

## 7. Done criteria

1. `SectionType` (`fdi.core.semantic`) is a pure `StrEnum` with the 8 documented labels; `build_snippet`
   truncates/joins purely (no I/O import in `fdi.core`).
2. `SemanticClient` protocol + `FakeSemanticClient` (`fdi.pipeline.semantic`) exist; the fake records
   every `(heading, text)` call and returns a canned label.
3. `SemanticStage` (`name="semantic"`, `version=1`, `deps=("ast",)`) is registered; `run(ctx)` reads the
   AST artifact, classifies each section via the injected `SemanticClient`, and returns
   `{schema_version:1, sections:[{heading, type, paragraph_count}]}`; an invalid LLM label raises.
4. Running `semantic` over a document with an `ast` artifact stores the result content-addressed on
   `derived_input_hash([ast.output_hash])` + `SemanticStage.version` and records a `done` stage_run; a
   second run is a store cache hit that does NOT call the LLM again.
5. `FDI_GPT_*` config is wired (Settings + compose + `.env.example`); the stack boots with Azure OpenAI
   unconfigured (endpoint/key None), and `semantic` then fails with a clear, recorded error.
6. `POST /documents/{id}/semantic` dispatches the semantic stage (202); unknown id → 404.
7. The opt-in real-Azure-OpenAI smoke test exists and skips cleanly when unconfigured.
8. All four static gates green; `core` stays pure (`SectionType` + `build_snippet` have no SDK import);
   full suite passes with NO live Azure OpenAI call (fake client only).

## 8. Done-criteria audit (2026-07-17)

| # | Criterion | Proven by |
|---|---|---|
| 1 | `SectionType` pure `StrEnum` (8 labels) + `build_snippet` | `tests/unit/test_semantic_core.py` |
| 2 | `SemanticClient` protocol + `FakeSemanticClient` (records calls) | `tests/unit/test_semantic_client.py` |
| 3 | `SemanticStage` (`deps=("ast",)`), coercion via `SectionType(label)` | `tests/integration/test_semantic_stage.py` |
| 4 | Content-addressed on `derived_input_hash([ast.output_hash])`; 2nd run = cache hit, no 2nd LLM call | `tests/integration/test_semantic_stage.py::test_semantic_stage_runs_and_caches` (`fake_semantic.calls == 1` after two `run_stage` calls) |
| 5 | `FDI_GPT_*` in Settings + compose + `.env.example`; stack boots unconfigured | `tests/unit/test_config.py::test_gpt_settings_default`; `backend/src/fdi/platform/config.py`; `docker/compose.yml`; `.env.example` |
| 6 | `POST /documents/{id}/semantic` → 202; unknown id → 404 | `tests/integration/test_api.py::test_trigger_semantic_on_a_document`, `::test_trigger_semantic_unknown_document_is_404` |
| 7 | Opt-in real-Azure-OpenAI smoke test skips cleanly unconfigured | `tests/azure/test_real_semantic.py` (skipped: "Azure OpenAI not configured") |
| 8 | Static gates green; `core` pure; no live Azure OpenAI call in the suite | `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (1 kept, 0 broken); full run `pytest -m "not e2e and not azure"` — 198 passed |
