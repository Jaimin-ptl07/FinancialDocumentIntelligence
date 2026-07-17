# SP-17b — Structure-aware Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Wire the already-written, already-unit-tested `tables_are_rectangular` structure validator
into `QualityStage`, producing ONE quality report spanning AST-level and structure-level defects —
the explicit follow-on named in SP-17's own handoff note.

**Architecture:** A new pure aggregator, `validate_all(pairs: Sequence[tuple[Mapping[str, object],
Sequence[Validator]]]) -> dict[str, object]`, added to `fdi.core.quality` alongside SP-17's
`Issue`/`Validator`/validators; `validate` is re-expressed as `validate_all([(artifact,
validators)])` so the single-artifact behavior stays byte-identical. `STRUCTURE_VALIDATORS =
(tables_are_rectangular,)` added next to `AST_VALIDATORS`. `QualityStage.deps` grows to `("ast",
"structure")`, `version` 1→2, and `run` calls `validate_all([(ast, AST_VALIDATORS), (structure,
STRUCTURE_VALIDATORS)])` — the pipeline's first two-dependency stage. Reuses SP-2's existing
multi-dep machinery unchanged (`input_hash = derived_input_hash([facts[d].output_hash for d in
stage.deps])`, `successors(name) = [n for n, s in stages.items() if name in s.deps]` — both already
iterate `deps` generically).

**Tech Stack:** Python 3.13, SQLAlchemy/MinIO/Postgres via the SP-2 framework, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` pure —
  `quality.py` stays free of I/O. `QualityStage` stays in `pipeline`.
- Determinism: `validate_all` and every validator are pure functions of their arguments; ids/
  timestamps live in Postgres, never in the artifact.
- `mypy --strict`: index `object`-typed values (`ctx.read(...)` returns `object`; `Mapping[str,
  object].get(...)` returns `object`) via `isinstance`/`cast`, as the existing `quality.py`/
  `build_ast`/`merge_cross_page_tables` already do.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.
- Integration tests: reuse `session_factory` + `store` fixtures; mint unique ids/checksums per test;
  use a LOCAL `StageRegistry` in the test (register `OcrStage` + `NormalizeStage` + `AstStage` +
  `StructureStage` + `QualityStage`) rather than the global registry, to avoid cross-test coupling.
- Changing `QualityStage.deps`/`version` changes what `DagResolver(REGISTRY.all()).successors(...)`
  returns elsewhere in the suite (`successors("structure")` now includes `"quality"` too) and changes
  the `quality` artifact key (`version=2`, two-hash `input_hash`). Every existing successor assertion
  in the suite already uses `in` (membership), not an exact list — confirmed by grep across
  `backend/tests` before starting (Task 2, Step 4) rather than assumed.
- The existing `backend/tests/integration/test_quality_stage.py` (from SP-17) asserted a v1,
  single-hash `quality` artifact key and never ran `StructureStage`. It MUST be updated in place (not
  left passing against stale assumptions) so the chain it drives now also runs `structure` before
  `quality` and reads the v2 key.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration
`pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports
--config .importlinter`.

---

### Task 1: `validate_all` aggregator (core, pure) + unit tests

**Files:** Modify `backend/src/fdi/core/quality.py`; Modify `backend/tests/unit/test_quality.py`.

**Interfaces:** `STRUCTURE_VALIDATORS: tuple[Validator, ...] = (tables_are_rectangular,)`;
`validate_all(pairs: Sequence[tuple[Mapping[str, object], Sequence[Validator]]]) -> dict[str,
object]`; `validate` re-expressed in terms of `validate_all` (identical external signature/behavior).

- [ ] **Step 1: Failing test** — append to `backend/tests/unit/test_quality.py`:
```python
# --- validate_all (append to backend/tests/unit/test_quality.py) ---

from fdi.core.quality import STRUCTURE_VALIDATORS, validate_all  # add to existing import block

_CLEAN_STRUCTURE = {
    "tables": [{"column_count": 2, "cells": [{"column_index": 0}, {"column_index": 1}]}],
}

_TORN_STRUCTURE = {
    "tables": [{"column_count": 2, "cells": [{"column_index": 0}, {"column_index": 2}]}],
}


def test_validate_all_merges_two_clean_artifacts_into_one_ok_report() -> None:
    report = validate_all(
        [(_CLEAN_AST, AST_VALIDATORS), (_CLEAN_STRUCTURE, STRUCTURE_VALIDATORS)]
    )
    assert report == {
        "schema_version": 1,
        "issues": [],
        "error_count": 0,
        "warning_count": 0,
        "ok": True,
    }


