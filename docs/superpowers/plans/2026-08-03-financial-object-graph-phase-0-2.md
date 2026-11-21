# Financial Object Graph — Phases 0–2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a measurable ground-truth harness, score today's pipeline against it to get an honest baseline, then build the complete **deterministic, zero-LLM** Financial Object Graph — every statement face row and every note row as a persistent, provenance-carrying `LineItem`.

**Architecture:** Three new DAG stages (`statement_index`, `note_index`, `fog_objects`) that compose the already-measured pure cores in `fdi.core` rather than reimplementing them. The FOG is a per-document MinIO artifact keyed by basis. No LLM call is made anywhere in this plan — `fog_mapping` is Phase 3, a separate plan.

**Tech Stack:** Python 3.12 · FastAPI · Celery · SQLAlchemy async · MinIO · pytest (`asyncio_mode = "auto"`) · `uv` for the runner · ruff + mypy as gates.

**Spec:** [`docs/superpowers/specs/2026-08-03-financial-object-graph-design.md`](../specs/2026-08-03-financial-object-graph-design.md)

## Global Constraints

- **`fdi.core` is pure and total.** No I/O, no config, no clock, no RNG. **Never raises on any input shape** — malformed input degrades field-by-field (`page → None`, `basis → "unknown"`). A `fdi.core` module may not import `fdi.pipeline`, `fdi.platform`, `fdi.api` or `fdi.workers`.
- **Layering (enforced by import-linter):** `core` ← `pipeline` → `platform`. `api` and `workers` are siblings; neither imports the other.
- **Decision 10 — never raise.** Every I/O step in a pipeline module is individually guarded. One note's failure degrades *that note's* output, never the document's.
- **Degradation is explicit in the graph.** A missing `REFERENCES` edge means "this issuer printed no note column", recorded as such. It never becomes a silent null. `absent ≠ missed` is a property of the data structure.
- **Determinism is mandatory.** Stage artifacts are content-addressed; any set iteration, `id()`, or dict-ordering dependence in an artifact body breaks caching. Sort explicitly.
- **The model never parses a number.** All amounts go through `fdi.core.numbers.parse_amount`.
- **Basis:** consolidated is primary, standalone secondary. Both are emitted when present.
- **Test runner:** `uv run --directory backend pytest -m "not integration and not e2e" -q`
- **Gates before every commit:** `uv run --directory backend ruff check .` and `uv run --directory backend mypy src` must both be clean.
- **CRLF warning:** several files in this repo are CRLF. If the `Edit` tool desyncs mid-edit, apply replacements via a Python script using `open(..., newline="")`.

---

## File Structure

**New — pure core (`backend/src/fdi/core/`)**

| File | Responsibility |
|---|---|
| `scoring.py` | Ground-truth record shape + `score_document`. Recall, precision, and the absent-vs-missed split. Pure. |
| `fog.py` | The FOG data model: `Statement`, `Note`, `LineItem`, `Edge`, stable id construction, artifact serialization. Pure, no algorithms. |
| `fog_build.py` | The algorithms that turn `statement_index` + `note_index` + `structure` into FOG nodes and edges. Pure. |

**New — stages (`backend/src/fdi/pipeline/stages/`)**

| File | Responsibility |
|---|---|
| `statement_index.py` | `StatementIndexStage`, deps `(ocr, structure, basis)` |
| `note_index.py` | `NoteIndexStage`, deps `(ocr, structure, basis)` |
| `fog_objects.py` | `FogObjectsStage`, deps `(statement_index, note_index, structure)` |

**New — scripts (`scripts/`)**

| File | Responsibility |
|---|---|
| `gt_template.py` | Emit a pre-filled ground-truth skeleton so keying is *checking numbers*, not typing 402 tags |
| `score_pipeline.py` | Score an extraction record or a FOG artifact against ground truth |

**New — data (`docs/groundtruth/`)** — `{issuer}-{fy}-{basis}.json`, hand-keyed (human deliverable, Task 3).

**Modified**

| File | Change |
|---|---|
| `backend/src/fdi/pipeline/stages/__init__.py` | Register the three new stages |
| `backend/src/fdi/core/statements.py` | Add `statement_footing_rules` + `describe_footing` (Task 5) |

**Unchanged in this plan:** `pipeline/extractor.py` and the whole similarity path keep running. They are deleted in Phase 6, only after the FOG beats the Task 4 baseline.

---

## Task 1: Ground-truth scoring core

**Files:**
- Create: `backend/src/fdi/core/scoring.py`
- Test: `backend/tests/unit/test_scoring.py`

**Interfaces:**
- Consumes: nothing (leaf module)
- Produces: `GroundTruth` (frozen dataclass: `issuer: str`, `fiscal_year: str`, `basis: str`, `statement: str`, `values: dict[str, float]`, `not_reported: tuple[str, ...]`); `parse_groundtruth(body: Mapping[str, object]) -> GroundTruth | None`; `Score` (frozen dataclass: `reported: int`, `correct: int`, `wrong: int`, `missed: int`, `false_positive: int`, `absent_correct: int`, `committed: int`); `Score.recall -> float`; `Score.precision -> float`; `score_document(gt: GroundTruth, produced: Mapping[str, object], *, tolerance: float = 0.01) -> Score`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_scoring.py`:

```python
"""Scoring must separate ABSENT from MISSED, or recall is meaningless on a
402-tag taxonomy where ~180 tags cannot apply to a services company."""

from fdi.core.scoring import GroundTruth, parse_groundtruth, score_document

_GT = GroundTruth(
    issuer="TCS",
    fiscal_year="FY2025-26",
    basis="consolidated",
    statement="profit_and_loss",
    values={"RFOGRS": 255324.0, "PBTR": 65000.0, "TOTFC": 800.0},
    not_reported=("EXCHEXP", "OTPRVE"),
)


def test_perfect_extraction_scores_1_0():
    produced = {"RFOGRS": 255324.0, "PBTR": 65000.0, "TOTFC": 800.0}
    score = score_document(_GT, produced)
    assert score.correct == 3
    assert score.missed == 0
    assert score.recall == 1.0
    assert score.precision == 1.0


def test_a_missed_tag_is_not_a_wrong_tag():
    """Producing nothing for a reported tag lowers recall but never precision."""
    produced = {"RFOGRS": 255324.0, "PBTR": None}
    score = score_document(_GT, produced)
    assert score.correct == 1
    assert score.missed == 2
    assert score.wrong == 0
    assert score.precision == 1.0


def test_a_value_on_a_not_reported_tag_is_a_false_positive():
    """The company does not disclose EXCHEXP; inventing it must be punished."""
    produced = {"RFOGRS": 255324.0, "EXCHEXP": 12.0}
    score = score_document(_GT, produced)
    assert score.false_positive == 1
    assert score.absent_correct == 1  # OTPRVE correctly left alone
    assert score.committed == 2
    assert score.precision == 0.5


def test_silence_on_a_not_reported_tag_is_correct_not_absent_credit():
    """absent_correct is reported, but it never inflates recall."""
    produced = {"RFOGRS": 255324.0, "PBTR": 65000.0, "TOTFC": 800.0}
    score = score_document(_GT, produced)
    assert score.absent_correct == 2
    assert score.reported == 3
    assert score.recall == 1.0


def test_wrong_value_counts_against_precision_and_recall():
    produced = {"RFOGRS": 999.0, "PBTR": 65000.0, "TOTFC": 800.0}
    score = score_document(_GT, produced)
    assert score.wrong == 1
    assert score.correct == 2
    assert score.precision == 2 / 3


def test_tags_outside_the_ground_truth_are_ignored_entirely():
    """A tag from another statement is out of scope, not a false positive."""
    produced = {"RFOGRS": 255324.0, "SOMEBSTAG": 5.0}
    score = score_document(_GT, produced)
    assert score.committed == 1
    assert score.false_positive == 0


def test_dual_period_values_are_scored_on_the_current_year():
    produced = {"RFOGRS": {"current": 255324.0, "prior": 240893.0}}
    score = score_document(_GT, produced)
    assert score.correct == 1


def test_tolerance_absorbs_float_noise_but_not_a_real_difference():
    assert score_document(_GT, {"RFOGRS": 255324.004}).correct == 1
    assert score_document(_GT, {"RFOGRS": 255325.0}).correct == 0


def test_empty_ground_truth_yields_zero_not_a_crash():
    empty = GroundTruth("X", "FY", "consolidated", "profit_and_loss", {}, ())
    score = score_document(empty, {})
    assert score.recall == 0.0
    assert score.precision == 0.0


def test_parse_rejects_a_tag_that_is_both_valued_and_not_reported():
    """The two lists must be disjoint or absent-vs-missed is undefined."""
    assert parse_groundtruth({
        "issuer": "TCS", "fiscal_year": "FY2025-26", "basis": "consolidated",
        "statement": "profit_and_loss",
        "values": {"RFOGRS": 1.0}, "not_reported": ["RFOGRS"],
    }) is None


def test_parse_reads_a_well_formed_record():
    gt = parse_groundtruth({
        "issuer": "TCS", "fiscal_year": "FY2025-26", "basis": "consolidated",
        "statement": "profit_and_loss",
        "values": {"RFOGRS": 255324.0}, "not_reported": ["EXCHEXP"],
    })
    assert gt is not None
    assert gt.values == {"RFOGRS": 255324.0}
    assert gt.not_reported == ("EXCHEXP",)


