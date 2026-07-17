# SP-7d — Linking: Figures + Footnotes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Enrich the OCR artifact with `figures` (schema 4), add a pure `extract_links` that surfaces figures
(with captions) and footnote paragraphs, and a `LinkingStage` that produces the `linking` artifact.

**Architecture:** `LinkingStage` (`deps=("ocr",)`) reads the enriched OCR artifact and returns
`{schema_version, figures:[{caption}], footnotes:[{text}]}`. The extraction is a pure, exhaustively-unit-
tested function, same posture as 7b's `merge_cross_page_tables` / 7c's `canonicalize_table`. Full cross-
reference resolution is deferred — see spec §6.

**Tech Stack:** Python 3.13, the SP-2 framework, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`. `core` pure (`extract_links`, `normalize_ocr` —
  no SDK import). Contract `1 kept, 0 broken`.
- Bumping `OcrStage.version` 3→4 (figures): the ocr key/output_hash changes; `normalize`/`ast`/`structure`
  keys derive from `output_hash` at runtime and self-adjust; only tests HARDCODING
  `artifact_key(...,"ocr",3,...)` need the 3→4 edit (confirmed: `test_ocr_stage.py` — the only such literal in
  the suite).
- Trust Azure's own grouping/classification (figure↔caption via `figures[].caption`, footnote via
  `role == "footnote"`) — no spatial re-derivation or marker-parsing heuristic in this slice (spec §2.1/2.2).
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`. mypy --strict: `isinstance`/`cast` for `object`-typed
  extraction; no blanket ignores.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration
`pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` ·
`PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: Add `figures` to the OCR enrichment (`OcrStage.version` → 4)

**Files:** Modify `backend/src/fdi/core/ocr_schema.py`, `backend/src/fdi/pipeline/stages/ocr.py`; Test
`backend/tests/unit/test_ocr_schema.py`, `backend/tests/integration/test_ocr_stage.py`.

**Interfaces:** enriched `normalize_ocr` (schema_version **4**) — each Azure `figures[]` entry becomes
`{"caption": str, "bounding_regions": [{page_number, polygon}]}` (`.caption.content`, missing `caption` key or
missing `content` → `""`; `bounding_regions` via the existing `_normalize_bounding_regions` helper).
`OcrStage.version = 4`.

- [ ] **Step 1: Failing test** — in `test_ocr_schema.py`, add a `figures` list to `_RAW` (one figure WITH a
  caption, one WITHOUT), bump the asserted `schema_version` to 4, and assert the normalized `figures` output:

```python
_RAW = {
    "content": "ACME LTD\nDirectors' Report\nRevenue 100",
    "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch",
               "lines": [{"content": "ACME LTD"}],
               "words": [{"content": "ACME"}, {"content": "LTD"}]}],
    "paragraphs": [
        {"role": "title", "content": "ACME LTD",
         "boundingRegions": [
             {"pageNumber": 1, "polygon": [0.0, 0.0, 1.0, 0.0, 1.0, 0.2, 0.0, 0.2]}
         ],
         "spans": [{"offset": 0, "length": 8}]},
        {"content": "Revenue 100", "boundingRegions": [],
         "spans": [{"offset": 9, "length": 11}]},
    ],
    "tables": [{"rowCount": 1, "columnCount": 2,
                "boundingRegions": [
                    {"pageNumber": 1, "polygon": [1.0, 1.0, 2.0, 1.0, 2.0, 1.5, 1.0, 1.5]}
                ],
                "cells": [{"rowIndex": 0, "columnIndex": 0, "rowSpan": 1, "columnSpan": 2,
                           "kind": "columnHeader", "content": "Year ended 31 March"}],
                "spans": [{"offset": 40, "length": 25}]}],
    "figures": [
        {"caption": {"content": "Fig 1: Revenue trend"},
         "boundingRegions": [
             {"pageNumber": 1, "polygon": [2.0, 2.0, 3.0, 2.0, 3.0, 2.5, 2.0, 2.5]}
         ]},
        {"boundingRegions": [{"pageNumber": 1, "polygon": []}]},  # no "caption" key at all
    ],
    "sections": [{"elements": ["/paragraphs/0", "/sections/1"]}],
}


