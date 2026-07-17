# SP-5 — OCR via Azure Document Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Run Azure Document Intelligence (`prebuilt-layout`) as the first real pipeline stage over an intake'd PDF, producing a normalized OCR artifact — proven end to end with a FAKE DI client (no live Azure), plus an opt-in real-DI smoke test.

**Architecture:** An `OcrStage` (SP-2 root stage) reads the document's source PDF via an extended `StageContext.read_source()`, calls an injected `OcrClient` (fake in tests, `AzureOcrClient` in prod), and returns `normalize_ocr(AnalyzeResult)`. Content-addressed on (PDF checksum + OcrStage version); DI output is non-deterministic so "run once, cache" is deliberate.

**Tech Stack:** Python 3.13, `azure-ai-documentintelligence` 1.0.x, FastAPI, Celery, SQLAlchemy, MinIO, Postgres, pytest + testcontainers, `uv`. Research: `.../tasks/wud6997io.output`; memory `external-services-azure`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` pure (`normalize_ocr` is pure — no SDK import). The `AzureOcrClient` (does I/O) lives in `platform`. The `OcrClient` protocol + `OcrStage` + `FakeOcrClient` live in `pipeline`.
- **Do not break SP-2:** the `StageContext`/`run_stage` extensions MUST be backward-compatible — the existing ping tests and every `run_stage` caller keep working (new params default to empty; `read_source` only raises when actually called on a checksum-less document).
- Determinism exception: OCR artifacts are content-addressed on (checksum + OcrStage `version`), NOT response bytes (DI is non-deterministic). A second OCR run on the same doc is a store cache hit that does NOT call Azure — prove it with a call counter on the fake.
- **No live Azure in the suite:** all unit/integration tests use `FakeOcrClient`. The real-DI test is `@pytest.mark.azure`, skipped unless `FDI_AZURE_DI_ENDPOINT`/`KEY` are set.
- Any new `FDI_` field → Settings AND compose `x-backend-env` AND `.env.example` (inert-config lesson). Azure endpoint/key are `str | None = None` so empty→None boots cleanly.
- Integration tests: reuse `session_factory` + `store` fixtures; mint unique ids/checksums per test (session-scoped DB).
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.

**Canonical commands:** fast `pytest -m "not integration and not e2e" -q`; integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `normalize_ocr` (core, pure)

**Files:** Create `backend/src/fdi/core/ocr_schema.py`; Test `backend/tests/unit/test_ocr_schema.py`.

**Interfaces:** `normalize_ocr(raw: Mapping[str, object]) -> dict[str, object]` — from an `AnalyzeResult`-shaped dict returns `{"schema_version": 1, "content": str, "pages": [{"page_number", "width", "height", "unit", "line_count", "word_count"}], "paragraphs": [{"role": str|None, "content": str}], "tables": [{"row_count", "column_count", "cell_count"}]}`. Missing `content` → `ValueError`. Pure; no SDK import.

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_ocr_schema.py
import pytest
from fdi.core.ocr_schema import normalize_ocr

_RAW = {
    "content": "ACME LTD\nRevenue 100",
    "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch",
               "lines": [{"content": "ACME LTD"}, {"content": "Revenue 100"}],
               "words": [{"content": "ACME"}, {"content": "LTD"}, {"content": "Revenue"}, {"content": "100"}]}],
    "paragraphs": [{"role": "title", "content": "ACME LTD"}, {"content": "Revenue 100"}],
    "tables": [{"rowCount": 2, "columnCount": 2, "cells": [{}, {}, {}, {}]}],
}


def test_normalize_extracts_stable_subset() -> None:
    out = normalize_ocr(_RAW)
    assert out["schema_version"] == 1
    assert out["content"] == "ACME LTD\nRevenue 100"
    assert out["pages"] == [{"page_number": 1, "width": 8.5, "height": 11.0, "unit": "inch",
                             "line_count": 2, "word_count": 4}]
    assert out["paragraphs"] == [{"role": "title", "content": "ACME LTD"},
                                 {"role": None, "content": "Revenue 100"}]
    assert out["tables"] == [{"row_count": 2, "column_count": 2, "cell_count": 4}]


def test_missing_content_raises() -> None:
    with pytest.raises(ValueError, match="content"):
        normalize_ocr({"pages": []})
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — pure dict-walking with `.get(...)` defaults (AnalyzeResult uses camelCase keys: `pageNumber`, `rowCount`, `columnCount`). Raise `ValueError("AnalyzeResult has no content")` if `content` absent. `paragraphs[].role` defaults to `None`.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/ocr_schema.py backend/tests/unit/test_ocr_schema.py && git commit -m "feat(core): normalize_ocr — stable subset of AnalyzeResult"`

