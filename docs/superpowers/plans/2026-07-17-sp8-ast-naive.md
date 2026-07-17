# SP-8 — Document AST (naive) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build a naive `title → sections → paragraphs` tree from the unified document via a pure `build_ast` + an `AstStage` (`deps=("normalize",)`) that auto-chains off Normalize.

**Architecture:** `AstStage` reads the unified doc (`ctx.read("normalize")`), returns `{schema_version, title, sections[{heading, paragraphs[]}]}` grouped by OCR role labels. Reuses SP-2's derived-stage machinery.

**Tech Stack:** Python 3.13, the SP-2 framework, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` pure — `ast.py` no I/O. `AstStage` in `pipeline`.
- **Module name caution:** do NOT shadow the stdlib `ast` module in a way that breaks imports. The core module is `fdi/core/ast.py` (a submodule — importing `fdi.core.ast` is unambiguous and safe; it does NOT shadow the stdlib `ast` for other modules). The stage file is `fdi/pipeline/stages/ast.py`. If any import-order or shadowing issue arises, rename to `fdi/core/doc_ast.py` and note it — but the submodule form is expected to be fine.
- Determinism: `build_ast` is a pure function of the unified doc.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.
- Integration: reuse `session_factory` + `store` fixtures; unique ids per test; LOCAL `StageRegistry` (register `OcrStage`+`NormalizeStage`+`AstStage`).

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `build_ast` (core, pure)

**Files:** Create `backend/src/fdi/core/ast.py`; Test `backend/tests/unit/test_build_ast.py`.

**Interfaces:** `build_ast(unified: Mapping[str, object]) -> dict[str, object]` → `{"schema_version": 1, "title": str | None, "sections": [{"heading": str | None, "paragraphs": [str]}]}`. Algorithm per spec §3.

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_build_ast.py
from fdi.core.ast import build_ast


def test_build_ast_groups_by_role() -> None:
    unified = {"schema_version": 1, "content": "...", "page_count": 1, "blocks": [
        {"role": "title", "text": "ACME LTD Annual Report"},
        {"role": "header", "text": "Confidential"},
        {"role": "heading", "text": "Directors' Report"},
        {"role": "body", "text": "The board is pleased."},
        {"role": "body", "text": "Revenue grew."},
        {"role": "heading", "text": "Auditor's Report"},
        {"role": "body", "text": "We audited."},
        {"role": "page_number", "text": "12"},
    ]}
    assert build_ast(unified) == {
        "schema_version": 1,
        "title": "ACME LTD Annual Report",
        "sections": [
            {"heading": "Directors' Report", "paragraphs": ["The board is pleased.", "Revenue grew."]},
            {"heading": "Auditor's Report", "paragraphs": ["We audited."]},
        ],
    }


def test_body_before_heading_is_a_preamble_section() -> None:
    unified = {"blocks": [
        {"role": "body", "text": "Preamble text."},
        {"role": "heading", "text": "Section One"},
        {"role": "body", "text": "In section."},
    ]}
    assert build_ast(unified) == {
        "schema_version": 1, "title": None,
        "sections": [
            {"heading": None, "paragraphs": ["Preamble text."]},
            {"heading": "Section One", "paragraphs": ["In section."]},
        ],
    }


def test_empty_blocks_is_empty_ast() -> None:
    assert build_ast({}) == {"schema_version": 1, "title": None, "sections": []}
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** the algorithm from spec §3 (pure). Use `isinstance`/`cast` to satisfy `mypy --strict` when indexing `object`-typed values (mirror `fdi/core/unified.py`). `body`/`footnote`/unknown roles → paragraph; `header`/`footer`/`page_number` → dropped; first `title` → title; `heading` → new section.
- [ ] **Step 4: Run → passes** (3 tests).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/ast.py backend/tests/unit/test_build_ast.py && git commit -m "feat(core): build_ast — naive title/sections/paragraphs tree"`

---

### Task 2: `AstStage` (pipeline) + the normalize→ast chain

**Files:** Create `backend/src/fdi/pipeline/stages/ast.py`; Modify `backend/src/fdi/pipeline/stages/__init__.py`; Test `backend/tests/integration/test_ast_stage.py`.

