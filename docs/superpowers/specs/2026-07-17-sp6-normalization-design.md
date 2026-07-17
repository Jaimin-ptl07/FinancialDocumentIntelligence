# SP-6 — Normalization (vendor OCR → unified internal schema)

- **Status:** Implemented 2026-07-17 on `feature/sp-1-project-setup`. All §7 done criteria verified (unit + testcontainers + static gates; full suite 174 passed, 4 skipped; ruff / mypy --strict / lint-imports 1 kept 0 broken).
- **Depends on:** SP-5 (OCR artifact), SP-2 (stage framework — this is the first DERIVED stage).
- **Roadmap:** phase 3 "Normalization: vendor output → internal unified schema".

## 1. Purpose

Map the OCR artifact (Azure-DI-shaped, from SP-5) into the project's **unified internal document schema** —
a vendor-neutral representation every downstream layer (SP-7 structural, SP-8 AST, SP-10 semantic) commits
to. This is the decoupling seam: swap the OCR engine (SP-3 measures candidates) and the unified schema
stays constant, so nothing downstream of Normalize depends on Azure's field names or role vocabulary.

Thin vertical slice: a pure `normalize_document` mapping + a `NormalizeStage` (`deps=("ocr",)`) that chains
off OCR automatically via the SP-2 DAG. Text blocks with **canonical roles** are the payload; table cell
contents and positional geometry are deferred to SP-7 (they need richer OCR data than SP-5 currently keeps).

## 2. Scope decisions (autonomous; documented)

1. **The unified schema is the stable contract**, owned by `fdi.core`. Downstream code imports the unified
   types, never the OCR artifact shape. Version it (`schema_version`) so it can evolve deliberately.
2. **Canonical roles, not Azure roles.** Azure's `title/sectionHeading/pageHeader/pageFooter/pageNumber/
   footnote/(none)` map to a project `Role` enum `title/heading/header/footer/page_number/footnote/body`.
   Role-less paragraphs become `body`. Downstream classifies on the canonical enum.
3. **`NormalizeStage` is a DERIVED stage** (`name="normalize"`, `version=1`, `deps=("ocr",)`). It reads the
   OCR artifact via `ctx.read("ocr")` and returns the unified document. Because it depends on `ocr`, the
   SP-2 resolver makes it a successor — `run_stage("ocr")` auto-enqueues `normalize`. No new trigger needed.
4. **Blocks over the full text.** The unified doc carries `content` (the full text, for search/embeddings)
   plus ordered `blocks` (`{role, text}`) from OCR paragraphs. Determinism holds (pure function of the OCR
   artifact; SP-1 `canonical_json`).
5. **Deferred, honestly:** table cell text + positional bboxes + reading-order correction need OCR data
   SP-5's `normalize_ocr` did not keep (it kept counts, not cells/words). Enriching the OCR artifact (and
   bumping `OcrStage.version`) is SP-7's concern, where structural intelligence needs geometry. SP-6 ships
   the text-block contract; tables appear as counts only, flagged.

## 3. Architecture

### 3.1 Layering

- **`fdi.core.unified`** — the `Role` enum (`StrEnum`), `canonical_role(azure_role: str | None) -> Role`,
  and `normalize_document(ocr: Mapping[str, object]) -> dict[str, object]` (pure; no I/O).
- **`fdi.pipeline.stages.normalize`** — `NormalizeStage`, self-registered into `REGISTRY`.

### 3.2 Unified schema

```json
{
  "schema_version": 1,
  "content": "<full text>",
  "page_count": 12,
  "blocks": [
    {"role": "title", "text": "ACME LTD Annual Report"},
    {"role": "heading", "text": "Directors' Report"},
    {"role": "body", "text": "The board is pleased ..."}
  ]
}
```
`normalize_document(ocr)`: `content = ocr["content"]` (missing → `ValueError`); `page_count = len(ocr.get("pages", []))`; `blocks = [{"role": canonical_role(p.get("role")), "text": p["content"]} for p in ocr.get("paragraphs", [])]`.

### 3.3 `NormalizeStage`

```python
class NormalizeStage:
    name = "normalize"
    version = 1
    deps = ("ocr",)
    async def run(self, ctx: StageContext) -> object:
        ocr = await ctx.read("ocr")           # the OCR artifact body (a dict)
        return normalize_document(ocr)
```
Reads no external service; `deps=("ocr",)` makes the framework schedule it after OCR and hash its
`input_hash` from OCR's `output_hash` (SP-2). `ctx.read("ocr")` returns the OCR artifact body as loaded from
the store — a `Mapping`; `normalize_document` accepts it.

## 4. Error handling

- OCR artifact missing `content` → `normalize_document` raises `ValueError`; `run_stage` records a failed
  `stage_run` (SP-2). No partial artifact.
- Running `normalize` before `ocr` has a recorded artifact → `ctx.read("ocr")` (via `latest_run`) asserts the
  upstream run exists; absent, it fails clearly (the DAG normally guarantees order, but a manual out-of-order
  dispatch fails loudly rather than silently).
- An OCR `role` value outside the known Azure set → `canonical_role` returns `body` (unknown roles are
  content, not a crash).

## 5. Testing

- **Unit** (no Docker): `canonical_role` maps every Azure role + None + unknown → the right `Role`;
  `normalize_document` produces the unified doc from a canned OCR artifact (content, page_count, blocks with
  canonical roles); missing `content` raises.
- **Integration** (testcontainers, fake OCR): intake a doc, run `ocr` (FakeOcrClient) then `normalize` via
  `run_stage` (both stages in a local registry), assert the normalize artifact is the unified doc and its
  `input_hash` derives from the OCR `output_hash` (a rerun is a cache hit). Prove the resolver makes
  `normalize` a successor of `ocr` (`DagResolver(REGISTRY.all()).successors("ocr")` contains `"normalize"`).

## 6. Explicitly deferred (YAGNI)

- Table cell contents + positional geometry + reading-order correction → SP-7 (needs richer OCR; bump
  `OcrStage.version` there to keep words/cells/bboxes).
- Language detection, section hierarchy, cross-page block stitching → SP-7/SP-8.
- A UI for the unified doc → later.

## 7. Done criteria

1. `Role` enum + `canonical_role` map all Azure roles (+ None + unknown) to the canonical set.
2. `normalize_document(ocr)` returns `{schema_version, content, page_count, blocks[{role,text}]}`; missing
   `content` raises.
3. `NormalizeStage` (`deps=("ocr",)`) reads the OCR artifact and produces the unified doc; a rerun is a cache
   hit; its `input_hash` derives from the OCR `output_hash`.
4. `DagResolver(REGISTRY.all()).successors("ocr")` includes `"normalize"` (the chain is automatic).
5. All four static gates green; `core` stays pure; full suite passes.