def test_parse_degrades_malformed_input_to_none_never_raises():
    assert parse_groundtruth({}) is None
    assert parse_groundtruth({"values": "not a mapping"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_scoring.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.scoring'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/src/fdi/core/scoring.py`:

```python
"""Score a produced fact map against hand-keyed ground truth.

WHY `not_reported` IS HALF THE RECORD
-------------------------------------
The taxonomy is a FIXED list of ~402 tags, and ~180 of them cannot apply to a
services company at all (25 extraordinary-item tags cannot apply to ANY Ind-AS
filer). Scoring recall against the whole list therefore measures the taxonomy's
breadth, not the extractor's reach. Ground truth must state, per statement, both
what the issuer reports AND what it genuinely does not -- which is the only way
to tell ABSENT from MISSED.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class GroundTruth:
    """One issuer's hand-read truth for ONE statement on ONE basis."""

    issuer: str
    fiscal_year: str
    basis: str
    statement: str
    values: dict[str, float]
    not_reported: tuple[str, ...]


@dataclass(frozen=True)
class Score:
    """`reported` is the denominator of recall; `committed` of precision.

    `absent_correct` is reported but never credited to recall: staying silent
    on a tag the issuer does not disclose is correct behaviour, not extraction.
    Crediting it would let a system that extracts nothing score well."""

    reported: int
    correct: int
    wrong: int
    missed: int
    false_positive: int
    absent_correct: int
    committed: int

    @property
    def recall(self) -> float:
        return self.correct / self.reported if self.reported else 0.0

    @property
    def precision(self) -> float:
        return self.correct / self.committed if self.committed else 0.0


def parse_groundtruth(body: Mapping[str, object]) -> GroundTruth | None:
    """A ground-truth file body as a `GroundTruth`, or None if malformed.

    Returns None -- never raises -- so a typo in one file skips that file
    instead of taking the whole scoring run down. The disjointness check is a
    hard reject: a tag that is both valued and `not_reported` makes the
    absent-vs-missed split undefined, and silently preferring one list would
    make the resulting number a lie."""
    if not isinstance(body, Mapping):
        return None
    issuer = body.get("issuer")
    fiscal_year = body.get("fiscal_year")
    basis = body.get("basis")
    statement = body.get("statement")
    raw_values = body.get("values")
    raw_absent = body.get("not_reported", [])
    if not all(isinstance(x, str) and x for x in (issuer, fiscal_year, basis, statement)):
        return None
    if not isinstance(raw_values, Mapping) or not isinstance(raw_absent, list):
        return None

    values: dict[str, float] = {}
    for tag, value in raw_values.items():
        if not isinstance(tag, str) or not tag:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        values[tag] = float(value)

    absent = tuple(sorted({t for t in raw_absent if isinstance(t, str) and t}))
    if set(absent) & set(values):
        return None

    # `issuer` etc. are `object` to the type checker until narrowed; the
    # all(isinstance(...)) guard above has already proved they are non-empty str.
    return GroundTruth(
        issuer=str(issuer), fiscal_year=str(fiscal_year), basis=str(basis),
        statement=str(statement), values=values, not_reported=absent,
    )


def _current(value: object) -> float | None:
    """The current-year number out of a scalar or a {current, prior} record."""
    if isinstance(value, Mapping):
        value = value.get("current")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def score_document(
    gt: GroundTruth, produced: Mapping[str, object], *, tolerance: float = 0.01
) -> Score:
    """Score `produced` (tag -> scalar or {current, prior}) against `gt`.

    Only tags IN SCOPE for this statement are judged: a balance-sheet tag
    appearing in a P&L score is ignored rather than counted as a false
    positive, because this record makes no claim about it.

    Tolerance is absolute and tight (0.01) by design -- ground truth is keyed
    AS PRINTED, so anything beyond float noise is a real disagreement, not
    rounding. Total: never raises."""
    correct = wrong = missed = false_positive = absent_correct = committed = 0

    for tag, expected in gt.values.items():
        actual = _current(produced.get(tag))
        if actual is None:
            missed += 1
            continue
        committed += 1
        if abs(actual - expected) <= tolerance:
            correct += 1
        else:
            wrong += 1

    for tag in gt.not_reported:
        if _current(produced.get(tag)) is None:
            absent_correct += 1
        else:
            committed += 1
            false_positive += 1

    # Tags in neither list are deliberately never visited -- see the docstring.
    return Score(
        reported=len(gt.values), correct=correct, wrong=wrong, missed=missed,
        false_positive=false_positive, absent_correct=absent_correct,
        committed=committed,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_scoring.py -q`
Expected: PASS, 12 passed

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/scoring.py backend/tests/unit/test_scoring.py
git commit -m "feat(scoring): ground-truth record + recall/precision with absent-vs-missed split"
```

---

## Task 2: Ground-truth template generator

**Files:**
- Create: `scripts/gt_template.py`
- Test: `backend/tests/unit/test_gt_template.py`

**Interfaces:**
- Consumes: `fdi.core.scoring.parse_groundtruth`
- Produces: `build_template(schema: Mapping[str, object], *, issuer: str, fiscal_year: str, basis: str, statement: str) -> dict[str, object]` — a ground-truth skeleton with every in-scope tag listed under `not_reported` and an empty `values` map.

**Why this task exists:** hand-keying 402 tags per statement per basis per issuer is where a ground-truth effort dies. The template inverts the work — every tag starts in `not_reported`, and the human *moves* the ones the issuer actually reports into `values` with the printed number. Keying becomes checking, and the disjointness invariant holds by construction.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_gt_template.py`:

```python
"""The template must start every tag as `not_reported` -- the human MOVES the
disclosed ones into `values`, so the disjointness invariant holds by
construction and nobody hand-types 402 tags."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from gt_template import build_template  # noqa: E402

from fdi.core.scoring import parse_groundtruth  # noqa: E402

_SCHEMA = {
    "modules": [
        {
            "module_id": "profit_and_loss",
            "retrieval_units": [
                {
                    "unit_id": "RU_PL",
                    "fields": [
                        {"tag": "RFOGRS", "field_name": "Gross revenue", "origin": "extracted"},
                        {"tag": "TOTFC", "field_name": "Finance cost", "origin": "extracted"},
                        {"tag": "PBTR", "field_name": "Profit before tax", "origin": "derived"},
                        {
                            "tag": "TOTOTING", "field_name": "Other income",
                            "origin": "extracted",
                            "fields": [
                                {"tag": "INTOI", "field_name": "Interest income",
                                 "origin": "extracted"},
                            ],
                        },
                    ],
                }
            ],
        },
        {"module_id": "assets", "retrieval_units": [
            {"unit_id": "RU_BS", "fields": [
                {"tag": "GRSPNE", "field_name": "Plant", "origin": "extracted"}]}]},
    ]
}


def test_every_extractable_tag_of_the_module_starts_as_not_reported():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    assert tpl["values"] == {}
    assert tpl["not_reported"] == ["INTOI", "RFOGRS", "TOTFC", "TOTOTING"]


def test_derived_tags_are_excluded_they_are_computed_not_read():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    assert "PBTR" not in tpl["not_reported"]


def test_nested_group_children_are_reached():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    assert "INTOI" in tpl["not_reported"]


def test_only_the_named_module_is_included():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    assert "GRSPNE" not in tpl["not_reported"]


def test_the_emitted_template_parses_as_valid_ground_truth():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    gt = parse_groundtruth(tpl)
    assert gt is not None
    assert gt.issuer == "TCS"
    assert gt.values == {}


def test_unknown_module_yields_an_empty_but_valid_template():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="nonexistent",
    )
    assert tpl["not_reported"] == []
    assert parse_groundtruth(tpl) is not None


def test_malformed_schema_does_not_raise():
    tpl = build_template(
        {}, issuer="X", fiscal_year="FY", basis="consolidated", statement="pl",
    )
    assert tpl["not_reported"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_gt_template.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'gt_template'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/gt_template.py`:

```python
"""Emit a ground-truth skeleton so keying is CHECKING, not typing.

Every extractable tag in the module starts life under `not_reported`. The human
reads the statement and MOVES each disclosed line into `values` with the number
exactly as printed. Two properties fall out for free: nobody hand-types 402
tags, and `values`/`not_reported` are disjoint by construction -- which is what
`fdi.core.scoring.parse_groundtruth` hard-rejects a violation of.

Usage:
    uv run --directory backend python ../scripts/gt_template.py \
        --schema docs/architecture/field-extraction-schema.v4_3-pl.json \
        --issuer TCS --fy FY2025-26 --basis consolidated \
        --statement profit_and_loss \
        --out docs/groundtruth/TCS-FY2025-26-consolidated-profit_and_loss.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path


def _collect(node: object, out: set[str]) -> None:
    """Depth-first walk collecting every EXTRACTABLE tag.

    A node with a non-empty `fields` list is a group: recurse into it AND still
    consider its own tag, because v4.3 hoists a subtotal's tag onto the
    container it totals -- the parent carries the tag, so skipping groups would
    drop every reported total."""
    if not isinstance(node, Mapping):
        return
    tag = node.get("tag")
    origin = node.get("origin")
    if isinstance(tag, str) and tag and origin != "derived":
        out.add(tag)
    children = node.get("fields")
    if isinstance(children, list):
        for child in children:
            _collect(child, out)


def build_template(
    schema: Mapping[str, object],
    *,
    issuer: str,
    fiscal_year: str,
    basis: str,
    statement: str,
) -> dict[str, object]:
    """A ground-truth skeleton for one issuer/basis/statement. Never raises."""
    tags: set[str] = set()
    modules = schema.get("modules") if isinstance(schema, Mapping) else None
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, Mapping) or module.get("module_id") != statement:
            continue
        units = module.get("retrieval_units")
        for unit in units if isinstance(units, list) else []:
            if not isinstance(unit, Mapping):
                continue
            fields = unit.get("fields")
            for field in fields if isinstance(fields, list) else []:
                _collect(field, tags)

    return {
        "issuer": issuer,
        "fiscal_year": fiscal_year,
        "basis": basis,
        "statement": statement,
        "_instructions": (
            "Read the statement. For every line the issuer ACTUALLY reports, "
            "move its tag from not_reported into values with the number EXACTLY "
            "as printed (no rescaling). Leave the rest in not_reported."
        ),
        "values": {},
        "not_reported": sorted(tags),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--fy", required=True)
    parser.add_argument("--basis", required=True, choices=["consolidated", "standalone"])
    parser.add_argument("--statement", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    template = build_template(
        schema, issuer=args.issuer, fiscal_year=args.fy,
        basis=args.basis, statement=args.statement,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out} with {len(template['not_reported'])} tags to triage")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_gt_template.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add scripts/gt_template.py backend/tests/unit/test_gt_template.py
git commit -m "feat(gt): ground-truth template generator — keying becomes checking"
```

---

## Task 3: Hand-key the ground truth (human deliverable)

**Files:**
- Create: `docs/groundtruth/{issuer}-{fy}-{basis}-{statement}.json` × 3 issuers × 2 bases × 4 statements (RIL is consolidated-only → 20 files, not 24)
- Create: `docs/groundtruth/README.md`
- Test: `backend/tests/unit/test_groundtruth_files.py`

**Interfaces:**
- Consumes: `build_template` (Task 2), `parse_groundtruth` (Task 1)
- Produces: the ground-truth corpus every later gate is measured against.

**This task is data entry against the real PDFs and cannot be automated.** The steps below generate the skeletons, define the validity gate, and state the acceptance criterion. The keying itself is human work.

- [ ] **Step 1: Generate every skeleton**

```bash
for BASIS in consolidated standalone; do
  for ST in profit_and_loss assets liabilities equity; do
    uv run --directory backend python ../scripts/gt_template.py \
      --schema "docs/architecture/field-extraction-schema.v4_3-${ST/profit_and_loss/pl}.json" \
      --issuer TCS --fy FY2025-26 --basis "$BASIS" --statement "$ST" \
      --out "docs/groundtruth/TCS-FY2025-26-$BASIS-$ST.json"
  done
done
```

Repeat for `RITES` (both bases) and `RIL` (**consolidated only** — it is a consolidated-only integrated report; generating a standalone file would create truth that does not exist).

- [ ] **Step 2: Write the validity gate test**

Create `backend/tests/unit/test_groundtruth_files.py`:

```python
"""Every ground-truth file on disk must parse. A malformed file silently skips
during scoring, which would quietly shrink the denominator and inflate recall."""

import json
from pathlib import Path

import pytest

from fdi.core.scoring import parse_groundtruth

_GT_DIR = Path(__file__).resolve().parents[3] / "docs" / "groundtruth"


def _files() -> list[Path]:
    return sorted(_GT_DIR.glob("*.json")) if _GT_DIR.is_dir() else []


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_ground_truth_file_parses(path: Path):
    body = json.loads(path.read_text(encoding="utf-8"))
    gt = parse_groundtruth(body)
    assert gt is not None, f"{path.name} is malformed (check values/not_reported disjointness)"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_filename_matches_its_contents(path: Path):
    """A file keyed for the wrong basis scores the wrong statement set and the
    error is invisible in the numbers."""
    body = json.loads(path.read_text(encoding="utf-8"))
    gt = parse_groundtruth(body)
    assert gt is not None
    assert path.stem == f"{gt.issuer}-{gt.fiscal_year}-{gt.basis}-{gt.statement}"


def test_the_corpus_is_not_empty():
    assert _files(), "no ground-truth files found — Task 3 is not done"
```

- [ ] **Step 3: Run it to verify it fails on the un-keyed corpus**

Run: `uv run --directory backend pytest tests/unit/test_groundtruth_files.py -q`
Expected: the parse and filename tests PASS on skeletons (they are valid, just empty); `test_the_corpus_is_not_empty` PASSES once Step 1 has run. This test is a *guard*, not a red-green cycle — it exists so a later malformed edit fails loudly.

- [ ] **Step 4: Key the values (human)**

For each file: open the issuer's PDF at the relevant statement, and for every line it reports, move that tag from `not_reported` into `values` with the number **exactly as printed** — no rescaling, no unit conversion, brackets as negatives.

Record the printed unit in the README rather than converting: TCS prints ₹ crore, RITES ₹ lakh. Converting during keying is how a ×10 unit bug gets baked into the truth set.

- [ ] **Step 5: Write the README**

Create `docs/groundtruth/README.md` documenting: the file naming convention, the "as printed, never rescaled" rule, the printed unit per issuer, that `values` and `not_reported` must stay disjoint, and that RIL has no standalone basis.

- [ ] **Step 6: Verify and commit**

Run: `uv run --directory backend pytest tests/unit/test_groundtruth_files.py -q`
Expected: PASS for every file.

**Acceptance criterion for this task:** every generated file has a non-empty `values` map, and the TCS consolidated P&L file agrees with the existing hand-read set from the note-driven design (§7.2) except where that set was itself corrected — specifically `PBMI = 48,496`, not 49,096.

```bash
git add docs/groundtruth backend/tests/unit/test_groundtruth_files.py
git commit -m "feat(gt): hand-keyed ground truth — TCS/RITES/RIL, all statements, both bases"
```

---

## Task 4: Baseline — score today's pipeline

**Files:**
- Create: `scripts/score_pipeline.py`
- Test: `backend/tests/unit/test_score_pipeline.py`

**Interfaces:**
- Consumes: `fdi.core.scoring.{GroundTruth, Score, parse_groundtruth, score_document}`
- Produces: `facts_from_extraction(record: Mapping[str, object]) -> dict[str, object]` — tag → value out of an extraction record; `facts_from_fog(fog: Mapping[str, object], *, basis: str) -> dict[str, object]` (returns `{}` until Phase 3 adds `MAPS_TO` edges); `report(rows: Sequence[tuple[GroundTruth, Score]]) -> str`

**This produces the number the whole project is gated on.** Phase 6 deletes the old path only when the FOG beats what this prints today.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_score_pipeline.py`:

```python
"""The baseline scorer reads BOTH shapes -- today's extraction record and
tomorrow's FOG -- so the cutover comparison is apples to apples."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from score_pipeline import facts_from_extraction, facts_from_fog, report  # noqa: E402

from fdi.core.scoring import GroundTruth, score_document  # noqa: E402

_RECORD = {
    "schema_version": 1,
    "fields": [
        {"tag": "RFOGRS", "value": 255324.0, "answered": True},
        {"tag": "TOTFC", "value": {"current": 800.0, "prior": 700.0}, "answered": True},
        {"tag": "PBTR", "value": None, "answered": True},
        {"field_name": "no tag here", "value": 5.0, "answered": True},
    ],
}


def test_extraction_record_flattens_to_a_tag_map():
    facts = facts_from_extraction(_RECORD)
    assert facts["RFOGRS"] == 255324.0
    assert facts["TOTFC"] == {"current": 800.0, "prior": 700.0}


def test_a_null_value_is_absent_not_a_zero():
    """Collapsing null to 0.0 would turn every missed field into a wrong one."""
    assert "PBTR" not in facts_from_extraction(_RECORD)


def test_a_field_without_a_tag_is_skipped():
    facts = facts_from_extraction(_RECORD)
    assert len(facts) == 2


def test_malformed_record_yields_empty_never_raises():
    assert facts_from_extraction({}) == {}
    assert facts_from_extraction({"fields": "nope"}) == {}


def test_fog_returns_empty_until_mapping_exists():
    """Phase 2 emits no MAPS_TO edges; the scorer must say 'nothing', not crash."""
    assert facts_from_fog({"bases": {"consolidated": {"edges": []}}}, basis="consolidated") == {}


def test_report_renders_a_row_per_ground_truth_file():
    gt = GroundTruth("TCS", "FY2025-26", "consolidated", "profit_and_loss",
                     {"RFOGRS": 255324.0}, ("EXCHEXP",))
    score = score_document(gt, {"RFOGRS": 255324.0})
    text = report([(gt, score)])
    assert "TCS" in text
    assert "profit_and_loss" in text
    assert "100.0%" in text


def test_report_of_nothing_is_a_message_not_a_crash():
    assert "no ground truth" in report([]).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_score_pipeline.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'score_pipeline'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/score_pipeline.py`:

```python
"""Score a pipeline's output against the hand-keyed ground-truth corpus.

Reads BOTH output shapes -- today's extraction record (`extractions/{doc_id}/
v2/{hash}.json`) and the Phase 2+ FOG artifact -- so the Phase 6 cutover
comparison is apples to apples on one harness.

    uv run --directory backend python ../scripts/score_pipeline.py \
        --gt-dir docs/groundtruth --extraction path/to/record.json --issuer TCS

Exit code is always 0: this is a measurement tool, not a gate. The number it
prints is the gate.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from fdi.core.scoring import GroundTruth, Score, parse_groundtruth, score_document


def facts_from_extraction(record: Mapping[str, object]) -> dict[str, object]:
    """tag -> value out of an extraction record, skipping nulls and untagged
    fields. A null value means ABSENT; collapsing it to 0.0 would turn every
    missed field into a wrong one and destroy the precision signal."""
    out: dict[str, object] = {}
    fields = record.get("fields") if isinstance(record, Mapping) else None
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, Mapping):
            continue
        tag = field.get("tag")
        value = field.get("value")
        if isinstance(tag, str) and tag and value is not None:
            out[tag] = value
    return out


def facts_from_fog(fog: Mapping[str, object], *, basis: str) -> dict[str, object]:
    """tag -> value out of a FOG artifact, via MAPS_TO edges.

    Phase 2 emits LineItems and CONTAINS/REFERENCES edges but NO MAPS_TO --
    mapping is Phase 3. Until then this correctly returns {}: the FOG has values
    but no opinion about which taxonomy concept they are, and inventing that
    opinion here would score the scorer instead of the pipeline."""
    bases = fog.get("bases") if isinstance(fog, Mapping) else None
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    if not isinstance(region, Mapping):
        return {}

    items: dict[str, Mapping[str, object]] = {}
    raw_items = region.get("line_items")
    for item in raw_items if isinstance(raw_items, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            items[str(item["id"])] = item

    out: dict[str, object] = {}
    edges = region.get("edges")
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, Mapping) or edge.get("type") != "MAPS_TO":
            continue
        item = items.get(str(edge.get("source", "")))
        tag = edge.get("target")
        if item is None or not isinstance(tag, str) or not tag:
            continue
        current, prior = item.get("current"), item.get("prior")
        if current is None:
            continue
        out[tag] = {"current": current, "prior": prior}
    return out


def report(rows: Sequence[tuple[GroundTruth, Score]]) -> str:
    """A fixed-width table, one row per ground-truth file, plus a total."""
    if not rows:
        return "no ground truth scored"
    lines = [
        f"{'issuer':<8} {'basis':<13} {'statement':<16} "
        f"{'recall':>8} {'prec':>8} {'ok':>4} {'wrong':>6} {'miss':>5} {'fp':>4}",
        "-" * 82,
    ]
    t_correct = t_reported = t_committed = t_wrong = t_missed = t_fp = 0
    for gt, s in rows:
        lines.append(
            f"{gt.issuer:<8} {gt.basis:<13} {gt.statement:<16} "
            f"{s.recall:>7.1%} {s.precision:>7.1%} {s.correct:>4} "
            f"{s.wrong:>6} {s.missed:>5} {s.false_positive:>4}"
        )
        t_correct += s.correct
        t_reported += s.reported
        t_committed += s.committed
        t_wrong += s.wrong
        t_missed += s.missed
        t_fp += s.false_positive
    lines.append("-" * 82)
    recall = t_correct / t_reported if t_reported else 0.0
    precision = t_correct / t_committed if t_committed else 0.0
    lines.append(
        f"{'TOTAL':<8} {'':<13} {'':<16} {recall:>7.1%} {precision:>7.1%} "
        f"{t_correct:>4} {t_wrong:>6} {t_missed:>5} {t_fp:>4}"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt-dir", required=True, type=Path)
    parser.add_argument("--extraction", type=Path, help="an extraction record JSON")
    parser.add_argument("--fog", type=Path, help="a FOG artifact JSON")
    parser.add_argument("--issuer", help="score only this issuer")
    args = parser.parse_args()

    rows: list[tuple[GroundTruth, Score]] = []
    for path in sorted(args.gt_dir.glob("*.json")):
        gt = parse_groundtruth(json.loads(path.read_text(encoding="utf-8")))
        if gt is None:
            print(f"SKIP malformed {path.name}")
            continue
        if args.issuer and gt.issuer != args.issuer:
            continue
        if args.extraction is not None:
            produced = facts_from_extraction(
                json.loads(args.extraction.read_text(encoding="utf-8"))
            )
        elif args.fog is not None:
            produced = facts_from_fog(
                json.loads(args.fog.read_text(encoding="utf-8")), basis=gt.basis
            )
        else:
            produced = {}
        rows.append((gt, score_document(gt, produced)))

    print(report(rows))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_score_pipeline.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Produce the actual baseline**

With the compose stack up and each issuer ingested through `index: done`, run today's extraction and score it:

```bash
curl -s -X POST localhost:8000/extract/document \
  -H 'content-type: application/json' \
  -d '{"doc_key":"TCS-FY2025-26","force":true}' > /tmp/tcs-extraction.json

uv run --directory backend python ../scripts/score_pipeline.py \
  --gt-dir docs/groundtruth --extraction /tmp/tcs-extraction.json --issuer TCS
```

Repeat for RITES and RIL. **Record the printed table verbatim** in `.superpowers/sdd/progress.md` under a `PHASE 0 BASELINE` heading, including the date and which schema version was live. This is the number Phase 6 must beat.

Note: `FDI_MILVUS_URI` must be set (`http://milvus:19530`) or `index` skips and every field comes back null — a baseline of zero is not a baseline.

- [ ] **Step 6: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add scripts/score_pipeline.py backend/tests/unit/test_score_pipeline.py .superpowers/sdd/progress.md
git commit -m "feat(scoring): baseline harness + recorded Phase 0 baseline for the current pipeline"
```

---

## Task 5: Footing rules for statement verification

**Files:**
- Modify: `backend/src/fdi/core/statements.py` (append; do not restructure)
- Test: `backend/tests/unit/test_statement_footing.py`

**Interfaces:**
- Consumes: `fdi.core.statements.{FaceRow, verify_footing}` (both already exist)
- Produces: `FOOTING_RULES: Mapping[str, tuple[tuple[str, ...], tuple[str, ...], str]]` — statement → `(plus, minus, target)`; `describe_footing(rows: Sequence[FaceRow], statement: str) -> dict[str, object]` returning `{"rule": str, "passed": bool | None, "checked": bool}`

**Why:** `verify_footing` exists but no caller names the *rules*. Footing is what proves the right table was chosen regardless of how it was found — it must be recorded on the artifact, not recomputed ad hoc.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_statement_footing.py`:

```python
"""Footing is what PROVES the right face table was chosen, whatever found it.
`income - expenses = PBT` ties on TCS, RITES and RIL."""

from fdi.core.statements import FaceRow, describe_footing


def _row(label: str, current: float | None) -> FaceRow:
    return FaceRow(label=label, caption="", ref="", current=current, prior=None)


_PL = [
    _row("Total income", 100.0),
    _row("Total expenses", 70.0),
    _row("Profit before tax", 30.0),
]


def test_a_footing_pl_passes():
    result = describe_footing(_PL, "profit_and_loss")
    assert result["passed"] is True
    assert result["checked"] is True
    assert "profit before tax" in result["rule"]


def test_a_non_footing_pl_fails_loudly():
    rows = [_row("Total income", 100.0), _row("Total expenses", 70.0),
            _row("Profit before tax", 44.0)]
    assert describe_footing(rows, "profit_and_loss")["passed"] is False


def test_a_missing_line_is_unchecked_not_failed():
    """None means 'could not check'. Reporting it as a failure would discard a
    correct table that merely labels a line unusually."""
    rows = [_row("Total income", 100.0), _row("Profit before tax", 30.0)]
    result = describe_footing(rows, "profit_and_loss")
    assert result["passed"] is None
    assert result["checked"] is False


def test_balance_sheet_foots_on_the_accounting_identity():
    rows = [_row("Total equity", 40.0), _row("Total liabilities", 60.0),
            _row("Total assets", 100.0)]
    assert describe_footing(rows, "balance_sheet")["passed"] is True


def test_a_statement_with_no_rule_is_unchecked():
    assert describe_footing(_PL, "cash_flow")["checked"] is False
    assert describe_footing(_PL, "cash_flow")["passed"] is None


def test_tolerance_absorbs_rounding_in_a_printed_statement():
    rows = [_row("Total income", 100.0), _row("Total expenses", 70.0),
            _row("Profit before tax", 30.4)]
    assert describe_footing(rows, "profit_and_loss")["passed"] is True


def test_empty_rows_never_raise():
    assert describe_footing([], "profit_and_loss")["passed"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_statement_footing.py -q`
Expected: FAIL — `ImportError: cannot import name 'describe_footing'`

- [ ] **Step 3: Append the implementation to `core/statements.py`**

Add at the end of `backend/src/fdi/core/statements.py`:

```python
# The arithmetic each face must satisfy, as (plus, minus, target) fragments
# matched against row labels. Cash flow has NO rule: its anchors already miss
# RITES and RIL entirely (see the design's Phase 5 hole), so asserting one would
# manufacture failures on documents whose face is perfectly correct.
FOOTING_RULES: Mapping[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "profit_and_loss": (("total income",), ("total expenses",), "profit before tax"),
    "balance_sheet": (("total equity", "total liabilities"), (), "total assets"),
}

_FOOTING_TOLERANCE = 1.0


def describe_footing(rows: Sequence[FaceRow], statement: str) -> dict[str, object]:
    """Whether this face's arithmetic ties, as a recordable verdict.

    `passed is None` means COULD NOT CHECK -- a needed line is absent, or the
    statement has no rule. That is deliberately distinct from False: discarding
    a correct table because it labels one line unusually is worse than not
    checking it. `checked` makes the distinction explicit on the artifact."""
    rule = FOOTING_RULES.get(statement)
    if rule is None:
        return {"rule": "", "passed": None, "checked": False}
    plus, minus, target = rule
    passed = verify_footing(
        rows, plus, minus, target, tolerance=_FOOTING_TOLERANCE
    )
    parts = " + ".join(plus) + ("".join(f" - {m}" for m in minus))
    return {
        "rule": f"{parts} = {target}",
        "passed": passed,
        "checked": passed is not None,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_statement_footing.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Run the existing statements tests to check for regressions**

Run: `uv run --directory backend pytest tests/unit -k "statement or note" -q`
Expected: PASS, no regressions

- [ ] **Step 6: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/statements.py backend/tests/unit/test_statement_footing.py
git commit -m "feat(statements): named footing rules + recordable verdict (None = unchecked, not failed)"
```

---

## Task 6: `statement_index` stage

**Files:**
- Create: `backend/src/fdi/core/fog_build.py`
- Create: `backend/src/fdi/pipeline/stages/statement_index.py`
- Modify: `backend/src/fdi/pipeline/stages/__init__.py`
- Test: `backend/tests/unit/test_statement_index.py`

**Interfaces:**
- Consumes: `fdi.core.statements.{STATEMENT_ANCHORS, find_statement_tables, parse_face_table, describe_footing}`, `fdi.core.basis.basis_regions`, `fdi.core.notes.find_statement_pages`
- Produces: `fdi.core.fog_build.build_statement_index(ocr, basis_artifact) -> dict[str, object]` with shape:
  ```
  {"schema_version": 1,
   "statements": [{"id", "statement", "basis", "page", "table_index",
                   "anchor_score", "footing": {"rule", "passed", "checked"}}]}
  ```
  `id` is `f"st:{basis}:{statement}"`. `table_index` indexes `ocr["tables"]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_statement_index.py`:

```python
"""Find the face by CONTENT, per basis. Heading detection returned three false
positives on TCS and could not say WHICH statement it had found."""

from fdi.core.fog_build import build_statement_index


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


def _pl_table(page: int, pbt: str = "30") -> dict[str, object]:
    return {
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": page * 1000, "length": 100}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2025-26"),
            _cell(1, 0, "Revenue from operations"), _cell(1, 1, "10"), _cell(1, 2, "90"),
            _cell(2, 0, "Total income"), _cell(2, 2, "100"),
            _cell(3, 0, "Total expenses"), _cell(3, 2, "70"),
            _cell(4, 0, "Profit before tax"), _cell(4, 2, pbt),
        ],
    }


_BASIS = {
    "default_basis": "unknown",
    "regions": [
        {"basis": "standalone", "start_page": 90, "end_page": 140},
        {"basis": "consolidated", "start_page": 141, "end_page": 200},
    ],
}


def test_both_bases_surface_and_are_typed():
    ocr = {"paragraphs": [], "tables": [_pl_table(100), _pl_table(150)]}
    index = build_statement_index(ocr, _BASIS)
    found = {(s["basis"], s["statement"]) for s in index["statements"]}
    assert ("standalone", "profit_and_loss") in found
    assert ("consolidated", "profit_and_loss") in found


def test_the_statement_id_is_stable_and_addressable():
    ocr = {"paragraphs": [], "tables": [_pl_table(150)]}
    index = build_statement_index(ocr, _BASIS)
    ids = [s["id"] for s in index["statements"]]
    assert "st:consolidated:profit_and_loss" in ids


def test_table_index_points_back_into_the_ocr_tables_list():
    ocr = {"paragraphs": [], "tables": [{"cells": []}, _pl_table(150)]}
    index = build_statement_index(ocr, _BASIS)
    stmt = next(s for s in index["statements"] if s["basis"] == "consolidated")
    assert stmt["table_index"] == 1


def test_footing_is_recorded_on_the_statement():
    ocr = {"paragraphs": [], "tables": [_pl_table(150)]}
    stmt = build_statement_index(ocr, _BASIS)["statements"][0]
    assert stmt["footing"]["passed"] is True


def test_a_non_footing_face_is_still_indexed_but_flagged():
    """A face that does not tie is a finding, not a reason to lose the statement."""
    ocr = {"paragraphs": [], "tables": [_pl_table(150, pbt="44")]}
    stmt = build_statement_index(ocr, _BASIS)["statements"][0]
    assert stmt["footing"]["passed"] is False
    assert stmt["page"] == 150


def test_the_best_scoring_table_wins_within_a_basis():
    """Two candidates in one region must collapse to one statement, not two."""
    ocr = {"paragraphs": [], "tables": [_pl_table(150), _pl_table(160)]}
    index = build_statement_index(ocr, _BASIS)
    pl = [s for s in index["statements"] if s["statement"] == "profit_and_loss"]
    assert len(pl) == 1


def test_a_document_with_no_basis_regions_still_indexes():
    """No anchors is a real shape; the whole document becomes one unknown region."""
    ocr = {"paragraphs": [], "tables": [_pl_table(10)]}
    index = build_statement_index(ocr, {"regions": [], "default_basis": "unknown"})
    assert index["statements"]
    assert index["statements"][0]["basis"] == "unknown"


def test_output_is_deterministic_for_content_addressing():
    ocr = {"paragraphs": [], "tables": [_pl_table(150), _pl_table(100)]}
    assert build_statement_index(ocr, _BASIS) == build_statement_index(ocr, _BASIS)


def test_malformed_input_never_raises():
    assert build_statement_index({}, {}) == {"schema_version": 1, "statements": []}
    assert build_statement_index({"tables": "nope"}, {})["statements"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_statement_index.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.fog_build'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/src/fdi/core/fog_build.py`:

```python
"""Build the Financial Object Graph's indices and nodes from OCR artifacts.

The algorithms; `fdi.core.fog` owns the shapes. Everything here composes the
already-measured primitives in `fdi.core.statements` / `notes` / `numbers`
rather than reimplementing them -- those carry scars (the comma bug, the RITES
note grammar, the caption rule) that are expensive to re-earn.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fdi.core.basis import basis_regions
from fdi.core.statements import (
    STATEMENT_ANCHORS,
    describe_footing,
    find_statement_tables,
    parse_face_table,
)

# A document with no basis anchors is a real shape (see core/basis.py): it
# yields `regions: []`. Indexing nothing would silently lose the whole filing,
# so the fallback is ONE region of unknown basis spanning every page.
_UNKNOWN_REGION = ("unknown", 1, 100_000)


def _regions_of(basis_artifact: Mapping[str, object]) -> list[tuple[str, int, int]]:
    regions = basis_regions(basis_artifact) if isinstance(basis_artifact, Mapping) else []
    return regions or [_UNKNOWN_REGION]


def _tables_of(ocr: Mapping[str, object]) -> list[object]:
    tables = ocr.get("tables") if isinstance(ocr, Mapping) else None
    return list(tables) if isinstance(tables, list) else []


def build_statement_index(
    ocr: Mapping[str, object], basis_artifact: Mapping[str, object]
) -> dict[str, object]:
    """Locate every statement face, per basis region, by table CONTENT.

    One statement per (basis, statement_type): `find_statement_tables` already
    returns candidates best-first (by anchor score, then page), so the first is
    the face and the rest are restatements or segment tables that happen to
    share vocabulary. Keeping several would give the FOG two competing
    balance sheets and no rule for choosing.

    Total: never raises on any input shape."""
    tables = _tables_of(ocr)
    statements: list[dict[str, object]] = []

    for basis, lo, hi in _regions_of(basis_artifact):
        for statement in STATEMENT_ANCHORS:
            found = find_statement_tables(tables, statement, lo, hi)
            if not found:
                continue
            best = found[0]
            # Identity, NOT equality: `list.index` compares with `==`, and two
            # structurally identical tables on different pages would both
            # resolve to the first one's index -- silently pointing every
            # LineItem at the wrong table.
            table_index = next(
                (i for i, t in enumerate(tables) if t is best.table), -1
            )
            if table_index < 0:  # pragma: no cover - find_ returns our own tables
                continue
            rows = parse_face_table(best.table)
            statements.append({
                "id": f"st:{basis}:{statement}",
                "statement": statement,
                "basis": basis,
                "page": best.page,
                "table_index": table_index,
                "anchor_score": best.score,
                "footing": describe_footing(rows, statement),
            })

    statements.sort(key=lambda s: (str(s["basis"]), str(s["statement"])))
    return {"schema_version": 1, "statements": statements}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_statement_index.py -q`
Expected: PASS, 9 passed

- [ ] **Step 5: Create the stage**

Create `backend/src/fdi/pipeline/stages/statement_index.py`:

```python
"""StatementIndexStage: promote face detection from a per-request helper to a
cached, inspectable artifact. Deps `(ocr, structure, basis)` -- `structure` is
depended on so a re-stitch invalidates the index even though the face is read
off `ocr` tables (stitching changes which tables exist downstream, and a stale
index would point at the wrong `table_index`)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_build import build_statement_index
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class StatementIndexStage:
    name = "statement_index"
    version = 1
    deps: tuple[str, ...] = ("ocr", "structure", "basis")

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        basis = cast(Mapping[str, object], await ctx.read("basis"))
        return build_statement_index(ocr, basis)


REGISTRY.register(StatementIndexStage())
```

Add to `backend/src/fdi/pipeline/stages/__init__.py`, keeping the existing alphabetical-ish import style:

```python
from fdi.pipeline.stages import (
    statement_index as statement_index,  # noqa: F401 - import for its registration side effect
)
```

- [ ] **Step 6: Write the registration test**

Append to `backend/tests/unit/test_statement_index.py`:

```python
def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401 - triggers registration
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("statement_index")
    assert stage.deps == ("ocr", "structure", "basis")
    assert stage.version == 1


def test_the_dag_still_resolves_with_the_new_stage():
    """An unknown dep or a cycle fails here, not at runtime on a real document."""
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver

    resolver = DagResolver(REGISTRY.all())
    assert "statement_index" in resolver.topological_order()
```

- [ ] **Step 7: Run the tests**

Run: `uv run --directory backend pytest tests/unit/test_statement_index.py -q`
Expected: PASS, 11 passed

- [ ] **Step 8: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_build.py backend/src/fdi/pipeline/stages/statement_index.py \
        backend/src/fdi/pipeline/stages/__init__.py backend/tests/unit/test_statement_index.py
git commit -m "feat(fog): statement_index stage — content-anchored face detection per basis"
```

---

## Task 7: `note_index` stage

**Files:**
- Modify: `backend/src/fdi/core/fog_build.py` (append)
- Create: `backend/src/fdi/pipeline/stages/note_index.py`
- Modify: `backend/src/fdi/pipeline/stages/__init__.py`
- Test: `backend/tests/unit/test_note_index.py`

**Interfaces:**
- Consumes: `fdi.core.statements.{note_windows, tables_in_window, NoteWindow}`, `fdi.core.notes.{basis_bounds, find_statement_pages}`, `build_statement_index` (Task 6)
- Produces: `fdi.core.fog_build.build_note_index(ocr, basis_artifact) -> dict[str, object]` with shape:
  ```
  {"schema_version": 1,
   "bases": {"<basis>": {"notes": [{"id", "ref", "title", "page",
                                    "start_offset", "end_offset",
                                    "table_indices": [int]}],
                         "page_lo": int, "page_hi": int}}}
  ```
  `id` is `f"nt:{basis}:{ref}"`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_note_index.py`:

```python
"""Notes are addressed by OFFSET WINDOW, not page: TCS p301 holds note 13(a) at
offset 897,095 and 13(b) at 897,486, so a page fetch hands both to whichever
concept asks first."""

from fdi.core.fog_build import build_note_index


def _head(text: str, page: int, offset: int) -> dict[str, object]:
    return {
        "content": text,
        "role": "sectionHeading",
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": len(text)}],
    }


def _table(page: int, offset: int) -> dict[str, object]:
    return {
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": 50}],
        "cells": [{"row_index": 0, "column_index": 0, "content": "Land"}],
    }


_BASIS = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 100, "end_page": 200}],
}

_OCR = {
    "paragraphs": [
        _head("10. Revenue from operations", 150, 1000),
        _head("13. Other expenses", 160, 2000),
        _head("(a) Facility costs", 160, 2100),
        _head("(b) Travel", 161, 2400),
    ],
    "tables": [_table(150, 1100), _table(160, 2150), _table(161, 2450)],
}


def test_flat_and_sub_notes_are_both_indexed():
    index = build_note_index(_OCR, _BASIS)
    refs = {n["ref"] for n in index["bases"]["consolidated"]["notes"]}
    assert {"10", "13", "13(a)", "13(b)"} <= refs


def test_the_note_id_is_basis_scoped():
    """A report with both bases has two 'note 8'; unscoped ids collide."""
    index = build_note_index(_OCR, _BASIS)
    ids = {n["id"] for n in index["bases"]["consolidated"]["notes"]}
    assert "nt:consolidated:13(a)" in ids


def test_a_note_carries_its_offset_window_not_just_a_page():
    index = build_note_index(_OCR, _BASIS)
    note = next(
        n for n in index["bases"]["consolidated"]["notes"] if n["ref"] == "13(a)"
    )
    assert note["start_offset"] == 2100
    assert note["end_offset"] == 2400


def test_tables_are_bound_to_the_note_whose_window_contains_them():
    index = build_note_index(_OCR, _BASIS)
    notes = {n["ref"]: n for n in index["bases"]["consolidated"]["notes"]}
    assert notes["13(a)"]["table_indices"] == [1]
    assert notes["13(b)"]["table_indices"] == [2]


def test_the_title_is_kept_it_is_how_uncited_notes_are_found():
    """A note the face never cites is reachable only by its own heading."""
    index = build_note_index(_OCR, _BASIS)
    note = next(
        n for n in index["bases"]["consolidated"]["notes"] if n["ref"] == "10"
    )
    assert note["title"] == "Revenue from operations"


def test_dotted_references_are_accepted():
    """RITES numbers its notes 2.01 / 2.08.2; the old regex parsed 0 of 30."""
    ocr = {
        "paragraphs": [_head("2.17 Revenue From Operations", 150, 1000)],
        "tables": [],
    }
    index = build_note_index(ocr, _BASIS)
    refs = {n["ref"] for n in index["bases"]["consolidated"]["notes"]}
    assert "2.17" in refs


def test_each_basis_gets_its_own_note_namespace():
    basis = {
        "default_basis": "unknown",
        "regions": [
            {"basis": "standalone", "start_page": 100, "end_page": 155},
            {"basis": "consolidated", "start_page": 156, "end_page": 200},
        ],
    }
    index = build_note_index(_OCR, basis)
    assert set(index["bases"]) == {"standalone", "consolidated"}
    standalone_refs = {n["ref"] for n in index["bases"]["standalone"]["notes"]}
    assert "10" in standalone_refs
    assert "13" not in standalone_refs


def test_output_is_deterministic():
    assert build_note_index(_OCR, _BASIS) == build_note_index(_OCR, _BASIS)


def test_malformed_input_never_raises():
    assert build_note_index({}, {})["bases"] == {"unknown": {
        "notes": [], "page_lo": 1, "page_hi": 100_000}}


def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401 - triggers registration
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("note_index")
    assert stage.deps == ("ocr", "structure", "basis")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_note_index.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_note_index'`

- [ ] **Step 3: Append the implementation to `core/fog_build.py`**

Add these imports at the top of `backend/src/fdi/core/fog_build.py`:

```python
from fdi.core.statements import note_windows, tables_in_window
```

Append:

```python
def build_note_index(
    ocr: Mapping[str, object], basis_artifact: Mapping[str, object]
) -> dict[str, object]:
    """ref -> offset window, title and the tables inside it, per basis.

    Basis scoping is not optional: a report carrying both consolidated and
    standalone statements has two "note 8", and an unscoped index resolves a
    reference to whichever came first.

    Offsets, not pages: TCS p301 carries note 13(a) at offset 897,095 and 13(b)
    at 897,486, so a page-granular fetch hands both to whichever concept asks
    first. Total: never raises."""
    paragraphs_raw = ocr.get("paragraphs") if isinstance(ocr, Mapping) else None
    paragraphs = paragraphs_raw if isinstance(paragraphs_raw, list) else []
    tables = _tables_of(ocr)

    bases: dict[str, object] = {}
    for basis, lo, hi in _regions_of(basis_artifact):
        windows = note_windows(paragraphs, lo, hi)
        notes: list[dict[str, object]] = []
        for ref in sorted(windows):
            window = windows[ref]
            # Identity, NOT equality -- see build_statement_index. Two notes can
            # hold structurally identical tables, and `.index()` would bind both
            # to the first.
            in_window = tables_in_window(tables, window)
            indices = [
                i for i, t in enumerate(tables) if any(t is w for w in in_window)
            ]
            notes.append({
                "id": f"nt:{basis}:{ref}",
                "ref": ref,
                "title": window.title,
                "page": window.page,
                "start_offset": window.start,
                "end_offset": window.end,
                "table_indices": sorted(indices),
            })
        bases[basis] = {"notes": notes, "page_lo": lo, "page_hi": hi}

    return {"schema_version": 1, "bases": bases}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_note_index.py -q`
Expected: FAIL on `test_the_stage_is_registered_with_the_right_deps` only — the core function now works. This is expected; Step 5 adds the stage.

- [ ] **Step 5: Create the stage**

Create `backend/src/fdi/pipeline/stages/note_index.py`:

```python
"""NoteIndexStage: cache the note index as an inspectable artifact.

Currently rebuilt in memory per extraction request. As an artifact it is
cached, diffable and auditable -- and a ref-grammar failure like RITES's 0/30
becomes visible in one file instead of silently degrading retrieval."""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_build import build_note_index
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class NoteIndexStage:
    name = "note_index"
    version = 1
    deps: tuple[str, ...] = ("ocr", "structure", "basis")

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        basis = cast(Mapping[str, object], await ctx.read("basis"))
        return build_note_index(ocr, basis)


REGISTRY.register(NoteIndexStage())
```

Add to `backend/src/fdi/pipeline/stages/__init__.py`:

```python
from fdi.pipeline.stages import (
    note_index as note_index,  # noqa: F401 - import for its registration side effect
)
```

- [ ] **Step 6: Run the tests**

Run: `uv run --directory backend pytest tests/unit/test_note_index.py -q`
Expected: PASS, 10 passed

- [ ] **Step 7: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_build.py backend/src/fdi/pipeline/stages/note_index.py \
        backend/src/fdi/pipeline/stages/__init__.py backend/tests/unit/test_note_index.py
git commit -m "feat(fog): note_index stage — offset-window note resolution, basis-scoped"
```

---

## Task 8: The FOG data model

**Files:**
- Create: `backend/src/fdi/core/fog.py`
- Test: `backend/tests/unit/test_fog_model.py`

**Interfaces:**
- Consumes: nothing (leaf module)
- Produces:
  - `LineItem` (frozen dataclass): `id: str`, `parent: str`, `origin: str`, `label: str`, `caption: str`, `ref: str`, `current: float | None`, `prior: float | None`, `page: int`, `table_index: int`, `row_index: int`
  - `Edge` (frozen dataclass): `type: str`, `source: str`, `target: str`
  - `line_item_id(parent: str, row_index: int) -> str`
  - `line_item_body(item: LineItem) -> dict[str, object]`
  - `edge_body(edge: Edge) -> dict[str, object]`
  - `EDGE_TYPES: frozenset[str]`
  - `build_fog_artifact(regions: Mapping[str, tuple[Sequence[Mapping[str, object]], Sequence[Mapping[str, object]], Sequence[LineItem], Sequence[Edge]]], *, doc_id: str, diagnostics: Mapping[str, object]) -> dict[str, object]`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_model.py`:

```python
"""The FOG's shapes. Ids must be STABLE and content-addressable: a stage
artifact whose ids move between identical runs breaks caching silently."""

import pytest

from fdi.core.fog import (
    EDGE_TYPES,
    Edge,
    LineItem,
    build_fog_artifact,
    edge_body,
    line_item_body,
    line_item_id,
)

_ITEM = LineItem(
    id="li:st:consolidated:profit_and_loss:r4",
    parent="st:consolidated:profit_and_loss",
    origin="face",
    label="Revenue from operations",
    caption="",
    ref="10",
    current=255324.0,
    prior=240893.0,
    page=253,
    table_index=88,
    row_index=4,
)


def test_the_line_item_id_is_derived_from_its_parent_and_row():
    assert line_item_id("st:consolidated:profit_and_loss", 4) == (
        "li:st:consolidated:profit_and_loss:r4"
    )


def test_the_same_inputs_always_produce_the_same_id():
    assert line_item_id("nt:standalone:13(b)", 7) == line_item_id("nt:standalone:13(b)", 7)


def test_line_item_body_round_trips_every_field():
    body = line_item_body(_ITEM)
    assert body["label"] == "Revenue from operations"
    assert body["current"] == 255324.0
    assert body["prior"] == 240893.0
    assert body["origin"] == "face"
    assert body["ref"] == "10"
    assert body["row_index"] == 4


def test_a_null_amount_stays_null_not_zero():
    item = LineItem(
        id="x", parent="p", origin="note", label="Land", caption="", ref="",
        current=None, prior=None, page=1, table_index=0, row_index=0,
    )
    body = line_item_body(item)
    assert body["current"] is None
    assert body["prior"] is None


def test_edge_body_names_source_and_target_explicitly():
    body = edge_body(Edge("REFERENCES", "li:a", "nt:consolidated:8(a)"))
    assert body == {"type": "REFERENCES", "source": "li:a", "target": "nt:consolidated:8(a)"}


def test_the_edge_vocabulary_is_closed_and_includes_the_reserved_types():
    assert "CONTAINS" in EDGE_TYPES
    assert "REFERENCES" in EDGE_TYPES
    assert "MAPS_TO" in EDGE_TYPES
    assert "GOVERNED_BY" in EDGE_TYPES  # reserved for domain 2, never emitted here


def test_the_artifact_keys_everything_by_basis():
    artifact = build_fog_artifact(
        {"consolidated": ([{"id": "st:consolidated:profit_and_loss"}], [], [_ITEM],
                          [Edge("CONTAINS", "st:consolidated:profit_and_loss", _ITEM.id)])},
        doc_id="doc-1",
        diagnostics={},
    )
    region = artifact["bases"]["consolidated"]
    assert region["statements"][0]["id"] == "st:consolidated:profit_and_loss"
    assert region["line_items"][0]["label"] == "Revenue from operations"
    assert region["edges"][0]["type"] == "CONTAINS"


def test_the_artifact_carries_its_doc_id_and_schema_version():
    artifact = build_fog_artifact({}, doc_id="doc-1", diagnostics={})
    assert artifact["doc_id"] == "doc-1"
    assert artifact["schema_version"] == 1


def test_edges_are_sorted_so_the_artifact_hashes_stably():
    edges = [
        Edge("REFERENCES", "li:b", "nt:x"),
        Edge("CONTAINS", "st:a", "li:b"),
        Edge("CONTAINS", "st:a", "li:a"),
    ]
    artifact = build_fog_artifact(
        {"consolidated": ([], [], [], edges)}, doc_id="d", diagnostics={}
    )
    got = [(e["type"], e["source"], e["target"]) for e in artifact["bases"]["consolidated"]["edges"]]
    assert got == sorted(got)


def test_an_unknown_edge_type_is_rejected_loudly():
    """A typo'd edge type would silently produce a graph nothing traverses."""
    with pytest.raises(ValueError, match="unknown edge type"):
        edge_body(Edge("POINTS_AT", "a", "b"))


def test_an_empty_document_is_a_valid_artifact_not_a_crash():
    artifact = build_fog_artifact({}, doc_id="d", diagnostics={})
    assert artifact["bases"] == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_model.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.fog'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/src/fdi/core/fog.py`:

```python
"""The Financial Object Graph's shapes -- nodes, edges, ids and serialization.

The FOG is PRE-ONTOLOGY: it is the issuer's own financial model, in the
issuer's own words, with values parsed deterministically. It is NOT the 402
taxonomy tags. `MAPS_TO` edges are the single seam where the issuer's model
meets the taxonomy, which is what lets document parsing be swapped without
touching financial reasoning.

This module owns shapes only; the algorithms live in `fdi.core.fog_build`.

Pure (`fdi.core` law): no I/O, deterministic, total -- except `edge_body`,
which raises on an unknown edge type ON PURPOSE (see its docstring).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# The closed edge vocabulary. GOVERNED_BY and the Policy node are RESERVED for
# domain 2 and never emitted here -- named now so the schema version that adds
# them is additive rather than breaking.
EDGE_TYPES: frozenset[str] = frozenset({
    "CONTAINS",
    "REFERENCES",
    "MAPS_TO",
    "COMPOSES",
    "RECONCILES_WITH",
    "SAME_AS",
    "GOVERNED_BY",
})


@dataclass(frozen=True)
class LineItem:
    """One row of a statement face or a note -- the FOG's atom.

    `origin` is "face" or "note". `ref` is the note reference printed in the
    face's note column, or "" -- an EMPTY ref on a face row means the issuer
    printed no pointer (RIL prints no note column at all), which is `absent`,
    not `missed`."""

    id: str
    parent: str
    origin: str
    label: str
    caption: str
    ref: str
    current: float | None
    prior: float | None
    page: int
    table_index: int
    row_index: int


@dataclass(frozen=True)
class Edge:
    type: str
    source: str
    target: str


def line_item_id(parent: str, row_index: int) -> str:
    """A stable, content-addressable id: parent node + row position.

    Stability is not cosmetic. Stage artifacts are content-addressed, so an id
    that moves between two identical runs changes the output hash and
    invalidates every downstream cache silently."""
    return f"li:{parent}:r{row_index}"


def line_item_body(item: LineItem) -> dict[str, object]:
    """A LineItem as a JSON-safe mapping. `None` amounts stay `None`:
    collapsing them to 0.0 would turn 'this row has no figure' into a fact."""
    return {
        "id": item.id,
        "parent": item.parent,
        "origin": item.origin,
        "label": item.label,
        "caption": item.caption,
        "ref": item.ref,
        "current": item.current,
        "prior": item.prior,
        "page": item.page,
        "table_index": item.table_index,
        "row_index": item.row_index,
    }


def edge_body(edge: Edge) -> dict[str, object]:
    """An Edge as a JSON-safe mapping.

    Raises on an unknown type -- the ONE deliberate exception to the `fdi.core`
    totality law. An edge type is authored in code, never read from a document,
    so a bad one is a programming error; degrading it to a silent skip would
    produce a graph that nothing traverses and no test notices."""
    if edge.type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type {edge.type!r}")
    return {"type": edge.type, "source": edge.source, "target": edge.target}


def build_fog_artifact(
    regions: Mapping[
        str,
        tuple[
            Sequence[Mapping[str, object]],
            Sequence[Mapping[str, object]],
            Sequence[LineItem],
            Sequence[Edge],
        ],
    ],
    *,
    doc_id: str,
    diagnostics: Mapping[str, object],
) -> dict[str, object]:
    """Assemble the per-basis FOG artifact from `(statements, notes, items,
    edges)` tuples. Everything is sorted so the body hashes stably."""
    bases: dict[str, object] = {}
    for basis in sorted(regions):
        statements, notes, items, edges = regions[basis]
        bases[basis] = {
            "statements": list(statements),
            "notes": list(notes),
            "line_items": [line_item_body(i) for i in items],
            "edges": [
                edge_body(e)
                for e in sorted(edges, key=lambda e: (e.type, e.source, e.target))
            ],
        }
    return {
        "schema_version": 1,
        "doc_id": doc_id,
        "bases": bases,
        "diagnostics": dict(diagnostics),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_model.py -q`
Expected: PASS, 11 passed

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog.py backend/tests/unit/test_fog_model.py
git commit -m "feat(fog): the Financial Object Graph data model — nodes, edges, stable ids"
```

---

## Task 9: Face rows → LineItems, with REFERENCES edges

**Files:**
- Modify: `backend/src/fdi/core/fog_build.py` (append)
- Test: `backend/tests/unit/test_fog_face.py`

**Interfaces:**
- Consumes: `fdi.core.fog.{LineItem, Edge, line_item_id}`, `fdi.core.statements.parse_face_table`, `build_statement_index` / `build_note_index` output shapes
- Produces: `fdi.core.fog_build.face_line_items(ocr, statement_index, note_index, *, basis) -> tuple[list[LineItem], list[Edge]]`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_face.py`:

```python
"""Face rows become persistent objects, and the note column becomes REFERENCES
edges -- printed navigation the issuer asserted, never a similarity guess."""

from fdi.core.fog_build import (
    build_note_index,
    build_statement_index,
    face_line_items,
)


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_FACE = {
    "bounding_regions": [{"page_number": 150}],
    "spans": [{"offset": 500, "length": 100}],
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "Note"),
        _cell(0, 2, "2025-26"), _cell(0, 3, "2024-25"),
        _cell(1, 0, "INCOME"),
        _cell(2, 0, "Revenue from operations"), _cell(2, 1, "10"),
        _cell(2, 2, "2,55,324"), _cell(2, 3, "2,40,893"),
        _cell(3, 0, "Total income"), _cell(3, 2, "100"), _cell(3, 3, "95"),
        _cell(4, 0, "Total expenses"), _cell(4, 2, "70"), _cell(4, 3, "65"),
        _cell(5, 0, "Profit before tax"), _cell(5, 2, "30"), _cell(5, 3, "30"),
    ],
}

_NOTE_HEAD = {
    "content": "10. Revenue from operations",
    "role": "sectionHeading",
    "bounding_regions": [{"page_number": 170}],
    "spans": [{"offset": 9000, "length": 27}],
}

_BASIS = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 100, "end_page": 200}],
}
_OCR = {"paragraphs": [_NOTE_HEAD], "tables": [_FACE]}


def _build():
    si = build_statement_index(_OCR, _BASIS)
    ni = build_note_index(_OCR, _BASIS)
    return face_line_items(_OCR, si, ni, basis="consolidated")


def test_every_valued_face_row_becomes_a_line_item():
    items, _ = _build()
    labels = [i.label for i in items]
    assert "Revenue from operations" in labels
    assert "Profit before tax" in labels


def test_indian_digit_grouping_is_parsed_deterministically():
    """'2,55,324' is 255324 -- the model never interprets a separator."""
    items, _ = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert revenue.current == 255324.0
    assert revenue.prior == 240893.0


def test_a_row_with_no_amount_is_a_caption_not_a_line_item():
    """RIL prints 'a) Owners of the Company' under three different captions;
    mapping caption rows as values produced several wrong assignments."""
    items, _ = _build()
    assert all(i.label != "INCOME" for i in items)
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert revenue.caption == "INCOME"


def test_the_printed_note_column_becomes_a_references_edge():
    items, edges = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    refs = [e for e in edges if e.type == "REFERENCES"]
    assert len(refs) == 1
    assert refs[0].source == revenue.id
    assert refs[0].target == "nt:consolidated:10"


def test_a_row_with_no_printed_ref_gets_no_edge_absent_not_missed():
    items, edges = _build()
    pbt = next(i for i in items if i.label == "Profit before tax")
    assert pbt.ref == ""
    assert not [e for e in edges if e.source == pbt.id and e.type == "REFERENCES"]


def test_a_ref_that_resolves_to_no_note_yields_no_edge():
    """A dangling pointer must not create an edge to a node that is not there."""
    ocr = {"paragraphs": [], "tables": [_FACE]}
    si = build_statement_index(ocr, _BASIS)
    ni = build_note_index(ocr, _BASIS)
    _, edges = face_line_items(ocr, si, ni, basis="consolidated")
    assert not [e for e in edges if e.type == "REFERENCES"]


def test_every_line_item_is_contained_by_its_statement():
    items, edges = _build()
    contains = {e.target for e in edges if e.type == "CONTAINS"}
    assert {i.id for i in items} == contains


def test_provenance_is_carried_on_every_item():
    items, _ = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert revenue.page == 150
    assert revenue.table_index == 0
    assert revenue.row_index == 2
    assert revenue.origin == "face"


def test_ids_are_unique_across_the_whole_face():
    items, _ = _build()
    assert len({i.id for i in items}) == len(items)


def test_a_basis_with_no_statements_yields_nothing_not_a_crash():
    si = build_statement_index(_OCR, _BASIS)
    ni = build_note_index(_OCR, _BASIS)
    items, edges = face_line_items(_OCR, si, ni, basis="standalone")
    assert items == []
    assert edges == []


def test_malformed_input_never_raises():
    assert face_line_items({}, {}, {}, basis="consolidated") == ([], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_face.py -q`
Expected: FAIL — `ImportError: cannot import name 'face_line_items'`

- [ ] **Step 3: Append the implementation to `core/fog_build.py`**

Add to the imports at the top of `backend/src/fdi/core/fog_build.py`:

```python
from fdi.core.fog import Edge, LineItem, line_item_id
```

Append:

```python
def _region_of(index: Mapping[str, object], basis: str) -> Mapping[str, object]:
    """One basis's slice of a `build_note_index` artifact, or an empty one."""
    bases = index.get("bases") if isinstance(index, Mapping) else None
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    return region if isinstance(region, Mapping) else {}


def _note_refs(note_index: Mapping[str, object], basis: str) -> set[str]:
    """The refs this basis actually resolved -- used to reject dangling
    pointers. An edge to a node that is not in the graph is worse than no edge:
    it looks like navigation and leads nowhere."""
    notes = _region_of(note_index, basis).get("notes")
    return {
        str(n["ref"])
        for n in (notes if isinstance(notes, list) else [])
        if isinstance(n, Mapping) and isinstance(n.get("ref"), str)
    }


def face_line_items(
    ocr: Mapping[str, object],
    statement_index: Mapping[str, object],
    note_index: Mapping[str, object],
    *,
    basis: str,
) -> tuple[list[LineItem], list[Edge]]:
    """Every valued face row of every statement in `basis`, as LineItems, plus
    CONTAINS and REFERENCES edges.

    This is the deterministic bootstrap: values, concepts and note pointers in
    ONE parse, with no model call. On TCS P&L, 22 of 31 facts came from here.

    A REFERENCES edge is emitted ONLY when the printed ref resolves to a note
    this basis actually has. Total: never raises."""
    tables = _tables_of(ocr)
    resolved = _note_refs(note_index, basis)
    raw_statements = (
        statement_index.get("statements") if isinstance(statement_index, Mapping) else None
    )

    items: list[LineItem] = []
    edges: list[Edge] = []
    for stmt in raw_statements if isinstance(raw_statements, list) else []:
        if not isinstance(stmt, Mapping) or stmt.get("basis") != basis:
            continue
        stmt_id = stmt.get("id")
        table_index = stmt.get("table_index")
        page = stmt.get("page")
        if not isinstance(stmt_id, str) or not isinstance(table_index, int):
            continue
        if not (0 <= table_index < len(tables)):
            continue
        table = tables[table_index]
        if not isinstance(table, Mapping):
            continue

        for row_index, row in enumerate(parse_face_table(table)):
            item = LineItem(
                id=line_item_id(stmt_id, row_index),
                parent=stmt_id,
                origin="face",
                label=row.label,
                caption=row.caption,
                ref=row.ref,
                current=row.current,
                prior=row.prior,
                page=page if isinstance(page, int) else 0,
                table_index=table_index,
                row_index=row_index,
            )
            items.append(item)
            edges.append(Edge("CONTAINS", stmt_id, item.id))
            if row.ref and row.ref in resolved:
                edges.append(Edge("REFERENCES", item.id, f"nt:{basis}:{row.ref}"))

    return items, edges
```

> **Note on `row_index`:** `parse_face_table` returns only *valued* rows, so this
> index is the position within that filtered list, not the raw table row. That is
> intentional and stable — the same table always yields the same sequence — and
> it is what `line_item_id` hashes on.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_face.py -q`
Expected: PASS, 11 passed

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_build.py backend/tests/unit/test_fog_face.py
git commit -m "feat(fog): face rows become LineItems; the printed note column becomes REFERENCES edges"
```

---

## Task 10: Note rows → LineItems

**Files:**
- Modify: `backend/src/fdi/core/fog_build.py` (append)
- Test: `backend/tests/unit/test_fog_notes.py`

**Interfaces:**
- Consumes: `fdi.core.fog.{LineItem, Edge, line_item_id}`, `fdi.core.numbers.{parse_amount, looks_numeric}`, `build_note_index` output shape
- Produces: `fdi.core.fog_build.note_line_items(ocr, note_index, *, basis) -> tuple[list[LineItem], list[Edge]]`

**Why this is the accuracy lever:** today "Facility expenses 2,966" vanishes silently. As a `LineItem` it becomes a visible node — later marked `unmapped` by Phase 3 rather than lost — and the note's own total becomes available as the arbiter for its components.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_notes.py`:

```python
"""Note rows become objects too. Today they vanish; as LineItems they are
visible, and the note's own total becomes the arbiter for its components."""

from fdi.core.fog_build import build_note_index, note_line_items


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_NOTE_TABLE = {
    "bounding_regions": [{"page_number": 301}],
    "spans": [{"offset": 2150, "length": 200}],
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"), _cell(0, 2, "2024-25"),
        _cell(1, 0, "Facility expenses"), _cell(1, 1, "2,966"), _cell(1, 2, "2,700"),
        _cell(2, 0, "Travel expenses"), _cell(2, 1, "(468)"), _cell(2, 2, "500"),
        _cell(3, 0, "Communication"), _cell(3, 1, "-"), _cell(3, 2, "-"),
        _cell(4, 0, "Total"), _cell(4, 1, "2,498"), _cell(4, 2, "3,200"),
    ],
}

_HEAD = {
    "content": "13. Other expenses",
    "role": "sectionHeading",
    "bounding_regions": [{"page_number": 301}],
    "spans": [{"offset": 2000, "length": 18}],
}

_BASIS = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 300, "end_page": 400}],
}
_OCR = {"paragraphs": [_HEAD], "tables": [_NOTE_TABLE]}


def _build():
    ni = build_note_index(_OCR, _BASIS)
    return note_line_items(_OCR, ni, basis="consolidated")


def test_every_valued_note_row_becomes_a_line_item():
    items, _ = _build()
    labels = [i.label for i in items]
    assert "Facility expenses" in labels
    assert "Travel expenses" in labels


def test_an_unmapped_disclosure_survives_instead_of_vanishing():
    """'Facility expenses' has no taxonomy home. It must still be a node."""
    items, _ = _build()
    facility = next(i for i in items if i.label == "Facility expenses")
    assert facility.current == 2966.0


def test_brackets_are_negative():
    items, _ = _build()
    travel = next(i for i in items if i.label == "Travel expenses")
    assert travel.current == -468.0


def test_a_dash_row_is_skipped_it_carries_no_figure():
    items, _ = _build()
    assert all(i.label != "Communication" for i in items)


def test_the_note_total_is_kept_it_is_the_arbiter():
    items, _ = _build()
    total = next(i for i in items if i.label == "Total")
    assert total.current == 2498.0


def test_note_items_are_contained_by_their_note():
    items, edges = _build()
    contains = [e for e in edges if e.type == "CONTAINS"]
    assert all(e.source == "nt:consolidated:13" for e in contains)
    assert {e.target for e in contains} == {i.id for i in items}


def test_the_origin_marks_them_as_note_rows_not_face_rows():
    """Face rows match PARENT concepts, note rows match DESCENDANTS. Conflating
    the two left 15 of TCS's 28 face rows unmapped."""
    items, _ = _build()
    assert all(i.origin == "note" for i in items)


def test_provenance_is_carried():
    items, _ = _build()
    facility = next(i for i in items if i.label == "Facility expenses")
    assert facility.page == 301
    assert facility.table_index == 0


def test_ids_are_unique_across_notes_sharing_a_page():
    """TCS p301 holds two notes; a page-derived id would collide."""
    items, _ = _build()
    assert len({i.id for i in items}) == len(items)


def test_a_note_with_no_tables_yields_nothing():
    ocr = {"paragraphs": [_HEAD], "tables": []}
    ni = build_note_index(ocr, _BASIS)
    assert note_line_items(ocr, ni, basis="consolidated") == ([], [])


def test_malformed_input_never_raises():
    assert note_line_items({}, {}, basis="consolidated") == ([], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_notes.py -q`
Expected: FAIL — `ImportError: cannot import name 'note_line_items'`

- [ ] **Step 3: Append the implementation to `core/fog_build.py`**

Add to the imports at the top of `backend/src/fdi/core/fog_build.py`:

```python
from fdi.core.statements import table_rows
```

Append:

```python
def _note_rows(table: Mapping[str, object]) -> list[tuple[str, float | None, float | None]]:
    """A note table's `(label, current, prior)` rows, header skipped.

    A note table has no note column to dodge, so every non-label cell is a
    candidate amount -- but a row with NO amount anywhere is a caption or a
    section break and is dropped, matching `parse_face_table`'s rule."""
    rows = table_rows(table)
    out: list[tuple[str, float | None, float | None]] = []
    for r in sorted(rows):
        cols = rows[r]
        label = cols.get(0, "").strip()
        if not label:
            continue
        amounts = [
            parsed
            for c in sorted(cols)
            if c != 0 and (parsed := parse_amount(cols.get(c))) is not None
        ]
        if not amounts:
            continue
        out.append((label, amounts[0], amounts[1] if len(amounts) > 1 else None))
    return out


def note_line_items(
    ocr: Mapping[str, object],
    note_index: Mapping[str, object],
    *,
    basis: str,
) -> tuple[list[LineItem], list[Edge]]:
    """Every valued row of every note in `basis`, as LineItems + CONTAINS edges.

    THE ACCURACY LEVER. Today a disclosed line with no taxonomy home -- TCS's
    "Facility expenses 2,966" -- vanishes silently. As a LineItem it is a
    visible node that Phase 3 can mark `unmapped` rather than lose, and the
    note's own total becomes available as the arbiter for its components.

    `origin` is "note", which is not cosmetic: face rows are summaries and match
    PARENT concepts, note rows are detail and match DESCENDANTS. Conflating the
    two candidate sets left 15 of TCS's 28 face rows unmapped. Total: never
    raises."""
    tables = _tables_of(ocr)
    notes = _region_of(note_index, basis).get("notes")

    items: list[LineItem] = []
    edges: list[Edge] = []
    for note in notes if isinstance(notes, list) else []:
        if not isinstance(note, Mapping):
            continue
        note_id = note.get("id")
        indices = note.get("table_indices")
        if not isinstance(note_id, str) or not isinstance(indices, list):
            continue

        row_index = 0
        for table_index in indices:
            if not isinstance(table_index, int) or not (0 <= table_index < len(tables)):
                continue
            table = tables[table_index]
            if not isinstance(table, Mapping):
                continue
            page = _page_of_table(table)
            for label, current, prior in _note_rows(table):
                item = LineItem(
                    id=line_item_id(note_id, row_index),
                    parent=note_id,
                    origin="note",
                    label=label,
                    caption="",
                    ref="",
                    current=current,
                    prior=prior,
                    page=page,
                    table_index=table_index,
                    row_index=row_index,
                )
                items.append(item)
                edges.append(Edge("CONTAINS", note_id, item.id))
                # Counter runs across ALL of a note's tables, deliberately: a
                # note split over two tables must not restart at r0 and collide.
                row_index += 1

    return items, edges


def _page_of_table(table: Mapping[str, object]) -> int:
    """A table's first page, or 0. Total."""
    regions = table.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return 0
    first = regions[0]
    if not isinstance(first, Mapping):
        return 0
    page = first.get("page_number")
    return page if isinstance(page, int) and not isinstance(page, bool) else 0
```

Also add to the top-of-file imports:

```python
from fdi.core.numbers import parse_amount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_notes.py -q`
Expected: PASS, 11 passed

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_build.py backend/tests/unit/test_fog_notes.py
git commit -m "feat(fog): note rows become LineItems — an unmapped disclosure is visible, not lost"
```

---

## Task 11: `fog_objects` stage

**Files:**
- Modify: `backend/src/fdi/core/fog_build.py` (append)
- Create: `backend/src/fdi/pipeline/stages/fog_objects.py`
- Modify: `backend/src/fdi/pipeline/stages/__init__.py`
- Test: `backend/tests/unit/test_fog_objects_stage.py`
- Test: `backend/tests/integration/test_fog_pipeline.py`

**Interfaces:**
- Consumes: everything from Tasks 6–10
- Produces: `fdi.core.fog_build.build_fog(ocr, statement_index, note_index, *, doc_id) -> dict[str, object]` — the complete FOG artifact body; `FogObjectsStage` with `deps = ("statement_index", "note_index", "structure")`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_objects_stage.py`:

```python
"""The whole deterministic FOG, assembled per basis, with the diagnostics that
make `absent` distinguishable from `missed`."""

from fdi.core.fog_build import build_fog, build_note_index, build_statement_index


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_FACE = {
    "bounding_regions": [{"page_number": 150}],
    "spans": [{"offset": 500, "length": 100}],
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2025-26"),
        _cell(1, 0, "Revenue from operations"), _cell(1, 1, "10"), _cell(1, 2, "90"),
        _cell(2, 0, "Total income"), _cell(2, 2, "100"),
        _cell(3, 0, "Total expenses"), _cell(3, 2, "70"),
        _cell(4, 0, "Profit before tax"), _cell(4, 2, "30"),
    ],
}
_NOTE_TABLE = {
    "bounding_regions": [{"page_number": 170}],
    "spans": [{"offset": 9100, "length": 60}],
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
        _cell(1, 0, "Sale of services"), _cell(1, 1, "90"),
    ],
}
_HEAD = {
    "content": "10. Revenue from operations",
    "role": "sectionHeading",
    "bounding_regions": [{"page_number": 170}],
    "spans": [{"offset": 9000, "length": 27}],
}

_BASIS = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 100, "end_page": 200}],
}
_OCR = {"paragraphs": [_HEAD], "tables": [_FACE, _NOTE_TABLE]}


def _fog():
    si = build_statement_index(_OCR, _BASIS)
    ni = build_note_index(_OCR, _BASIS)
    return build_fog(_OCR, si, ni, doc_id="doc-1")


def test_face_and_note_items_both_land_in_one_basis_region():
    region = _fog()["bases"]["consolidated"]
    origins = {i["origin"] for i in region["line_items"]}
    assert origins == {"face", "note"}


def test_the_references_edge_connects_the_face_to_its_note():
    region = _fog()["bases"]["consolidated"]
    refs = [e for e in region["edges"] if e["type"] == "REFERENCES"]
    assert len(refs) == 1
    assert refs[0]["target"] == "nt:consolidated:10"


def test_diagnostics_report_note_resolution_per_basis():
    """A ref-grammar failure like RITES's 0/30 must be visible in one file."""
    diag = _fog()["diagnostics"]["consolidated"]
    assert diag["face_refs"] == 1
    assert diag["refs_resolved"] == 1
    assert diag["notes_indexed"] == 1
    assert diag["line_items"] == 5


def test_an_issuer_with_no_note_column_records_absent_not_zero_items():
    """RIL prints no note column on its P&L face. That is ABSENT, and the FOG
    must say so rather than look like a failed extraction."""
    face = {
        "bounding_regions": [{"page_number": 150}],
        "spans": [{"offset": 500, "length": 100}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
            _cell(1, 0, "Total income"), _cell(1, 1, "100"),
            _cell(2, 0, "Total expenses"), _cell(2, 1, "70"),
            _cell(3, 0, "Profit before tax"), _cell(3, 1, "30"),
        ],
    }
    ocr = {"paragraphs": [], "tables": [face]}
    si = build_statement_index(ocr, _BASIS)
    ni = build_note_index(ocr, _BASIS)
    fog = build_fog(ocr, si, ni, doc_id="ril")
    assert fog["diagnostics"]["consolidated"]["face_refs"] == 0
    assert len(fog["bases"]["consolidated"]["line_items"]) == 3


def test_the_artifact_is_deterministic_for_content_addressing():
    assert _fog() == _fog()


def test_no_maps_to_edges_are_emitted_mapping_is_phase_3():
    region = _fog()["bases"]["consolidated"]
    assert not [e for e in region["edges"] if e["type"] == "MAPS_TO"]


def test_no_governed_by_edges_are_emitted_policy_is_domain_2():
    region = _fog()["bases"]["consolidated"]
    assert not [e for e in region["edges"] if e["type"] == "GOVERNED_BY"]


def test_malformed_input_yields_a_well_formed_empty_fog():
    fog = build_fog({}, {}, {}, doc_id="d")
    assert fog["doc_id"] == "d"
    assert fog["schema_version"] == 1


def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401 - triggers registration
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("fog_objects")
    assert stage.deps == ("statement_index", "note_index", "structure")
    assert stage.version == 1


def test_the_dag_resolves_and_orders_fog_objects_after_its_indices():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver

    order = DagResolver(REGISTRY.all()).topological_order()
    assert order.index("statement_index") < order.index("fog_objects")
    assert order.index("note_index") < order.index("fog_objects")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_objects_stage.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_fog'`

- [ ] **Step 3: Append `build_fog` to `core/fog_build.py`**

Add to the imports:

```python
from fdi.core.fog import build_fog_artifact
```

Append:

```python
def build_fog(
    ocr: Mapping[str, object],
    statement_index: Mapping[str, object],
    note_index: Mapping[str, object],
    *,
    doc_id: str,
) -> dict[str, object]:
    """The complete deterministic FOG for one document -- every basis.

    Zero LLM calls. Diagnostics are per basis and deliberately name the
    quantities that distinguish ABSENT from MISSED: an issuer that prints no
    note column (`face_refs == 0`, RIL) looks nothing like one whose reference
    grammar failed to parse (`face_refs > 0, refs_resolved == 0`, the RITES
    bug). Both were previously invisible. Total: never raises."""
    raw_bases = note_index.get("bases") if isinstance(note_index, Mapping) else None
    bases = sorted(raw_bases) if isinstance(raw_bases, Mapping) else []

    regions: dict[
        str,
        tuple[
            list[Mapping[str, object]], list[Mapping[str, object]],
            list[LineItem], list[Edge],
        ],
    ] = {}
    diagnostics: dict[str, object] = {}

    raw_statements = (
        statement_index.get("statements") if isinstance(statement_index, Mapping) else None
    )
    all_statements = raw_statements if isinstance(raw_statements, list) else []

    for basis in bases:
        face_items, face_edges = face_line_items(
            ocr, statement_index, note_index, basis=basis
        )
        note_items, note_edges = note_line_items(ocr, note_index, basis=basis)

        statements = [
            s for s in all_statements
            if isinstance(s, Mapping) and s.get("basis") == basis
        ]
        raw_notes = _region_of(note_index, basis).get("notes")
        notes = [n for n in (raw_notes if isinstance(raw_notes, list) else [])
                 if isinstance(n, Mapping)]

        regions[basis] = (
            statements, notes, [*face_items, *note_items], [*face_edges, *note_edges],
        )
        face_refs = sum(1 for i in face_items if i.ref)
        diagnostics[basis] = {
            "statements": len(statements),
            "notes_indexed": len(notes),
            "face_refs": face_refs,
            "refs_resolved": sum(1 for e in face_edges if e.type == "REFERENCES"),
            "line_items": len(face_items) + len(note_items),
            "face_items": len(face_items),
            "note_items": len(note_items),
        }

    return build_fog_artifact(regions, doc_id=doc_id, diagnostics=diagnostics)
```

- [ ] **Step 4: Create the stage**

Create `backend/src/fdi/pipeline/stages/fog_objects.py`:

```python
"""FogObjectsStage: the deterministic Financial Object Graph.

Zero LLM calls -- every value here is parsed, not read by a model. Deps
`(statement_index, note_index, structure)`; `ocr` is reached through the
indices' own dependency chain, and `structure` is depended on so a re-stitch
invalidates the graph."""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_build import build_fog
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class FogObjectsStage:
    name = "fog_objects"
    version = 1
    deps: tuple[str, ...] = ("statement_index", "note_index", "structure")

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        statement_index = cast(Mapping[str, object], await ctx.read("statement_index"))
        note_index = cast(Mapping[str, object], await ctx.read("note_index"))
        return build_fog(
            ocr, statement_index, note_index, doc_id=ctx.doc_id
        )


REGISTRY.register(FogObjectsStage())
```

Add to `backend/src/fdi/pipeline/stages/__init__.py`:

```python
from fdi.pipeline.stages import (
    fog_objects as fog_objects,  # noqa: F401 - import for its registration side effect
)
```

- [ ] **Step 5: Run the unit tests**

Run: `uv run --directory backend pytest tests/unit/test_fog_objects_stage.py -q`
Expected: PASS, 10 passed

- [ ] **Step 6: Write the integration test**

Create `backend/tests/integration/test_fog_pipeline.py`:

```python
"""The three FOG stages run in order against fixture artifacts, through the
real StageContext -- proving the deps chain and the artifact shapes line up."""

from collections.abc import Mapping

import pytest

from fdi.pipeline.stage import StageContext
from fdi.pipeline.stages.fog_objects import FogObjectsStage
from fdi.pipeline.stages.note_index import NoteIndexStage
from fdi.pipeline.stages.statement_index import StatementIndexStage

pytestmark = pytest.mark.integration


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_OCR: dict[str, object] = {
    "paragraphs": [{
        "content": "10. Revenue from operations",
        "role": "sectionHeading",
        "bounding_regions": [{"page_number": 170}],
        "spans": [{"offset": 9000, "length": 27}],
    }],
    "tables": [
        {
            "bounding_regions": [{"page_number": 150}],
            "spans": [{"offset": 500, "length": 100}],
            "cells": [
                _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2025-26"),
                _cell(1, 0, "Revenue from operations"), _cell(1, 1, "10"),
                _cell(1, 2, "2,55,324"),
                _cell(2, 0, "Total income"), _cell(2, 2, "100"),
                _cell(3, 0, "Total expenses"), _cell(3, 2, "70"),
                _cell(4, 0, "Profit before tax"), _cell(4, 2, "30"),
            ],
        },
        {
            "bounding_regions": [{"page_number": 170}],
            "spans": [{"offset": 9100, "length": 60}],
            "cells": [
                _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
                _cell(1, 0, "Sale of services"), _cell(1, 1, "2,55,324"),
            ],
        },
    ],
}
_BASIS: dict[str, object] = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 100, "end_page": 200}],
}


async def test_the_three_stages_chain_into_a_populated_fog():
    artifacts: dict[str, object] = {"ocr": _OCR, "structure": {}, "basis": _BASIS}

    async def read(_doc_id: str, stage: str) -> object:
        return artifacts[stage]

    ctx = StageContext(doc_id="doc-1", read_artifact=read)

    artifacts["statement_index"] = await StatementIndexStage().run(ctx)
    artifacts["note_index"] = await NoteIndexStage().run(ctx)
    fog = await FogObjectsStage().run(ctx)

    assert isinstance(fog, Mapping)
    region = fog["bases"]["consolidated"]
    labels = {i["label"] for i in region["line_items"]}
    assert "Revenue from operations" in labels
    assert "Sale of services" in labels
    assert [e for e in region["edges"] if e["type"] == "REFERENCES"]
    assert fog["diagnostics"]["consolidated"]["refs_resolved"] == 1


async def test_a_second_run_produces_a_byte_identical_body():
    """Content-addressed caching depends on this. Any set iteration or dict
    ordering leaking into the body breaks it silently."""
    artifacts: dict[str, object] = {"ocr": _OCR, "structure": {}, "basis": _BASIS}

    async def read(_doc_id: str, stage: str) -> object:
        return artifacts[stage]

    ctx = StageContext(doc_id="doc-1", read_artifact=read)
    artifacts["statement_index"] = await StatementIndexStage().run(ctx)
    artifacts["note_index"] = await NoteIndexStage().run(ctx)

    assert await FogObjectsStage().run(ctx) == await FogObjectsStage().run(ctx)
```

- [ ] **Step 7: Run the integration test**

Run: `uv run --directory backend pytest tests/integration/test_fog_pipeline.py -q`
Expected: PASS, 2 passed

- [ ] **Step 8: Run the whole fast suite for regressions**

Run: `uv run --directory backend pytest -m "not integration and not e2e" -q`
Expected: PASS, no failures. The stage count in any test asserting on `REGISTRY.all()` size will need updating by +3 — fix those assertions, do not weaken them.

- [ ] **Step 9: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_build.py backend/src/fdi/pipeline/stages/fog_objects.py \
        backend/src/fdi/pipeline/stages/__init__.py \
        backend/tests/unit/test_fog_objects_stage.py backend/tests/integration/test_fog_pipeline.py
git commit -m "feat(fog): fog_objects stage — the complete deterministic FOG, zero LLM calls"
```

---

## Task 12: Phase 2 gate — measure the deterministic FOG on real reports

**Files:**
- Modify: `.superpowers/sdd/progress.md`
- Modify: `docs/architecture/pipeline-end-to-end.md` (§12 live-vs-designed table)

**Interfaces:**
- Consumes: `scripts/score_pipeline.py` (Task 4), the FOG artifact (Task 11)
- Produces: the recorded Phase 2 number that Phase 3's plan is written against.

**No new code.** This task runs the real pipeline and writes down what happened. It is a separate task because a measurement nobody records is a measurement nobody can be held to.

- [ ] **Step 1: Reprocess each issuer through the new stages**

With the compose stack up and each document already at `index: done`:

```bash
for DOC in "$TCS_DOC_ID" "$RITES_DOC_ID" "$RIL_DOC_ID"; do
  curl -s -X POST "localhost:8000/documents/$DOC/reprocess?from=statement_index"
done
```

Poll `GET /documents/{doc_id}/stages` until `fog_objects: done`.

- [ ] **Step 2: Pull each FOG and record its diagnostics**

```bash
curl -s "localhost:8000/documents/$TCS_DOC_ID/artifacts/fog_objects" > /tmp/tcs-fog.json
python -c "import json,sys; print(json.load(open('/tmp/tcs-fog.json'))['diagnostics'])"
```

Record per issuer per basis: `statements`, `notes_indexed`, `face_refs`, `refs_resolved`, `face_items`, `note_items`.

- [ ] **Step 3: Check the gate**

The Phase 2 gate from the spec is **deterministic-only recall measured, 0 LLM calls**. Concretely:

| Check | Expectation |
|---|---|
| Statements found and typed | balance sheet + P&L on all 3 issuers, both bases where present |
| Footing | `passed: true` on P&L for TCS, RITES and RIL |
| Notes resolved | TCS ≥ 9, RITES ≥ 8 (matching the ported-core measurement) |
| RIL face refs | `0` — recorded as absent, *not* as a failure |
| LLM calls | **0** — confirm via `GET /documents/{doc_id}/cost` showing no new `llm_usage_event` rows for these stages |

If notes resolved falls below the recorded 9/8, that is a regression against `core/statements.py`'s existing measurement and must be diagnosed before Phase 3 — the note-grammar bug is exactly this shape.

- [ ] **Step 4: Score the FOG (expect near-zero, and understand why)**

```bash
uv run --directory backend python ../scripts/score_pipeline.py \
  --gt-dir docs/groundtruth --fog /tmp/tcs-fog.json --issuer TCS
```

**This will report near-zero recall, and that is correct.** The FOG has values but no `MAPS_TO` edges — it holds no opinion yet about which taxonomy concept each row is. Phase 3 supplies that. Record the number anyway: it is the floor that the mapping stage must lift, and a *non*-zero result here would mean `facts_from_fog` is inventing mappings.

- [ ] **Step 5: Record everything**

Append to `.superpowers/sdd/progress.md` under `PHASE 2 — DETERMINISTIC FOG`: the diagnostics table per issuer/basis, the gate checks with pass/fail, the LLM call count, and the near-zero FOG score with the one-line reason.

- [ ] **Step 6: Update the live-vs-designed table**

In `docs/architecture/pipeline-end-to-end.md` §12, move `NoteIndexStage`/`FaceIndexStage` out of the "designed but not wired" table and into the live list as `statement_index`, `note_index`, `fog_objects`. Leave `note_plan`, `mapping`, `memory`, `compose` where they are — they are Phase 3 and 4, still unwired.

- [ ] **Step 7: Commit**

```bash
git add .superpowers/sdd/progress.md docs/architecture/pipeline-end-to-end.md
git commit -m "docs(fog): Phase 2 gate — deterministic FOG measured on TCS/RITES/RIL"
```

---

## What this plan deliberately does NOT do

- **No LLM call is added.** `fog_mapping` is Phase 3.
- **No route changes.** `/extract/document` and `/factsheet/document` keep working exactly as they do today, on the old path.
- **Nothing is deleted.** `pipeline/extractor.py`, `linking`, `graph_load`, `ontology → quality → knowledge` all keep running. Removal is Phase 6, gated on beating the Task 4 baseline.
- **No `MAPS_TO`, `COMPOSES`, `RECONCILES_WITH`, `SAME_AS` or `GOVERNED_BY` edges** are emitted. The vocabulary is declared in Task 8 so later phases are additive.
- **No `bounding_regions` are copied onto FOG nodes.** A `LineItem` carries its *address* (`page`, `table_index`, `row_index`), which resolves to exactly one cell. Geometry stays read-side through the existing `core/provenance.py` helpers — the same pattern `resolve_field_regions` already uses — so the artifact never holds a second copy of polygon data that would then need keeping in sync with `structure`. The audit trail is complete and cell-precise either way; see spec §3.2.

## Follow-on plans

| Plan | Phases | Content |
|---|---|---|
| 2 | 3–4 | `fog_mapping` (push model, the only billed stage) · `fog_reason` (compose · validate · confidence · coverage) |
| 3 | 5–6 | v4.3 assets/liabilities/equity + **the cash-flow module the taxonomy does not have** · `applicability` metadata · routes read the FOG · stage removals · `FDI_MILVUS_URI` · delete the old path |
