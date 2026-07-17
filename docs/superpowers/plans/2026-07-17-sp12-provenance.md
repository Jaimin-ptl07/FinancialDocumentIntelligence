# SP-12 — Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Trace every SP-9 graph node back to its source OCR page + bounding geometry via a pure
`build_provenance(graph, ocr)` + a `ProvenanceStage` (`deps=("graph", "ocr")`) that auto-chains off
BOTH artifacts. This is the codebase's **first stage to declare two dependencies** — it exercises
SP-2's already-generic multi-dep machinery, not a framework change.

**Architecture:** `ProvenanceStage` reads the graph (`ctx.read("graph")`) and the OCR artifact
(`ctx.read("ocr")`), returns `{schema_version, node_provenance:[{node_id, page, bounding_regions}]}`.
For each graph node: a `paragraph` node matches the FIRST OCR paragraph (document order) whose
`content` startswith the node's (80-char-truncated) `label`; a `section` node matches the FIRST OCR
paragraph whose `content` equals the node's (untruncated) `label`; the `doc` node takes the page of
the FIRST OCR paragraph with any non-empty `bounding_regions`. A node with no match, or whose match
has empty/unusable geometry, is omitted. Reuses SP-2's derived-stage machinery unchanged
(`input_hash = derived_input_hash([facts[d].output_hash for d in stage.deps])`, `successors(name) =
[n for n, s in stages.items() if name in s.deps]` — both already iterate `deps` generically; no
`run.py`/`resolver.py` change needed). Design: `docs/superpowers/specs/2026-07-17-sp12-provenance-design.md`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` pure —
  `fdi/core/provenance.py` has no I/O. `ProvenanceStage` lives in `pipeline`.
- `mypy --strict`: use `isinstance` narrowing (mirror `fdi/core/graph.py`'s and
  `fdi/core/ontology.py`'s defensive style) when reading `object`-typed values out of the `graph`/
  `ocr` `Mapping`s; no blanket `# type: ignore`. `cast` only where `isinstance` alone can't satisfy
  the checker (narrowing `ctx.read("graph")`/`ctx.read("ocr")`'s `object` return to `Mapping[str,
  object]` before calling `build_provenance`, same as every other derived stage's `run` method).
- Determinism: `build_provenance` is a pure function of its two `Mapping` arguments — no RNG/clock;
  "first match in document order" is a deterministic (if imprecise — spec §2 decision 3) resolution
  rule, itself a tested property, not incidental dict/list iteration order.
- `ProvenanceStage.deps = ("graph", "ocr")` — this fixed order is part of the artifact's cache
  identity (`derived_input_hash([graph.output_hash, ocr.output_hash])`); do not reorder.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.
- Integration: reuse `session_factory` + `store` fixtures; unique ids per test; **LOCAL**
  `StageRegistry` (register `OcrStage`+`NormalizeStage`+`AstStage`+`GraphStage`+`ProvenanceStage`, not
  the global `REGISTRY`).
- Registering `ProvenanceStage` into the global `REGISTRY` (via `stages/__init__.py`) means the
  process-wide `DagResolver(REGISTRY.all()).successors("graph")` AND `.successors("ocr")` also gain
  `"provenance"`. Any test asserting on either successor list must use a **membership** check
  (`"provenance" in ...`), never an exact-list equality — grep for exact-list assertions on
  `successors("graph")`/`successors("ocr")` before landing Task 2 and fix any found (none are
  expected; every prior stage-adding SP — SP-6/SP-8/SP-9/SP-11/SP-7d — already left the suite
  membership-only).

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration
`pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` ·
`PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `build_provenance` (core, pure)

**Files:** Create `backend/src/fdi/core/provenance.py`; Test `backend/tests/unit/test_build_provenance.py`.

