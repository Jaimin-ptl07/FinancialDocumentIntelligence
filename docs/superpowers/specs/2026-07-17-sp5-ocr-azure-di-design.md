# SP-5 — OCR & Layout Extraction via Azure Document Intelligence

- **Status:** Approved (autonomously, per standing mandate) 2026-07-17. Not yet implemented.
- **Depends on:** SP-2 (stage framework), SP-4 (intake — provides the checksummed PDF at `documents/{checksum}.pdf`).
- **Research:** `.../tasks/wud6997io.output` (deep-research 2026-07-17, 25 claims verified 3-0 from MS Learn + azure-sdk-for-python). Key facts in memory `external-services-azure` and roadmap §7.

## 1. Purpose

Turn an intake'd PDF into a structured OCR artifact — text, reading order, tables, paragraphs with roles,
selection marks, figures, and hierarchical sections — by running **Azure Document Intelligence**
(`prebuilt-layout`, v4.0 GA `api_version=2024-11-30`) as the first real pipeline **stage** over a document.
Every later layer (normalize, structural intelligence, AST) consumes this artifact instead of the PDF.

Thin vertical slice: the OCR stage + a mockable Azure client + config wiring + an explicit trigger, proven
with a **fake DI client** in the test suite (no live Azure, no cost) plus an **opt-in real-DI smoke test**.

## 2. Scope decisions (made autonomously, grounded in the verified research)

1. **Model = `prebuilt-layout`, `api_version=2024-11-30`** (research: the recommended default for
   table/layout-heavy reports; `prebuilt-read` omits tables/roles/marks; `prebuilt-document` is retired).
   Both are configurable (`FDI_AZURE_DI_MODEL`, `FDI_AZURE_DI_API_VERSION`) so SP-3's harness can later
   measure `read` vs `layout` candidates.
2. **SDK = `azure-ai-documentintelligence` (1.0.2)**, sync client, `begin_analyze_document(model, body=pdf_bytes).result()`.
3. **OCR is a `Stage`** (`name="ocr"`, `deps=()` — a root stage over the document source). It slots into the
   SP-2 framework so reprocessing, versioning, and the DAG all apply for free.
4. **The Azure client is an injected `OcrClient` protocol**, so the suite runs against a `FakeOcrClient`
   (canned `AnalyzeResult`-shaped dict) with no network, and a real `AzureOcrClient` (SDK) is used in
   production + an opt-in smoke test. This is the SP-3 lesson applied: the engine is a swappable candidate.
5. **Determinism exception (Global Constraint 6), decided explicitly:** Azure DI output is NOT byte-stable
   (confidence, polygons, ordering can drift; research §caveats). So the OCR artifact is **content-addressed
   on (PDF checksum + DI model + api_version), NOT on response bytes** — the SP-2 root-stage `input_hash`
   already = `sha256(document.checksum)`, and the DI **model + api_version are folded into the OcrStage
   `version`** (bump it when either changes). The framework caches the FIRST successful OCR at that key and
   does not re-run; a deliberate reprocess or a version bump regenerates. Re-OCR is expensive and
   non-deterministic, so "run once, cache" is correct, not a compromise.
6. **The stored artifact is a NORMALIZED subset**, not the raw SDK object: a stable JSON
   `{schema_version, model, api_version, content, pages[], paragraphs[], tables[]}` extracted from
   `AnalyzeResult`. Downstream (SP-6 normalize) depends on this shape, not on Azure's wire format, so an
   Azure schema change is absorbed in one mapper. Canonical JSON via SP-1 `canonical_json`.
7. **Config, not secrets in repo:** `FDI_AZURE_DI_ENDPOINT` / `FDI_AZURE_DI_KEY` are `str | None = None`
   (the stack boots without Azure; OCR raises a clear error if unconfigured). Operator copies real values
   from the sibling `D:\FinancialDocIntelligence\.env` into the gitignored `.env`.

## 3. Architecture

### 3.1 Minimal stage-framework extension (SP-2)

OCR is the first stage that needs the raw source PDF and an external service — neither is in today's
`StageContext(doc_id, read_artifact)`. Extend it, generically (so future stages benefit):

- `StageContext.read_source() -> bytes` — reads the document's source PDF (`documents/{checksum}.pdf`). Root
  stages use it; derived stages keep using `read(upstream)`.
- `StageContext.services: Mapping[str, object]` — a name→service bag the orchestration fills (OCR pulls
  `services["ocr"]` and casts to `OcrClient`). Keeps the framework generic; no OCR-specific field.
- `run_stage(...)` gains a `services: Mapping[str, object] = {}` param and builds the `read_source` closure
  from the store + the document's `checksum` (looked up via `DocumentRepository`). A root stage over a
  checksum-less (ping) document has no source; `read_source` raises if called with no checksum.
- `_run_stage_async` (workers) builds the `services` dict — including the real `AzureOcrClient` from Settings
  when Azure is configured — and passes it to `run_stage`.

### 3.2 `OcrClient` (protocol + two implementations)

- **`fdi.pipeline.ocr.OcrClient`** (Protocol): `def analyze(self, pdf: bytes) -> Mapping[str, object]` —
  returns an `AnalyzeResult`-shaped dict (already `.as_dict()`-ed, JSON-able). Pure interface.
- **`fdi.platform.ocr.AzureOcrClient`** (I/O — lives in `platform`): wraps the SDK; constructed with
  endpoint/key/model/api_version/timeout from Settings; `analyze` calls `begin_analyze_document(model,
  body=pdf).result().as_dict()`.
- **`FakeOcrClient`** (test helper): returns a canned dict; records the pdf it was given.

