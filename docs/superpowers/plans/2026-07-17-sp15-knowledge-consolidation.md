# SP-15 — Knowledge Store Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fold a document's already-derived knowledge artifacts — SP-10's section classification
(`semantic`), SP-11's concept mappings (`ontology`), SP-17b's quality report (`quality`) — into ONE
unified per-document knowledge record via a pure `consolidate(semantic, ontology, quality)` + a
`KnowledgeStage` (`deps=("semantic", "ontology", "quality")`) that auto-chains off ALL THREE. This is
the codebase's **first stage to declare three dependencies** — it exercises SP-2's already-generic
N-ary dep machinery, not a framework change.

**Architecture:** `KnowledgeStage` reads `semantic` (`ctx.read("semantic")`), `ontology`
(`ctx.read("ontology")`), and `quality` (`ctx.read("quality")`), returns `{schema_version:1,
section_count, section_types, concepts, concept_count, quality}`. `section_count`/`section_types`
bucket `semantic["sections"]` by their `type` string; `concepts` is the sorted, deduplicated,
non-`UNKNOWN` set of `ontology["mappings"][].concept`; `quality` echoes `{ok, error_count,
warning_count}` from the quality report. Every field degrades independently on missing/malformed
input — never raises. Reuses SP-2's derived-stage machinery unchanged (`input_hash =
derived_input_hash([facts[d].output_hash for d in stage.deps])`, `successors(name) = sorted(n for n,
s in stages.items() if name in s.deps)` — both already iterate `deps` generically over any length; no
`run.py`/`resolver.py` change needed). Design: `docs/superpowers/specs/2026-07-17-sp15-knowledge-consolidation-design.md`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` pure —
  `fdi/core/knowledge.py` has no I/O and does NOT import `fdi.core.semantic`/`fdi.core.ontology`/
  `fdi.core.quality` (spec §2 decision 3 — treat all three inputs as plain `Mapping[str, object]`).
  `KnowledgeStage` lives in `pipeline`.
- `mypy --strict`: use `isinstance` narrowing (mirror `fdi/core/provenance.py`'s and
  `fdi/core/ontology.py`'s defensive style) when reading `object`-typed values out of the
  `semantic`/`ontology`/`quality` `Mapping`s; no blanket `# type: ignore`. `cast` only where
  `isinstance` alone can't satisfy the checker (narrowing `ctx.read("semantic")`/`ctx.read("ontology")`/
  `ctx.read("quality")`'s `object` return to `Mapping[str, object]` before calling `consolidate`, same
  as every other derived stage's `run` method).
- Determinism: `consolidate` is a pure function of its three `Mapping` arguments — no RNG/clock;
  `concepts` is built from a `set` internally but MUST be `sorted(...)` before returning (a bare
  `set` iteration order is not a deterministic artifact body).
- `KnowledgeStage.deps = ("semantic", "ontology", "quality")` — this fixed order is part of the
  artifact's cache identity (`derived_input_hash([semantic.output_hash, ontology.output_hash,
  quality.output_hash])`); do not reorder.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.
- Integration: reuse `session_factory` + `store` fixtures; unique ids per test; **LOCAL**
  `StageRegistry` (register `OcrStage`+`NormalizeStage`+`AstStage`+`StructureStage`+`SemanticStage`+
  `OntologyStage`+`QualityStage`+`KnowledgeStage`, not the global `REGISTRY`). `QualityStage` is
  already `deps=("ast","structure")` (SP-17b, `version=2`) — the integration chain MUST run
  `structure` before `quality`, and `semantic` needs an injected `FakeSemanticClient` via
  `services={"semantic": fake}` (mirror `test_semantic_stage.py`).
- Registering `KnowledgeStage` into the global `REGISTRY` (via `stages/__init__.py`) means the
  process-wide `DagResolver(REGISTRY.all()).successors("semantic")`, `.successors("ontology")`, AND
  `.successors("quality")` also gain `"knowledge"`. Any test asserting on any of those successor
  lists must use a **membership** check (`"knowledge" in ...`), never an exact-list equality — grep
  for exact-list assertions on `successors("semantic")`/`successors("ontology")`/`successors("quality")`
  before landing Task 2 and fix any found (none are expected; every prior stage-adding SP —
  SP-6/SP-8/SP-9/SP-11/SP-12/SP-17b — already left the suite membership-only).

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration
`pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` ·
`PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `consolidate` (core, pure)