def test_normalize_preserves_structural_payload() -> None:
    out = normalize_ocr(_RAW)
    assert out["schema_version"] == 4
    assert out["content"].startswith("ACME LTD")
    assert out["paragraphs"][0] == {
        "role": "title", "content": "ACME LTD",
        "bounding_regions": [
            {"page_number": 1, "polygon": [0.0, 0.0, 1.0, 0.0, 1.0, 0.2, 0.0, 0.2]}
        ],
        "spans": [{"offset": 0, "length": 8}],
    }
    assert out["paragraphs"][1]["role"] is None and out["paragraphs"][1]["bounding_regions"] == []
    assert out["tables"][0]["cells"][0] == {
        "row_index": 0, "column_index": 0, "row_span": 1, "column_span": 2,
        "kind": "columnHeader", "content": "Year ended 31 March",
    }
    assert out["tables"][0]["bounding_regions"][0]["page_number"] == 1
    assert out["tables"][0]["spans"] == [{"offset": 40, "length": 25}]
    assert out["sections"] == [{"elements": ["/paragraphs/0", "/sections/1"]}]
    assert out["figures"][0] == {
        "caption": "Fig 1: Revenue trend",
        "bounding_regions": [
            {"page_number": 1, "polygon": [2.0, 2.0, 3.0, 2.0, 3.0, 2.5, 2.0, 2.5]}
        ],
    }
    assert out["figures"][1] == {"caption": "", "bounding_regions": [{"page_number": 1, "polygon": []}]}


def test_empty_structure_is_empty_lists() -> None:
    out = normalize_ocr({"content": "x"})
    assert out["schema_version"] == 4
    assert out["paragraphs"] == []
    assert out["tables"] == []
    assert out["sections"] == []
    assert out["figures"] == []
```

- [ ] **Step 2: Run → fails** (`KeyError`/`AssertionError` — no `figures` key yet, `schema_version` still 3).
- [ ] **Step 3: Implement** — in `normalize_ocr` (`backend/src/fdi/core/ocr_schema.py`), add figures extraction
  alongside the existing tables/sections blocks (reuses `_normalize_bounding_regions`, already defined above
  in the same file):

```python
    # Extract and normalize figures
    figures_data: list[dict[str, object]] = []
    figures_raw = raw.get("figures")
    if isinstance(figures_raw, list):
        for fig in figures_raw:
            if isinstance(fig, dict):
                caption_raw = fig.get("caption")
                caption_content = ""
                if isinstance(caption_raw, dict):
                    content_val = caption_raw.get("content")
                    caption_content = content_val if isinstance(content_val, str) else ""
                figures_data.append(
                    {
                        "caption": caption_content,
                        "bounding_regions": _normalize_bounding_regions(fig.get("boundingRegions")),
                    }
                )
```

  Insert this block after the sections extraction (before `# Build result`) and add `"figures": figures_data`
  to the `result` dict; bump `result["schema_version"]` to `4`. In `backend/src/fdi/pipeline/stages/ocr.py`,
  bump `version = 4` and extend the version-history comment:

```python
class OcrStage:
    name = "ocr"
    # BUMP when FDI_AZURE_DI_MODEL or api_version changes (they define the output), or when
    # normalize_ocr's schema changes. version=2: SP-7a enriched the artifact with the
    # structural payload (bounding_regions, spans, cells, sections) as schema_version 2.
    # version=3: SP-7b added table spans (offset/length) as schema_version 3.
    # version=4: SP-7d added figures (caption + bounding_regions) as schema_version 4.
    version = 4
    deps: tuple[str, ...] = ()
```

- [ ] **Step 4: Run → passes**; update `test_ocr_stage.py` (`artifact_key(doc.id, "ocr", 3, ...)` →
  `artifact_key(doc.id, "ocr", 4, ...)`; `assert body["schema_version"] == 3` → `== 4`); run
  `pytest tests/integration/test_ocr_stage.py tests/integration/test_normalize_stage.py tests/integration/test_ast_stage.py tests/integration/test_structure_stage.py -q`
  (self-adjust — none of these hardcode the ocr version literal, confirmed by inspection) + full non-e2e
  suite. Gates.
- [ ] **Step 5: Commit** —
  `git add backend/src/fdi/core/ocr_schema.py backend/src/fdi/pipeline/stages/ocr.py backend/tests/unit/test_ocr_schema.py backend/tests/integration/test_ocr_stage.py && git commit -m "feat(ocr): preserve figures (schema 4); OcrStage.version 4"`

---

### Task 2: `extract_links` (pure, core)

**Files:** Create `backend/src/fdi/core/links.py`; Test `backend/tests/unit/test_links.py`.

**Interfaces:** `extract_links(ocr: Mapping[str, object]) -> dict[str, object]` →
`{"schema_version": 1, "figures": [{"caption": str}], "footnotes": [{"text": str}]}`. Figures come from the
enriched OCR artifact's `figures` (each already `{"caption", "bounding_regions"}` post-Task-1); footnotes are
paragraphs with `role == "footnote"` (`text` = `content`).

