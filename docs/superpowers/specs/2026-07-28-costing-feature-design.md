# Costing Feature — end-to-end OCR + LLM cost tracking, reported in INR

- **Status:** Design approved 2026-07-28, not yet implemented.
- **Depends on:**
  - **`fdi.pipeline.run.run_stage`** (`backend/src/fdi/pipeline/run.py:24-100`) — the single place every
    pipeline stage executes; the cache-miss branch (`run.py:84-96`) is the OCR instrumentation point.
  - **`StageRun`** (`backend/src/fdi/platform/db/models.py:60-83`) — the per-stage-execution row this
    feature's OCR events key off (`stage_run_id`), and whose `created_at`/`updated_at` already establish
    the "duration derived from timestamps" precedent this feature extends with true per-call timing.
  - **`platform/agent.py`, `platform/extractor.py`, `platform/semantic.py`** — the three Azure OpenAI call
    sites, all of which currently discard `resp.usage` (confirmed by exploration: zero existing
    token/usage/timing instrumentation anywhere in the codebase — this is greenfield).
  - **`platform/ocr.py`** (`AzureOcrClient.analyze`) — the one Azure Document Intelligence call site;
    page count is already recoverable post-hoc from its normalized artifact body (`pages` list), so no
    client-level change is needed there.
  - **`routes.py` `/ask` and `/extract/document`** — the two LLM call paths that run **outside**
    `run_stage`/`StageContext` entirely (no Celery task, no `StageRun` row), which is why this design
    cannot key cost purely off `stage_run_id`.

## 1. Purpose

Track the real-money cost of running a document through this pipeline — OCR page charges and LLM token
charges — per document ("job") and corpus-wide, reported in Indian Rupees. Today nothing is tracked: all
three LLM call sites discard `resp.usage`, and no page-count/cost concept exists anywhere in the schema.
This is genuinely new instrumentation, not an extension of an existing partial feature.

Pricing given by the user: OCR = ₹1/page. LLM = $2.5 per million input tokens, $10 per million output
tokens, converted to INR at report time.

## 2. Scope decisions (autonomous unless marked user-selected; documented)

1. **Backend + frontend** *(user-selected)*. Both the tracking/API layer and a matching frontend surface
   (ingest page running cost, analytics page cost tile/column) are in scope for this spec.
2. **Convert to INR at read-time, not write-time** *(user-selected)*. Only raw usage units (pages,
   input/output tokens, duration) are persisted. INR cost is always computed at query time from the
   *currently configured* pricing/FX settings. A later change to `FDI_USD_TO_INR_RATE` or the per-unit
   prices recomputes historical totals too — there is no per-event price snapshot. This is the direct
   consequence of the user's choice; it trades ledger-style immutability for simplicity and
   re-priceability.
3. **`/ask` cost is tracked** *(user-selected)*, even though `/ask` is corpus-wide RAG chat, not scoped
   to one document. Its LLM calls are recorded with `doc_id = NULL` and roll into corpus-wide totals
   only — never into any single document's cost breakdown.
