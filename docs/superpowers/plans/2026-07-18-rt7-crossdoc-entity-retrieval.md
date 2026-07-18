# RT-7 Cross-Document / Entity Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** `doc_key`-scoped filing timelines (`entity_timeline`) and cross-version concept comparisons
(`compare_concept_across_versions`), reusing RT-6's `concept_lookup` and SP-16's `consolidate_entity`
unchanged. Two new HTTP routes.

**Architecture:** Pure `fdi.core.entities.build_entity_timeline`/`build_concept_comparison` assemble
output from data an I/O orchestrator (`fdi.pipeline.entities`) hands them. The orchestrator resolves a
`doc_key`'s document versions via `DocumentRepository.list_by_doc_key` (no clustering — `doc_key` IS the
entity identity, see the design spec's decision 1), reads each member's `knowledge` artifact (via a
newly-public `fdi.pipeline.crossdoc.read_artifact`) or calls RT-6's `concept_lookup` per member, and
delegates assembly to `fdi.core.entities`.

**Tech Stack:** Python 3.13, `uv`, SQLAlchemy async, pytest + testcontainers. Reuses `fdi.core.crossdoc.
consolidate_entity`, `fdi.pipeline.tables.concept_lookup`, `DocumentRepository.list_by_doc_key`.

**Read first:** `docs/superpowers/specs/2026-07-18-rt7-crossdoc-entity-retrieval-design.md` (the design
spec this plan implements — especially §2 decision 1, the `doc_key`-vs-crossdoc-`entity_id` fork).

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive), CI-enforced; stay "1 kept,
  0 broken". `fdi.core.entities` is PURE: no I/O/config/clock/RNG, no import of `fdi.pipeline.*` or
  `fdi.core.ontology`.
- `fdi.core.entities.build_entity_timeline` REUSES `fdi.core.crossdoc.consolidate_entity` — do not
  reimplement version-ordering or concept-presence aggregation.
- `fdi.pipeline.entities` REUSES RT-6's `fdi.pipeline.tables.concept_lookup` UNCHANGED — do not
  reimplement numeric extraction or concept matching.
- Preserve RT-6's `None` (no `structure` yet) vs. `[]` (structure present, concept not found)
  distinction all the way to the API response as an explicit `status` field — never collapse both to an
  empty `rows` list without a `status` alongside it.
- Per-task gates before every commit: `uv run --directory backend ruff check .` ·
  `uv run --directory backend mypy --strict src` ·
  `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter` · the task's pytest.
  Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) → commit.
- Intake dedups by PDF-byte checksum (`intake_document`) — to produce two VERSIONS of the same
  `doc_key`, call `intake_document` twice with the SAME `doc_key` but DIFFERENT pdf bytes (a fresh
  `uuid4()` in the fake body is enough); identical bytes collapse to the SAME document, not a new
  version.

---

### Task 1: Promote `fdi.pipeline.crossdoc._read_artifact` to public `read_artifact`

**Files:**
- Modify: `backend/src/fdi/pipeline/crossdoc.py`
- Verify (no code change expected): `backend/tests/integration/test_resolve_entities.py`

**Interfaces:**
- Renames: `_read_artifact(repo, store, doc_id, stage) -> Mapping[str, object] | None` →
  `read_artifact(repo, store, doc_id, stage) -> Mapping[str, object] | None`. No behavior change.

This is a pure rename (drop the leading underscore) so `fdi.pipeline.entities` (Task 4) can reuse the
exact same "latest artifact or `None`, isolate one document's store failure" helper `resolve_entities`
already relies on, instead of a second copy. There is no new test to write FOR this rename — the
existing `test_resolve_entities.py` suite is the regression check (it exercises `_read_artifact`'s
behavior indirectly through `resolve_entities`; renaming must not change any of its assertions).

- [ ] **Step 1: Confirm the only call site**

Run: `grep -rn "_read_artifact" backend/src` — expect exactly one definition (in `crossdoc.py`) and one
call site (`resolve_entities`, same file). If any other file imports `_read_artifact` directly, note it
and update that import too (none are expected — it is a module-private helper).

- [ ] **Step 2: Rename**

In `backend/src/fdi/pipeline/crossdoc.py`:
```python
async def read_artifact(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str, stage: str
) -> Mapping[str, object] | None:
    """The latest `stage` artifact for `doc_id`, or None -- same read path as
    the GET /documents/{id}/artifacts/{stage} route.

    Public (RT-7's fdi.pipeline.entities reuses this for `knowledge`, the same
    "one document's store failure isolates, does not abort the whole scan"
    posture resolve_entities relies on for its corpus-wide loop).

    A store-level read failure (the recorded `artifact_key` no longer resolves
    against THIS store -- e.g. deleted, or from a differently-configured store)
    degrades to "no artifact" rather than aborting the whole corpus scan over
    one document. Same isolate-one-failure posture as `routes._probe`: a scan
    over many independently-produced artifacts should not let one bad object
    sink every other document's resolution."""
    run = await repo.latest_run(doc_id, stage)
    if run is None or run.artifact_key is None:
        return None
    try:
        return cast(Mapping[str, object], await store.get_json(run.artifact_key))
    except Exception:
        return None
```
Update the ONE call site inside `resolve_entities` (`await _read_artifact(...)` → `await
read_artifact(...)`, twice: once for `embedding`, once for `knowledge`).

- [ ] **Step 3: Run the regression test**

