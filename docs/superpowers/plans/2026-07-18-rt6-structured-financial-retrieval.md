# RT-6 Structured Financial Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Query the ingestion layer's canonicalized table grids (`structure` artifact) as first-class retrieval: `get_tables(doc_id, keyword)` returns a document's table grids, optionally filtered by column-header keyword; `concept_lookup(doc_id, concept)` links a `CanonicalConcept` to real numbers by matching its ontology synonyms against table row labels and parsing every numeric column for the matched row (never guessing which period is "the answer"). Two GET routes expose both, citation-carrying (`doc_id`, `table_index`, `row_label`, `column_header`).

**Architecture:** Two new pure functions in a new `fdi.core.tables` module — `parse_numeric` (financial-format cell parsing: thousands separators, parenthetical negatives, currency, percent, blanks → `None`) and `find_concept_rows` (normalized word-token-subsequence match of a concept's synonyms against grid column 0, over ALL data columns) — plus a tiny `filter_tables` keyword filter. `fdi.core.tables` reuses `fdi.core.ontology`'s synonym data via two new public functions there (`normalize_label`, `synonyms_for`). No new pipeline stage or artifact: a `fdi.pipeline.tables` orchestrator module reads the EXISTING `structure` artifact on demand (same `latest_run`→`get_json` path as RT-1's `resolve_chunk`). Two GET routes (`/documents/{doc_id}/tables`, `/documents/{doc_id}/concepts/{concept}`) expose it.

**Tech Stack:** Python 3.13, `uv`, SQLAlchemy async, pytest + testcontainers. Reuses the `structure` artifact already produced by ingestion (SP-7b/7c); no new external dependency.

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive), CI-enforced; stay "1 kept, 0 broken". `fdi.core.tables` is PURE: no I/O/config/clock/RNG. Its only cross-`core` import is `fdi.core.ontology` (spec decision 2b, roadmap-sanctioned core→core reuse) — it does NOT import `fdi.core.structure`/`fdi.core.table_grid` (reads the `grid` shape as a plain `Mapping`, same posture RT-1 took toward `ast`/`semantic`/`provenance`).
- No new pipeline stage, no new artifact (spec decision 1) — `fdi.pipeline.tables` reads the `structure` artifact that already exists; do not add anything to `fdi.pipeline.stages.__init__` or `REGISTRY` in this plan.
- `parse_numeric`/`find_concept_rows`/`filter_tables` never raise; malformed/partial input degrades (`None`, `[]`, skip-and-continue) — the same no-raise convention as `fdi.core.retrieval.build_chunks` and `fdi.core.table_grid.canonicalize_table`.
- Per-task gates before every commit: `uv run --directory backend ruff check .` · `uv run --directory backend mypy --strict src` · `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter` · the task's pytest. Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) → commit.

---

### Task 1: `fdi.core.ontology` — promote `normalize_label`, add `synonyms_for`

**Files:**
- Modify: `backend/src/fdi/core/ontology.py`
- Modify: `backend/tests/unit/test_ontology.py`

**Interfaces:**
- Produces: `normalize_label(label: str) -> str` (renamed from the private `_normalize_label`; same body, same behavior — `map_label`'s one call site updated in place); `synonyms_for(concept: CanonicalConcept) -> list[str]` (new).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/unit/test_ontology.py`:

```python
from fdi.core.ontology import normalize_label, synonyms_for


def test_normalize_label_public() -> None:
    assert normalize_label("  Revenue: ") == "revenue"
    assert normalize_label("REVENUE, net") == "revenue net"


def test_synonyms_for_revenue() -> None:
    syns = synonyms_for(CanonicalConcept.REVENUE)
    assert set(syns) == {
        "revenue", "sales", "turnover", "revenue from operations",
        "income from operations", "total revenue",
    }


def test_synonyms_for_unknown_is_empty() -> None:
    assert synonyms_for(CanonicalConcept.UNKNOWN) == []


def test_synonyms_for_every_concept_nonempty_except_unknown() -> None:
    for concept in CanonicalConcept:
        if concept is CanonicalConcept.UNKNOWN:
            continue
        assert synonyms_for(concept), f"{concept} has no synonyms"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_ontology.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_label'` (and `synonyms_for`).

- [ ] **Step 3: Write the implementation**

In `backend/src/fdi/core/ontology.py`, rename `_normalize_label` → `normalize_label` (public) and update its one call site, then add `synonyms_for`:

```python
def normalize_label(label: str) -> str:
    lowered = label.lower()
    stripped_punct = _PUNCT_RE.sub("", lowered)
    return _WS_RE.sub(" ", stripped_punct).strip()


def map_label(label: str) -> CanonicalConcept:
    """Normalize `label` and look it up in the synonym dictionary.

    Returns CanonicalConcept.UNKNOWN for anything not recognized -- an
    expected, non-error outcome, not a failure mode.
    """
    normalized = normalize_label(label)
    return _SYNONYMS.get(normalized, CanonicalConcept.UNKNOWN)


def synonyms_for(concept: CanonicalConcept) -> list[str]:
    """Every normalized synonym key in `_SYNONYMS` that maps to `concept`, in
    dict-declaration order. `CanonicalConcept.UNKNOWN` -> [] (no dictionary
    entry ever maps to UNKNOWN by construction). Used by
    `fdi.core.tables.find_concept_rows` (RT-6) -- see the design spec's
    decision 2b for why that module reuses this data via a public function
    rather than reimplementing/duplicating the synonym dictionary."""
    return [k for k, v in _SYNONYMS.items() if v is concept]
```

Also update the comment above `_SYNONYMS` (`"-- see _normalize_label."` → `"-- see normalize_label."`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_ontology.py -v`
Expected: PASS (all, including the pre-existing `map_label`/`CanonicalConcept` tests — confirms the rename didn't change behavior).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/ontology.py backend/tests/unit/test_ontology.py
git commit -m "feat(core): ontology.normalize_label (public) + synonyms_for -- RT-6's synonym-reuse surface"
```

---

### Task 2: `fdi.core.tables.parse_numeric`

**Files:**
- Create: `backend/src/fdi/core/tables.py`
- Test: `backend/tests/unit/test_tables.py`

**Interfaces:**
- Produces: `parse_numeric(cell: object) -> float | None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_tables.py
import pytest

from fdi.core.tables import parse_numeric


@pytest.mark.parametrize(("raw", "expected"), [
    ("12,345", 12345.0),
    ("1,234.56", 1234.56),
    ("(1,234)", -1234.0),
    ("-1,234", -1234.0),
    ("+1,234", 1234.0),
    ("1.2", 1.2),
    ("$12,345.67", 12345.67),
    ("Rs. 1,234", 1234.0),
    ("Rs 1234", 1234.0),
    ("INR 500", 500.0),
    ("₹1,00,000", 100000.0),  # Indian lakh grouping (rupee sign)
    ("12.5%", 12.5),
    ("(12.5%)", -12.5),
    ("  42  ", 42.0),
    ("0", 0.0),
    ("0.00", 0.0),
    (42, 42.0),
    (42.5, 42.5),
])
def test_parse_numeric_recognized_formats(raw: object, expected: float) -> None:
    assert parse_numeric(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "-", "--", "–", "—", "N/A", "n/a", "NA", "Nil", "NIL", "NM", "nm",
    "abc", "1.2.3", "1.5x", "TBD", None, True, False, [], {},
])
def test_parse_numeric_unrecognized_returns_none(raw: object) -> None:
    assert parse_numeric(raw) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_tables.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.tables'`.

- [ ] **Step 3: Write the implementation**

```python
# backend/src/fdi/core/tables.py
"""Numeric table-cell parsing + concept->row matching over canonicalized table
grids (fdi.core.table_grid.canonicalize_table's output shape:
`{n_rows, n_cols, header_row_count, column_headers, grid}`). Pure -- no I/O.

This is the RT-6 "deferred numeric extraction" fdi.core.ontology's
map_document docstring points at: a table's row-label cell (grid column 0) is
already a bare label with no trailing number baked in (unlike an AST
paragraph, where a real line item mixes label+number in one string), so this
module's matching problem is "which concept does this label mean", not "strip
the number first". See
docs/superpowers/specs/2026-07-18-rt6-structured-financial-retrieval-design.md
for the full design rationale (decisions 4-8 in particular).
"""

import re
from collections.abc import Mapping

_BLANK_TOKENS = {
    "", "-", "--", "–", "—",
    "n/a", "na", "nil", "nm", "n.a.", "n.m.",
}
_CURRENCY_RE = re.compile(r"(?:rs\.?|inr|usd|₹|\$|€|£)", re.IGNORECASE)
_NUMERIC_BODY_RE = re.compile(r"^\d+(\.\d+)?$")


def parse_numeric(cell: object) -> float | None:
    """Parse a table cell's raw content into a float.

    Handles: thousands separators of any grouping ("12,345", Indian
    "1,00,000" -- ALL commas are stripped, no grouping-shape validation),
    parenthetical negatives ("(1,234)" -> -1234.0), a leading -/+ sign, a
    trailing "%" (returned as the printed number, NOT divided by 100 -- RT-6
    has no unit-of-measure model), a leading/embedded currency marker (Rs.,
    INR, USD, currency symbols -- stripped, no FX conversion), and
    surrounding whitespace.

    Returns None for: blanks/dashes/"N/A" family (any case; a financial
    statement's "no value reported" marker, never coerced to 0.0), non-str/
    non-(int|float) input, bool (Python bool is an int subclass but is never
    a table cell value), and any text that still isn't a plain decimal after
    the above stripping. Never raises.
    """
    if isinstance(cell, bool):
        return None
    if isinstance(cell, int | float):
        return float(cell)
    if not isinstance(cell, str):
        return None

    text = cell.strip()
    if text.lower() in _BLANK_TOKENS:
        return None

    negative = False
    if len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    text = _CURRENCY_RE.sub("", text).strip()

    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()

    if text.endswith("%"):
        text = text[:-1].strip()

    text = text.replace(",", "").replace(" ", "")

    if not text or not _NUMERIC_BODY_RE.match(text):
        return None

    value = float(text)
    return -value if negative else value
```

(`find_concept_rows`/`filter_tables` land in Task 3 — this task ships `parse_numeric` alone so it gates and commits independently.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_tables.py -v`
Expected: PASS (both parametrized tests, all cases).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/tables.py backend/tests/unit/test_tables.py
git commit -m "feat(core): tables.parse_numeric -- financial cell parsing (thousands, parens, %, currency, blanks)"
```

---

### Task 3: `fdi.core.tables.find_concept_rows` + `filter_tables`

**Files:**
- Modify: `backend/src/fdi/core/tables.py`
- Modify: `backend/tests/unit/test_tables.py`

**Interfaces:**
- Consumes: `fdi.core.ontology.{CanonicalConcept, normalize_label, synonyms_for}` (Task 1); `parse_numeric` (Task 2).
- Produces: `find_concept_rows(grid: Mapping[str, object], concept: CanonicalConcept) -> list[dict[str, object]]`; `filter_tables(tables: list[Mapping[str, object]], keyword: str | None) -> list[Mapping[str, object]]`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/unit/test_tables.py`:

```python
from fdi.core.ontology import CanonicalConcept
from fdi.core.tables import filter_tables, find_concept_rows


def _grid(
    column_headers: list[str], rows: list[list[str]], header_row_count: int = 1
) -> dict[str, object]:
    return {
        "n_rows": len(rows) + header_row_count,
        "n_cols": len(column_headers),
        "header_row_count": header_row_count,
        "column_headers": column_headers,
        # header row(s) first, then data rows -- mirrors canonicalize_table's grid shape
        "grid": [column_headers] * header_row_count + rows,
    }


def test_find_concept_rows_matches_qualified_label() -> None:
    grid = _grid(
        ["Particulars", "FY24", "FY23"],
        [["Total revenue from operations", "12,345", "10,000"]],
    )
    matches = find_concept_rows(grid, CanonicalConcept.REVENUE)
    assert matches == [{
        "row_label": "Total revenue from operations",
        "values": [
            {"column_header": "FY24", "raw": "12,345", "value": 12345.0},
            {"column_header": "FY23", "raw": "10,000", "value": 10000.0},
        ],
    }]


def test_find_concept_rows_multi_word_synonym() -> None:
    grid = _grid(["Particulars", "FY24"], [["Cash and cash equivalents", "500"]])
    matches = find_concept_rows(grid, CanonicalConcept.CASH)
    assert matches[0]["row_label"] == "Cash and cash equivalents"
    assert matches[0]["values"] == [{"column_header": "FY24", "raw": "500", "value": 500.0}]


def test_find_concept_rows_known_false_positive_documented() -> None:
    """'Cost of revenue' contains the REVENUE synonym 'revenue' as a token --
    matches REVENUE even though the row is a cost line. Spec decision 5:
    documented, not solved, and pinned here so a future fix is a deliberate
    change, not a silent one."""
    grid = _grid(["Particulars", "FY24"], [["Cost of revenue", "900"]])
    assert len(find_concept_rows(grid, CanonicalConcept.REVENUE)) == 1
    assert find_concept_rows(grid, CanonicalConcept.COST_OF_GOODS_SOLD) == []


def test_find_concept_rows_skips_header_rows() -> None:
    # header row's own text ("Revenue Statement") contains a synonym word but
    # must never be treated as a data row.
    grid = {
        "n_rows": 2, "n_cols": 2, "header_row_count": 1,
        "column_headers": ["Revenue Statement", "FY24"],
        "grid": [["Revenue Statement", "FY24"], ["Net profit", "100"]],
    }
    assert find_concept_rows(grid, CanonicalConcept.REVENUE) == []


def test_find_concept_rows_no_match_returns_empty() -> None:
    grid = _grid(["Particulars", "FY24"], [["Some Random Line", "1"]])
    assert find_concept_rows(grid, CanonicalConcept.REVENUE) == []


def test_find_concept_rows_empty_and_malformed_input() -> None:
    assert find_concept_rows({}, CanonicalConcept.REVENUE) == []
    assert find_concept_rows({"n_rows": 1, "n_cols": 0, "grid": [[]]}, CanonicalConcept.REVENUE) == []


def test_find_concept_rows_ragged_row_degrades_not_raises() -> None:
    grid = {
        "n_rows": 1, "n_cols": 3, "header_row_count": 0,
        "column_headers": ["Particulars", "FY24", "FY23"],
        "grid": [["Total revenue", "12,345"]],  # missing the FY23 cell
    }
    matches = find_concept_rows(grid, CanonicalConcept.REVENUE)
    assert matches[0]["values"] == [
        {"column_header": "FY24", "raw": "12,345", "value": 12345.0},
        {"column_header": "FY23", "raw": "", "value": None},
    ]


def test_filter_tables_by_column_header_keyword() -> None:
    t1 = {"grid": {"column_headers": ["Particulars", "FY24"]}}
    t2 = {"grid": {"column_headers": ["Particulars", "FY23"]}}
    assert filter_tables([t1, t2], "fy24") == [t1]
    assert filter_tables([t1, t2], None) == [t1, t2]
    assert filter_tables([t1, t2], "") == [t1, t2]


def test_filter_tables_drops_malformed_grid_only_when_keyword_given() -> None:
    malformed = {"grid": "not-a-mapping"}
    assert filter_tables([malformed], "fy24") == []
    assert filter_tables([malformed], None) == [malformed]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_tables.py -v`
Expected: FAIL — `ImportError: cannot import name 'find_concept_rows'` (and `filter_tables`).

- [ ] **Step 3: Write the implementation**

Append to `backend/src/fdi/core/tables.py` (add the import line to the existing header):

```python
from typing import cast

from fdi.core.ontology import CanonicalConcept, normalize_label, synonyms_for
```

```python
def _contains_subsequence(haystack: list[str], needle: list[str]) -> bool:
    """True if `needle` (non-empty) appears as a CONTIGUOUS run inside
    `haystack`, at any offset."""
    if not needle:
        return False
    span = len(needle)
    return any(
        haystack[i : i + span] == needle for i in range(len(haystack) - span + 1)
    )


def find_concept_rows(
    grid: Mapping[str, object], concept: CanonicalConcept
) -> list[dict[str, object]]:
    """Match `concept`'s ontology synonyms against `grid`'s row-label column
    (column 0) using normalized word-token-subsequence containment -- e.g.
    synonym tokens ["revenue","from","operations"] match inside label tokens
    ["total","revenue","from","operations"], so "Total revenue from
    operations" matches REVENUE even though map_label's exact lookup would
    not. Header rows (`grid["header_row_count"]`) are never treated as data.

    For each matched row, returns {row_label, values: [{column_header, raw,
    value}, ...]} covering every non-label column (column_index >= 1);
    `value` is `parse_numeric(raw)` -- ALL numeric columns are returned, this
    function never picks "the" period.

    KNOWN LIMITATION (documented, not solved): a synonym word can appear
    inside a label that means a different concept -- "Cost of revenue"
    contains the REVENUE synonym "revenue" and WILL match REVENUE. See the
    design spec's decision 5.

    Degrades to [] on a malformed/empty `grid` (missing keys, n_cols == 0,
    ragged rows shorter than n_cols) -- never raises.
    """
    n_rows_raw = grid.get("n_rows")
    n_rows = n_rows_raw if isinstance(n_rows_raw, int) else 0
    n_cols_raw = grid.get("n_cols")
    n_cols = n_cols_raw if isinstance(n_cols_raw, int) else 0
    header_row_count_raw = grid.get("header_row_count")
    header_row_count = header_row_count_raw if isinstance(header_row_count_raw, int) else 0

    rows_raw = grid.get("grid")
    rows = cast("list[object]", rows_raw) if isinstance(rows_raw, list) else []
    headers_raw = grid.get("column_headers")
    column_headers = cast("list[object]", headers_raw) if isinstance(headers_raw, list) else []

    synonym_tokens = [
        tokens
        for s in synonyms_for(concept)
        if (tokens := normalize_label(s).split())
    ]

    matches: list[dict[str, object]] = []
    for r in range(header_row_count, n_rows):
        row_obj = rows[r] if r < len(rows) else None
        row = cast("list[object]", row_obj) if isinstance(row_obj, list) else []

        label_obj = row[0] if row else ""
        label_raw = label_obj if isinstance(label_obj, str) else ""
        label_tokens = normalize_label(label_raw).split()
        if not label_tokens:
            continue
        if not any(_contains_subsequence(label_tokens, syn) for syn in synonym_tokens):
            continue

        values: list[dict[str, object]] = []
        for c in range(1, n_cols):
            raw_obj = row[c] if c < len(row) else ""
            raw = raw_obj if isinstance(raw_obj, str) else ""
            header_obj = column_headers[c] if c < len(column_headers) else ""
            header = header_obj if isinstance(header_obj, str) else ""
            values.append({"column_header": header, "raw": raw, "value": parse_numeric(raw)})
        matches.append({"row_label": label_raw, "values": values})

    return matches


def filter_tables(
    tables: list[Mapping[str, object]], keyword: str | None
) -> list[Mapping[str, object]]:
    """Keep only tables where some `grid["column_headers"][c]` contains
    `keyword` (case-insensitive substring). `keyword` None or "" returns
    `tables` unchanged. A table with a missing/malformed `grid`/
    `column_headers` is dropped when a keyword IS given (nothing to match
    against), kept unchanged when it isn't."""
    if not keyword:
        return tables
    needle = keyword.lower()
    out: list[Mapping[str, object]] = []
    for t in tables:
        grid = t.get("grid")
        headers = grid.get("column_headers") if isinstance(grid, Mapping) else None
        if isinstance(headers, list) and any(
            isinstance(h, str) and needle in h.lower() for h in headers
        ):
            out.append(t)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_tables.py -v`
Expected: PASS (all cases, including the documented-false-positive test and the ragged-row degrade test).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/core/tables.py backend/tests/unit/test_tables.py
git commit -m "feat(core): tables.find_concept_rows + filter_tables -- concept->row matching over table grids"
```

---

### Task 4: `fdi.pipeline.tables` (orchestrator: `get_tables` / `concept_lookup`)

**Files:**
- Create: `backend/src/fdi/pipeline/tables.py`
- Test: `backend/tests/integration/test_tables_lookup.py`

**Interfaces:**
- Consumes: `RegistryRepository.latest_run` + `ArtifactStore.get_json` (existing, same path as `fdi.pipeline.retrieval._read_chunks`); `fdi.core.tables.{filter_tables, find_concept_rows}`; `fdi.core.ontology.CanonicalConcept`.
- Produces:
  - `get_tables(*, session, store, doc_id, keyword=None) -> list[dict[str, object]] | None`
  - `concept_lookup(*, session, store, doc_id, concept) -> list[dict[str, object]] | None`

**Pre-check (do first, document finding in the test module docstring):** grep every `FakeOcrClient(...)` fixture already in the suite (`test_structure_stage.py`, `test_chunks_stage.py`, `test_ontology_stage.py`, `test_api.py`'s `resolved_corpus_via_api`, etc.) — confirm NONE of them has a table with numeric DATA-ROW cells (they are header-only, or `"cells": []`). This task's integration test therefore needs a NEW, LOCAL `FakeOcrClient` table fixture with at least one non-header row carrying numeric content; do not attempt to reuse or repoint an existing fixture other stage tests assert exact-equality against.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_tables_lookup.py
"""fdi.pipeline.tables.get_tables/concept_lookup: read a document's `structure`
artifact on demand (no new stage/artifact) and expose its canonicalized table
grids + concept->row matches. Against real Postgres + MinIO with a FAKE DI
client.

Pre-check finding: every existing FakeOcrClient table fixture in this suite
(test_structure_stage.py, test_chunks_stage.py, test_api.py's
resolved_corpus_via_api, ...) is header-only or has `"cells": []` -- none
carries a numeric data row. `_numeric_table_ocr()` below is a NEW, LOCAL
fixture built for this test; it does not repoint any shared fixture other
stage tests pin exact-equality assertions against.
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.ontology import CanonicalConcept
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.structure import StructureStage
from fdi.pipeline.tables import concept_lookup, get_tables
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


def _numeric_table_ocr() -> FakeOcrClient:
    return FakeOcrClient(
        {
            "content": "ACME LTD",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 0, "length": 8}],
                },
            ],
            "tables": [
                {
                    "rowCount": 3,
                    "columnCount": 2,
                    "spans": [{"offset": 20, "length": 60}],
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "cells": [
                        {
                            "rowIndex": 0, "columnIndex": 0,
                            "kind": "columnHeader", "content": "Particulars",
                        },
                        {
                            "rowIndex": 0, "columnIndex": 1,
                            "kind": "columnHeader", "content": "FY24",
                        },
                        {
                            "rowIndex": 1, "columnIndex": 0,
                            "content": "Total revenue from operations",
                        },
                        {"rowIndex": 1, "columnIndex": 1, "content": "12,345"},
                        {"rowIndex": 2, "columnIndex": 0, "content": "Net profit"},
                        {"rowIndex": 2, "columnIndex": 1, "content": "(500)"},
                    ],
                },
            ],
            "sections": [],
        }
    )


@pytest.fixture
async def structured_doc(session_factory, store) -> str:
    """Intake one FakeOcr document (numeric table) and run ocr -> structure.
    Returns doc.id."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
    resolver = DagResolver(reg.all())
    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()),
        services={"ocr": _numeric_table_ocr()}, **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id


async def test_get_tables_returns_tagged_tables(session_factory, store, structured_doc) -> None:
    async with session_factory() as s:
        tables = await get_tables(session=s, store=store, doc_id=structured_doc)
    assert tables is not None and len(tables) == 1
    assert tables[0]["table_index"] == 0
    assert tables[0]["grid"]["column_headers"] == ["Particulars", "FY24"]


async def test_get_tables_keyword_filter(session_factory, store, structured_doc) -> None:
    async with session_factory() as s:
        hit = await get_tables(session=s, store=store, doc_id=structured_doc, keyword="fy24")
        miss = await get_tables(session=s, store=store, doc_id=structured_doc, keyword="fy99")
    assert hit is not None and len(hit) == 1
    assert miss == []


async def test_get_tables_unknown_doc_returns_none(session_factory, store) -> None:
    async with session_factory() as s:
        assert await get_tables(session=s, store=store, doc_id="no-such-doc") is None


async def test_concept_lookup_revenue_and_net_profit(
    session_factory, store, structured_doc
) -> None:
    async with session_factory() as s:
        revenue = await concept_lookup(
            session=s, store=store, doc_id=structured_doc, concept=CanonicalConcept.REVENUE
        )
        net_profit = await concept_lookup(
            session=s, store=store, doc_id=structured_doc, concept=CanonicalConcept.NET_PROFIT
        )
    assert revenue == [{
        "table_index": 0,
        "row_label": "Total revenue from operations",
        "values": [{"column_header": "FY24", "raw": "12,345", "value": 12345.0}],
    }]
    assert net_profit == [{
        "table_index": 0,
        "row_label": "Net profit",
        "values": [{"column_header": "FY24", "raw": "(500)", "value": -500.0}],
    }]


async def test_concept_lookup_no_match_returns_empty_list(
    session_factory, store, structured_doc
) -> None:
    async with session_factory() as s:
        result = await concept_lookup(
            session=s, store=store, doc_id=structured_doc, concept=CanonicalConcept.EQUITY
        )
    assert result == []  # document HAS structure, just no matching row -- not None


async def test_concept_lookup_unknown_doc_returns_none(session_factory, store) -> None:
    async with session_factory() as s:
        result = await concept_lookup(
            session=s, store=store, doc_id="no-such-doc", concept=CanonicalConcept.REVENUE
        )
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_tables_lookup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.pipeline.tables'`.

- [ ] **Step 3: Implement**

```python
# backend/src/fdi/pipeline/tables.py
"""RT-6 orchestrator: read a document's `structure` artifact on demand and
expose its canonicalized table grids (get_tables) and concept->row matches
(concept_lookup) via the pure fdi.core.tables functions. Pipeline layer (does
I/O) -- imports platform (repo + store) + core, the same shape as
fdi.pipeline.retrieval.resolve_chunk. No new stage, no new artifact (spec
decision 1): the `structure` artifact already exists per document.

concept_lookup does NOT read the `ontology` artifact (spec decision 2) --
only `structure`, plus fdi.core.ontology's synonym DATA reused as a pure
library via fdi.core.tables.find_concept_rows. A document that has no
`structure` artifact yet returns None from both functions (caller maps this
to 404); a document that HAS `structure` but no matching row/table returns
[] -- a real, distinct, non-error outcome.
"""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.ontology import CanonicalConcept
from fdi.core.tables import filter_tables, find_concept_rows
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def _read_structure(
    session: AsyncSession, store: ArtifactStore, doc_id: str
) -> Mapping[str, object] | None:
    repo = RegistryRepository(session)
    run = await repo.latest_run(doc_id, "structure")
    if run is None or run.artifact_key is None:
        return None
    return cast(Mapping[str, object], await store.get_json(run.artifact_key))


def _tables_of(structure: Mapping[str, object]) -> list[Mapping[str, object]]:
    tables_raw = structure.get("tables")
    if not isinstance(tables_raw, list):
        return []
    return [t for t in tables_raw if isinstance(t, Mapping)]


async def get_tables(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, keyword: str | None = None
) -> list[dict[str, object]] | None:
    """A document's canonicalized table grids, each tagged with its
    `table_index` (position within `structure["tables"]`), optionally
    filtered by `keyword` (case-insensitive substring against any column
    header). None if the document has no `structure` artifact yet."""
    structure = await _read_structure(session, store, doc_id)
    if structure is None:
        return None
    tagged = [{"table_index": i, **t} for i, t in enumerate(_tables_of(structure))]
    return [dict(t) for t in filter_tables(tagged, keyword)]


async def concept_lookup(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, concept: CanonicalConcept
) -> list[dict[str, object]] | None:
    """Every table row across the document whose label matches `concept`'s
    ontology synonyms (fdi.core.tables.find_concept_rows), each tagged with
    its source `table_index`. None if the document has no `structure`
    artifact yet; [] if it has one but nothing matched (a real answer, not
    a missing-artifact signal)."""
    structure = await _read_structure(session, store, doc_id)
    if structure is None:
        return None
    matches: list[dict[str, object]] = []
    for i, table in enumerate(_tables_of(structure)):
        grid = table.get("grid")
        if not isinstance(grid, Mapping):
            continue
        for row in find_concept_rows(grid, concept):
            matches.append({"table_index": i, **row})
    return matches
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_tables_lookup.py -v`
Expected: PASS (all 7 tests) — the `(500)` → `-500.0` and `12,345` → `12345.0` cases prove `parse_numeric` round-trips end-to-end through the real `structure` artifact, not just the Task 2 unit test.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/tables.py backend/tests/integration/test_tables_lookup.py
git commit -m "feat(pipeline): tables.get_tables/concept_lookup -- on-demand structure-artifact query, no new stage"
```

---

### Task 5: Routes — `GET /documents/{doc_id}/tables`, `GET /documents/{doc_id}/concepts/{concept}`

**Files:**
- Modify: `backend/src/fdi/api/schemas.py`
- Modify: `backend/src/fdi/api/routes.py`
- Test: `backend/tests/integration/test_api.py`

**Interfaces:**
- Consumes: `fdi.pipeline.tables.{get_tables, concept_lookup}` (Task 4); existing `SessionDep`/`StoreDep` (no new `deps.py` entries — neither route touches Milvus/embedder/dispatcher).
- Produces: `TableGridBody`, `TableView`, `TablesView`, `ConceptValueView`, `ConceptRowView`, `ConceptLookupView` (schemas); `GET /documents/{doc_id}/tables` (optional `?keyword=`), `GET /documents/{doc_id}/concepts/{concept}`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_api.py` (reuses the file's existing `client`/`_alembic_applied` fixtures; add a small local fixture for a processed doc with the numeric table, mirroring `resolved_corpus_via_api`'s structure but scoped to one document):

```python
# --------------------------------------------------------------------------
# RT-6: GET /documents/{doc_id}/tables, GET /documents/{doc_id}/concepts/{concept}
# --------------------------------------------------------------------------


@pytest.fixture
async def doc_with_numeric_table(session_factory, store) -> str:
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.structure import StructureStage

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
            "content": "ACME LTD",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {
                    "role": "title", "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 0, "length": 8}],
                },
            ],
            "tables": [
                {
                    "rowCount": 2, "columnCount": 2,
                    "spans": [{"offset": 20, "length": 40}],
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "cells": [
                        {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader", "content": "Particulars"},
                        {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "FY24"},
                        {"rowIndex": 1, "columnIndex": 0, "content": "Total revenue from operations"},
                        {"rowIndex": 1, "columnIndex": 1, "content": "12,345"},
                    ],
                },
            ],
            "sections": [],
        }
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
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id


async def test_get_tables_route(
    client: AsyncClient, _alembic_applied, doc_with_numeric_table
) -> None:
    resp = await client.get(f"/documents/{doc_with_numeric_table}/tables")
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == doc_with_numeric_table
    assert len(body["tables"]) == 1
    assert body["tables"][0]["table_index"] == 0
    assert body["tables"][0]["grid"]["column_headers"] == ["Particulars", "FY24"]


async def test_get_tables_route_keyword_filter(
    client: AsyncClient, _alembic_applied, doc_with_numeric_table
) -> None:
    resp = await client.get(f"/documents/{doc_with_numeric_table}/tables", params={"keyword": "fy99"})
    assert resp.status_code == 200
    assert resp.json()["tables"] == []


async def test_get_tables_route_404_unknown_doc(client: AsyncClient, _alembic_applied) -> None:
    resp = await client.get("/documents/no-such-doc/tables")
    assert resp.status_code == 404


async def test_concept_lookup_route(
    client: AsyncClient, _alembic_applied, doc_with_numeric_table
) -> None:
    resp = await client.get(f"/documents/{doc_with_numeric_table}/concepts/REVENUE")
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == doc_with_numeric_table
    assert body["concept"] == "REVENUE"
    assert body["rows"] == [{
        "table_index": 0,
        "row_label": "Total revenue from operations",
        "values": [{"column_header": "FY24", "raw": "12,345", "value": 12345.0}],
    }]


async def test_concept_lookup_route_invalid_concept_422(
    client: AsyncClient, _alembic_applied, doc_with_numeric_table
) -> None:
    resp = await client.get(f"/documents/{doc_with_numeric_table}/concepts/NOT_A_CONCEPT")
    assert resp.status_code == 422


async def test_concept_lookup_route_404_unknown_doc(client: AsyncClient, _alembic_applied) -> None:
    resp = await client.get("/documents/no-such-doc/concepts/REVENUE")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "tables_route or concept_lookup_route" -v`
Expected: FAIL — 404s from FastAPI (routes don't exist yet) instead of the expected 200/422.

- [ ] **Step 3: Implement schemas + routes**

Append to `backend/src/fdi/api/schemas.py`:

```python
class TableGridBody(BaseModel):
    n_rows: int
    n_cols: int
    header_row_count: int
    column_headers: list[str]
    grid: list[list[str]]


class TableView(BaseModel):
    table_index: int
    row_count: int
    column_count: int
    stitched: bool = False
    grid: TableGridBody


class TablesView(BaseModel):
    doc_id: str
    tables: list[TableView]


class ConceptValueView(BaseModel):
    column_header: str
    raw: str
    value: float | None


class ConceptRowView(BaseModel):
    table_index: int
    row_label: str
    values: list[ConceptValueView]


class ConceptLookupView(BaseModel):
    doc_id: str
    concept: str
    rows: list[ConceptRowView]
```

In `backend/src/fdi/api/routes.py`, add imports:

```python
from fdi.core.ontology import CanonicalConcept
from fdi.pipeline.tables import concept_lookup, get_tables
```

(add `ConceptLookupView, TablesView` to the existing `from fdi.api.schemas import (...)` block, alphabetized like its neighbors), and append the routes near the other `/documents/{doc_id}/...` routes:

```python
# --------------------------------------------------------------------------
# RT-6: structured financial retrieval over the `structure` artifact's
# canonicalized table grids. No new stage/artifact -- reads `structure` on
# demand via fdi.pipeline.tables, the same shape resolve_chunk (RT-1) reads
# `chunks`.
# --------------------------------------------------------------------------


@router.get("/documents/{doc_id}/tables", response_model=TablesView)
async def get_tables_route(
    doc_id: str, session: SessionDep, store: StoreDep, keyword: str | None = Query(default=None)
) -> TablesView:
    tables = await get_tables(session=session, store=store, doc_id=doc_id, keyword=keyword)
    if tables is None:
        raise HTTPException(
            status_code=404, detail=f"no structure artifact for document {doc_id}"
        )
    return TablesView.model_validate({"doc_id": doc_id, "tables": tables})


@router.get("/documents/{doc_id}/concepts/{concept}", response_model=ConceptLookupView)
async def concept_lookup_route(
    doc_id: str, concept: CanonicalConcept, session: SessionDep, store: StoreDep
) -> ConceptLookupView:
    rows = await concept_lookup(session=session, store=store, doc_id=doc_id, concept=concept)
    if rows is None:
        raise HTTPException(
            status_code=404, detail=f"no structure artifact for document {doc_id}"
        )
    return ConceptLookupView.model_validate(
        {"doc_id": doc_id, "concept": concept.value, "rows": rows}
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "tables_route or concept_lookup_route" -v`
Expected: PASS (all 6).

Then the whole file, to confirm no route-ordering conflict (FastAPI matches `/documents/{doc_id}/concepts/{concept}` against a literal path segment, not a numeric one, so it cannot collide with `/documents/{doc_id}/artifacts/{stage}` or any other existing route — confirm by running the full `test_api.py`):

Run: `uv run --directory backend pytest tests/integration/test_api.py -v`
Expected: PASS (all, old and new).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(api): GET /documents/{id}/tables + GET /documents/{id}/concepts/{concept} -- RT-6 routes"
```

---

### Task 6: Full gates, roadmap + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-rt6-structured-financial-retrieval-design.md` (Status → Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (RT-6 row → done + handoff note for RT-7)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (1 kept/0 broken). Grep-confirm `fdi.core.tables` imports only `fdi.core.ontology` within `fdi.core` (no `fdi.core.structure`/`table_grid` import) and no I/O/`fdi.platform`/`fdi.pipeline` import.

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`; green (e2e/azure/neo4j/milvus/bge skip). Note the passed count against the prior baseline (RT-1's post-implementation count + this plan's new unit/integration/API tests: Task 1 adds 3, Task 2 adds ~26 parametrized cases across 2 tests, Task 3 adds 9, Task 4 adds 7, Task 5 adds 6).

- [ ] **Step 3: Audit RT-6 done criteria** (§7 of the spec) — map each criterion to the task that ships it; confirm the documented-false-positive test (Task 3), the ragged-row degrade test (Task 3), and the end-to-end `(500)`→`-500.0` round-trip (Task 4) all pass; confirm no new pipeline stage/artifact was introduced (grep `REGISTRY.register` — no new call; `fdi.pipeline.stages.__init__` unchanged).

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps 1–3 green).

- [ ] **Step 5: Roadmap** — flip the RT-6 row to `✅ done`; add a short handoff for RT-7 (which depends on RT-6 per the roadmap): the `concept_lookup(session, store, doc_id, concept)` signature RT-7 will call once per `doc_id` from a `list_documents()` loop (the same shape `resolve_entities` already loops), the citation shape (`table_index`, `row_label`, `column_header`), and decision 5's known false-positive limitation ("Cost of revenue" matching REVENUE) that RT-7 inherits unless it adds disambiguation itself. Also note the two forks left open in the spec's §8 (persisted-vs-on-demand, matching aggressiveness) as still-open questions, not resolved by this implementation.

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: RT-6 done-criteria audit, spec status, roadmap handoff for RT-7"
```

---

## Self-Review

**Spec coverage:** §3.1 `parse_numeric`/`find_concept_rows`/`filter_tables` → Tasks 2–3. §3.2 `ontology.normalize_label`/`synonyms_for` → Task 1. §3.3 `fdi.pipeline.tables.get_tables`/`concept_lookup` → Task 4. §3.4 routes/schemas → Task 5. §2 decisions: no new stage/artifact (d1) → Task 4's docstring + Task 6's grep-confirm; no `ontology`-artifact read (d2) → Task 4's `_read_structure` only reads `structure`; core→core reuse (d2b) → Task 1 + Task 3's import; public `normalize_label`/`synonyms_for` (d3) → Task 1; word-token-subsequence matching (d4) → Task 3's `_contains_subsequence` + qualified-label test; documented false-positive (d5) → Task 3's dedicated test; percent/currency/no-FX (d6) → Task 2's parametrized cases; blanks never 0.0, never raise (d7) → Task 2's unrecognized-cases test; all numeric columns returned (d8) → Task 3's `values` assertions (never a single "answer" picked); GET routes (d9) → Task 5; per-doc only, RT-7 handoff (d10) → Task 6 Step 5; citation shape (d11) → Task 4 + Task 5's `table_index`/`row_label`/`column_header` assertions. §4 error handling → Task 2/3's malformed-input tests, Task 4's `None`-vs-`[]` tests, Task 5's 404/422 tests. §5 testing → Tasks 1–5 (unit: 1–3; integration: 4; API: 5). §6 deferred → not built (Task 6 audit confirms). §7 done criteria → Task 6. §8 forks → Task 6 Step 5 carries them forward explicitly, not silently closed.

**Placeholder scan:** every task (1–5) carries complete, runnable code — no task defers its implementation to "reuse an existing harness" the way RT-1's Task 2/3/4 fixture bodies pointed at `test_knowledge_stage.py`/`test_index_stage.py`. RT-6 does need ONE new fixture not derivable from an existing one (`_numeric_table_ocr`/`doc_with_numeric_table`, per the Task 4/5 pre-check finding that no existing `FakeOcrClient` fixture has numeric table cells) — that fixture is written out in full in both Task 4 and Task 5 (kept separate, not shared, since integration and API tests in this codebase consistently build their own local fixtures rather than cross-import test modules).

**Type consistency:** `parse_numeric(cell: object) -> float | None` (Task 2) is the exact signature `find_concept_rows` (Task 3) calls per-cell. `find_concept_rows(grid: Mapping[str, object], concept: CanonicalConcept) -> list[dict[str, object]]` (Task 3) is consumed unchanged by `concept_lookup` (Task 4), which only adds `table_index`. `get_tables`/`concept_lookup`'s `list[dict[str, object]] | None` return (Task 4) is exactly what Task 5's routes pass into `TablesView.model_validate`/`ConceptLookupView.model_validate` — field names match end to end (`table_index`, `row_label`, `column_header`, `raw`, `value`) with no renaming at the route boundary.

**Risk flagged:** Task 3's synonym-token comprehension uses a walrus inside a list-comprehension filter (`if (tokens := normalize_label(s).split())`) — same pattern RT-1's Task 3 flagged as a possible ruff/mypy friction point; if it is rejected, expand to an explicit loop building `synonym_tokens`. Task 5's `TableView`/`TablesView.model_validate` relies on Pydantic v2's default `extra="ignore"` behavior to silently drop the raw table dict's `cells`/`spans`/`bounding_regions` fields — this mirrors the existing `EntitiesView.model_validate(result)` call in `resolve_entities_route` (routes.py), not a new pattern, but the executor should confirm no `model_config = ConfigDict(extra="forbid")` has been added to `BaseModel`-derived schemas elsewhere in the file since RT-1 (a project-wide default would break this without touching RT-6's own code).
