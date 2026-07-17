# SP-6 — Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Map the OCR artifact into the unified internal schema via a pure `normalize_document` + a `NormalizeStage` (`deps=("ocr",)`) that chains off OCR automatically.

**Architecture:** `NormalizeStage` reads the OCR artifact (`ctx.read("ocr")`) and returns `{schema_version, content, page_count, blocks[{role,text}]}` with Azure roles mapped to a canonical `Role` enum. Reuses SP-2's derived-stage machinery (input_hash from the OCR output_hash; auto-scheduled as a successor of `ocr`).

**Tech Stack:** Python 3.13, SQLAlchemy/MinIO/Postgres via the SP-2 framework, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` pure — `unified.py` has no I/O. `NormalizeStage` in `pipeline`.
- Determinism: `normalize_document` is a pure function of the OCR artifact; ids/timestamps in Postgres.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.
- Integration tests: reuse `session_factory` + `store` fixtures; mint unique ids/checksums per test; use a LOCAL `StageRegistry` in the test (register `OcrStage` + `NormalizeStage`) rather than the global registry, to avoid cross-test coupling.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `Role` enum + `canonical_role` (core, pure)

**Files:** Create `backend/src/fdi/core/unified.py`; Test `backend/tests/unit/test_unified.py`.

**Interfaces:** `class Role(enum.StrEnum)` with `TITLE="title"`, `HEADING="heading"`, `HEADER="header"`, `FOOTER="footer"`, `PAGE_NUMBER="page_number"`, `FOOTNOTE="footnote"`, `BODY="body"`. `canonical_role(azure_role: str | None) -> Role` maps `title→TITLE`, `sectionHeading→HEADING`, `pageHeader→HEADER`, `pageFooter→FOOTER`, `pageNumber→PAGE_NUMBER`, `footnote→FOOTNOTE`, and `None`/anything-else → `BODY`.

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_unified.py
import pytest
from fdi.core.unified import Role, canonical_role


@pytest.mark.parametrize(("azure", "expected"), [
    ("title", Role.TITLE),
    ("sectionHeading", Role.HEADING),
    ("pageHeader", Role.HEADER),
    ("pageFooter", Role.FOOTER),
    ("pageNumber", Role.PAGE_NUMBER),
    ("footnote", Role.FOOTNOTE),
    (None, Role.BODY),
    ("somethingUnknown", Role.BODY),
])
def test_canonical_role(azure: str | None, expected: Role) -> None:
    assert canonical_role(azure) == expected


def test_role_values_are_snake_case_strings() -> None:
    assert Role.PAGE_NUMBER.value == "page_number" and Role.HEADING == "heading"
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — `enum.StrEnum` (NOT `(str, enum.Enum)` — ruff UP042) + a dict lookup with `.get(azure_role, Role.BODY)`.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/unified.py backend/tests/unit/test_unified.py && git commit -m "feat(core): canonical Role enum + azure role mapping"`

---

### Task 2: `normalize_document` (core, pure)

**Files:** Modify `backend/src/fdi/core/unified.py`; Test `backend/tests/unit/test_normalize_document.py`.

**Interfaces:** `normalize_document(ocr: Mapping[str, object]) -> dict[str, object]` → `{"schema_version": 1, "content": <str>, "page_count": <int>, "blocks": [{"role": <str>, "text": <str>}]}`. `content` from `ocr["content"]` (missing → `ValueError` mentioning "content"); `page_count = len(ocr.get("pages", []))`; `blocks` from `ocr.get("paragraphs", [])` mapping each `{"role": canonical_role(p.get("role")).value, "text": p["content"]}`.

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_normalize_document.py
import pytest
from fdi.core.unified import normalize_document

_OCR = {
    "schema_version": 1,
    "content": "ACME LTD\nDirectors' Report\nThe board ...",
    "pages": [{"page_number": 1}, {"page_number": 2}],
    "paragraphs": [
        {"role": "title", "content": "ACME LTD"},
        {"role": "sectionHeading", "content": "Directors' Report"},
        {"content": "The board ..."},
    ],
    "tables": [],
}


def test_normalize_document_builds_unified_doc() -> None:
    out = normalize_document(_OCR)
    assert out["schema_version"] == 1
    assert out["content"].startswith("ACME LTD")
    assert out["page_count"] == 2
    assert out["blocks"] == [
        {"role": "title", "text": "ACME LTD"},
        {"role": "heading", "text": "Directors' Report"},
        {"role": "body", "text": "The board ..."},
    ]