**Files:** Create `backend/src/fdi/core/knowledge.py`; Test `backend/tests/unit/test_knowledge.py`.

**Interfaces:** `consolidate(semantic: Mapping[str, object], ontology: Mapping[str, object],
quality: Mapping[str, object]) -> dict[str, object]` → `{"schema_version": 1, "section_count": int,
"section_types": dict[str, int], "concepts": list[str], "concept_count": int, "quality": {"ok": bool,
"error_count": int, "warning_count": int}}`. Algorithm per spec §3.2.

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_knowledge.py
from fdi.core.knowledge import consolidate

_SEMANTIC = {
    "schema_version": 1,
    "sections": [
        {"heading": "Balance Sheet", "type": "BALANCE_SHEET", "paragraph_count": 1},
        {"heading": "Directors' Report", "type": "DIRECTORS_REPORT", "paragraph_count": 1},
        {"heading": "Notes", "type": "BALANCE_SHEET", "paragraph_count": 3},
    ],
}

_ONTOLOGY = {
    "schema_version": 1,
    "mappings": [
        {"label": "Total Assets", "concept": "TOTAL_ASSETS"},
        {"label": "Revenue", "concept": "REVENUE"},
        {"label": "Sales", "concept": "REVENUE"},
    ],
}

_QUALITY = {
    "schema_version": 1,
    "issues": [{"severity": "warning", "code": "structure.torn_table", "message": "torn"}],
    "error_count": 0,
    "warning_count": 1,
    "ok": True,
}


def test_consolidate_counts_section_types_and_total() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY, _QUALITY)
    assert out["section_count"] == 3
    assert out["section_types"] == {"BALANCE_SHEET": 2, "DIRECTORS_REPORT": 1}


def test_consolidate_sorted_unique_concepts_excludes_unknown() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY, _QUALITY)
    assert out["concepts"] == ["REVENUE", "TOTAL_ASSETS"]
    assert out["concept_count"] == 2


def test_consolidate_echoes_quality_summary() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY, _QUALITY)
    assert out["quality"] == {"ok": True, "error_count": 0, "warning_count": 1}


def test_consolidate_schema_version_is_1() -> None:
    assert consolidate(_SEMANTIC, _ONTOLOGY, _QUALITY)["schema_version"] == 1


def test_consolidate_empty_inputs_yield_zeros_and_empties() -> None:
    assert consolidate({}, {}, {}) == {
        "schema_version": 1,
        "section_count": 0,
        "section_types": {},
        "concepts": [],
        "concept_count": 0,
        "quality": {"ok": False, "error_count": 0, "warning_count": 0},
    }


def test_consolidate_present_but_keyless_artifacts_degrade_like_empty() -> None:
    assert consolidate(
        {"schema_version": 1}, {"schema_version": 1}, {"schema_version": 1}
    ) == {
        "schema_version": 1,
        "section_count": 0,
        "section_types": {},
        "concepts": [],
        "concept_count": 0,
        "quality": {"ok": False, "error_count": 0, "warning_count": 0},
    }


def test_consolidate_malformed_semantic_sections_degrade_without_raising() -> None:
    semantic = {
        "sections": [
            "not a mapping",
            {"heading": "X", "type": 42, "paragraph_count": 1},
            {"heading": "Y"},
        ],
    }
    out = consolidate(semantic, {}, {})
    assert out["section_count"] == 2  # the string entry doesn't count as a section
    assert out["section_types"] == {}  # neither surviving entry has a usable str type


def test_consolidate_malformed_ontology_mappings_degrade_without_raising() -> None:
    ontology = {
        "mappings": [
            "not a mapping",
            {"label": "X", "concept": 42},
            {"label": "Y"},
            {"label": "Z", "concept": "UNKNOWN"},
        ],
    }
    out = consolidate({}, ontology, {})
    assert out["concepts"] == []
    assert out["concept_count"] == 0