def test_validate_all_unions_counts_ast_issues_before_structure_issues() -> None:
    sparse_ast = {"sections": [], "title": None}
    report = validate_all(
        [(sparse_ast, AST_VALIDATORS), (_TORN_STRUCTURE, STRUCTURE_VALIDATORS)]
    )
    codes = [issue["code"] for issue in report["issues"]]
    assert codes == ["ast.no_sections", "ast.missing_title", "structure.torn_table"]
    assert report["error_count"] == 1
    assert report["warning_count"] == 2
    assert report["ok"] is False


def test_validate_all_single_pair_matches_validate() -> None:
    assert validate_all([(_CLEAN_AST, AST_VALIDATORS)]) == validate(_CLEAN_AST, AST_VALIDATORS)

    sparse_ast = {"sections": [], "title": "T"}
    assert validate_all([(sparse_ast, AST_VALIDATORS)]) == validate(sparse_ast, AST_VALIDATORS)


def test_validate_all_raises_on_unknown_severity_from_either_pair() -> None:
    def _bogus(_artifact: object) -> list[dict[str, object]]:
        return [{"severity": "critical", "code": "x.bogus", "message": "m"}]

    with pytest.raises(ValueError, match="critical"):
        validate_all([(_CLEAN_AST, AST_VALIDATORS), (_CLEAN_STRUCTURE, (_bogus,))])


def test_validate_all_empty_pairs_is_ok_with_no_issues() -> None:
    assert validate_all([]) == {
        "schema_version": 1,
        "issues": [],
        "error_count": 0,
        "warning_count": 0,
        "ok": True,
    }
```
- [ ] **Step 2: Run → fails** (`ImportError: cannot import name 'validate_all' from 'fdi.core.quality'`).
- [ ] **Step 3: Implement** — edit `backend/src/fdi/core/quality.py`, replacing the existing
  `AST_VALIDATORS`/`validate` tail (everything from `AST_VALIDATORS: tuple[...]` to the end of the
  file) with:
```python
AST_VALIDATORS: tuple[Validator, ...] = (ast_has_sections, sections_have_content, title_present)
STRUCTURE_VALIDATORS: tuple[Validator, ...] = (tables_are_rectangular,)


def validate_all(
    pairs: Sequence[tuple[Mapping[str, object], Sequence[Validator]]],
) -> dict[str, object]:
    issues: list[Issue] = [
        issue
        for artifact, validators in pairs
        for validator in validators
        for issue in validator(artifact)
    ]
    for issue in issues:
        if issue["severity"] not in _KNOWN_SEVERITIES:
            raise ValueError(f"unknown issue severity: {issue['severity']!r}")
    error_count = sum(1 for issue in issues if issue["severity"] == "error")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    return {
        "schema_version": 1,
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
        "ok": error_count == 0,
    }


def validate(artifact: Mapping[str, object], validators: Sequence[Validator]) -> dict[str, object]:
    """Single-artifact convenience wrapper: `validate_all` generalized to one pair."""
    return validate_all([(artifact, validators)])
```
  (Everything above `AST_VALIDATORS` — `Issue`, `Validator`, `_KNOWN_SEVERITIES`, `_issue`,
  `ast_has_sections`, `sections_have_content`, `title_present`, `tables_are_rectangular` — is
  untouched.)
- [ ] **Step 4: Run → passes**, including every pre-existing `test_validate_*` test in
  `test_quality.py` unchanged (proves `validate`'s behavior is preserved by the rewrite).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/quality.py backend/tests/unit/test_quality.py && git commit -m "feat(core): validate_all aggregates issues across N artifacts; validate is now its single-pair case"`

---

### Task 2: Extend `QualityStage` to `deps=("ast","structure")` + integration test

**Files:** Modify `backend/src/fdi/pipeline/stages/quality.py`; Modify
`backend/tests/integration/test_quality_stage.py`.

**Interfaces:** `class QualityStage` (`name="quality"`, `version=2`, `deps=("ast","structure")`);
`run(ctx)`: reads both `ast` and `structure`, returns `validate_all([(ast, AST_VALIDATORS),
(structure, STRUCTURE_VALIDATORS)])`. No change to `backend/src/fdi/pipeline/stages/__init__.py` —
`quality` and `structure` are both already imported there for their registration side effects.