4. **Billing policy: bill on any successful provider response** *(user-selected)*. If Azure DI or Azure
   OpenAI returns a response at all — even one this code later fails to parse or use (e.g.
   `extractor.py`'s `json.JSONDecodeError` → `{}` path) — the tokens/pages were consumed and must be
   counted. Only a request that errors *before* any response arrives (network error, timeout, non-2xx)
   counts as ₹0. This governs where the recording call sits relative to error handling at each call site
   (§3.3).
5. **Per-call event log, not per-stage aggregate** *(user-selected)*. Every real OCR/LLM API call gets
   its own row. Call count is `COUNT(*)`; nothing is pre-aggregated at write time. This directly satisfies
   "how many calls, how much time taken" at full fidelity, and keeps every individual call inspectable
   after the fact. Volume is not a concern at this app's single-tenant/local scale.
6. **Default `FDI_USD_TO_INR_RATE = 95.0`** *(user-selected)*, overridable via env var like every other
   `FDI_*` setting.
7. **Two typed tables, not one polymorphic table** (autonomous, proposed and approved). `ocr_usage_event`
   and `llm_usage_event`, each with only the columns it needs. Considered and rejected: a single
   `cost_event` table with nullable kind-specific columns, and a JSON-metadata blob table. Both conflict
   with this codebase's established convention — confirmed by direct inspection of `models.py` and every
   migration in `backend/alembic/versions/` — of explicit, strongly-typed, handwritten tables
   (`Document`/`StageRun`/`EvalRun` are separate purpose-built tables, not one polymorphic "record"
   table; no JSONB column exists anywhere in the schema).
8. **Tables store usage, not cost** (autonomous). Named `*_usage_event`, not `*_cost_event`, because per
   decision 2 they hold raw units; "cost" is a value derived at read time by a pure pricing function, never
   a column.
9. **Only billable (successful) calls create a row** (autonomous). These tables are pure billing ledgers.
   A call that never got a response has no row — full failure observability already exists via
   `StageRun.error` / route-level error handling; duplicating it here would give the tables two
   conflicting purposes (per the project's own "each unit has one clear purpose" design guidance).
10. **OCR is instrumented generically in `run_stage`; LLM is instrumented via a per-execution
    accumulator** (autonomous, architecture consequence of decision 5). OCR is always exactly one Azure
    call per stage execution — `run_stage` can just time the whole `stage.run()` call and derive page
    count from the returned artifact body, no client change needed. LLM stages are *not* 1:1 — `semantic`
    loops one `classify()` call per AST section — so per-call timing/tokens must be captured at each
    individual `chat.completions.create()` call, inside the platform client methods themselves, then
    reported up via a small in-memory accumulator (§3.2) rather than by widening every stage's return
    contract.
11. **`GET /documents/{doc_id}/stages` is left untouched** (autonomous). It's polled every 1 second by the
    frontend during ingest (`frontend/app/ingest/page.tsx`); adding cost aggregation to that hot path adds
    avoidable DB load. Cost is fetched from a separate endpoint on a slower cadence.

## 3. Architecture

### 3.1 Data model

**New Settings fields** (`backend/src/fdi/platform/config.py`, `FDI_` prefix, mirrored into
`docker/compose.yml`'s `x-backend-env` and `.env.example` — the existing 3-file "inert config" discipline
this codebase enforces for every `FDI_*` field):

| field | env var | default |
|---|---|---|
| `ocr_cost_per_page_inr` | `FDI_OCR_COST_PER_PAGE_INR` | `1.0` |
| `llm_input_cost_per_mtok_usd` | `FDI_LLM_INPUT_COST_PER_MTOK_USD` | `2.5` |
| `llm_output_cost_per_mtok_usd` | `FDI_LLM_OUTPUT_COST_PER_MTOK_USD` | `10.0` |
| `usd_to_inr_rate` | `FDI_USD_TO_INR_RATE` | `95.0` |

**Migration `0008_usage_events.py`** (next sequential id after `0007_drop_chunk.py`), handwritten per this
project's alembic convention (explicit `op.create_table`, `CheckConstraint` for the bounded `source`
column, explicit indexes on FK/filter columns, `String(36)` UUID PKs, `server_default=func.now()`):

```python
class OcrUsageEvent(Base):
    __tablename__ = "ocr_usage_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(36), ForeignKey("document.id"), index=True)
    stage_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("stage_run.id"), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_count: Mapped[int] = mapped_column(Integer)
    azure_model: Mapped[str] = mapped_column(String(64))       # e.g. "prebuilt-layout"
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class LlmUsageEvent(Base):
    __tablename__ = "llm_usage_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("document.id"), nullable=True, index=True)
    stage_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("stage_run.id"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(32))            # CHECK: semantic_classify | extract_document | ask_agent
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

`llm_usage_event.doc_id`/`stage_run_id` are nullable (decision 3, 10); `ocr_usage_event`'s are not — OCR
always runs inside `run_stage` for a specific document (confirmed: `OcrStage` is only ever dispatched via
`/documents/{doc_id}/ocr` or `/docs/{doc_id}/reprocess?from=ocr`, both doc-scoped).

### 3.2 Pure pricing core — `fdi.core.pricing`

No I/O, no config object, no DB — matches this codebase's "pure core" layering
(`api|workers → pipeline → platform → core`) already used by `fdi.core.crossdoc`/`fdi.core.knowledge`.

```python
def ocr_cost_inr(page_count: int, price_per_page_inr: float) -> float:
    return page_count * price_per_page_inr

def llm_cost_inr(
    input_tokens: int, output_tokens: int,
    input_usd_per_mtok: float, output_usd_per_mtok: float, usd_to_inr: float,
) -> float:
    usd = (input_tokens / 1_000_000) * input_usd_per_mtok + (output_tokens / 1_000_000) * output_usd_per_mtok
    return usd * usd_to_inr
```

Called only at read time (repository/route layer), never at write time (decision 2) — write-time code
never imports `Settings` pricing fields at all.

### 3.3 Instrumentation

**OCR** — generic, in `run_stage` (`backend/src/fdi/pipeline/run.py:84-96`). Time the `await
stage.run(ctx)` call; on success, if `stage_name == "ocr"`, derive `page_count = len(body.get("pages",
[]))` from the returned artifact body and write one `ocr_usage_event` row (in the same transaction as
`finish_run`, `run.py:97-99`) with `stage_run_id = run_id`, `azure_model` read from `Settings`. No change
to `platform/ocr.py` or `AzureOcrClient` is needed.

**LLM — `UsageAccumulator`.** A small in-memory (no I/O) object, constructed fresh per `run_stage`
invocation and injected into `StageContext.services["usage"]` alongside the existing `oc`/`sc`/`ec`
clients (`workers/tasks.py:86-113`):

```python
@dataclass
class LlmCallUsage:
    source: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int

@dataclass
class UsageAccumulator:
    llm_calls: list[LlmCallUsage] = field(default_factory=list)
    def record_llm(self, usage: LlmCallUsage) -> None:
        self.llm_calls.append(usage)
```

This respects the principle already stated in `stage.py`'s own docstring — "the framework owns all I/O,
hashing, and persistence" — a stage only *reports* what it did; it never writes to the DB itself. The
three platform client methods must each start returning per-call usage alongside their existing payload,
since `resp.usage` is only visible inside `platform/agent.py` / `platform/extractor.py` /
`platform/semantic.py` and is gone by the time a `Protocol`-typed result crosses into `pipeline/`:

- `AgentResponse` (`platform/agent.py`) gains a `usage: LlmCallUsage` field.
- `SemanticClient.classify()` (`pipeline/semantic.py:14-16`) widens from `-> str` to `-> ClassifyResult
  (label: str, usage: LlmCallUsage)`.
- `ExtractorClient.extract()` (`pipeline/extractor.py`) widens from `-> dict` to `-> ExtractResult(data:
  dict, usage: LlmCallUsage)`.

`SemanticStage.run()` (`pipeline/stages/semantic.py:44`) calls `ctx.services["usage"].record_llm(...)`
after each `classify()` call, inside its existing per-section loop — no change to what it returns as the
stage's artifact body. After `stage.run()` returns (success **or** failure — decision 4/9: a partial loop
that made 3 real calls before erroring on the 4th still billed for those 3), `run_stage` reads
`accumulator.llm_calls` and writes one `llm_usage_event` row per entry with `stage_run_id = run_id`,
`source = "semantic_classify"`, `doc_id = ctx.doc_id`.

**`/ask` and `/extract/document`** run outside `run_stage` entirely (no Celery task, no `StageRun`), so
they record directly: after each `agent_client.complete(...)` call in `pipeline/ask.py`'s round loop, and
after each `extractor_client.extract(...)` call in `pipeline/extractor.py`'s per-unit loop, the caller
writes an `llm_usage_event` row itself (`stage_run_id = None`; `doc_id = None` for `/ask`, the resolved
document id for `/extract/document`; `source = "ask_agent"` / `"extract_document"` respectively). Both
already have DB session access at their call sites (routes inject `SessionDep`); the exact plumbing
(whether the repository call happens in the pipeline function or the route handler) is an implementation
detail for the plan phase — bill-on-response (decision 4) still governs where the recording call sits
relative to each site's existing error handling (e.g. `extractor.py`'s `contextlib.suppress(Exception)` —
usage must be recorded *before* that suppression swallows a downstream parse failure, since the response
itself succeeded).

### 3.4 API surface

- **New `GET /documents/{doc_id}/cost`** — aggregated breakdown for one document: total pages, total
  input/output tokens, total calls, total duration, total cost (₹), grouped by `source` (`ocr`,
  `semantic_classify`, `extract_document`). Backed by a new `RegistryRepository` method querying both
  tables `WHERE doc_id = :doc_id` and applying §3.2's pricing functions. Not a raw event listing by
  default — storage keeps full per-call granularity (decision 5) for a future drill-down endpoint if
  needed, but the default response stays aggregate-sized.
- **Extend `GET /analytics`** (`AnalyticsView`, `schemas.py:561-569`) — add corpus-wide `total_cost_inr`,
  and a `cost_inr` field on each entry of the existing `stages[]` array, mirroring how `median_seconds` is
  already computed there (`repository.py:216-269`, `stage_throughput()`). `/ask`'s untethered
  (`doc_id = NULL`) cost rolls into `total_cost_inr` but not into any per-stage or per-document figure.
- **`GET /documents/{doc_id}/stages` unchanged** (decision 11).

### 3.5 Frontend

Per this codebase's explicit 1:1 backend/frontend type-mirroring convention (`frontend/lib/types.ts`'s own
header comment: "there is no camelCase translation layer, so drift is visible"):

- `frontend/lib/types.ts` — new `DocumentCostView`/extended `AnalyticsView` types mirroring the new
  Pydantic views.
- `frontend/lib/api.ts` — `getDocumentCost(docId)` calling the new endpoint.
- `frontend/app/ingest/page.tsx` — a total-cost readout next to the existing "N/15 stages" counter,
  fetched on a slower interval than the 1s stage-status poll (or once the terminal `index` stage is done).
- `frontend/components/pipeline/StageGraph.tsx` — optional per-stage cost as a tooltip on the `ocr`/
  `semantic` nodes only (the only two stages that ever cost anything), following the existing pattern
  that already shows `run?.error` as a tooltip.
- `frontend/app/analytics/page.tsx` + `components/analytics/StageThroughput.tsx` — a corpus-wide
  total-cost tile, and a cost column alongside the existing `median_seconds` column.

## 4. Error handling

- **Cache-hit stage re-runs cost nothing.** `run_stage`'s existing cache-hit branch (`run.py:75-80`)
  returns before `stage.run()` is ever called — no accumulator is populated, no Azure/OpenAI call
  happened, no usage row is written. Re-triggering `/documents/{doc_id}/ocr` twice on an unchanged
  document bills once, not twice.
- **A failing stage still bills for the real calls it made** (decision 4/9/10). If `semantic`'s loop makes
  3 successful `classify()` calls and the 4th raises, `run_stage`'s exception handler
  (`run.py:92-96`) still flushes the 3 already-accumulated `LlmCallUsage` entries before re-raising and
  calling `fail_run`.
- **A response Azure OpenAI returns but this app can't use still bills** (decision 4). E.g.
  `extractor.py`'s JSON-decode failure path (`json.JSONDecodeError` → `{}`) still recorded a real
  `chat.completions.create()` response with real `resp.usage` — the usage record is captured before that
  suppression/fallback logic discards the content.
  `AzureOcrClient.analyze()` — no response, no page count, no row; `StageRun.error` already captures the
  failure via the existing `fail_run` path.
- **`/ask` costs with no attributable document** are recorded with `doc_id = NULL`, appear in corpus-wide
  `total_cost_inr` only, never in a per-document breakdown or a per-document total (decision 3).
- **A document with zero usage events** (e.g. never OCR'd, or OCR'd but never sent through `semantic`)
  returns a `DocumentCostView` with all-zero totals — not a 404 or an error; the document itself still
  exists.
- **Pricing settings change after events were recorded**: totals recompute automatically on next read
  (decision 2) — this is intentional, not a bug to guard against.

## 5. Testing

- **Unit, pure** (`backend/tests/unit/test_pricing.py`): `ocr_cost_inr`/`llm_cost_inr` — zero units → ₹0;
  known page count / token counts → exact expected ₹ figure against the default settings (hand-computed);
  fractional-token-count rounding behavior at small values.
- **Unit** (`backend/tests/unit/test_config.py` extension): the four new `FDI_*` defaults.
- **Integration** (testcontainers Postgres+MinIO, `FakeOcrClient`/`FakeSemanticClient` extended to return
  `LlmCallUsage`/deterministic page counts):
  - Drive a document through `ocr` → assert exactly one `ocr_usage_event` row with the fake's page count;
    re-trigger `ocr` on the same document (cache hit) → assert still exactly one row (no double-bill).
  - Drive `semantic` over a document with N AST sections → assert exactly N `llm_usage_event` rows,
    `source = "semantic_classify"`, `stage_run_id` matching that stage's `StageRun.id`.
  - `GET /documents/{doc_id}/cost` after both stages ran → correct aggregated totals and ₹ conversion
    against the configured (test-overridden) pricing settings.
  - `GET /analytics` → `total_cost_inr` matches the sum of all documents' costs plus any untethered
    `/ask` cost in the same test corpus.
  - A `FakeSemanticClient` configured to raise on its Nth call → assert the first N-1 calls' usage rows
    still exist after the stage's `StageRun` ends up `failed` (decision 4/9 proof).
  - `POST /ask` (agent loop, `FakeAgentClient`) → assert `llm_usage_event` rows with `doc_id = NULL`,
    `source = "ask_agent"`, one per round actually executed.
  - `POST /extract/document` → assert one `llm_usage_event` row per unit actually extracted,
    `doc_id` = the resolved document, `stage_run_id = NULL`.

## 6. Explicitly deferred (YAGNI)

- **Raw per-call event listing endpoint** — storage supports it (decision 5), but no `GET
  /documents/{doc_id}/cost/events` is built now; add if drill-down is actually needed.
- **Per-model pricing table** — a single global `llm_input_cost_per_mtok_usd`/`..._output_...` applies
  regardless of which `model`/`deployment` string an event recorded. Fine while only one Azure OpenAI
  deployment is configured (`FDI_GPT_DEPLOYMENT`); a multi-model pricing table is a clean follow-on if
  that changes.
- **Live/fetched FX rate** — `FDI_USD_TO_INR_RATE` is a static configured value (decision 6), not fetched
  from any FX API. Manual updates only.
- **Budget alerts / cost caps** — no threshold-exceeded warnings or hard stops are built; this feature is
  tracking and reporting only.
- **Time-series cost trend (daily/weekly)** — `created_at` on both tables makes this a straightforward
  follow-on query later; not built now since it wasn't asked for.
- **Multi-tenant cost attribution** — no `User`/`Tenant` concept exists anywhere in this codebase; out of
  scope by construction.

## 7. Done criteria

1. Migration `0008_usage_events.py` creates `ocr_usage_event` and `llm_usage_event` per §3.1, with a
   working `downgrade()`, mirrored exactly in `models.py`.
2. The four new `FDI_*` pricing/FX settings exist with the specified defaults, mirrored across
   `config.py`, `docker/compose.yml`'s `x-backend-env`, and `.env.example` (inert-config discipline).
3. `fdi.core.pricing` provides pure `ocr_cost_inr`/`llm_cost_inr`; unit-tested against hand-computed
   values including zero-usage.
4. OCR: exactly one `ocr_usage_event` row is written per real (cache-miss) `ocr` stage execution, never on
   a cache hit; page count matches the artifact's `pages` length.
5. LLM: `AgentResponse`, `SemanticClient.classify`, and `ExtractorClient.extract` all surface per-call
   `LlmCallUsage`; `semantic` stage executions write one `llm_usage_event` row per AST section processed,
   correctly attributed to their `StageRun`, including the calls made before a mid-loop failure.
6. `/ask` and `/extract/document` each write one `llm_usage_event` row per real LLM call they make, with
   `stage_run_id = NULL`; `/ask`'s rows also have `doc_id = NULL`.
7. `GET /documents/{doc_id}/cost` returns correct aggregated totals (pages, tokens, calls, duration, ₹) by
   source for a document with a mix of ocr/semantic/extract_document usage.
8. `GET /analytics` exposes a corpus-wide `total_cost_inr` and a per-stage `cost_inr`, both correct against
   a known test corpus including untethered `/ask` cost.
9. `GET /documents/{doc_id}/stages` is unchanged (no cost fields added, no new queries on that path).
10. Frontend: ingest page shows a document's running/final cost; analytics page shows a corpus total-cost
    tile and a per-stage cost column; `frontend/lib/types.ts` mirrors the new backend views exactly.
11. All static gates green (`ruff`, `mypy --strict src`, `lint-imports`, full non-e2e suite); no new Stage
    is registered (OCR/LLM instrumentation lives inside `run_stage` and the platform clients, not as a new
    pipeline stage).