def test_consolidate_malformed_quality_summary_degrades_without_raising() -> None:
    quality = {"ok": "yes", "error_count": "1", "warning_count": None}
    out = consolidate({}, {}, quality)
    assert out["quality"] == {"ok": False, "error_count": 0, "warning_count": 0}
```
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** the algorithm from spec §3.2 (pure). Use `isinstance` to narrow
  `object`-typed values read off the `Mapping`s (mirror `fdi/core/provenance.py`/
  `fdi/core/ontology.py`):
```python
# backend/src/fdi/core/knowledge.py
"""Knowledge consolidation: fold a document's derived knowledge artifacts --
SP-10's section classification (`semantic`), SP-11's concept mappings
(`ontology`), SP-17b's quality report (`quality`) -- into ONE unified
per-document knowledge record. Pure, deterministic; no I/O; degrades on
missing/malformed input, never raises. SP-16's cross-document layer consumes
this record as its per-document input."""

from collections.abc import Mapping


def consolidate(
    semantic: Mapping[str, object],
    ontology: Mapping[str, object],
    quality: Mapping[str, object],
) -> dict[str, object]:
    """Fold `semantic`, `ontology`, and `quality` artifacts into one record:
    `{schema_version, section_count, section_types, concepts, concept_count,
    quality}`.

    - `section_count`/`section_types`: every `Mapping` entry in
      `semantic["sections"]` counts toward `section_count`; those with a
      `str` `type` also bucket into `section_types`. Non-`Mapping` entries
      are skipped entirely (do not count as a section).
    - `concepts`/`concept_count`: the sorted, DEDUPLICATED set of
      `ontology["mappings"][].concept` values that are `str` and not
      `"UNKNOWN"`. Malformed/non-`str`/`UNKNOWN` entries contribute nothing.
    - `quality`: a three-key echo (`ok`, `error_count`, `warning_count`) of
      the quality report; each field independently degrades to its zero
      value (`False`/`0`/`0`) if missing or the wrong type -- never coerced.

    Every input is read defensively (`.get`, `isinstance` checks); a
    missing/malformed key degrades that field to zero/empty, never raises.
    `consolidate({}, {}, {})` is a fully defined, zeroed/emptied record.
    """
    section_types: dict[str, int] = {}
    section_count = 0
    sections_raw = semantic.get("sections")
    if isinstance(sections_raw, list):
        for section in sections_raw:
            if not isinstance(section, Mapping):
                continue
            section_count += 1
            type_ = section.get("type")
            if isinstance(type_, str):
                section_types[type_] = section_types.get(type_, 0) + 1

    concepts_set: set[str] = set()
    mappings_raw = ontology.get("mappings")
    if isinstance(mappings_raw, list):
        for mapping in mappings_raw:
            if not isinstance(mapping, Mapping):
                continue
            concept = mapping.get("concept")
            if isinstance(concept, str) and concept != "UNKNOWN":
                concepts_set.add(concept)
    concepts = sorted(concepts_set)

    ok_raw = quality.get("ok")
    error_count_raw = quality.get("error_count")
    warning_count_raw = quality.get("warning_count")

    return {
        "schema_version": 1,
        "section_count": section_count,
        "section_types": section_types,
        "concepts": concepts,
        "concept_count": len(concepts),
        "quality": {
            "ok": ok_raw if isinstance(ok_raw, bool) else False,
            "error_count": error_count_raw if isinstance(error_count_raw, int) else 0,
            "warning_count": warning_count_raw if isinstance(warning_count_raw, int) else 0,
        },
    }
```
- [ ] **Step 4: Run → passes** (9 tests).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/knowledge.py backend/tests/unit/test_knowledge.py && git commit -m "feat(core): consolidate -- fold semantic+ontology+quality into one knowledge record"`

---

### Task 2: `KnowledgeStage` (pipeline) + the semantic+ontology+quality→knowledge chain

**Files:** Create `backend/src/fdi/pipeline/stages/knowledge.py`; Modify
`backend/src/fdi/pipeline/stages/__init__.py`; Test `backend/tests/integration/test_knowledge_stage.py`.