- [ ] **Step 1: Failing test** — replace `backend/tests/integration/test_quality_stage.py` in full:
```python
# backend/tests/integration/test_quality_stage.py
"""QualityStage auto-chains off AstStage AND StructureStage: `quality` is a
DERIVED stage (`deps=("ast","structure")`) that validates the naive AST for
internal defects AND the structure artifact's tables for shape defects,
merging both into ONE report via `validate_all`. Against real Postgres +
MinIO with a FAKE OCR client. (SP-17b: the pipeline's first
two-dependency stage.)"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.quality import QualityStage
from fdi.pipeline.stages.structure import StructureStage
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository
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


async def test_quality_chains_off_ast_and_structure(session_factory, store) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    fake = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Directors' Report"},
                {"content": "The board is pleased."},
            ],
            "tables": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(QualityStage())
    resolver = DagResolver(reg.all())

    assert "quality" in resolver.successors("ast")  # the chain is automatic
    assert "quality" in resolver.successors("structure")  # NEW: second dep also schedules quality

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="quality", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    quality_key = artifact_key(
        doc.id, "quality", 2,
        derived_input_hash([facts["ast"].output_hash, facts["structure"].output_hash]),
    )
    body = await store.get_json(quality_key)
    assert body == {
        "schema_version": 1,
        "issues": [],
        "error_count": 0,
        "warning_count": 0,
        "ok": True,
    }


async def test_quality_merges_ast_and_structure_defects_into_one_report(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="b.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    # No title, no heading, no paragraphs -> normalize/ast produce an empty AST.
    # ONE table whose second cell's column_index (2) >= its column_count (2) -> a torn grid.
    fake = FakeOcrClient(
        {
            "content": "",
            "pages": [],
            "paragraphs": [],
            "tables": [
                {
                    "rowCount": 1,
                    "columnCount": 2,
                    "cells": [
                        {"rowIndex": 0, "columnIndex": 0, "content": "Q1"},
                        {"rowIndex": 0, "columnIndex": 2, "content": "Q2"},
                    ],
                },
            ],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(QualityStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id,
        "correlation_id": "c2",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="quality", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    quality_key = artifact_key(
        doc.id, "quality", 2,
        derived_input_hash([facts["ast"].output_hash, facts["structure"].output_hash]),
    )
    body = await store.get_json(quality_key)

    codes = [issue["code"] for issue in body["issues"]]
    assert codes == ["ast.no_sections", "ast.missing_title", "structure.torn_table"]
    assert body["error_count"] == 1  # only ast.no_sections is an error
    assert body["warning_count"] == 2  # ast.missing_title + structure.torn_table
    assert body["ok"] is False
```
- [ ] **Step 2: Run → fails** (`test_quality_chains_off_ast_and_structure` fails: `KeyError:
  'structure'` reading `facts["structure"]` before the stage reads two deps, and/or the stored key is
  still `v1` with a one-hash `input_hash` — mismatch against the `v2`/two-hash key the test now
  computes; `test_quality_merges_ast_and_structure_defects_into_one_report` fails the same way, plus
  it doesn't even find a `structure.torn_table` code since `QualityStage` doesn't read `structure` yet).
- [ ] **Step 3: Implement** — replace `backend/src/fdi/pipeline/stages/quality.py` in full:
```python
# backend/src/fdi/pipeline/stages/quality.py
"""QualityStage: a DERIVED stage that validates the AST for internal defects
AND the structure artifact's tables for shape defects, merging both into ONE
quality report via `validate_all`. Auto-chains off BOTH AstStage and
StructureStage via `deps=("ast", "structure")` -- the pipeline's first
two-dependency stage (SP-17b)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.quality import AST_VALIDATORS, STRUCTURE_VALIDATORS, validate_all
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class QualityStage:
    name = "quality"
    version = 2
    deps: tuple[str, ...] = ("ast", "structure")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        return validate_all([(ast, AST_VALIDATORS), (structure, STRUCTURE_VALIDATORS)])


REGISTRY.register(QualityStage())
```
  `backend/src/fdi/pipeline/stages/__init__.py` is unchanged — it already imports both `quality` and
  `structure` for their registration side effects (confirm by reading the file; do not edit it unless
  that assumption is wrong).
