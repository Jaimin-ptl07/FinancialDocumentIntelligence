# TR-1 P1 — basis stage (standalone/consolidated region segmentation)

## Status
Complete.

## What was built

- `backend/src/fdi/core/basis.py` (new, pure, no I/O):
  - `detect_basis_regions(ast, provenance) -> dict` — walks `ast["sections"]`
    in order, classifies each heading against SA-700/Schedule III anchor
    phrases (`_CONSOLIDATED_ANCHORS`, and `_STANDALONE_ANCHORS` derived from
    them by substituting "standalone" for "consolidated"), resolves each
    anchor's page via `provenance["node_provenance"]` (`node_id == f"s{i}"`).
    An anchor opens a region of its basis running to the next
    different-basis anchor's page - 1, or the document's max resolvable page
    for the final region. Consecutive same-basis anchors collapse to one
    region-open. A document with anchors of only one basis collapses to a
    single region spanning the document's full min/max page (covers
    front/back matter). No anchors at all -> `regions: []`,
    `default_basis: "unknown"`. Total — never raises on malformed input.
  - `page_basis(basis_artifact, page) -> str` — inclusive region lookup,
    falls back to the artifact's `default_basis` (or `"unknown"`);
    `page=None` -> `"unknown"`. Total.
- `backend/src/fdi/pipeline/stages/basis.py` (new): `BasisStage`
  (`name="basis"`, `version=1`, `deps=("ast", "provenance")`), thin
  pass-through to `detect_basis_regions`. Registered via
  `REGISTRY.register(BasisStage())` at module bottom, same pattern as
  `linking.py`/`structure.py`.
- `backend/src/fdi/pipeline/stages/__init__.py`: added the one import line
  (`from fdi.pipeline.stages import (basis as basis, ...)`) so `BasisStage`
  is discovered on package import, alphabetically between `ast` and
  `chunks`.
- Tests:
  - `backend/tests/unit/test_basis.py` — 9 cases: standalone-then-
    consolidated two-region split with correct bounds, standalone-only doc
    (one region spanning full page range), consolidated-only doc
    (symmetric), no anchors (empty regions + unknown default), an anchor
    with no resolvable page (skipped, not a phantom region), malformed
    ast/provenance shapes (never raises), `page_basis` hit/miss/boundary
    cases, `page_basis(None)`, and `page_basis` on malformed artifacts.
  - `backend/tests/integration/test_basis_stage.py`
    (`pytestmark = pytest.mark.integration`) — a full-chain test
    (ocr→normalize→ast→structure→graph→provenance→basis) against real
    Postgres+MinIO with `FakeOcrClient`, asserting `basis` auto-chains off
    both `ast` and `provenance` and the persisted artifact is correct; plus
    a no-Docker smoke test driving `BasisStage.run` directly against a
    hand-built `StageContext` (mirrors `test_index_enrichment.py`'s
    no-container pattern).

## Constraints honored
- `fdi.core.basis` has zero imports of `fdi.pipeline`/`fdi.platform` (pure).
- Only new files + the one `stages/__init__.py` import line + tests were
  touched; no changes to chunks/index/extractor/schema.
- Both `detect_basis_regions` and `page_basis` are total (verified by
  dedicated malformed-input test cases).

## Verification run (all in `backend/`)
- `uv run pytest tests/unit/test_basis.py -q` → 9 passed.
- `uv run pytest -m "not integration and not e2e" -q` → 604 passed, 11
  skipped, 213 deselected (pre-existing skips, unrelated to this change).
- `uv run pytest tests/integration/test_basis_stage.py -q -m integration`
  (real Docker Postgres/MinIO) → 2 passed.
- `uv run ruff check .` → All checks passed.
- `uv run mypy --strict src` → Success: no issues found in 103 source files.

## Commit
See parent report for SHA (single commit, per instructions).