**Interfaces:** `class KnowledgeStage` (`name="knowledge"`, `version=1`, `deps=("semantic",
"ontology", "quality")`); `run(ctx)`: `semantic = cast(Mapping[str, object], await
ctx.read("semantic"))`; `ontology = cast(Mapping[str, object], await ctx.read("ontology"))`; `quality
= cast(Mapping[str, object], await ctx.read("quality"))`; `return consolidate(semantic, ontology,
quality)`. Self-registers.

- [ ] **Step 1: Failing test** (integration; run
  ocr→normalize→ast→structure→semantic→ontology→quality→knowledge in a LOCAL registry)
```python
# backend/tests/integration/test_knowledge_stage.py
"""KnowledgeStage auto-chains off SemanticStage, OntologyStage, AND
QualityStage: `knowledge` is the codebase's FIRST three-dependency DERIVED
stage (`deps=("semantic", "ontology", "quality")`), folding SP-10's section
classification, SP-11's concept mappings, and SP-17b's quality report into
ONE per-document knowledge record. Against real Postgres + MinIO with FAKE
OCR + semantic clients."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.knowledge import KnowledgeStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.ontology import OntologyStage
from fdi.pipeline.stages.quality import QualityStage
from fdi.pipeline.stages.semantic import SemanticStage
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


async def test_knowledge_chains_off_semantic_ontology_and_quality(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
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
                {"role": "title", "content": "ACME LTD Annual Report"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Total Assets"},
                {"role": "sectionHeading", "content": "Directors' Report"},
                {"content": "The board is pleased with results."},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient(
        {
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Directors' Report": SectionType.DIRECTORS_REPORT.value,
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(SemanticStage())
    reg.register(OntologyStage())
    reg.register(QualityStage())
    reg.register(KnowledgeStage())
    resolver = DagResolver(reg.all())

    assert "knowledge" in resolver.successors("semantic")  # the chain is automatic
    assert "knowledge" in resolver.successors("ontology")  # ... off ALL THREE deps
    assert "knowledge" in resolver.successors("quality")

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic",
        run_id=str(uuid.uuid4()),
        services={"semantic": fake_semantic},
        **common,
    )
    await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="quality", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="knowledge", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    knowledge_key = artifact_key(
        doc.id,
        "knowledge",
        1,
        derived_input_hash(
            [
                facts["semantic"].output_hash,
                facts["ontology"].output_hash,
                facts["quality"].output_hash,
            ]
        ),
    )
    body = await store.get_json(knowledge_key)
    assert body == {
        "schema_version": 1,
        "section_count": 2,
        "section_types": {"BALANCE_SHEET": 1, "DIRECTORS_REPORT": 1},
        "concepts": ["TOTAL_ASSETS"],
        "concept_count": 1,
        "quality": {"ok": True, "error_count": 0, "warning_count": 0},
    }
```
- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement**:
```python
# backend/src/fdi/pipeline/stages/knowledge.py
"""KnowledgeStage: a DERIVED stage that folds a document's semantic section
classification, ontology concept mappings, and quality report into ONE
unified per-document knowledge record via the pure `consolidate`. Auto-chains
off ALL THREE of SemanticStage, OntologyStage, and QualityStage via
deps=("semantic", "ontology", "quality") -- the codebase's first
THREE-dependency stage (SP-15)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.knowledge import consolidate
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class KnowledgeStage:
    name = "knowledge"
    version = 1
    deps: tuple[str, ...] = ("semantic", "ontology", "quality")

    async def run(self, ctx: StageContext) -> object:
        semantic = cast(Mapping[str, object], await ctx.read("semantic"))
        ontology = cast(Mapping[str, object], await ctx.read("ontology"))
        quality = cast(Mapping[str, object], await ctx.read("quality"))
        return consolidate(semantic, ontology, quality)


REGISTRY.register(KnowledgeStage())
```
  Register in `stages/__init__.py` keeping imports ALPHABETICAL (`ast`, `embedding`, `graph`,
  `graphload`, `index`, `knowledge`, `linking`, `normalize`, `ocr`, `ontology`, `ping`, `provenance`,
  `quality`, `semantic`, `structure`):
```python
from fdi.pipeline.stages import (
    knowledge as knowledge,  # noqa: F401 - import for its registration side effect
)
```
  inserted between the existing `index` and `linking` import blocks.