---

### Task 2: `OcrClient` protocol + `FakeOcrClient` (pipeline)

**Files:** Create `backend/src/fdi/pipeline/ocr.py`; Test `backend/tests/unit/test_ocr_client.py`.

**Interfaces:** `class OcrClient(Protocol)` with `def analyze(self, pdf: bytes) -> Mapping[str, object]`. `class FakeOcrClient` (`__init__(self, result: Mapping[str, object])`; `analyze` records `self.calls += 1`, `self.last_pdf = pdf`, returns `result`).

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_ocr_client.py
from fdi.pipeline.ocr import FakeOcrClient, OcrClient


def test_fake_records_and_returns() -> None:
    fake = FakeOcrClient({"content": "x"})
    client: OcrClient = fake
    assert client.analyze(b"PDF") == {"content": "x"}
    assert fake.calls == 1 and fake.last_pdf == b"PDF"
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — `Protocol` + `runtime_checkable`; `FakeOcrClient` with `calls`/`last_pdf`.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/ocr.py backend/tests/unit/test_ocr_client.py && git commit -m "feat(pipeline): OcrClient protocol + FakeOcrClient"`

---

### Task 3: Extend `StageContext` + `run_stage` (read_source + services) — backward-compatible

**Files:** Modify `backend/src/fdi/pipeline/stage.py`, `backend/src/fdi/pipeline/run.py`; Test `backend/tests/integration/test_run_stage.py` (ADD; existing tests must still pass).

**Interfaces:** `StageContext` gains `read_source: Callable[[], Awaitable[bytes]]` and `services: Mapping[str, object]` (both with safe defaults — `read_source` default raises "no source", `services` default `{}`). `run_stage(...)` gains `services: Mapping[str, object] = {}`; it builds a `read_source` closure that loads `documents/{checksum}.pdf` via the store (looking up the document's `checksum` through `DocumentRepository`; if `checksum` is None, `read_source` raises `ValueError("document {id} has no source")`). The `StageContext` passed to `stage.run` carries both.

- [ ] **Step 1: Read the current code** — `stage.py` (`StageContext` dataclass) and `run.py` (`run_stage`, where `StageContext(doc_id=..., read_artifact=...)` is built and where the `Document` is already fetched for the root `input_hash`). Extend, do not rewrite.

- [ ] **Step 2: Failing test** (ADD to `test_run_stage.py`) — a stage whose `run` calls `ctx.read_source()` and `ctx.services["x"]`:
```python
async def test_read_source_and_services_reach_the_stage(session_factory, store) -> None:
    import uuid
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stage import StageContext
    from fdi.platform.db.repository import DocumentRepository

    doc = str(uuid.uuid4()); checksum = str(uuid.uuid4())
    await store.put_bytes(f"documents/{checksum}.pdf", b"PDFBYTES", "application/pdf")
    async with session_factory() as s:
        await DocumentRepository(s).create(id=doc, checksum=checksum, filename="a.pdf",
            byte_size=8, content_type="application/pdf", doc_key=doc, version=1)
        await s.commit()

    class _Probe:
        name = "probe"; version = 1; deps: tuple[str, ...] = ()
        async def run(self, ctx: StageContext) -> object:
            return {"src": (await ctx.read_source()).decode(), "svc": ctx.services["greet"]}

    reg = StageRegistry(); reg.register(_Probe()); resolver = DagResolver(reg.all())
    await run_stage(doc_id=doc, stage_name="probe", run_id=str(uuid.uuid4()), correlation_id="c1",
                    session_factory=session_factory, store=store, registry=reg, resolver=resolver,
                    services={"greet": "hi"})
    key = f"artifacts/{doc}/probe/v1/"  # prefix; body carries the source + service
    # fetch the artifact and assert its body
    from fdi.pipeline.keys import artifact_key, root_input_hash
    body = await store.get_json(artifact_key(doc, "probe", 1, root_input_hash(checksum.encode())))
    assert body == {"src": "PDFBYTES", "svc": "hi"}