**Interfaces:** `class AstStage` (`name="ast"`, `version=1`, `deps=("normalize",)`); `run(ctx)`: `unified = await ctx.read("normalize")`; `return build_ast(cast(Mapping[str, object], unified))`. Self-registers.

- [ ] **Step 1: Failing test** (integration; run ocr→normalize→ast in a LOCAL registry)
```python
# backend/tests/integration/test_ast_stage.py — sketch
async def test_ast_chains_off_normalize(session_factory, store) -> None:
    import uuid
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.keys import artifact_key, derived_input_hash
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.platform.db.repository import RegistryRepository

    doc = await intake_document(pdf=b"%PDF-1.7 uniq-ast", filename="a.pdf", doc_key=str(uuid.uuid4()),
                                content_type="application/pdf", session_factory=session_factory, store=store)
    fake = FakeOcrClient({"content": "ACME", "pages": [{"pageNumber": 1, "lines": [], "words": []}],
                          "paragraphs": [{"role": "title", "content": "ACME LTD"},
                                         {"role": "sectionHeading", "content": "Directors' Report"},
                                         {"content": "The board is pleased."}], "tables": []})
    reg = StageRegistry(); reg.register(OcrStage()); reg.register(NormalizeStage()); reg.register(AstStage())
    resolver = DagResolver(reg.all())
    assert "ast" in resolver.successors("normalize")

    common = dict(session_factory=session_factory, store=store, registry=reg, resolver=resolver)
    for st, svc in [("ocr", {"ocr": fake}), ("normalize", None), ("ast", None)]:
        await run_stage(doc_id=doc.id, stage_name=st, run_id=str(uuid.uuid4()), correlation_id="c1",
                        services=svc or {}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)
    ast_key = artifact_key(doc.id, "ast", 1, derived_input_hash([facts["normalize"].output_hash]))
    body = await store.get_json(ast_key)
    assert body["title"] == "ACME LTD"
    assert body["sections"] == [{"heading": "Directors' Report", "paragraphs": ["The board is pleased."]}]
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** the stage (`cast`/`Mapping` for `ctx.read`); register in `stages/__init__.py` keeping imports ALPHABETICAL (`ast`, `normalize`, `ocr`, `ping` — note: if ruff wraps the multi-line import block, keep alphabetical order inside it).
- [ ] **Step 4: Run → passes**; full non-e2e suite green. NOTE: registering `AstStage` globally means `successors("normalize")` now includes `"ast"` — a search-and-fix like SP-6 if any test asserts an exact successors/stale list (use membership assertions).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages/ast.py backend/src/fdi/pipeline/stages/__init__.py backend/tests/integration/test_ast_stage.py && git commit -m "feat(pipeline): AstStage — unified doc -> naive AST, auto-chained"`

---

### Task 3: Audit + docs

**Files:** SP-8 spec status; `docs/architecture/ingestion-roadmap.md` (SP-8 → done + handoff).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0 broken`; `core` pure).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 build_ast, 2 AstStage+cache, 3 successors chain, 4 gates) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-17.**
- [ ] **Step 5: Roadmap SP-8 → ✅ done** + handoff: the AST `{schema_version, title, sections[{heading, paragraphs[]}]}` at the `ast` artifact key; the pipeline chain is now `intake → ocr → normalize → ast`; **SP-18 (thin UI) is next** to complete the Stage-1 spine (show a document's AST); then Stage 2 opens with SP-7 structural (which will deepen the AST — nested sections, page spans, tables — via a new AST `schema_version` + enriched OCR).
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-8 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1 (role-driven grouping), T2 (derived stage + chain). §3 architecture → T1 (core), T2 (pipeline). §4 errors → T1 (empty blocks → empty AST). §5 testing → unit T1, integration T2. §7 done criteria → T3 audit.

**Type consistency:** `build_ast(unified: Mapping) -> dict` (T1) consumed by `AstStage` (T2). The stage `deps=("normalize",)` matches the `successors("normalize")` assertion (T2).

**Placeholder scan:** the module-shadowing note (T-level global constraint) is a concrete guard with a fallback, not a blank. No TODO/TBD.
