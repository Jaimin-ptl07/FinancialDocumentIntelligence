# SP-18 — Thin UI (document journey)

- **Status:** Approved (autonomously, per standing mandate) 2026-07-17. Not yet implemented.
- **Depends on:** SP-1 (Next.js shell + CORS), SP-4 (intake), SP-5 (OCR trigger), SP-6/8 (normalize/ast artifacts).
- **Roadmap:** build order §4 — SP-18 (thin UI) completes the demoable Stage-1 spine.

## 1. Purpose

Show a document's journey through the pipeline in the browser: upload a PDF, run OCR, watch the stages
(`ocr → normalize → ast`) settle, and render the resulting AST. This makes the whole Stage-1 spine
demoable. It stays THIN — the SP-1 shell's aesthetic (a page, buttons, `<pre>` JSON), not a product UI.

## 2. Scope decisions (autonomous; documented)

1. **The API proxies artifact bodies** (`GET /documents/{id}/artifacts/{stage}`) rather than handing the
   browser a presigned MinIO URL. Reason: a browser fetching `localhost:9000` cross-origin would need MinIO
   CORS config; proxying through the already-CORS'd API (`localhost:8000`) is simpler and keeps the UI to one
   origin. (Presigned URLs remain available via `/jobs` for direct downloads; the UI uses the proxy for
   inline rendering.)
2. **A per-document stages endpoint** (`GET /documents/{id}/stages`) returns the latest run per stage
   (name/status/error) so the UI can render pipeline progress without knowing the stage list.
3. **Frontend verification is build + lint only in this environment.** The live browser round-trip needs the
   api/worker Docker image rebuilt (ghcr unreachable) AND Azure creds AND a running worker — all unavailable
   here. So SP-18 proves the backend endpoints via testcontainers and the frontend via `next build`/`lint`;
   the live click-through is deferred with a clear note (as SP-1's UI note did).

## 3. Architecture

### 3.1 Backend (api)

- `RegistryRepository.latest_runs(doc_id) -> dict[str, StageRun]` — the latest `stage_run` per `stage_name`
  for a document (any status).
- `GET /documents/{doc_id}/stages` → `{"stages": [{"name": str, "status": str, "error": str | None}]}`
  (sorted by name; 404 if the document is unknown).
- `GET /documents/{doc_id}/artifacts/{stage}` → the artifact JSON body (`store.get_json(artifact_key)`), or
  404 if the document/stage has no completed artifact.

### 3.2 Frontend (Next.js, extends `frontend/app/page.tsx`)

- File input → **Upload** → `POST /documents` (multipart) → show `{id, checksum, version}`.
- **Run OCR** button → `POST /documents/{id}/ocr` (202).
- Poll `GET /documents/{id}/stages` every 1s; render each stage's name + status.
- When `ast` is `done`, fetch `GET /documents/{id}/artifacts/ast` and render `title` + `sections`
  (`heading` + `paragraphs`) — plus a raw `<pre>` fallback.
- `API` base from `NEXT_PUBLIC_API_URL` (SP-1). Cross-origin is covered by the SP-1/SP-5 CORS allowlist.

## 4. Error handling

- Unknown document → 404 on both endpoints.
- A stage with no completed run → `/artifacts/{stage}` 404; the UI shows "not ready".
- OCR unconfigured (no Azure) or worker not running → stages stay `queued`/`running`/`failed`; the UI shows
  the status + any recorded error. (This is the expected state in an env without Azure + a worker.)

## 5. Testing

- **Integration** (testcontainers): seed a document + `stage_run`s (ocr done with an artifact, normalize
  queued), then `GET /documents/{id}/stages` returns both with correct statuses; `GET
  /documents/{id}/artifacts/ocr` returns the stored artifact body; unknown doc/stage → 404.
- **Frontend:** `pnpm run lint` + `pnpm run build` (via `corepack pnpm`) pass. (No live round-trip — deferred.)

## 6. Explicitly deferred (YAGNI)

- Live browser round-trip of the real pipeline → needs the rebuilt image (ghcr) + Azure creds + a worker.
- Auth, multi-document dashboard, artifact diffing, styling beyond the thin shell → later.
- Rendering tables/figures/positions → when SP-7 enriches them.

## 7. Done criteria

1. `GET /documents/{id}/stages` returns the latest run per stage (name/status/error); 404 for unknown doc.
2. `GET /documents/{id}/artifacts/{stage}` returns the stored artifact body; 404 when absent.
3. `frontend/app/page.tsx` uploads a PDF, triggers OCR, polls stages, and renders the AST (title/sections);
   `next build` + `lint` pass.
4. Backend gates green (ruff/mypy/lint-imports/full suite); frontend build+lint green. Live round-trip
   deferred with a note (ghcr + Azure).