**Implementation (transcribe exactly):**

```python
"""Surface figures (with captions) and footnotes from the enriched OCR artifact as a
linking artifact. Pure — no I/O. Cross-reference resolution (matching an in-text marker
like "see Note 5" to its target) is explicitly deferred — see the design doc §6."""

from collections.abc import Mapping
from typing import cast


def extract_links(ocr: Mapping[str, object]) -> dict[str, object]:
    """Extract figures (captions) and footnote paragraphs from the enriched OCR artifact.

    Args:
        ocr: The enriched OCR artifact (`normalize_ocr` schema_version >= 4 shape),
             with `figures: [{caption, bounding_regions}]` and
             `paragraphs: [{role, content, ...}]`.

    Returns:
        `{"schema_version": 1, "figures": [{"caption": str}], "footnotes": [{"text": str}]}`.
    """
    figures_raw = cast(list[Mapping[str, object]], ocr.get("figures") or [])
    figures = [{"caption": cast(str, f.get("caption", ""))} for f in figures_raw]

    paragraphs_raw = cast(list[Mapping[str, object]], ocr.get("paragraphs") or [])
    footnotes = [
        {"text": cast(str, p.get("content", ""))}
        for p in paragraphs_raw
        if p.get("role") == "footnote"
    ]

    return {"schema_version": 1, "figures": figures, "footnotes": footnotes}
```

- [ ] **Step 1: Failing test** — `backend/tests/unit/test_links.py`, canned cases:

```python
from fdi.core.links import extract_links


def test_extracts_figures_and_footnotes() -> None:
    ocr = {
        "figures": [
            {"caption": "Fig 1: Revenue trend", "bounding_regions": []},
            {"caption": "Fig 2: Segment mix", "bounding_regions": []},
        ],
        "paragraphs": [
            {"role": "title", "content": "ACME LTD"},
            {"role": "footnote", "content": "1. Restated for FY23 comparatives."},
            {"role": None, "content": "Revenue 100"},
        ],
    }
    out = extract_links(ocr)
    assert out == {
        "schema_version": 1,
        "figures": [{"caption": "Fig 1: Revenue trend"}, {"caption": "Fig 2: Segment mix"}],
        "footnotes": [{"text": "1. Restated for FY23 comparatives."}],
    }


def test_no_figures_or_footnotes_is_empty_lists() -> None:
    ocr = {"figures": [], "paragraphs": [{"role": "title", "content": "ACME LTD"}]}
    assert extract_links(ocr) == {"schema_version": 1, "figures": [], "footnotes": []}


def test_missing_keys_default_to_empty() -> None:
    assert extract_links({}) == {"schema_version": 1, "figures": [], "footnotes": []}
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** `links.py` exactly as above.
- [ ] **Step 4: Run → passes** (3 tests). Gates (ruff/mypy/lint-imports).
- [ ] **Step 5: Commit** —
  `git add backend/src/fdi/core/links.py backend/tests/unit/test_links.py && git commit -m "feat(core): extract_links — surface figures and footnotes"`

---

### Task 3: `LinkingStage` (pipeline) + the ocr→linking chain

**Files:** Create `backend/src/fdi/pipeline/stages/linking.py`; Modify
`backend/src/fdi/pipeline/stages/__init__.py`; Test `backend/tests/integration/test_linking_stage.py`.

**Interfaces:** `class LinkingStage` (`name="linking"`, `version=1`, `deps=("ocr",)`); `run(ctx)`: reads
`ctx.read("ocr")`, returns `extract_links(ocr)`. Self-registers.

**Implementation (transcribe exactly):**

```python
"""LinkingStage: a DERIVED stage that surfaces figures (with captions) and footnotes
as structured elements. Auto-chains off OcrStage via `deps=("ocr",)`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.links import extract_links
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class LinkingStage:
    name = "linking"
    version = 1
    deps: tuple[str, ...] = ("ocr",)

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        return extract_links(ocr)


REGISTRY.register(LinkingStage())
```

- [ ] **Step 1: Failing test** (integration; LOCAL `StageRegistry`, fake OCR with 2 figures — one WITH a
  caption, one WITHOUT — and a footnote paragraph):

```python
"""LinkingStage auto-chains off OcrStage: `linking` is a DERIVED stage (deps=("ocr",))
that surfaces figures (with captions) and footnotes, against real Postgres + MinIO with
a FAKE DI client."""

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
from fdi.pipeline.stages.linking import LinkingStage
from fdi.pipeline.stages.ocr import OcrStage
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


async def test_linking_surfaces_figures_and_footnotes(  # fixtures from conftest
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

    fake = FakeOcrClient(
        {
            "content": "ACME LTD\nFig 1: Revenue trend\n1. Restated for FY23 comparatives.",
            "pages": [{"pageNumber": 1}],
            "paragraphs": [
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 0, "length": 8}],
                },
                {
                    "role": "footnote",
                    "content": "1. Restated for FY23 comparatives.",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 40, "length": 34}],
                },
            ],
            "tables": [],
            "figures": [
                {
                    "caption": {"content": "Fig 1: Revenue trend"},
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                },
                {
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],  # no caption -> ""
                },
            ],
            "sections": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(LinkingStage())
    resolver = DagResolver(reg.all())

    assert "linking" in resolver.successors("ocr")  # the chain is automatic

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="linking", run_id=str(uuid.uuid4()), **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    linking_key = artifact_key(
        doc.id, "linking", 1, derived_input_hash([facts["ocr"].output_hash])
    )
    body = await store.get_json(linking_key)
    assert body["schema_version"] == 1
    assert body["figures"] == [{"caption": "Fig 1: Revenue trend"}, {"caption": ""}]
    assert body["footnotes"] == [{"text": "1. Restated for FY23 comparatives."}]
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError: fdi.pipeline.stages.linking`).
- [ ] **Step 3: Implement** the stage; register in `stages/__init__.py` (alphabetical: `ast`, `linking`,
  `normalize`, `ocr`, `ping`, `quality`, `semantic`, `structure`):

```python
"""Importing this package registers every built-in stage."""

from fdi.pipeline.stages import (
    ast as ast,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    linking as linking,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    normalize as normalize,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import ocr as ocr  # noqa: F401 - import for its registration side effect
from fdi.pipeline.stages import ping as ping  # noqa: F401 - import for its registration side effect
from fdi.pipeline.stages import (
    quality as quality,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    semantic as semantic,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    structure as structure,  # noqa: F401 - import for its registration side effect
)
```

- [ ] **Step 4: Run → passes**; full non-e2e suite green (registering `linking` adds it to
  `successors("ocr")` — membership-fix any exact-list assertion; confirmed by inspection that
  `test_normalize_stage.py`/`test_structure_stage.py` both already use `in`, not `==`, so no other test file
  needs a change). Gates.
- [ ] **Step 5: Commit** —
  `git add backend/src/fdi/pipeline/stages/linking.py backend/src/fdi/pipeline/stages/__init__.py backend/tests/integration/test_linking_stage.py && git commit -m "feat(pipeline): LinkingStage — surface figures and footnotes"`

---

### Task 4: Audit + docs

- [ ] **Step 1: Gates** — ruff, mypy --strict src, PYTHONUTF8=1 lint-imports (1 kept, 0 broken; core pure).
- [ ] **Step 2: Full suite** — `pytest -q` (green; e2e+azure skip).
- [ ] **Step 3: Audit spec §7** — map criteria (1 figures enrichment + version bump, 2 `extract_links` cases,
  3 `LinkingStage`, 4 gates) to tests.
- [ ] **Step 4: Spec status → Implemented 2026-07-17.**
- [ ] **Step 5: Roadmap** — SP-7d done (figures with captions + footnotes surfaced via `LinkingStage`); note
  deferred: cross-reference detection (in-text marker → target resolution), footnote-marker →
  referencing-paragraph linking, caption → nearest-figure spatial re-association beyond Azure's own grouping
  — each a MEASURED (SP-3) candidate. Update the SP-7 row (`SP-7d` → done) and the "Next" pointer to SP-9
  (canonical graph).
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-7d done — figures + footnotes linking; roadmap"`

## Self-Review

**Spec coverage:** §2.4 figures enrichment → T1. §3.2 `extract_links` (figures + footnotes) → T2. §3.3
`LinkingStage` + chain → T3. §5 testing → unit T1/T2, integration T3. §7 done criteria → T4.

**Type consistency:** `extract_links(ocr: Mapping[str, object]) -> dict[str, object]` (T2) consumed by
`LinkingStage` (T3). Figure dict shape (`caption`, `bounding_regions`) matches T1's enrichment; footnote
extraction reuses the paragraph `role`/`content` shape already established by 7a. No new type introduced that
isn't already load-bearing elsewhere in the SP-7 line.

**Placeholder scan:** cross-reference detection, footnote-marker linking, and spatial caption re-association
are explicit, justified deferrals (§6 of the spec), not gaps papered over. No TODO/TBD in any code block.