```
(Also keep an existing ping/cache test in the file passing — do not modify those.)

- [ ] **Step 3: Run → fails** (`run_stage` has no `services`; `StageContext` has no `read_source`).
- [ ] **Step 4: Implement** — add the two fields to `StageContext` (defaults: `services` = `MappingProxyType({})` or `dict`; `read_source` default an async fn that raises). In `run_stage`, add the `services` param; build `read_source` = an async closure that reads the doc row's `checksum` (via `DocumentRepository(s).get(doc_id)`) and returns `await store.get_bytes(f"documents/{checksum}.pdf")`, raising if checksum is None; pass `read_source=...` + `services=services` into the `StageContext(...)`. Keep `read_artifact` untouched. Type `services: Mapping[str, object]`.
- [ ] **Step 5: Run → passes**; then the whole non-e2e suite (existing ping/run tests still green).
- [ ] **Step 6: Commit** — `git add backend/src/fdi/pipeline/stage.py backend/src/fdi/pipeline/run.py backend/tests/integration/test_run_stage.py && git commit -m "feat(pipeline): StageContext.read_source + services; run_stage services param"`

---

### Task 4: `FDI_AZURE_DI_*` config (Settings + compose + env)

**Files:** Modify `backend/src/fdi/platform/config.py`, `docker/compose.yml`, `.env.example`; Test `backend/tests/unit/test_config.py` (ADD if the file asserts field presence).

**Interfaces:** Settings gains `azure_di_endpoint: str | None = None`, `azure_di_key: str | None = None`, `azure_di_model: str = "prebuilt-layout"`, `azure_di_api_version: str = "2024-11-30"`, `azure_di_polling_timeout_s: int = 600`.

- [ ] **Step 1: Add fields** to `config.py` (place beside the other fields; the `str | None = None` ones make empty env legal). Add all five `FDI_AZURE_DI_*` to `docker/compose.yml`'s `x-backend-env` block, and to `.env.example` with placeholders: `FDI_AZURE_DI_ENDPOINT=`, `FDI_AZURE_DI_KEY=`, `FDI_AZURE_DI_MODEL=prebuilt-layout`, `FDI_AZURE_DI_API_VERSION=2024-11-30`, `FDI_AZURE_DI_POLLING_TIMEOUT_S=600`.
- [ ] **Step 2: Test** — a unit test that `get_settings()` (with the FDI_ isolation fixture) exposes `azure_di_model == "prebuilt-layout"` and `azure_di_endpoint is None` by default. Run → fails, implement (Step 1 already did), → passes.
- [ ] **Step 3: Gates + config consistency** — `ruff`, `mypy`, and confirm the five fields are in Settings + compose + `.env.example`.
- [ ] **Step 4: Commit** — `git add backend/src/fdi/platform/config.py docker/compose.yml .env.example backend/tests/unit/test_config.py && git commit -m "feat(config): FDI_AZURE_DI_* settings"`

---

### Task 5: `AzureOcrClient` (platform) + the `azure-ai-documentintelligence` dependency

**Files:** Add dep to `backend/pyproject.toml` (+ `uv.lock`); Create `backend/src/fdi/platform/ocr.py`; Test `backend/tests/unit/test_azure_ocr_client.py`.

**Interfaces:** `class AzureOcrClient` (implements `OcrClient`): `__init__(self, *, endpoint, key, model, api_version, polling_timeout_s)`; `analyze(self, pdf: bytes) -> Mapping[str, object]` constructs `DocumentIntelligenceClient(endpoint, AzureKeyCredential(key), api_version=api_version)`, calls `begin_analyze_document(model, body=pdf)`, `.result()`, returns `result.as_dict()`. Plus `from_settings(settings) -> AzureOcrClient | None` (None when endpoint/key unset).

- [ ] **Step 1: Add the dependency** — `uv add --directory backend azure-ai-documentintelligence`. Commit `pyproject.toml` + `uv.lock` with this task.
- [ ] **Step 2: Failing test** — since a real Azure call needs creds, the unit test MONKEYPATCHES the SDK: patch `fdi.platform.ocr.DocumentIntelligenceClient` with a fake whose `begin_analyze_document(...).result().as_dict()` returns a canned dict; assert `AzureOcrClient(...).analyze(b"pdf")` returns it and the client was called with `(model, body=b"pdf")`. Also `from_settings` returns None when endpoint is None.
```python
# sketch
def test_azure_client_calls_sdk(monkeypatch) -> None:
    from fdi.platform import ocr
    calls = {}
    class _Poller:
        def result(self): 
            class _R:  # noqa
                def as_dict(self): return {"content": "hi"}
            return _R()
    class _Client:
        def __init__(self, *a, **k): calls["init"] = (a, k)
        def begin_analyze_document(self, model, body): calls["analyze"] = (model, body); return _Poller()
    monkeypatch.setattr(ocr, "DocumentIntelligenceClient", _Client)
    monkeypatch.setattr(ocr, "AzureKeyCredential", lambda k: k)
    c = ocr.AzureOcrClient(endpoint="e", key="k", model="prebuilt-layout", api_version="2024-11-30", polling_timeout_s=600)
    assert c.analyze(b"pdf") == {"content": "hi"}
    assert calls["analyze"][0] == "prebuilt-layout"