Run: `uv run --directory backend pytest tests/integration/test_resolve_entities.py -v`
Expected: PASS, unchanged (4/4 or whatever the current count is — the rename must not alter any
assertion's outcome).

- [ ] **Step 4: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/crossdoc.py
git commit -m "refactor(pipeline): crossdoc.read_artifact -- promote to public for RT-7 reuse"
```

---

### Task 2: `fdi.core.entities.build_entity_timeline` (pure)

**Files:**
- Create: `backend/src/fdi/core/entities.py`
- Test: `backend/tests/unit/test_entities_timeline.py`

**Interfaces:**
- Produces: `build_entity_timeline(doc_key: str, members: list[Mapping[str, object]]) -> dict[str, object]`
  → `{schema_version:1, doc_key, member_count, timeline:[{doc_id, doc_key, version, section_count,
  concept_count, quality_ok, concepts, quality:{ok,error_count,warning_count}}], concepts,
  concept_presence}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_entities_timeline.py
from fdi.core.entities import build_entity_timeline


def _member(doc_id: str, version: int | None, concepts: list[str], ok: bool) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "doc_key": "acme.pdf",
        "version": version,
        "knowledge": {
            "section_count": 3,
            "concepts": concepts,
            "concept_count": len(concepts),
            "quality": {"ok": ok, "error_count": 0 if ok else 2, "warning_count": 1},
        },
    }


def test_build_entity_timeline_two_versions_ordered() -> None:
    members = [
        _member("d2", 2, ["NET_PROFIT"], True),
        _member("d1", 1, ["REVENUE", "NET_PROFIT"], True),
    ]
    out = build_entity_timeline("acme.pdf", members)
    assert out["schema_version"] == 1
    assert out["doc_key"] == "acme.pdf"
    assert out["member_count"] == 2
    assert "entity_id" not in out

    timeline = out["timeline"]
    assert [t["doc_id"] for t in timeline] == ["d1", "d2"]  # version-ordered, not input order
    assert [t["version"] for t in timeline] == [1, 2]
    assert timeline[0]["concepts"] == ["NET_PROFIT", "REVENUE"]
    assert timeline[1]["concepts"] == ["NET_PROFIT"]
    assert timeline[0]["section_count"] == 3
    assert timeline[0]["quality"] == {"ok": True, "error_count": 0, "warning_count": 1}
    assert out["concepts"] == ["NET_PROFIT", "REVENUE"]


def test_build_entity_timeline_missing_knowledge_degrades() -> None:
    members = [{"doc_id": "d1", "doc_key": "k", "version": 1, "knowledge": {}}]
    out = build_entity_timeline("k", members)
    t = out["timeline"][0]
    assert t["section_count"] == 0
    assert t["concept_count"] == 0
    assert t["quality_ok"] is False
    assert t["concepts"] == []


def test_build_entity_timeline_none_version_sorts_last() -> None:
    members = [
        {"doc_id": "d1", "doc_key": "k", "version": None, "knowledge": {}},
        {"doc_id": "d2", "doc_key": "k", "version": 1, "knowledge": {}},
    ]
    out = build_entity_timeline("k", members)
    assert [t["doc_id"] for t in out["timeline"]] == ["d2", "d1"]