- [ ] **Step 4: Run → passes**; full non-e2e suite green. Grep the test suite for any exact-list
  assertion on `successors("semantic")`, `successors("ontology")`, or `successors("quality")` (e.g.
  `== ["ontology"]` or a hardcoded list) and convert it to a membership check if found — the global
  `REGISTRY` registration now adds `"knowledge"` to all three lists too (mirrors the
  SP-6/SP-8/SP-9/SP-11/SP-12/SP-17b "search-and-fix" note; none expected).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages/knowledge.py backend/src/fdi/pipeline/stages/__init__.py backend/tests/integration/test_knowledge_stage.py && git commit -m "feat(pipeline): KnowledgeStage -- semantic+ontology+quality -> knowledge record, the first 3-dep stage"`

---

### Task 3: Audit + docs

**Files:** SP-15 spec status; `docs/architecture/ingestion-roadmap.md` (SP-15 → done + handoff).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0
  broken`; `core` pure — `fdi/core/knowledge.py` has no I/O and imports nothing from
  `fdi.core.semantic`/`fdi.core.ontology`/`fdi.core.quality`).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 `consolidate`, 2 `KnowledgeStage` +
  cache, 3 `successors("semantic")`/`successors("ontology")`/`successors("quality")` membership, 4
  gates + core purity + deferred scope) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-18** (once Steps 1-3 are actually green — do not
  flip status on a plan that hasn't been executed).
- [ ] **Step 5: Roadmap SP-15 → done** + handoff: the knowledge artifact `{schema_version:1,
  section_count, section_types, concepts, concept_count, quality}` at the `knowledge` artifact key;
  the pipeline chain is now `intake → ocr → normalize → ast → {structure, ontology, semantic} ;
  {ast,structure} → quality ; {semantic, ontology, quality} → knowledge` — the first stage in the
  codebase to actually declare and exercise THREE dependencies; note in the handoff that
  graph/provenance/embedding inclusion, numeric roll-ups, and external persistence remain explicitly
  deferred (§6), and that this per-document record is SP-16's per-document input for its
  cross-document entity-resolution layer.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-15 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1 (fixed three-artifact input order, five-key output shape,
decoupled `type`/`concept` handling with no enum import, sorted-unique-non-UNKNOWN concepts,
per-field not per-artifact degrade, quality-echo zero defaults), T2 (fixed `deps=("semantic",
"ontology", "quality")` order as cache identity, derived-stage auto-chain off all three, no injected
service). §3 architecture → T1 (core), T2 (pipeline). §4 errors → T1 (every malformed-input case
degrades per-field, never raises — covered by the malformed-semantic/malformed-ontology/
malformed-quality tests and the empty/keyless-artifact tests). §5 testing → unit T1 (9 cases incl.
type-bucketing, dedup, zero-default, malformed-input defense at each artifact), integration T2 (chain
+ ALL-THREE-successor membership assertions + full consolidated-record equality). §6 deferred → T3
roadmap handoff names each deferred item and what unblocks it. §7 done criteria → T3 audit.

**Type consistency:** `consolidate(semantic: Mapping[str, object], ontology: Mapping[str, object],
quality: Mapping[str, object]) -> dict[str, object]` (T1) consumed by `KnowledgeStage` (T2) via
`cast(Mapping[str, object], await ctx.read(...))` for all three reads — same `cast` idiom as
`ProvenanceStage`/`QualityStage`. The stage `deps=("semantic", "ontology", "quality")` matches BOTH
the `successors("semantic")`/`successors("ontology")`/`successors("quality")` membership assertions
(T2) and the `derived_input_hash([facts["semantic"].output_hash, facts["ontology"].output_hash,
facts["quality"].output_hash])` key computation (T2) — dep order and hash order are the same tuple,
stated once (spec §2 decision 7) and carried through consistently.

**Placeholder scan:** the T2 Step 4 grep-and-fix instruction is a concrete, bounded check (like
SP-9/SP-11/SP-12/SP-17b's identical note), not an open-ended TODO. No TODO/TBD anywhere; the
graph/provenance/embedding inclusion, numeric-roll-up, and external-persistence deferrals are
documented scope decisions (spec §6) with named follow-ons, not silently dropped requirements.