```
- [ ] **Step 3: Run → fails** (ModuleNotFoundError / import).
- [ ] **Step 4: Implement** — import `DocumentIntelligenceClient` from `azure.ai.documentintelligence`, `AzureKeyCredential` from `azure.core.credentials` (module-level, so monkeypatch works). `analyze` builds the client, calls `begin_analyze_document(self._model, body=pdf)`, `.result()`, `return dict(result.as_dict())`. `from_settings` returns None if `endpoint`/`key` is None else the client.
- [ ] **Step 5: Run → passes**; `mypy --strict` may need `# type: ignore` for the untyped Azure SDK — scope it narrowly (the SDK ships no `py.typed`); confirm `lint-imports` keeps the contract (platform → external is fine; `ocr.py` must NOT import from `pipeline`).
- [ ] **Step 6: Commit** — `git add backend/pyproject.toml backend/uv.lock backend/src/fdi/platform/ocr.py backend/tests/unit/test_azure_ocr_client.py && git commit -m "feat(platform): AzureOcrClient over azure-ai-documentintelligence"`

---

### Task 6: `OcrStage` (pipeline/stages) — proven with FakeOcrClient

**Files:** Create `backend/src/fdi/pipeline/stages/ocr.py`; Modify `backend/src/fdi/pipeline/stages/__init__.py` (register it); Test `backend/tests/integration/test_ocr_stage.py`.

**Interfaces:** `class OcrStage` (`name="ocr"`, `version=1`, `deps=()`); `run(ctx)`: `pdf = await ctx.read_source()`; `client = cast(OcrClient, ctx.services["ocr"])`; `raw = await asyncio.to_thread(client.analyze, pdf)`; `return normalize_ocr(raw)`. Self-registers into `REGISTRY`.