def test_missing_content_raises() -> None:
    with pytest.raises(ValueError, match="content"):
        normalize_document({"paragraphs": []})
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — pure; use `isinstance`/`cast` to satisfy `mypy --strict` when indexing `object`-typed values (as SP-5's `normalize_ocr` did). Map roles via `canonical_role(...).value`.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/unified.py backend/tests/unit/test_normalize_document.py && git commit -m "feat(core): normalize_document — OCR artifact -> unified schema"`

---

### Task 3: `NormalizeStage` (pipeline) + the ocr→normalize chain

**Files:** Create `backend/src/fdi/pipeline/stages/normalize.py`; Modify `backend/src/fdi/pipeline/stages/__init__.py`; Test `backend/tests/integration/test_normalize_stage.py`.

**Interfaces:** `class NormalizeStage` (`name="normalize"`, `version=1`, `deps=("ocr",)`); `run(ctx)`: `ocr = await ctx.read("ocr")`; `return normalize_document(cast(Mapping[str, object], ocr))`. Self-registers.

- [ ] **Step 1: Failing test** (integration; run ocr then normalize in a LOCAL registry with both stages)
```python
# backend/tests/integration/test_normalize_stage.py — sketch
async def test_normalize_chains_off_ocr(session_factory, store) -> None:
    import uuid
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.keys import artifact_key, root_input_hash, derived_input_hash
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.platform.db.repository import RegistryRepository

    doc = await intake_document(pdf=b"%PDF-1.7 uniq", filename="a.pdf", doc_key=str(uuid.uuid4()),
                                content_type="application/pdf", session_factory=session_factory, store=store)
    fake = FakeOcrClient({"content": "ACME LTD", "pages": [{"pageNumber": 1, "lines": [], "words": []}],
                          "paragraphs": [{"role": "title", "content": "ACME LTD"}], "tables": []})
    reg = StageRegistry(); reg.register(OcrStage()); reg.register(NormalizeStage())
    resolver = DagResolver(reg.all())
    assert "normalize" in resolver.successors("ocr")   # the chain is automatic

    common = dict(session_factory=session_factory, store=store, registry=reg, resolver=resolver)
    await run_stage(doc_id=doc.id, stage_name="ocr", run_id=str(uuid.uuid4()), correlation_id="c1",
                    services={"ocr": fake}, **common)
    await run_stage(doc_id=doc.id, stage_name="normalize", run_id=str(uuid.uuid4()), correlation_id="c1",
                    **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)
    norm_key = artifact_key(doc.id, "normalize", 1, derived_input_hash([facts["ocr"].output_hash]))
    body = await store.get_json(norm_key)
    assert body["schema_version"] == 1
    assert body["content"] == "ACME LTD"
    assert body["blocks"] == [{"role": "title", "text": "ACME LTD"}]
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** the stage (`cast`/`Mapping` for the `ctx.read` result which is typed `object`); register it in `stages/__init__.py` (`from fdi.pipeline.stages import normalize as normalize  # noqa: F401`, keep imports alphabetical: normalize, ocr, ping).
- [ ] **Step 4: Run → passes**; full non-e2e suite green. NOTE: registering `NormalizeStage` globally means `DagResolver(REGISTRY.all()).successors("ocr")` now returns `["normalize"]` — check no existing test asserted `successors("ocr") == []` or a stale-list that a new global successor breaks (fix by membership assertions if so, as was done for `/stale`).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages/normalize.py backend/src/fdi/pipeline/stages/__init__.py backend/tests/integration/test_normalize_stage.py && git commit -m "feat(pipeline): NormalizeStage — OCR -> unified schema, auto-chained"`

---

### Task 4: Audit + docs

**Files:** SP-6 spec status; `docs/architecture/ingestion-roadmap.md` (SP-6 → done + handoff).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0 broken`; `core` pure).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 canonical_role, 2 normalize_document, 3 NormalizeStage+cache, 4 successors chain, 5 gates) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-17.**
- [ ] **Step 5: Roadmap SP-6 → ✅ done** + handoff: the unified doc `{schema_version, content, page_count, blocks[{role,text}]}` at the `normalize` artifact key is the vendor-neutral contract; **SP-7 (Structural Intelligence)** consumes it AND needs richer geometry than SP-5 kept, so SP-7 enriches the OCR artifact (words/cells/bboxes) and bumps `OcrStage.version`; the canonical `Role` enum is where downstream classification anchors.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-6 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1 (canonical roles), T2 (unified schema + content), T3 (derived stage + auto-chain). §3 architecture → T1/T2 (core), T3 (pipeline stage). §4 errors → T2 (missing content), T1 (unknown role → body). §5 testing → unit T1/T2, integration T3. §7 done criteria → T4 audit.

**Type consistency:** `Role`/`canonical_role` (T1) used by `normalize_document` (T2), used by `NormalizeStage` (T3). `normalize_document(ocr: Mapping) -> dict` signature consistent T2→T3. The stage `deps=("ocr",)` matches the `successors("ocr")` assertion (T3).

**Placeholder scan:** the T3 "check no existing test asserted successors('ocr')==[]" is a concrete verification instruction (with the fix pattern), not a blank. No TODO/TBD.
