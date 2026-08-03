"""Score a produced fact map against hand-keyed ground truth.

WHY `not_reported` IS HALF THE RECORD


---

# Plan header (global constraints)

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
"""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class GroundTruth:
    """Hand-keyed ground truth for a financial statement."""

    issuer: str
    fiscal_year: str
    basis: str
    statement: str
    values: dict[str, float]
    not_reported: tuple[str, ...]


def parse_groundtruth(body: Mapping[str, object]) -> GroundTruth | None:
    """Parse and validate a ground truth record.

    Returns None if the record is malformed or if values and not_reported
    have overlapping tags.
    """
    try:
        issuer = body.get("issuer")
        fiscal_year = body.get("fiscal_year")
        basis = body.get("basis")
        statement = body.get("statement")
        values_raw = body.get("values")
        not_reported_raw = body.get("not_reported")

        if not isinstance(issuer, str):
            return None
        if not isinstance(fiscal_year, str):
            return None
        if not isinstance(basis, str):
            return None
        if not isinstance(statement, str):
            return None
        if not isinstance(values_raw, Mapping):
            return None
        if not isinstance(not_reported_raw, (list, tuple)):
            return None

        values: dict[str, float] = {}
        for k, v in values_raw.items():
            if not isinstance(k, str) or not isinstance(v, (int, float)):
                return None
            values[k] = float(v)

        not_reported = tuple(not_reported_raw)

        # Validate disjointness
        values_set = set(values.keys())
        not_reported_set = set(not_reported)
        if values_set & not_reported_set:
            return None

        return GroundTruth(
            issuer=issuer,
            fiscal_year=fiscal_year,
            basis=basis,
            statement=statement,
            values=values,
            not_reported=not_reported,
        )
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


@dataclass(frozen=True)
class Score:
    """Scoring result for a produced fact map."""

    reported: int
    correct: int
    wrong: int
    missed: int
    false_positive: int
    absent_correct: int
    committed: int

    @property
    def recall(self) -> float:
        """Recall: correct / reported (0.0 if no tags reported)."""
        if self.reported == 0:
            return 0.0
        return self.correct / self.reported

    @property
    def precision(self) -> float:
        """Precision: correct / committed (0.0 if no tags committed)."""
        if self.committed == 0:
            return 0.0
        return self.correct / self.committed


def score_document(
    gt: GroundTruth,
    produced: Mapping[str, object],
    *,
    tolerance: float = 0.01,
) -> Score:
    """Score a produced fact map against ground truth.

    Args:
        gt: The ground truth record.
        produced: The extracted values (tag -> value mapping).
        tolerance: Float comparison tolerance (default 0.01).

    Returns:
        A Score object with detailed breakdown.

    Scoring rules:
    - Values in gt.values that match (within tolerance) are correct.
    - Values in gt.values that don't match are wrong.
    - Tags in gt.values not produced are missed.
    - Tags in gt.not_reported that are produced are false positives.
    - Tags in gt.not_reported correctly not produced are absent_correct.
    - None values and missing dict["current"] keys count as not produced.
    - Tags outside GT are ignored (not counted as false positives).
    """
    reported = len(gt.values)
    correct = 0
    wrong = 0
    missed = 0
    false_positive = 0
    absent_correct = 0
    committed = 0

    # Score tags in gt.values
    for tag, expected_value in gt.values.items():
        if tag in produced:
            produced_value = produced[tag]

            # Handle dual-period values (dict with current/prior)
            if isinstance(produced_value, Mapping):
                if "current" in produced_value:
                    produced_value = produced_value["current"]
                else:
                    # Dict doesn't have "current" key, treat as missed
                    missed += 1
                    continue

            # At this point, produced_value is either a scalar or None
            if produced_value is None:
                missed += 1
            elif isinstance(produced_value, (int, float)):
                if abs(produced_value - expected_value) <= tolerance:
                    correct += 1
                else:
                    wrong += 1
                committed += 1
            else:
                # Non-numeric value, count as wrong
                wrong += 1
                committed += 1
        else:
            # Tag not produced
            missed += 1

    # Score tags in not_reported
    for tag in gt.not_reported:
        if tag in produced:
            produced_value = produced[tag]

            # Handle dual-period values
            if isinstance(produced_value, Mapping):
                if "current" in produced_value:
                    produced_value = produced_value["current"]
                else:
                    # Dict doesn't have "current" key, treat as correctly absent
                    absent_correct += 1
                    continue

            # At this point, produced_value is either a scalar or None
            if produced_value is None:
                absent_correct += 1
            else:
                # Tag is in not_reported but was produced → false positive
                false_positive += 1
                committed += 1
        else:
            # Tag correctly not produced
            absent_correct += 1

    return Score(
        reported=reported,
        correct=correct,
        wrong=wrong,
        missed=missed,
        false_positive=false_positive,
        absent_correct=absent_correct,
        committed=committed,
    )