def test_build_entity_timeline_empty_members() -> None:
    assert build_entity_timeline("k", []) == {
        "schema_version": 1, "doc_key": "k", "member_count": 0,
        "timeline": [], "concepts": [], "concept_presence": {},
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_entities_timeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.entities'`.

- [ ] **Step 3: Write the implementation**

```python
# backend/src/fdi/core/entities.py
"""Cross-document entity retrieval assembly (pure; no I/O, config, clock, RNG).

build_entity_timeline reshapes fdi.core.crossdoc.consolidate_entity's existing
output into a doc_key-scoped, version-ordered timeline -- REUSING consolidate_
entity's version-ordering and concept-presence math rather than reimplementing
them (design spec docs/superpowers/specs/2026-07-18-rt7-crossdoc-entity-
retrieval-design.md, decision 2). build_concept_comparison (added alongside
this function) assembles a cross-version concept comparison from a list of
per-document fdi.pipeline.tables.concept_lookup results, preserving concept_
lookup's None ("no structure artifact yet") vs [] ("structure present,
concept not found") distinction per version rather than collapsing both to an
empty row list (decision 3). Reads inputs as plain Mapping/list -- no import
of fdi.core.ontology or any fdi.pipeline module.
"""

from collections.abc import Mapping

from fdi.core.crossdoc import consolidate_entity


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _version_sort_key(entry: Mapping[str, object]) -> tuple[int, int, str]:
    """Version ascending, None-version last, doc_id tie-break -- the SAME
    ordering consolidate_entity uses internally. Duplicated (not imported):
    consolidate_entity's sort_key is a local closure; reaching into fdi.core.
    crossdoc's private internals from a sibling module is worse than these
    five lines."""
    doc_id = entry.get("doc_id")
    doc_id_s = doc_id if isinstance(doc_id, str) else ""
    v = _int_or_none(entry.get("version"))
    return (0, v, doc_id_s) if v is not None else (1, 0, doc_id_s)


def build_entity_timeline(
    doc_key: str, members: list[Mapping[str, object]]
) -> dict[str, object]:
    """`members`: [{doc_id, doc_key, version, knowledge}], the same shape
    fdi.pipeline.crossdoc.resolve_entities' `stash` values use. A missing/
    malformed `knowledge` degrades per consolidate_entity's own rules -- this
    function adds no new validation on top of it."""
    entity = consolidate_entity(list(members))

    member_views_raw = entity["members"]
    quality_trend_raw = entity["quality_trend"]
    concept_presence_raw = entity["concept_presence"]
    member_views = member_views_raw if isinstance(member_views_raw, list) else []
    quality_trend = quality_trend_raw if isinstance(quality_trend_raw, list) else []
    concept_presence = concept_presence_raw if isinstance(concept_presence_raw, dict) else {}

    timeline: list[dict[str, object]] = []
    for mv_raw, q_raw in zip(member_views, quality_trend, strict=True):
        mv = mv_raw if isinstance(mv_raw, Mapping) else {}
        q = q_raw if isinstance(q_raw, Mapping) else {}
        version = mv.get("version")
        presence_key: object = version if version is not None else mv.get("doc_id")
        concepts = sorted(
            c for c, versions in concept_presence.items()
            if isinstance(versions, list) and presence_key in versions
        )
        timeline.append({
            "doc_id": mv.get("doc_id"),
            "doc_key": mv.get("doc_key"),
            "version": version,
            "section_count": mv.get("section_count"),
            "concept_count": mv.get("concept_count"),
            "quality_ok": mv.get("quality_ok"),
            "concepts": concepts,
            "quality": {
                "ok": q.get("ok"),
                "error_count": q.get("error_count"),
                "warning_count": q.get("warning_count"),
            },
        })

    return {
        "schema_version": 1,
        "doc_key": doc_key,
        "member_count": entity["member_count"],
        "timeline": timeline,
        "concepts": entity["concepts"],
        "concept_presence": concept_presence,
    }
```

(`_version_sort_key`/`_int_or_none` are unused by this function alone — they are added here now because
Task 3's `build_concept_comparison` lives in the SAME file and needs them; if `ruff` flags them as
unused after Task 2 alone, that is expected and resolves itself once Task 3 lands in the same commit
sequence. Do not suppress the warning — just proceed to Task 3 before the final gate check of Task 2 if
the linter blocks the commit; otherwise land Task 2's gate check tolerating the not-yet-used private
helpers is fine since `ruff`/mypy do not treat module-private unused functions as unused-import errors.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_entities_timeline.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/entities.py backend/tests/unit/test_entities_timeline.py
git commit -m "feat(core): entities.build_entity_timeline -- doc_key-scoped version timeline via consolidate_entity"
```

---

### Task 3: `fdi.core.entities.build_concept_comparison` (pure)

**Files:**
- Modify: `backend/src/fdi/core/entities.py`
- Test: `backend/tests/unit/test_entities_comparison.py`

**Interfaces:**
- Produces: `build_concept_comparison(doc_key: str, concept: str, member_lookups: list[Mapping[str, object]])
  -> dict[str, object]` → `{schema_version:1, doc_key, concept, versions:[{doc_id, doc_key, version,
  status, rows}]}`, `status ∈ {"matched","no_match","no_structure"}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_entities_comparison.py
from fdi.core.entities import build_concept_comparison

_ROW = {
    "table_index": 0,
    "row_label": "Total revenue",
    "values": [{"column_header": "FY23", "raw": "1,000", "value": 1000.0}],
}


def test_build_concept_comparison_status_per_version() -> None:
    member_lookups = [
        {"doc_id": "d3", "doc_key": "k", "version": 3, "rows": [_ROW]},
        {"doc_id": "d1", "doc_key": "k", "version": 1, "rows": None},
        {"doc_id": "d2", "doc_key": "k", "version": 2, "rows": []},
    ]
    out = build_concept_comparison("k", "REVENUE", member_lookups)
    assert out == {
        "schema_version": 1,
        "doc_key": "k",
        "concept": "REVENUE",
        "versions": [
            {"doc_id": "d1", "doc_key": "k", "version": 1, "status": "no_structure", "rows": []},
            {"doc_id": "d2", "doc_key": "k", "version": 2, "status": "no_match", "rows": []},
            {"doc_id": "d3", "doc_key": "k", "version": 3, "status": "matched", "rows": [_ROW]},
        ],
    }


def test_build_concept_comparison_matched_rows_are_unchanged() -> None:
    # proves no re-shaping/renaming of concept_lookup's own row shape
    member_lookups = [{"doc_id": "d1", "doc_key": "k", "version": 1, "rows": [_ROW, _ROW]}]
    out = build_concept_comparison("k", "REVENUE", member_lookups)
    assert out["versions"][0]["rows"] == [_ROW, _ROW]


def test_build_concept_comparison_defensively_resorts_input() -> None:
    member_lookups = [
        {"doc_id": "d2", "doc_key": "k", "version": 2, "rows": []},
        {"doc_id": "d1", "doc_key": "k", "version": 1, "rows": []},
    ]
    out = build_concept_comparison("k", "REVENUE", member_lookups)
    assert [v["version"] for v in out["versions"]] == [1, 2]


def test_build_concept_comparison_empty() -> None:
    assert build_concept_comparison("k", "REVENUE", []) == {
        "schema_version": 1, "doc_key": "k", "concept": "REVENUE", "versions": [],
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_entities_comparison.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_concept_comparison'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/src/fdi/core/entities.py`:
```python
def build_concept_comparison(
    doc_key: str, concept: str, member_lookups: list[Mapping[str, object]]
) -> dict[str, object]:
    """`member_lookups`: [{doc_id, doc_key, version, rows: list[Mapping] | None}]
    -- `rows` is exactly fdi.pipeline.tables.concept_lookup's return value for
    that member, UNCHANGED. `status` makes concept_lookup's None ("no
    structure artifact yet") vs [] ("structure present, concept not found")
    distinction explicit and durable through this assembly step rather than
    collapsing both to an indistinguishable empty `rows` list. Defensively
    re-sorts by version (mirrors consolidate_entity's own posture) regardless
    of input order."""
    versions: list[dict[str, object]] = []
    for ml in sorted(member_lookups, key=_version_sort_key):
        rows_raw = ml.get("rows")
        status: str
        rows: list[dict[str, object]]
        if isinstance(rows_raw, list) and rows_raw:
            status = "matched"
            rows = [dict(r) for r in rows_raw if isinstance(r, Mapping)]
        elif isinstance(rows_raw, list):
            status, rows = "no_match", []
        else:
            status, rows = "no_structure", []  # None, or malformed -> degrade safely

        versions.append({
            "doc_id": ml.get("doc_id"),
            "doc_key": ml.get("doc_key"),
            "version": ml.get("version"),
            "status": status,
            "rows": rows,
        })

    return {
        "schema_version": 1,
        "doc_key": doc_key,
        "concept": concept,
        "versions": versions,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_entities_comparison.py tests/unit/test_entities_timeline.py -v`
Expected: PASS (all 8).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/entities.py backend/tests/unit/test_entities_comparison.py
git commit -m "feat(core): entities.build_concept_comparison -- cross-version concept comparison, status-tagged"
```

---

### Task 4: `fdi.pipeline.entities.entity_timeline`

**Files:**
- Create: `backend/src/fdi/pipeline/entities.py`
- Test: `backend/tests/integration/test_entity_timeline.py`

**Interfaces:**
- Consumes: `DocumentRepository.list_by_doc_key`; `read_artifact` (Task 1); `build_entity_timeline`
  (Task 2).
- Produces: `entity_timeline(*, session: AsyncSession, store: ArtifactStore, doc_key: str) ->
  dict[str, object] | None` — `None` if `doc_key` matches zero documents.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_entity_timeline.py
"""fdi.pipeline.entities.entity_timeline against real Postgres + MinIO, FAKE
OCR/semantic clients. Reuses the ocr->...->knowledge harness
test_resolve_entities.py established (no `embedding` stage -- RT-7's entity
identity is `doc_key`, not an embedding cluster; see design spec decision 1).

Session-scoped DB note: unlike resolve_entities (a corpus-wide scan),
entity_timeline is scoped to exactly one doc_key (DocumentRepository.
list_by_doc_key) -- a fresh uuid4() doc_key per test cannot collide with any
other integration test file's documents, so EXACT member_count/timeline
assertions are safe here (no membership-only hedge needed, unlike
test_resolve_entities.py).
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.entities import entity_timeline
from fdi.pipeline.intake import intake_document
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


def _build_registry() -> tuple[StageRegistry, DagResolver]:
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(SemanticStage())
    reg.register(OntologyStage())
    reg.register(QualityStage())
    reg.register(KnowledgeStage())
    return reg, DagResolver(reg.all())


async def _process_document(*, doc_id, session_factory, store, reg, resolver, title) -> None:
    fake_ocr = FakeOcrClient({
        "content": title,
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "title", "content": title},
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": "Total Assets"},
        ],
        "tables": [],
    })
    fake_semantic = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})
    common = {
        "doc_id": doc_id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    for stage, services in (
        ("ocr", {"ocr": fake_ocr}), ("normalize", {}), ("ast", {}), ("structure", {}),
        ("semantic", {"semantic": fake_semantic}), ("ontology", {}), ("quality", {}),
        ("knowledge", {}),
    ):
        await run_stage(stage_name=stage, run_id=str(uuid.uuid4()), services=services, **common)


@pytest.fixture
async def two_version_doc_key(session_factory, store) -> tuple[str, str, str]:
    """Two documents, SAME doc_key, DIFFERENT pdf bytes (distinct checksums ->
    version 1, version 2). Returns (doc_key, doc_id_v1, doc_id_v2)."""
    doc_key = str(uuid.uuid4())
    reg, resolver = _build_registry()
    ids: list[str] = []
    for title in ("ACME FY23", "ACME FY24"):
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
            filename=f"{title}.pdf", doc_key=doc_key, content_type="application/pdf",
            session_factory=session_factory, store=store,
        )
        await _process_document(
            doc_id=doc.id, session_factory=session_factory, store=store,
            reg=reg, resolver=resolver, title=title,
        )
        ids.append(doc.id)
    return doc_key, ids[0], ids[1]


async def test_entity_timeline_two_versions_ordered(
    session_factory, store, two_version_doc_key
) -> None:
    doc_key, id_v1, id_v2 = two_version_doc_key
    async with session_factory() as s:
        result = await entity_timeline(session=s, store=store, doc_key=doc_key)

    assert result is not None
    assert result["doc_key"] == doc_key
    assert result["member_count"] == 2
    assert "entity_id" not in result
    timeline = result["timeline"]
    assert [t["doc_id"] for t in timeline] == [id_v1, id_v2]
    assert [t["version"] for t in timeline] == [1, 2]
    assert timeline[0]["section_count"] >= 1
    assert "TOTAL_ASSETS" in timeline[0]["concepts"]
    assert "TOTAL_ASSETS" in result["concepts"]


async def test_entity_timeline_unknown_doc_key_returns_none(session_factory, store) -> None:
    async with session_factory() as s:
        assert await entity_timeline(session=s, store=store, doc_key="no-such-key") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_entity_timeline.py -v`
Expected: FAIL — `ModuleNotFoundError: fdi.pipeline.entities`.

- [ ] **Step 3: Implement**

```python
# backend/src/fdi/pipeline/entities.py
"""RT-7 orchestrator: turn a `doc_key`'s document versions into a filing
timeline (entity_timeline) or a cross-version concept comparison
(compare_concept_across_versions). Pipeline layer -- imports platform (repo +
store) + core, the same shape as fdi.pipeline.tables/fdi.pipeline.crossdoc. No
new stage, no new artifact, no clustering: `doc_key` IS the entity identity
here (design spec docs/superpowers/specs/2026-07-18-rt7-crossdoc-entity-
retrieval-design.md, decision 1) -- fdi.pipeline.crossdoc.resolve_entities
(corpus-wide, embedding-similarity clustering) is a SEPARATE, complementary
discovery step this module does not call. Both functions return None when
`doc_key` matches zero documents (the caller maps this to 404).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.entities import build_entity_timeline
from fdi.pipeline.crossdoc import read_artifact
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def entity_timeline(
    *, session: AsyncSession, store: ArtifactStore, doc_key: str
) -> dict[str, object] | None:
    docs = await DocumentRepository(session).list_by_doc_key(doc_key)
    if not docs:
        return None
    reg_repo = RegistryRepository(session)
    members: list[dict[str, object]] = []
    for doc in docs:
        knowledge = await read_artifact(reg_repo, store, doc.id, "knowledge")
        members.append({
            "doc_id": doc.id,
            "doc_key": doc.doc_key,
            "version": doc.version,
            "knowledge": knowledge if knowledge is not None else {},
        })
    return build_entity_timeline(doc_key, members)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_entity_timeline.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/entities.py backend/tests/integration/test_entity_timeline.py
git commit -m "feat(pipeline): entities.entity_timeline -- doc_key-scoped filing timeline via consolidate_entity"
```

---

### Task 5: `fdi.pipeline.entities.compare_concept_across_versions`

**Files:**
- Modify: `backend/src/fdi/pipeline/entities.py`
- Test: `backend/tests/integration/test_compare_concept.py`

**Interfaces:**
- Consumes: `DocumentRepository.list_by_doc_key`; `fdi.pipeline.tables.concept_lookup` (RT-6,
  unchanged); `build_concept_comparison` (Task 3).
- Produces: `compare_concept_across_versions(*, session: AsyncSession, store: ArtifactStore, doc_key: str,
  concept: CanonicalConcept) -> dict[str, object] | None` — `None` if `doc_key` matches zero documents.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_compare_concept.py
"""fdi.pipeline.entities.compare_concept_across_versions against real
Postgres + MinIO, FAKE OCR. Reuses test_tables_lookup.py's numeric-table OCR
fixture pattern (structure only -- concept_lookup never reads `knowledge`),
intaking the SAME doc_key across MULTIPLE versions with DIFFERENT revenue
figures to prove the cross-version comparison the roadmap's "REVENUE across
FY2021->FY2023" example describes, AND a third version left un-structured to
exercise the None ("no_structure") vs [] ("no_match") distinction end to end
(design spec decision 3) -- not just at the pure-function level
(test_entities_comparison.py already covers that in isolation).
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.ontology import CanonicalConcept
from fdi.pipeline.entities import compare_concept_across_versions
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.structure import StructureStage
from fdi.platform.db.engine import make_session_factory
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


def _numeric_table_ocr(header: str, value: str) -> FakeOcrClient:
    return FakeOcrClient({
        "content": "ACME LTD",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "title", "content": "ACME LTD",
             "boundingRegions": [{"pageNumber": 1, "polygon": []}],
             "spans": [{"offset": 0, "length": 8}]},
        ],
        "tables": [{
            "rowCount": 2, "columnCount": 2,
            "spans": [{"offset": 20, "length": 60}],
            "boundingRegions": [{"pageNumber": 1, "polygon": []}],
            "cells": [
                {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader", "content": "Particulars"},
                {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": header},
                {"rowIndex": 1, "columnIndex": 0, "content": "Total revenue from operations"},
                {"rowIndex": 1, "columnIndex": 1, "content": value},
            ],
        }],
        "sections": [],
    })


async def _intake_and_structure(session_factory, store, doc_key, ocr) -> str:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
    resolver = DagResolver(reg.all())
    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": ocr}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id


async def _intake_unstructured(session_factory, store, doc_key) -> str:
    """A THIRD version of the same doc_key, intaken but never run through
    `structure` -- exercises concept_lookup's None ("no_structure") branch."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
        filename="c.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    return doc.id


@pytest.fixture
async def three_version_doc_key(session_factory, store) -> tuple[str, str, str, str]:
    """v1: FY24 revenue 12,345. v2: FY25 revenue 15,000. v3: intaken only, no
    `structure` artifact. Returns (doc_key, id_v1, id_v2, id_v3)."""
    doc_key = str(uuid.uuid4())
    id_v1 = await _intake_and_structure(
        session_factory, store, doc_key, _numeric_table_ocr("FY24", "12,345")
    )
    id_v2 = await _intake_and_structure(
        session_factory, store, doc_key, _numeric_table_ocr("FY25", "15,000")
    )
    id_v3 = await _intake_unstructured(session_factory, store, doc_key)
    return doc_key, id_v1, id_v2, id_v3


async def test_compare_revenue_across_versions(
    session_factory, store, three_version_doc_key
) -> None:
    doc_key, id_v1, id_v2, id_v3 = three_version_doc_key
    async with session_factory() as s:
        result = await compare_concept_across_versions(
            session=s, store=store, doc_key=doc_key, concept=CanonicalConcept.REVENUE
        )
    assert result is not None
    assert result["doc_key"] == doc_key
    assert result["concept"] == "REVENUE"
    versions = {v["doc_id"]: v for v in result["versions"]}

    assert versions[id_v1]["status"] == "matched"
    assert versions[id_v1]["rows"][0]["values"][0] == {
        "column_header": "FY24", "raw": "12,345", "value": 12345.0,
    }
    assert versions[id_v2]["status"] == "matched"
    assert versions[id_v2]["rows"][0]["values"][0] == {
        "column_header": "FY25", "raw": "15,000", "value": 15000.0,
    }
    assert versions[id_v3]["status"] == "no_structure"
    assert versions[id_v3]["rows"] == []


async def test_compare_concept_not_present_is_no_match(
    session_factory, store, three_version_doc_key
) -> None:
    doc_key, id_v1, id_v2, _id_v3 = three_version_doc_key
    async with session_factory() as s:
        result = await compare_concept_across_versions(
            session=s, store=store, doc_key=doc_key, concept=CanonicalConcept.EQUITY
        )
    assert result is not None
    versions = {v["doc_id"]: v for v in result["versions"]}
    assert versions[id_v1]["status"] == "no_match"
    assert versions[id_v2]["status"] == "no_match"


async def test_compare_concept_unknown_doc_key_returns_none(session_factory, store) -> None:
    async with session_factory() as s:
        result = await compare_concept_across_versions(
            session=s, store=store, doc_key="no-such-key", concept=CanonicalConcept.REVENUE
        )
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_compare_concept.py -v`
Expected: FAIL — `ImportError: cannot import name 'compare_concept_across_versions'`.

- [ ] **Step 3: Implement**

Append to `backend/src/fdi/pipeline/entities.py`:
```python
from fdi.core.entities import build_concept_comparison  # add to the existing import line
from fdi.core.ontology import CanonicalConcept
from fdi.pipeline.tables import concept_lookup


async def compare_concept_across_versions(
    *, session: AsyncSession, store: ArtifactStore, doc_key: str, concept: CanonicalConcept
) -> dict[str, object] | None:
    docs = await DocumentRepository(session).list_by_doc_key(doc_key)
    if not docs:
        return None
    member_lookups: list[dict[str, object]] = []
    for doc in docs:
        rows = await concept_lookup(session=session, store=store, doc_id=doc.id, concept=concept)
        member_lookups.append({
            "doc_id": doc.id, "doc_key": doc.doc_key, "version": doc.version, "rows": rows,
        })
    return build_concept_comparison(doc_key, concept.value, member_lookups)
```
(Merge the new imports into the file's existing import block rather than appending a second one; keep
alphabetical order per the project's ruff import-sort convention.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_compare_concept.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/entities.py backend/tests/integration/test_compare_concept.py
git commit -m "feat(pipeline): entities.compare_concept_across_versions -- cross-version concept comparison via RT-6 concept_lookup"
```

---

### Task 6: API — schemas + `GET /entities/{doc_key}/timeline` + `POST /entities/compare`

**Files:**
- Modify: `backend/src/fdi/api/schemas.py`
- Modify: `backend/src/fdi/api/routes.py`
- Modify: `backend/tests/integration/test_api.py` (append a new section — this file already carries
  every other route's tests, including RT-6's `/documents/{doc_id}/tables`/`/concepts/{concept}`
  section immediately above; RT-7's routes are added the SAME way, not a new file, matching that
  precedent exactly)

**Interfaces:**
- Produces: `EntityTimelineQualityView`, `EntityTimelineEntryView`, `EntityTimelineView`,
  `ConceptComparisonValueView`, `ConceptComparisonRowView`, `ConceptComparisonVersionView`,
  `ConceptCompareRequest`, `ConceptComparisonView` (schemas); `GET /entities/{doc_key}/timeline`,
  `POST /entities/compare` (routes).

**Pre-check confirmed (do not re-derive):** `test_api.py` already defines everything this task's tests
need — `client` (real ASGI app over real Postgres/MinIO, `make_client` factory underneath),
`_alembic_applied`, `session_factory` (line ~88), `store` (bucket `"artifacts"`, line ~95 — the bucket
the RUNNING APP reads, distinct from the `"test-artifacts"` bucket every OTHER integration file's
`store` fixture uses; RT-6's own `doc_with_numeric_table` fixture at line ~1285 uses THIS file's
`session_factory`/`store`, not a locally redefined pair — RT-7's new fixture must do the same). Model the
new section directly on RT-6's `doc_with_numeric_table` + `test_get_tables_route`/`test_concept_lookup_
route` block (lines ~1280–1408) immediately preceding where this section is appended.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_api.py`, directly after the existing RT-6 section (after line
~1408's `test_concept_lookup_route_404_unknown_doc`):

```python
# --------------------------------------------------------------------------
# RT-7: GET /entities/{doc_key}/timeline, POST /entities/compare
# --------------------------------------------------------------------------


@pytest.fixture
async def two_version_doc_key_via_api(session_factory, store) -> str:
    """Two documents, SAME doc_key, DIFFERENT pdf bytes (distinct checksums ->
    version 1 revenue FY24=12,345, version 2 revenue FY25=15,000). Mirrors
    doc_with_numeric_table's OCR shape (RT-6, immediately above) but run
    TWICE under one doc_key to exercise RT-7's cross-version comparison."""
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.structure import StructureStage

    def _ocr(header: str, value: str) -> FakeOcrClient:
        return FakeOcrClient({
            "content": "ACME LTD",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [{
                "role": "title", "content": "ACME LTD",
                "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                "spans": [{"offset": 0, "length": 8}],
            }],
            "tables": [{
                "rowCount": 2, "columnCount": 2,
                "spans": [{"offset": 20, "length": 40}],
                "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                     "content": "Particulars"},
                    {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": header},
                    {"rowIndex": 1, "columnIndex": 0, "content": "Total revenue from operations"},
                    {"rowIndex": 1, "columnIndex": 1, "content": value},
                ],
            }],
            "sections": [],
        })

    doc_key = str(uuid.uuid4())
    for header, value in (("FY24", "12,345"), ("FY25", "15,000")):
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
            filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
            session_factory=session_factory, store=store,
        )
        reg = StageRegistry()
        reg.register(OcrStage())
        reg.register(StructureStage())
        resolver = DagResolver(reg.all())
        common = {
            "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
            "store": store, "registry": reg, "resolver": resolver,
        }
        await run_stage(
            stage_name="ocr", run_id=str(uuid.uuid4()),
            services={"ocr": _ocr(header, value)}, **common,
        )
        await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    return doc_key


async def test_compare_route_matched_across_versions(
    client: AsyncClient, _alembic_applied, two_version_doc_key_via_api
) -> None:
    resp = await client.post(
        "/entities/compare",
        json={"doc_key": two_version_doc_key_via_api, "concept": "REVENUE"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_key"] == two_version_doc_key_via_api
    assert body["concept"] == "REVENUE"
    assert [v["status"] for v in body["versions"]] == ["matched", "matched"]
    assert body["versions"][0]["rows"][0]["values"][0] == {
        "column_header": "FY24", "raw": "12,345", "value": 12345.0,
    }
    assert body["versions"][1]["rows"][0]["values"][0] == {
        "column_header": "FY25", "raw": "15,000", "value": 15000.0,
    }


async def test_compare_route_404_unknown_doc_key(
    client: AsyncClient, _alembic_applied
) -> None:
    resp = await client.post(
        "/entities/compare", json={"doc_key": "no-such-key", "concept": "REVENUE"}
    )
    assert resp.status_code == 404


async def test_entity_timeline_route(
    client: AsyncClient, _alembic_applied, two_version_doc_key_via_api
) -> None:
    resp = await client.get(f"/entities/{two_version_doc_key_via_api}/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_key"] == two_version_doc_key_via_api
    assert body["member_count"] == 2
    assert "entity_id" not in body
    assert [t["version"] for t in body["timeline"]] == [1, 2]


async def test_entity_timeline_route_404_unknown_doc_key(
    client: AsyncClient, _alembic_applied
) -> None:
    resp = await client.get("/entities/no-such-key/timeline")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "compare_route or entity_timeline_route" -v`
Expected: FAIL — `404`/routing errors (routes not registered yet) or `ImportError` if the schema names
are referenced before existing. Confirm the failure is "route not found," not a fixture-setup error
unrelated to this task (e.g. a `doc_with_numeric_table`-style fixture typo).

- [ ] **Step 3: Add schemas**

Append to `backend/src/fdi/api/schemas.py`:
```python
class EntityTimelineQualityView(BaseModel):
    ok: bool
    error_count: int
    warning_count: int


class EntityTimelineEntryView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    section_count: int
    concept_count: int
    quality_ok: bool
    concepts: list[str]
    quality: EntityTimelineQualityView


class EntityTimelineView(BaseModel):
    schema_version: int
    doc_key: str
    member_count: int
    timeline: list[EntityTimelineEntryView]
    concepts: list[str]
    concept_presence: dict[str, list[int | str]]


class ConceptComparisonValueView(BaseModel):
    column_header: str
    raw: str
    value: float | None


class ConceptComparisonRowView(BaseModel):
    table_index: int
    row_label: str
    values: list[ConceptComparisonValueView]


class ConceptComparisonVersionView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    status: str
    rows: list[ConceptComparisonRowView]


class ConceptCompareRequest(BaseModel):
    doc_key: str
    concept: CanonicalConcept


class ConceptComparisonView(BaseModel):
    schema_version: int
    doc_key: str
    concept: str
    versions: list[ConceptComparisonVersionView]
```
`CanonicalConcept` is not currently imported in `schemas.py` — add `from fdi.core.ontology import
CanonicalConcept` to its import block (schemas.py currently only imports from `pydantic`; confirm no
existing conflicting import before adding).

- [ ] **Step 4: Add routes**

In `backend/src/fdi/api/routes.py`: add `ConceptCompareRequest`, `ConceptComparisonView`,
`EntityTimelineView` to the `fdi.api.schemas` import block (alphabetical); add `compare_concept_across_
versions, entity_timeline` to a new `from fdi.pipeline.entities import (...)` import line (alphabetical
among the existing `fdi.pipeline.*` imports). Append near the existing `/entities/resolve` section:
```python
# --------------------------------------------------------------------------
# RT-7: cross-document / entity retrieval -- doc_key-scoped filing timeline
# and cross-version concept comparison. `doc_key` IS the entity identity here
# (design spec docs/superpowers/specs/2026-07-18-rt7-crossdoc-entity-
# retrieval-design.md, decision 1) -- distinct from POST /entities/resolve's
# corpus-wide, embedding-similarity `entity_id`, which is recomputed per call
# and therefore not a stable route identifier.
# --------------------------------------------------------------------------


@router.get("/entities/{doc_key}/timeline", response_model=EntityTimelineView)
async def entity_timeline_route(
    doc_key: str, session: SessionDep, store: StoreDep
) -> EntityTimelineView:
    result = await entity_timeline(session=session, store=store, doc_key=doc_key)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no documents for doc_key {doc_key!r}")
    return EntityTimelineView.model_validate(result)


@router.post("/entities/compare", response_model=ConceptComparisonView)
async def compare_concept_route(
    body: ConceptCompareRequest, session: SessionDep, store: StoreDep
) -> ConceptComparisonView:
    result = await compare_concept_across_versions(
        session=session, store=store, doc_key=body.doc_key, concept=body.concept
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"no documents for doc_key {body.doc_key!r}")
    return ConceptComparisonView.model_validate(result)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "compare_route or entity_timeline_route" -v`
Expected: PASS (all 4 new tests). Then run the FULL `test_api.py` file to confirm nothing above the new
section regressed: `uv run --directory backend pytest tests/integration/test_api.py -v`.

- [ ] **Step 6: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(api): GET /entities/{doc_key}/timeline, POST /entities/compare"
```

---

### Task 7: Full gates, roadmap + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-rt7-crossdoc-entity-retrieval-design.md` (Status →
  Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (RT-7 row → done + "what RT-7 shipped" handoff)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`,
  `PYTHONUTF8=1 lint-imports` (1 kept/0 broken). Grep-confirm `fdi.core.entities` imports neither
  `fdi.pipeline.*` nor `fdi.core.ontology`.

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`; green (e2e/azure/neo4j/milvus/bge
  skip). Note the passed count against the prior total (RT-6's handoff count + this plan's new unit +
  integration + API tests).

- [ ] **Step 3: Audit RT-7 done criteria** (§7 of the design spec) — map each criterion to the task that
  satisfies it; specifically confirm (a) `build_entity_timeline` never emits `entity_id`, (b)
  `build_concept_comparison`'s `status` field is exercised end-to-end (integration test, not just unit),
  (c) both pipeline functions return `None` for an unknown `doc_key` and both routes 404 on it, (d)
  `test_resolve_entities.py` still passes after the `read_artifact` rename.

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps 1–3 green).

- [ ] **Step 5: Roadmap** — flip the RT-7 row to `✅ done`; add a "What RT-7 shipped that RT-8 must
  know" handoff summarizing: `doc_key` is the entity identity for `entity_timeline`/`compare_concept_
  across_versions` (NOT a crossdoc `entity_id` — `resolve_entities` stays the corpus-wide discovery step
  whose `members[].doc_key` feeds these tools, one `doc_key` at a time); the `status` field
  (`matched`/`no_match`/`no_structure`) on every comparison version; both functions/routes reuse RT-6's
  `concept_lookup` and SP-16's `consolidate_entity` unchanged; no persisted index (on-demand, cheap —
  `list_by_doc_key` is a single indexed query, not a corpus scan); the deferred multi-`doc_key`
  cluster-fusion follow-on (§6 of the design spec) RT-8's agent may need to work around by calling
  `/entities/resolve` first and looping RT-7's tools per discovered `doc_key`.

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: RT-7 done-criteria audit, spec status, roadmap handoff"
```

---

## Self-Review

**Spec coverage:** §2 decision 1 (doc_key identity, the central fork) → Task 6's route wiring +
docstring, called out again in Task 7's roadmap handoff and flagged for human review in the design
spec's §7 done-criterion 7 (not auto-satisfied by writing code). §2 decision 2 (`build_entity_timeline`
reuses `consolidate_entity`, overrides `entity_id`) → Task 2, tested by
`test_build_entity_timeline_two_versions_ordered`'s `"entity_id" not in out` assertion. §2 decision 3
(`status` field, no None/[] conflation) → Task 3 (unit) + Task 5 (integration, the `id_v3` "no_structure"
case). §2 decision 4 (pure/impure split, new modules not extending crossdoc/tables) → Tasks 2–5. §2
decision 5 (no persisted index) → not built, confirmed absent in Task 7's audit. §2 decision 6 (route
shapes) → Task 6. §3.1–3.4 → Tasks 2, 3, 4/5, 6 respectively, code close to 1:1 with the design spec
(minor mypy-narrowing differences between the spec's `assert isinstance` sketch and the plan's
`... if isinstance(...) else ...` ternary-narrowing style — functionally identical, the plan's version is
what actually gets typed and tested). §4 error handling → Task 2/3 unit tests (empty/malformed input) +
Task 4/5 integration tests (unknown doc_key, missing knowledge, no structure). §5 testing → Tasks 2–6
implement every test file the design spec names. §6 deferred → not built, confirmed absent in Task 7.
§7 done criteria → Task 7's audit step maps each one explicitly.

**Placeholder scan:** All six tasks carry complete, runnable code, including Task 6's API test — it was
written against `test_api.py`'s ACTUAL fixtures (`client`, `_alembic_applied`, `session_factory`,
`store` at bucket `"artifacts"`), read directly (lines ~35–119, ~1280–1408) rather than guessed, and
modeled 1:1 on RT-6's own `doc_with_numeric_table`/`test_get_tables_route` section immediately
preceding it in the same file. No task defers its test content to "read X and figure it out."

**Type consistency:** `build_entity_timeline(str, list[Mapping]) -> dict` (Task 2) and
`build_concept_comparison(str, str, list[Mapping]) -> dict` (Task 3) share one `_version_sort_key`/
`_int_or_none` pair in the same file. `fdi.pipeline.entities.entity_timeline`/`compare_concept_across_
versions` (Tasks 4/5) call them with the exact member/lookup shapes the pure functions expect —
`{doc_id, doc_key, version, knowledge}` and `{doc_id, doc_key, version, rows}` respectively, matching
§3.1's documented input contracts. `CanonicalConcept` flows unchanged from `fdi.core.ontology` through
`concept_lookup` (RT-6, untouched) into `ConceptCompareRequest.concept` (Task 6) — no re-validation, no
new enum. `read_artifact`'s public signature (Task 1) is consumed identically by both its original
caller (`resolve_entities`) and its new one (`entity_timeline`).

**Risk flagged:** Task 2's note about `_version_sort_key`/`_int_or_none` being unused until Task 3 lands
in the same file — if the executor runs Task 2's `ruff check .` gate in true isolation (e.g. a stash or
separate worktree) before Task 3 exists, `ruff` may flag them as unused private functions (F401-adjacent
dead-code lint, not an import error) and block the commit; the plan's intent is both functions land in
one file across two consecutive commits, so either commit Tasks 2+3 together, or accept a one-commit
`ruff` suppression window is not needed because `ruff` does not flag unused *functions* (only unused
imports/variables) by default — confirm this against the project's actual `ruff` config before treating
it as a blocker.
