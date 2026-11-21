"""Score what an answering endpoint said against the benchmark's gold.

One entry point, `score_run(benchmark, answers) -> EvalReport`. It hides
number recognition in free text, unit normalisation, tolerance and
aggregation; callers never see any of it.

WHICH FIGURES ARE THE ANSWER
----------------------------
Only the answer's HEADLINE figures count: the first N amounts it states,
for an item with N gold values. An answer usually also states a comparison
("up from ₹45,000 crore in FY2024-25"), and reading the wrong column or row
is exactly the failure this harness exists to catch -- crediting any figure
anywhere in the text would score that failure as correct.

An amount written with a currency marker or a scale word (₹, INR, crore,
lakh...) is preferred: when the answer has any, only those are headline
candidates, so "for the year ended 31 March 2026" never becomes the answer.
Not an amount at all: a percentage, a page / Note / schedule reference, a
fiscal-year label, and either half of a year range (2025-26, 2025–26).

HOW A FIGURE MATCHES A GOLD VALUE
---------------------------------
Gold is keyed AS PRINTED, in the issuer's unit ("INR crore", "INR lakh", or
"INR" for per-share figures). An answer may restate it in any scale an Indian
reader would: `₹2,71,423 crore`, `INR 2,71,423 Cr`, `₹2.71 lakh crore`. Each
figure is rescaled into the gold unit -- a figure with no scale word is read
in the gold unit -- and matches when it is within the item's tolerance.

An answer that rounds is allowed its own rounding (2.71 lakh crore covers
271,423 crore to within half of 0.01 lakh crore), but only when it keeps at
least three significant figures. `2.7 lakh crore` is also 271,423 rounded,
to a precision that states almost nothing; it passes only if the item's own
tolerance allows it.

A figure written with a sign -- a minus or accounting brackets -- must match
the gold sign. An unsigned figure matches on magnitude: "current tax was
₹2,681.75 lakh" correctly answers a value REMC prints as (2,681.75), because
the sign there is presentation, and the words carry the direction.

CORRECT IS NOT COMPLETED
------------------------
A citation is valid only when its Report `doc_key` and page are one of the
item's gold evidence locations. An item is completed only when it is
correct AND at least one citation is valid (§16.2): a fluent, correct
number that cites the wrong page, or nothing, is a failed task. Citation
validity, evidence support and completion are reported beside accuracy,
never blended into it.

Pure (`fdi.core` law): no I/O, no LLM, deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from fdi.core.benchmark import UNIT_SCALES, Benchmark, BenchmarkItem, GoldValue
from fdi.core.numbers import parse_amount

ItemStatus = Literal["correct", "wrong", "failed"]


@dataclass(frozen=True)
class Citation:
    """One source an answer cites, in endpoint-neutral form. Only `doc_key`
    and `page` decide validity; `doc_id`, `table_index` and `row_label` are
    carried for the report reader when the endpoint supplies them."""

    doc_key: str | None = None
    page: int | None = None
    doc_id: str | None = None
    table_index: int | None = None
    row_label: str | None = None


@dataclass(frozen=True)
class EndpointAnswer:
    """What the answering endpoint returned for one item, already normalised
    by its adapter so the scorer never sees a wire format. `error` is set
    when the endpoint failed or timed out; `answer` is then usually None."""

    item_id: str
    answer: str | None
    error: str | None = None
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True)
class ItemScore:
    """`status` is "failed" when the endpoint never gave an answer. `reason`
    says why an item is not correct; empty when it is.

    A citation is VALID when its (`doc_key`, page) is one of the item's gold
    evidence locations; "cited something" is not "cited the source". The
    item is `supported` when at least one citation is valid, and `completed`
    -- the task outcome (§16.2) -- when it is correct AND supported. A
    correct number nobody can trace to its source is a failed task."""

    item_id: str
    kind: str
    status: ItemStatus
    reason: str
    citations: int
    valid_citations: int
    supported: bool
    completed: bool

    @property
    def citation_validity(self) -> float:
        """Valid citations over citations given; 0.0 when none were given."""
        return self.valid_citations / self.citations if self.citations else 0.0


@dataclass(frozen=True)
class KindSummary:
    """Counts over a set of scored items: one kind's, or the whole run's.
    Every rate is over items RUN, so a failed item counts against it."""

    run: int
    correct: int
    wrong: int
    failed: int
    citations: int
    valid_citations: int
    supported: int
    completed: int

    @property
    def accuracy(self) -> float:
        """Correct over items RUN: a failed item is a miss, never excused."""
        return self.correct / self.run if self.run else 0.0

    @property
    def citation_validity(self) -> float:
        """Valid citations over ALL citations given across the items."""
        return self.valid_citations / self.citations if self.citations else 0.0

    @property
    def evidence_support_rate(self) -> float:
        """Items with at least one valid citation, over items run."""
        return self.supported / self.run if self.run else 0.0

    @property
    def completion_rate(self) -> float:
        """Completed tasks (correct AND supported) over items run."""
        return self.completed / self.run if self.run else 0.0


@dataclass(frozen=True)
class EvalReport:
    """Only items that were run are scored; `benchmark_size` keeps a limited
    run from being read as the whole benchmark."""

    benchmark_version: str
    benchmark_size: int
    items: tuple[ItemScore, ...]
    by_kind: dict[str, KindSummary]
    overall: KindSummary


# Scale words, in rupees per unit. Longest first so "lakh crore" is never
# read as "lakh" followed by a stray "crore".
_SCALES: tuple[tuple[str, float], ...] = (
    (r"lakhs?\s+crores?", 1e12),
    (r"thousand\s+crores?", 1e10),
    (r"crores?", 1e7),
    (r"cr\b\.?", 1e7),
    (r"lakhs?", 1e5),
    (r"lacs?", 1e5),
    (r"billion", 1e9),
    (r"bn\b", 1e9),
    (r"million", 1e6),
    (r"mn\b", 1e6),
    (r"thousand", 1e3),
)

# Escaped: a bare "-" inside a [class] would form a range, not a hyphen.
_DASHES = r"\-−–—"

# The lookbehind keeps "2025-26" from yielding -26 and "v4.3" from yielding .3.
_AMOUNT = re.compile(
    rf"(?<![\w.{_DASHES}])"
    r"(?P<open>\()?\s*"
    rf"(?P<minus>[{_DASHES}])?\s*"
    r"(?P<currency>₹|INR|Rs\.?)?\s*"
    rf"(?P<minus2>[{_DASHES}])?\s*"
    r"(?P<digits>\d[\d,]*(?:\.\d+)?)"
    r"(?P<close>\))?"
    r"(?:\s*(?P<scale>" + "|".join(p for p, _ in _SCALES) + r"))?"
    r"(?P<after>\s*(?:%|per\s*cent|percent|[" + _DASHES + r"]\d)|\s+(?:january|february|"
    r"march|april|may|june|july|august|september|october|november|december)\b)?",
    re.IGNORECASE,
)
# A reference, not an amount: "page 362", "Note 4", "p. 29", "FY2025".
_REFERENCE = re.compile(
    r"(?:\b(?:pages?|pp?\.?|notes?|schedules?|sections?|FY)\s*)$", re.IGNORECASE
)

_MIN_SIGNIFICANT_FIGURES = 3


@dataclass(frozen=True)
class _Figure:
    """One amount as written in an answer, before any unit conversion."""

    amount: float
    scale: float | None
    decimals: int
    significant: int
    signed: bool
    marked: bool


def _scale_of(word: str | None) -> float | None:
    if word is None:
        return None
    for pattern, scale in _SCALES:
        if re.fullmatch(pattern, word.strip(), re.IGNORECASE):
            return scale
    return None


def _headline_figures(text: str) -> list[_Figure]:
    """The answer's amounts in order: the marked ones when there are any."""
    figures: list[_Figure] = []
    for m in _AMOUNT.finditer(text):
        if m.group("after") or _REFERENCE.search(text[: m.start()]):
            continue
        digits = m.group("digits")
        bracketed = bool(m.group("open") and m.group("close"))
        amount = parse_amount(f"({digits})" if bracketed else digits)
        if amount is None:
            continue
        minus = bool(m.group("minus") or m.group("minus2"))
        if minus:
            amount = -abs(amount)
        scale = _scale_of(m.group("scale"))
        figures.append(_Figure(
            amount=amount,
            scale=scale,
            decimals=len(digits.split(".")[1]) if "." in digits else 0,
            significant=len(digits.replace(",", "").replace(".", "").lstrip("0")),
            signed=bracketed or minus,
            marked=bool(m.group("currency")) or scale is not None,
        ))
    marked = [f for f in figures if f.marked]
    return marked or figures