### 3.3 `OcrStage` (`fdi.pipeline.stages.ocr`)

```python
class OcrStage:
    name = "ocr"
    version = 1   # BUMP when FDI_AZURE_DI_MODEL or api_version changes (they define the output)
    deps: tuple[str, ...] = ()
    async def run(self, ctx: StageContext) -> object:
        pdf = await ctx.read_source()
        client = cast(OcrClient, ctx.services["ocr"])
        raw = await asyncio.to_thread(client.analyze, pdf)   # SDK is sync; off the loop
        return normalize_ocr(raw)   # -> {schema_version, model, api_version, content, pages, paragraphs, tables}
```
Self-registers into `REGISTRY`. `normalize_ocr(raw)` (pure, `fdi.core` or `fdi.pipeline`) extracts the stable
subset from the `AnalyzeResult` dict.

### 3.4 Config (Settings + x-backend-env + .env.example)

New `FDI_` fields (all added to Settings AND compose `x-backend-env` AND `.env.example` — the inert-config
lesson): `FDI_AZURE_DI_ENDPOINT: str | None = None`, `FDI_AZURE_DI_KEY: str | None = None`,
`FDI_AZURE_DI_MODEL: str = "prebuilt-layout"`, `FDI_AZURE_DI_API_VERSION: str = "2024-11-30"`,
`FDI_AZURE_DI_POLLING_TIMEOUT_S: int = 600`. `.env.example` carries placeholders (`FDI_AZURE_DI_ENDPOINT=`,
`FDI_AZURE_DI_KEY=`) — the `str | None` type makes empty→None legal, so no boot crash.

### 3.5 Trigger

`POST /documents/{id}/ocr` → 202: dispatch `run_stage_task(doc_id, "ocr", correlation_id)`. (Auto-dispatch
on upload is deferred; explicit trigger keeps intake and OCR decoupled for the slice.) `GET /jobs/{doc_id}`
already reports the run; extend it or add `GET /documents/{id}/artifacts/ocr` for the OCR artifact URL.

### 3.6 Dependency

Add `azure-ai-documentintelligence` (1.0.x) to `backend/pyproject.toml`.

## 4. Error handling

- Azure unconfigured (`endpoint`/`key` None) when OCR runs → the stage fails with a clear message; the
  `run_stage` fail-path records `stage_run(failed, error=...)` (SP-2). No partial artifact.
- Azure API error / timeout → the SDK raises; `run_stage` records the failure; retryable per Celery.
- OCR over a checksum-less (ping) document → `read_source` raises "document has no source" (OCR is only for
  intake'd documents).
- `normalize_ocr` on an unexpected AnalyzeResult shape → defensive `.get(...)` with a schema_version bump if
  the mapping changes; a missing `content` is a hard error (the artifact would be useless).

## 5. Testing

- **Unit** (no Docker, no Azure): `normalize_ocr` maps a canned `AnalyzeResult` dict → the normalized artifact
  (content, pages count, paragraphs with roles, tables with row/col counts); missing `content` raises.
- **Integration** (testcontainers, `FakeOcrClient` — NO live Azure): intake a PDF (SP-4), dispatch/run the
  OCR stage with `services={"ocr": FakeOcrClient(canned)}`, assert a `done` stage_run + a stored OCR artifact
  at the content-addressed key + the normalized body; a second run is a cache hit (Azure NOT called twice —
  proven by a call counter on the fake).
- **Opt-in real-DI smoke test** (`@pytest.mark.azure`, skipped unless `FDI_AZURE_DI_ENDPOINT`/`KEY` set AND a
  sample PDF fixture exists): constructs the real `AzureOcrClient`, analyzes a small real PDF, asserts a
  non-empty `content` and ≥1 page. NEVER in CI (no creds, no cost).
- **API** (integration): `POST /documents/{id}/ocr` → 202; after the (faked) worker runs, the OCR artifact is
  fetchable.
- Config consistency: `FDI_AZURE_DI_*` present in Settings + compose + `.env.example`.

## 6. Explicitly deferred (YAGNI)

- Normalization into the unified internal schema → SP-6 (this ships the raw-ish normalized OCR subset).
- Batch analysis (`begin_analyze_batch_documents`) for corpus-scale → when the 5000-doc run is real (SP-13/14).
- Structural intelligence (reading-order correction, header/footer removal, table stitching) → SP-7.
- `features=keyValuePairs` / high-res OCR / query fields add-ons → when a downstream layer needs them.
- Cost controls / rate limiting / async SDK client → when corpus-scale ingestion arrives.

## 7. Done criteria

1. `OcrStage` (`name="ocr"`, `version=1`, `deps=()`) is registered; `run(ctx)` reads the source PDF, calls the
   injected `OcrClient`, and returns the normalized artifact.
2. `normalize_ocr` extracts `{schema_version, model, api_version, content, pages, paragraphs, tables}` from an
   `AnalyzeResult` dict; a missing `content` raises.
3. Running OCR over an intake'd document stores the artifact content-addressed on (checksum + OcrStage
   version) and records a `done` stage_run; a second run is a store cache hit that does NOT call Azure again.
4. `StageContext.read_source()` returns the document's PDF bytes; a checksum-less document raises.
5. `FDI_AZURE_DI_*` config is wired (Settings + compose + `.env.example`); the stack boots with Azure
   unconfigured (endpoint/key None), and OCR then fails with a clear, recorded error.
6. `POST /documents/{id}/ocr` dispatches the OCR stage (202).
7. The opt-in real-DI smoke test exists and skips cleanly when Azure is unconfigured.
8. All four static gates green; `core` stays pure (`normalize_ocr` is pure); full suite passes with NO live
   Azure call (fake client only).