**Interfaces:** `build_provenance(graph: Mapping[str, object], ocr: Mapping[str, object]) -> dict[str,
object]` → `{"schema_version": 1, "node_provenance": [{"node_id": str, "page": int,
"bounding_regions": [dict, ...]}]}`. Algorithm per spec §3.2.

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_build_provenance.py
from fdi.core.provenance import build_provenance

_GRAPH = {
    "schema_version": 1,
    "nodes": [
        {"id": "doc", "type": "document", "label": "ACME LTD Annual Report"},
        {"id": "s0", "type": "section", "label": "Directors' Report"},
        {"id": "s0p0", "type": "paragraph", "label": "The board is pleased with results."},
    ],
    "edges": [],
}

_REGIONS_TITLE = [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
_REGIONS_HEADING = [{"page_number": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]
_REGIONS_PARA = [{"page_number": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]

_OCR = {
    "schema_version": 4,
    "content": "ACME",
    "paragraphs": [
        {"role": "title", "content": "ACME LTD Annual Report", "bounding_regions": _REGIONS_TITLE, "spans": []},
        {"role": "sectionHeading", "content": "Directors' Report", "bounding_regions": _REGIONS_HEADING, "spans": []},
        {
            "content": "The board is pleased with results. Revenue grew twelve percent year over year.",
            "bounding_regions": _REGIONS_PARA,
            "spans": [],
        },
    ],
}


def test_paragraph_node_prefix_match_returns_page_and_regions() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "s0p0")
    assert entry == {"node_id": "s0p0", "page": 2, "bounding_regions": _REGIONS_PARA}


def test_section_node_exact_heading_match_returns_page_and_regions() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "s0")
    assert entry == {"node_id": "s0", "page": 1, "bounding_regions": _REGIONS_HEADING}


def test_document_node_uses_first_paragraph_with_regions() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "doc")
    assert entry == {"node_id": "doc", "page": 1, "bounding_regions": _REGIONS_TITLE}


def test_unmatched_node_is_omitted() -> None:
    graph = {
        "nodes": [{"id": "s9", "type": "section", "label": "Nonexistent Heading"}],
    }
    out = build_provenance(graph, _OCR)
    assert out["node_provenance"] == []


def test_matched_paragraph_with_empty_bounding_regions_is_omitted() -> None:
    ocr = {
        "paragraphs": [
            {"role": "sectionHeading", "content": "Directors' Report", "bounding_regions": [], "spans": []},
        ],
    }
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "Directors' Report"}]}
    out = build_provenance(graph, ocr)
    assert out["node_provenance"] == []


def test_empty_graph_yields_empty_node_provenance() -> None:
    assert build_provenance({}, _OCR) == {"schema_version": 1, "node_provenance": []}
    assert build_provenance({"nodes": []}, _OCR) == {"schema_version": 1, "node_provenance": []}


def test_ambiguous_shared_prefix_resolves_to_first_in_document_order() -> None:
    ocr = {
        "paragraphs": [
            {"content": "Revenue grew twelve percent.", "bounding_regions": [{"page_number": 3, "polygon": []}], "spans": []},
            {"content": "Revenue grew twelve percent in the prior year too.", "bounding_regions": [{"page_number": 4, "polygon": []}], "spans": []},
        ],
    }
    graph = {"nodes": [{"id": "s0p0", "type": "paragraph", "label": "Revenue grew twelve percent."}]}
    out = build_provenance(graph, ocr)
    assert out["node_provenance"] == [
        {"node_id": "s0p0", "page": 3, "bounding_regions": [{"page_number": 3, "polygon": []}]}
    ]


def test_malformed_ocr_paragraphs_degrade_without_raising() -> None:
    ocr = {
        "paragraphs": [
            "not a mapping",
            {"content": None, "bounding_regions": [], "spans": []},
            {"content": "Directors' Report", "bounding_regions": "not a list", "spans": []},
            {"content": "Directors' Report", "bounding_regions": [{"page_number": "not an int", "polygon": []}], "spans": []},
        ],
    }
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "Directors' Report"}]}
    out = build_provenance(graph, ocr)
    assert out == {"schema_version": 1, "node_provenance": []}
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** the algorithm from spec §3.2 (pure). Use `isinstance` to narrow
  `object`-typed values read off the `Mapping`s (mirror `fdi/core/graph.py`/`fdi/core/ontology.py`):