def _matches(figure: _Figure, gold: GoldValue) -> bool:
    gold_scale = UNIT_SCALES[gold.unit]
    ratio = (figure.scale or gold_scale) / gold_scale
    value = figure.amount * ratio
    # The answer's own rounding, if it kept enough figures to mean something.
    slack = (
        0.5 * 10.0 ** -figure.decimals * ratio
        if figure.significant >= _MIN_SIGNIFICANT_FIGURES else 0.0
    )
    allowed = max(gold.tolerance, slack) + 1e-9 * max(1.0, abs(gold.value))
    if figure.signed:
        return abs(value - gold.value) <= allowed
    return abs(abs(value) - abs(gold.value)) <= allowed


def score_run(benchmark: Benchmark, answers: Sequence[EndpointAnswer]) -> EvalReport:
    """Score every benchmark item that has an answer, in benchmark order.
    Answers for ids the benchmark does not hold are ignored."""
    by_id = {a.item_id: a for a in answers}
    scores = tuple(
        _score_item(item, by_id[item.id]) for item in benchmark.items if item.id in by_id
    )
    kinds = sorted({s.kind for s in scores})
    return EvalReport(
        benchmark_version=benchmark.version,
        benchmark_size=len(benchmark.items),
        items=scores,
        by_kind={k: _summarise([s for s in scores if s.kind == k]) for k in kinds},
        overall=_summarise(scores),
    )