- [ ] **Step 4: Run → passes**; full non-e2e suite green. Before moving on, grep the suite for any
  successor assertion this change could break: `grep -rn "successors(" backend/tests` — confirm every
  hit is a membership check (`in resolver.successors(...)`) except
  `backend/tests/unit/test_resolver.py`, which builds its own tiny LOCAL a/b/c graph unrelated to the
  global registry and is unaffected. If a hit is instead an exact-list equality
  (`successors("ast") == [...]`), fix it to a membership check, as was done for every prior stage
  addition (`NormalizeStage`, `AstStage`, `StructureStage`, `OntologyStage`, `GraphStage`,
  `SemanticStage`, `LinkingStage`, SP-17's original `QualityStage`).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages/quality.py backend/tests/integration/test_quality_stage.py && git commit -m "feat(pipeline): QualityStage reads ast+structure, merges AST and table-shape issues into one report"`

---

### Task 3: Audit + docs

**Files:** SP-17b spec status; `docs/architecture/ingestion-roadmap.md` (add SP-17b row; update the
top status line's **Next** bullet and the "What SP-17 shipped" handoff note now that its named
follow-on is done).

- [ ] **Step 1: Static gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports
  --config .importlinter` (`1 kept, 0 broken`; `core` stays pure).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip).
- [ ] **Step 3: Audit spec §7** — map each done criterion to its test:
  1. `validate_all` aggregation, ordering, `ok` gating, unknown-severity raise, `validate` equivalence
     → `test_quality.py` (`validate_all` tests, Task 1).
  2. `STRUCTURE_VALIDATORS` exists → `fdi/core/quality.py` (Task 1); exercised via
     `test_validate_all_merges_two_clean_artifacts_into_one_ok_report` and the torn-table case.
  3. `QualityStage.deps`/`version`/`run` → `fdi/pipeline/stages/quality.py` (Task 2); exercised by both
     integration tests.
  4. `successors("ast")` and `successors("structure")` both include `"quality"`; v2 two-hash key →
     `test_quality_chains_off_ast_and_structure` (Task 2).
  5. Merged report contains both an `ast.*` issue and `structure.torn_table` in one artifact →
     `test_quality_merges_ast_and_structure_defects_into_one_report` (Task 2).
  6. Static gates + full suite green + no stale exact-list successor assertion → Steps 1-2 of this
     task + Task 2 Step 4's grep audit.
- [ ] **Step 4: Spec status → Implemented 2026-07-17** (once Steps 1-3 are actually green — do not
  flip status on a plan that hasn't been executed).
- [ ] **Step 5: Roadmap** — add the **SP-17b** row to the §3 table (phase **enterprise**, weight
  **light**, status ✅ done); update the top status line's **Next** bullet to drop
  "structure-aware quality (SP-17 follow-on)" (it is no longer next — it's done); append a short
  note to (or replace) the existing "What SP-17 shipped that its follow-on... must know" section
  recording that `QualityStage` now reads `deps=("ast", "structure")` at `version=2`, the merged
  report shape, and that any future validator addition (numeric-total reconciliation waiting on SP-11,
  cross-document consistency waiting on SP-16) is the next quality follow-on, not this one.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-17b done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1 (`validate_all` as the merge point, `validate` re-expressed
without duplicating aggregation logic, deterministic pair-then-validator ordering), T2 (`deps` order
as cache-identity, `version` bump rationale, `STRUCTURE_VALIDATORS` placement, severity policy
unchanged). §3 architecture → T1 (core: `validate_all`/`STRUCTURE_VALIDATORS`), T2 (pipeline:
extended `QualityStage`). §4 errors → T1 (`validate_all` raises on unknown severity from either pair),
T2 (upstream-missing `ctx.read("structure")`, inherited unchanged from the existing framework). §5
testing → unit T1 (every `validate_all` row: clean merge, ordered union, single-pair equivalence,
raise, empty-pairs edge case), integration T2 (clean chain → `ok=True`; sparse-AST + torn-table chain
→ both `ast.*` and `structure.torn_table` in one report). §7 done criteria → T3 audit.

**Type consistency:** `STRUCTURE_VALIDATORS`/`validate_all` (T1) consumed unchanged by `QualityStage`
(T2) — `validate_all([(ast, AST_VALIDATORS), (structure, STRUCTURE_VALIDATORS)])` matches T1's
signature exactly (`Sequence[tuple[Mapping[str, object], Sequence[Validator]]]`). The stage
`deps=("ast", "structure")` matches both `successors("ast")`/`successors("structure")` assertions and
the `derived_input_hash([ast.output_hash, structure.output_hash])` key computation (T2) — dep order,
merge order, and hash order are all the same tuple, stated once in the spec (§2 decision 3) and
carried through consistently rather than three independent choices that happen to agree.

**Placeholder scan:** T2's "grep for `successors(` and confirm every hit is membership" is a concrete,
runnable verification instruction with the exact command and the fix pattern named, not a blank. T3's
audit table is fully populated, not a TODO list. No TBD/FIXME anywhere in the plan.