```python
# backend/src/fdi/core/provenance.py
"""Provenance: trace every SP-9 graph node back to its source OCR paragraph's
page and bounding geometry. Pure, deterministic; no I/O."""

from collections.abc import Mapping

_Candidate = tuple[str, list[dict[str, object]]]


def _ocr_paragraph_candidates(ocr: Mapping[str, object]) -> list[_Candidate]:
    """(content, bounding_regions) pairs from the OCR artifact's paragraphs,
    in document order. A paragraph whose `content` isn't a `str` is dropped
    -- it cannot be matched against a node's `str` label. `bounding_regions`
    degrades to `[]` when absent/malformed (never raises)."""
    candidates: list[_Candidate] = []
    paragraphs_raw = ocr.get("paragraphs")
    if not isinstance(paragraphs_raw, list):
        return candidates
    for para in paragraphs_raw:
        if not isinstance(para, Mapping):
            continue
        content = para.get("content")
        if not isinstance(content, str):
            continue
        regions_raw = para.get("bounding_regions")
        regions: list[dict[str, object]] = []
        if isinstance(regions_raw, list):
            for region in regions_raw:
                if isinstance(region, Mapping):
                    regions.append(dict(region))
        candidates.append((content, regions))
    return candidates


def _page_of(regions: list[dict[str, object]]) -> int | None:
    """The first region's page_number, if present and an int."""
    if not regions:
        return None
    page = regions[0].get("page_number")
    return page if isinstance(page, int) else None


def build_provenance(graph: Mapping[str, object], ocr: Mapping[str, object]) -> dict[str, object]:
    """Resolve every `graph` node to the page + bounding geometry of the OCR
    paragraph it was built from.

    - a "paragraph" node: the FIRST ocr paragraph (document order) whose
      `content` startswith the node's (80-char-truncated) `label`.
    - a "section" node: the FIRST ocr paragraph whose `content` equals the
      node's (untruncated) `label`.
    - the "doc" node (`id == "doc"`): the page of the FIRST ocr paragraph
      with any non-empty `bounding_regions`.

    A node with no match, or whose matched paragraph has empty/unusable
    geometry, is OMITTED from `node_provenance` -- never emitted with a
    null/placeholder page. Ambiguous matches (shared prefixes/duplicate
    headings) resolve to the first in document order -- deterministic, a
    documented thin-slice limitation (spec §2 decision 3 / §6).
    """
    candidates = _ocr_paragraph_candidates(ocr)

    doc_entry: dict[str, object] | None = None
    for _content, regions in candidates:
        page = _page_of(regions)
        if page is not None:
            doc_entry = {"node_id": "doc", "page": page, "bounding_regions": regions}
            break

    nodes_raw = graph.get("nodes")
    nodes: list[Mapping[str, object]] = []
    if isinstance(nodes_raw, list):
        for entry in nodes_raw:
            if isinstance(entry, Mapping):
                nodes.append(entry)

    node_provenance: list[dict[str, object]] = []
    for node in nodes:
        node_id = node.get("id")
        if node_id == "doc":
            if doc_entry is not None:
                node_provenance.append(doc_entry)
            continue

        node_type = node.get("type")
        label_raw = node.get("label")
        label = label_raw if isinstance(label_raw, str) else ""

        match: _Candidate | None = None
        if node_type == "paragraph":
            match = next((c for c in candidates if c[0].startswith(label)), None)
        elif node_type == "section":
            match = next((c for c in candidates if c[0] == label), None)
        else:
            continue

        if match is None or not isinstance(node_id, str):
            continue
        _, regions = match
        page = _page_of(regions)
        if page is None:
            continue
        node_provenance.append({"node_id": node_id, "page": page, "bounding_regions": regions})

    return {"schema_version": 1, "node_provenance": node_provenance}
```
- [ ] **Step 4: Run → passes** (8 tests).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/provenance.py backend/tests/unit/test_build_provenance.py && git commit -m "feat(core): build_provenance -- graph node -> OCR page/geometry join"`

---

### Task 2: `ProvenanceStage` (pipeline) + the graph+ocr→provenance chain

**Files:** Create `backend/src/fdi/pipeline/stages/provenance.py`; Modify
`backend/src/fdi/pipeline/stages/__init__.py`; Test `backend/tests/integration/test_provenance_stage.py`.

**Interfaces:** `class ProvenanceStage` (`name="provenance"`, `version=1`, `deps=("graph", "ocr")`);
`run(ctx)`: `graph = cast(Mapping[str, object], await ctx.read("graph"))`; `ocr =
cast(Mapping[str, object], await ctx.read("ocr"))`; `return build_provenance(graph, ocr)`.
Self-registers.

- [ ] **Step 1: Failing test** (integration; run ocr→normalize→ast→graph→provenance in a LOCAL registry)
```python
# backend/tests/integration/test_provenance_stage.py
"""ProvenanceStage auto-chains off BOTH GraphStage and OcrStage: `provenance`
is the codebase's FIRST two-dependency DERIVED stage (`deps=("graph", "ocr")`),
joining the SP-9 graph's nodes against the enriched OCR artifact's paragraph
geometry. Against real Postgres + MinIO with a FAKE OCR client."""

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
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
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


