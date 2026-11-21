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
    make the resulting number a lie.

    A single unparseable VALUE is skipped rather than rejecting the file: one
    stray entry must not silently shrink the denominator to zero and report
    perfect recall."""
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
        # `bool` is an `int` subclass; True would otherwise score as 1.0.
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        values[tag] = float(value)

    # Sorted + de-duplicated: this tuple reaches artifact bodies, and any
    # ordering wobble there breaks content-addressed caching.
    absent = tuple(sorted({t for t in raw_absent if isinstance(t, str) and t}))
    if set(absent) & set(values):
        return None

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