def _summarise(scores: Sequence[ItemScore]) -> KindSummary:
    statuses = [s.status for s in scores]
    return KindSummary(
        run=len(statuses), correct=statuses.count("correct"),
        wrong=statuses.count("wrong"), failed=statuses.count("failed"),
        citations=sum(s.citations for s in scores),
        valid_citations=sum(s.valid_citations for s in scores),
        supported=sum(s.supported for s in scores),
        completed=sum(s.completed for s in scores),
    )


def _score_item(item: BenchmarkItem, answer: EndpointAnswer) -> ItemScore:
    gold_pages = {(e.doc_key, e.page) for e in item.gold.evidence}
    valid = sum((c.doc_key, c.page) in gold_pages for c in answer.citations)

    def score(status: ItemStatus, reason: str = "") -> ItemScore:
        return ItemScore(
            item_id=item.id, kind=item.kind, status=status, reason=reason,
            citations=len(answer.citations), valid_citations=valid,
            supported=valid > 0, completed=status == "correct" and valid > 0,
        )

    if answer.error is not None or answer.answer is None:
        return score("failed", answer.error or "no answer returned")
    # Each gold value must be matched by a DIFFERENT headline figure.
    unclaimed = _headline_figures(answer.answer)[: len(item.gold.values)]
    missing: list[GoldValue] = []
    for gold in item.gold.values:
        hit = next((f for f in unclaimed if _matches(f, gold)), None)
        if hit is None:
            missing.append(gold)
        else:
            unclaimed.remove(hit)
    if missing:
        return score("wrong", "; ".join(f"{g.value:g} {g.unit} not stated" for g in missing))
    return score("correct")