async def test_provenance_chains_off_graph_and_ocr(session_factory, store) -> None:
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
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
                },
                {
                    "role": "sectionHeading",
                    "content": "Directors' Report",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
                },
                {
                    "content": "The board is pleased.",
                    "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
                },
            ],
            "tables": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    resolver = DagResolver(reg.all())

    assert "provenance" in resolver.successors("graph")  # the chain is automatic
    assert "provenance" in resolver.successors("ocr")    # ... off BOTH deps

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
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    provenance_key = artifact_key(
        doc.id, "provenance", 1,
        derived_input_hash([facts["graph"].output_hash, facts["ocr"].output_hash]),
    )
    body = await store.get_json(provenance_key)
    assert body["schema_version"] == 1
    assert body["node_provenance"] == [
        {
            "node_id": "doc",
            "page": 1,
            "bounding_regions": [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
        },
        {
            "node_id": "s0",
            "page": 1,
            "bounding_regions": [{"page_number": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
        },
        {
            "node_id": "s0p0",
            "page": 2,
            "bounding_regions": [{"page_number": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
        },
    ]
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement**:
```python
# backend/src/fdi/pipeline/stages/provenance.py
"""ProvenanceStage: a DERIVED stage that resolves every SP-9 graph node back
to its source OCR paragraph's page + bounding geometry. The codebase's FIRST
two-dependency stage: deps=("graph", "ocr")."""

from collections.abc import Mapping
from typing import cast

from fdi.core.provenance import build_provenance
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class ProvenanceStage:
    name = "provenance"
    version = 1
    deps: tuple[str, ...] = ("graph", "ocr")

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        return build_provenance(graph, ocr)


REGISTRY.register(ProvenanceStage())
```
  Register in `stages/__init__.py` keeping imports ALPHABETICAL (`ast`, `graph`, `linking`,
  `normalize`, `ocr`, `ontology`, `ping`, `provenance`, `quality`, `semantic`, `structure`):
```python
from fdi.pipeline.stages import (
    provenance as provenance,  # noqa: F401 - import for its registration side effect
)
```
  inserted between the existing `ping` and `quality` import blocks.
- [ ] **Step 4: Run → passes**; full non-e2e suite green. Grep the test suite for any exact-list
  assertion on `successors("graph")` or `successors("ocr")` (e.g. `== ["graph"]` or a hardcoded list
  containing `"ocr"`'s successors) and convert it to a membership check if found — the global
  `REGISTRY` registration now adds `"provenance"` to both lists too (mirrors the SP-6/SP-8/SP-9/SP-11
  "search-and-fix" note; none expected).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages/provenance.py backend/src/fdi/pipeline/stages/__init__.py backend/tests/integration/test_provenance_stage.py && git commit -m "feat(pipeline): ProvenanceStage -- graph+ocr -> node provenance, the first 2-dep stage"`

---

### Task 3: Audit + docs

**Files:** SP-12 spec status; `docs/architecture/ingestion-roadmap.md` (SP-12 → done + handoff).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0
  broken`; `core` pure — `fdi/core/provenance.py` has no I/O).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 `build_provenance`, 2 `ProvenanceStage` +
  cache, 3 `successors("graph")`/`successors("ocr")` membership, 4 gates + core purity + deferred
  scope) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-17** (once Steps 1-3 are actually green — do not
  flip status on a plan that hasn't been executed).
- [ ] **Step 5: Roadmap SP-12 → done** + handoff: the provenance artifact `{schema_version:1,
  node_provenance:[{node_id, page, bounding_regions}]}` at the `provenance` artifact key; the pipeline
  chain is now `intake → ocr → normalize → ast → {graph, ontology, quality, semantic} ; graph + ocr →
  provenance` — the first stage in the codebase to actually declare and exercise two dependencies (note
  in the handoff that SP-17b's own spec, if it lands second, should have its "first two-dependency
  stage" claim corrected to reflect landing order); ambiguous shared-prefix/duplicate-heading matches
  resolve to "first in document order" (a documented limitation, not a bug) until a span-offset join
  replaces it; table-cell/figure/footnote node provenance and cross-reference edge provenance remain
  explicitly deferred pending SP-9's graph gaining those node types and SP-7d's linking being wired into
  the graph as edges.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-12 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1 (label-based join not span-offset, per-type match rule, first-
match-wins determinism, omission-over-null, `core` purity), T2 (fixed `deps=("graph","ocr")` order as
cache identity, derived-stage auto-chain off both). §3 architecture → T1 (core), T2 (pipeline). §4
errors → T1 (every malformed-input case degrades, never raises — covered by
`test_malformed_ocr_paragraphs_degrade_without_raising` and the empty-graph/unmatched/empty-regions
tests). §5 testing → unit T1 (8 cases incl. ambiguous-prefix determinism + malformed-data defense),
integration T2 (chain + BOTH-successor membership assertions). §6 deferred → T3 roadmap handoff names
each deferred item and what unblocks it. §7 done criteria → T3 audit.

**Type consistency:** `build_provenance(graph: Mapping[str, object], ocr: Mapping[str, object]) ->
dict[str, object]` (T1) consumed by `ProvenanceStage` (T2) via `cast(Mapping[str, object], await
ctx.read(...))` for both reads — same `cast` idiom as `GraphStage`/`OntologyStage`/`QualityStage`. The
stage `deps=("graph", "ocr")` matches BOTH the `successors("graph")`/`successors("ocr")` membership
assertions (T2) and the `derived_input_hash([facts["graph"].output_hash, facts["ocr"].output_hash])`
key computation (T2) — dep order and hash order are the same tuple, stated once (spec §2 decision 5)
and carried through consistently.

**Placeholder scan:** the T2 Step 4 grep-and-fix instruction is a concrete, bounded check (like
SP-9/SP-11's identical note), not an open-ended TODO. No TODO/TBD anywhere; the span-offset-matching
and richer-node-type deferrals are documented scope decisions (spec §6) with named follow-ons, not
silently dropped requirements.