- [ ] **Step 1: Failing test** (integration; intake a doc, run OCR via `run_stage` with a FakeOcrClient, assert artifact + cache-hit-does-not-call-Azure-twice)
```python
# backend/tests/integration/test_ocr_stage.py — sketch
async def test_ocr_stage_runs_and_caches(session_factory, store) -> None:
    import uuid
    from fdi.pipeline.candidates.uppercase import ...  # not needed
    from fdi.pipeline.intake import document_key, intake_document
    from fdi.pipeline.keys import artifact_key, root_input_hash
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stages.ocr import OcrStage

    doc = await intake_document(pdf=b"%PDF-1.7", filename="a.pdf", doc_key=str(uuid.uuid4()),
                                content_type="application/pdf", session_factory=session_factory, store=store)
    fake = FakeOcrClient({"content": "ACME", "pages": [{"pageNumber": 1, "lines": [], "words": []}],
                          "paragraphs": [], "tables": []})
    reg = StageRegistry(); reg.register(OcrStage()); resolver = DagResolver(reg.all())
    common = dict(doc_id=doc.id, stage_name="ocr", correlation_id="c1", session_factory=session_factory,
                  store=store, registry=reg, resolver=resolver, services={"ocr": fake})
    await run_stage(run_id=str(uuid.uuid4()), **common)
    await run_stage(run_id=str(uuid.uuid4()), **common)  # cache hit
    assert fake.calls == 1  # Azure NOT called twice
    key = artifact_key(doc.id, "ocr", 1, root_input_hash(doc.checksum.encode()))
    body = await store.get_json(key)
    assert body["content"] == "ACME" and body["schema_version"] == 1
```
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** the stage (+ register in `stages/__init__.py`: `from fdi.pipeline.stages import ocr as ocr  # noqa: F401`).
- [ ] **Step 4: Run → passes** (cache hit → `fake.calls == 1`); full non-e2e suite green.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages backend/tests/integration/test_ocr_stage.py && git commit -m "feat(pipeline): OcrStage over the intake'd PDF"`

---

### Task 7: Wire the OCR service into the worker + API trigger

**Files:** Modify `backend/src/fdi/workers/tasks.py` (build `services={"ocr": AzureOcrClient.from_settings(...)}` in `_run_stage_async`, pass to `run_stage`); `backend/src/fdi/api/routes.py` (`POST /documents/{id}/ocr`); Test `backend/tests/integration/test_api.py` (ADD).

**Interfaces:** `_run_stage_async` builds the services dict (OCR client from Settings; may be None → omit or include a clear-error client) and passes `services=` to `run_stage`. `POST /documents/{doc_id}/ocr` → 202: `dispatcher.dispatch(doc_id, "ocr", correlation_id)` (reuse `StageDispatcher`), 404 if the document is unknown.

- [ ] **Step 1: Read** `workers/tasks.py` `_run_stage_async` (SP-2/4) and `routes.py`.
- [ ] **Step 2: Failing test** — `POST /documents/{id}/ocr` on an intake'd doc → 202; unknown id → 404. (The worker isn't run in `test_api`; assert the 202 + that a stage was dispatched, mirroring the ping-job dispatch test.)
- [ ] **Step 3: Implement** — in `_run_stage_async`, after building `store`/`session_factory`, add `from fdi.platform.ocr import AzureOcrClient; oc = AzureOcrClient.from_settings(get_settings()); services = {"ocr": oc} if oc else {}` and pass `services=services` to `run_stage`. (Layering: `workers` may import `platform` — fine.) Add the route (guard with `DocumentRepository.get` → 404).
- [ ] **Step 4: Run → passes**; full non-e2e suite green; gates green.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/workers/tasks.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py && git commit -m "feat: wire Azure OCR client into the worker; POST /documents/{id}/ocr"`

---

### Task 8: Opt-in real-DI smoke test

**Files:** Create `backend/tests/azure/test_real_ocr.py`; register the `azure` marker in `backend/pyproject.toml`.

**Interfaces:** `@pytest.mark.azure`, skipped unless `FDI_AZURE_DI_ENDPOINT` and `FDI_AZURE_DI_KEY` env are set. Uses a tiny real PDF fixture (`backend/tests/azure/sample.pdf` — a 1-page PDF; generate a minimal one or commit a small public-domain sample). Builds `AzureOcrClient.from_settings(get_settings())`, analyzes the PDF, asserts `normalize_ocr(result)["content"]` is non-empty and `pages` has ≥1.

- [ ] **Step 1: Register the marker** — add `"azure: needs live Azure Document Intelligence credentials"` to `pyproject.toml` markers; update the README fast-command note if it enumerates markers (`-m "not integration and not e2e and not azure"` is the fully-offline fast loop).
- [ ] **Step 2: Write the test** with a module-level skip: `pytestmark = [pytest.mark.azure, pytest.mark.skipif(not (os.getenv("FDI_AZURE_DI_ENDPOINT") and os.getenv("FDI_AZURE_DI_KEY")), reason="Azure DI not configured")]`. Add a minimal `sample.pdf`.
- [ ] **Step 3: Run** `uv run --directory backend pytest tests/azure -rs` → 1 skipped (Azure unconfigured in this env). If the operator has creds set, it runs against real Azure. Confirm the honest skip.
- [ ] **Step 4: Commit** — `git add backend/tests/azure backend/pyproject.toml && git commit -m "test(azure): opt-in real Document Intelligence smoke test"`

---

### Task 9: Audit + docs

**Files:** SP-5 spec status; `docs/architecture/ingestion-roadmap.md` (SP-5 → done + handoff).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0 broken`; `core` pure — `normalize_ocr` has no SDK import; `AzureOcrClient` is in `platform`). Config consistency: `FDI_AZURE_DI_*` in Settings + compose + `.env.example`.
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip; NO live Azure call).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 OcrStage, 2 normalize_ocr, 3 content-address+cache-no-2nd-Azure-call, 4 read_source, 5 config boots without Azure, 6 POST trigger, 7 opt-in smoke skips, 8 gates) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-17.**
- [ ] **Step 5: Roadmap SP-5 → ✅ done** + handoff: OCR artifact = normalized `{schema_version, content, pages, paragraphs, tables}` at the content-addressed key; **SP-6 (Normalization) consumes it** and maps it into the unified internal schema; the OcrStage `version` encodes the DI model+api_version (bump on change); the `OcrClient` protocol means SP-3's harness can measure `read` vs `layout` (or non-Azure) OCR candidates.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-5 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1(normalize) T2(OcrClient) T3(read_source+services) T4(config) T5(AzureClient) T6(OcrStage+cache) T7(wire+trigger) T8(opt-in real). §3 architecture → all. §4 errors → T3 (no-source raise), T6 (fake), T7 (from_settings None). §5 testing → unit T1/T2/T4/T5, integration T3/T6/T7, azure T8. §7 done criteria → T9 audit.

**Type consistency:** `OcrClient.analyze(pdf: bytes) -> Mapping[str,object]` (T2) matches `FakeOcrClient` (T2), `AzureOcrClient` (T5), and `OcrStage` usage (T6). `StageContext.read_source()`/`services` (T3) match `OcrStage` (T6). `normalize_ocr` (T1) consumed in T6. `run_stage(..., services=)` (T3) matches T6/T7 calls. `AzureOcrClient.from_settings` (T5) matches T7.

**Placeholder scan:** T3/T5/T7 "read current code then extend" are precise-additions instructions (the exact additions are specified), not blanks. The T5 `# type: ignore` is scoped to the untyped Azure SDK with a reason. No TODO/TBD.
