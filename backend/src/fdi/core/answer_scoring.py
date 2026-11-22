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

A `calculation` item's gold may be dimensionless. A "percent" gold (a margin,
a tax rate) is matched only against the answer's percentages, never
rescaled; a "ratio" gold only against its unmarked, non-percentage numbers.
Each unit kind (rupees, percent, ratio) takes its own headline figures, so
"₹49,454 crore over ₹2,71,423 crore gives 18.22%" answers a margin of 18.22
-- the inputs it shows do not crowd the margin out.

CORRECT IS NOT COMPLETED
------------------------
A citation is valid only when its Report `doc_key` and page are one of the
item's gold evidence locations. An item is completed only when it is
correct AND at least one citation is valid (§16.2): a fluent, correct
number that cites the wrong page, or nothing, is a failed task. Citation
validity, evidence support and completion are reported beside accuracy,
never blended into it.

TOOL SELECTION
--------------
Scored only for an item whose gold names `expected_tools`; elsewhere it is
ABSENT (None), never 0, and the item is outside every tool-selection
denominator. An item's tool selection is the share of its expected tools
the endpoint called at least once -- set recall:

    |expected ∩ called| / |expected|

Order and repeats are ignored: the benchmark names which tools a good plan
needs, not a script, and two `search`es are not a better plan than one.
Extra tools are not penalised either: looking at a Report's structure
before reading its tables is sound exploration, and an exact-set rule would
score that as a planning failure. Over-calling shows up where it costs --
latency and tokens -- not here. A failed item that names tools scores 0.0,
whatever partial trace it carries: an endpoint that never answered did not
carry its plan out. A kind's (and the run's) tool-selection accuracy is the
mean over its items that name tools; None when none do.

UNANSWERABLE ITEMS
------------------
An item whose gold is not answerable (`unanswerable`: a concept the Reports
do not disclose, a Company not in the corpus) is correct only when the
answer asserts NO figure AND says explicitly that the information was not
found ("could not find", "is not disclosed", "not among the ingested
Reports"). Any figure -- marked, bare or a percentage -- is a hallucination
and scores wrong, the reason quoting it; only page / Note / fiscal-year
references and a bare calendar year are not figures. A refusal that never
says "not found" is an evasion and scores wrong too.

Such an item has no gold evidence, so no citation can be valid and it is
never `supported`. Completion therefore does NOT demand a valid citation
for it: a correct refusal is the whole task, and requiring a citation that
cannot exist would make the kind uncompletable. An endpoint that failed is
still `failed`, never a correct refusal.

Pure (`fdi.core` law): no I/O, no LLM, deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from fdi.core.benchmark import (
    PERCENT,
    RATIO,
    UNIT_SCALES,
    Benchmark,
    BenchmarkItem,
    GoldValue,
)
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
    when the endpoint failed or timed out; `answer` is then usually None.
    `tool_trace` is the names of the tools the endpoint called, in call
    order, repeats kept."""

    item_id: str
    answer: str | None
    error: str | None = None
    citations: tuple[Citation, ...] = ()
    tool_trace: tuple[str, ...] = ()


@dataclass(frozen=True)
class ItemScore:
    """`status` is "failed" when the endpoint never gave an answer. `reason`
    says why an item is not correct; empty when it is.

    A citation is VALID when its (`doc_key`, page) is one of the item's gold
    evidence locations; "cited something" is not "cited the source". The
    item is `supported` when at least one citation is valid, and `completed`
    -- the task outcome (§16.2) -- when it is correct AND supported. A
    correct number nobody can trace to its source is a failed task. An
    unanswerable item has nothing to cite: it completes when correct.

    `tool_selection` is the share of the item's expected tools that were
    called, `missing_tools` the ones that were not; None and () when the
    item names no expected tools."""

    item_id: str
    kind: str
    status: ItemStatus
    reason: str
    citations: int
    valid_citations: int
    supported: bool
    completed: bool
    tool_selection: float | None = None
    missing_tools: tuple[str, ...] = ()

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
    # Over items that name expected tools only; see TOOL SELECTION.
    tool_selection_items: int = 0
    tool_selection_total: float = 0.0

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

    @property
    def tool_selection_accuracy(self) -> float | None:
        """Mean tool selection over items that name expected tools; None --
        not 0.0 -- when none do."""
        if not self.tool_selection_items:
            return None
        return self.tool_selection_total / self.tool_selection_items


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
    r"(?:(?P<percent>\s*(?:%|per\s*cent))"
    r"|(?P<after>\s*[" + _DASHES + r"]\d|\s+(?:january|february|"
    r"march|april|may|june|july|august|september|october|november|december)\b))?",
    re.IGNORECASE,
)
# A reference, not an amount: "page 362", "Note 4", "p. 29", "FY2025".
_REFERENCE = re.compile(
    r"(?:\b(?:pages?|pp?\.?|notes?|schedules?|sections?|FY)\s*)$", re.IGNORECASE
)

_MIN_SIGNIFICANT_FIGURES = 3

_CALENDAR_YEAR = re.compile(r"(?:19|20)\d\d")

# An explicit statement that the answer is not in the Reports: "could not
# find", "is not disclosed", "no information on", "not among the ingested
# Reports". A refusal that never says so is an evasion, not a correct answer.
_NOT_FOUND = re.compile(
    r"\b(?:could|can|did|does|do|was|is|are|were)\s*(?:not|n['’]t)\s+"
    r"(?:find|locate|see|identify|contain|include|cover|disclose|report|have|hold|mention|"
    r"(?:be\s+)?(?:found|located|available|disclosed|reported|included|covered|present|"
    r"among|in\b))"
    r"|\bcannot\s+(?:find|locate|answer|be\s+(?:found|answered|determined))"
    r"|\bunable\s+to\s+(?:find|locate|answer|determine|identify)"
    r"|\bnot\s+(?:found|available|disclosed|reported|present|among|in\s+the\s+(?:corpus|"
    r"ingested|available|provided))"
    r"|\bno\s+(?:information|data|record|figure|mention|disclosure|such|relevant|report)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Figure:
    """One amount as written in an answer, before any unit conversion."""

    amount: float
    scale: float | None
    decimals: int
    significant: int
    signed: bool
    marked: bool
    percent: bool
    text: str


def _scale_of(word: str | None) -> float | None:
    if word is None:
        return None
    for pattern, scale in _SCALES:
        if re.fullmatch(pattern, word.strip(), re.IGNORECASE):
            return scale
    return None


def _unit_kind(unit: str) -> str:
    """PERCENT, RATIO, or "INR" for every rupee unit (rescalable into each other)."""
    return unit if unit in (PERCENT, RATIO) else "INR"


def _headline_figures(text: str, unit: str) -> list[_Figure]:
    """The answer's candidate figures for a gold value in `unit`, in order: a
    percentage for a percent gold, an unmarked number for a ratio, and for a
    rupee gold an amount -- the marked ones when there are any."""
    figures = _figures(text)
    if unit == PERCENT:
        return [f for f in figures if f.percent]
    amounts = [f for f in figures if not f.percent]
    if unit == RATIO:
        return [f for f in amounts if not f.marked]
    marked = [f for f in amounts if f.marked]
    return marked or amounts


def _asserted_figures(text: str) -> list[_Figure]:
    """Every figure the answer states, marked or not, percentages included,
    except a bare calendar year ("as at 31 March 2026"), which asserts
    nothing about an amount."""
    return [f for f in _figures(text) if not _CALENDAR_YEAR.fullmatch(f.text)]


def _figures(text: str) -> list[_Figure]:
    """Every figure in `text`, in order, references excluded."""
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
            percent=bool(m.group("percent")),
            text=m.group(0).strip(),
        ))
    return figures


def _matches(figure: _Figure, gold: GoldValue) -> bool:
    # Percent and ratio gold are dimensionless: never rescaled.
    gold_scale = UNIT_SCALES.get(gold.unit, 1.0)
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
        _with_tool_selection(_score_item(item, by_id[item.id]), item, by_id[item.id])
        for item in benchmark.items if item.id in by_id
    )
    kinds = sorted({s.kind for s in scores})
    return EvalReport(
        benchmark_version=benchmark.version,
        benchmark_size=len(benchmark.items),
        items=scores,
        by_kind={k: _summarise([s for s in scores if s.kind == k]) for k in kinds},
        overall=_summarise(scores),
    )


def _with_tool_selection(
    score: ItemScore, item: BenchmarkItem, answer: EndpointAnswer
) -> ItemScore:
    expected = item.gold.expected_tools
    if expected is None:
        return score
    called = set() if score.status == "failed" else set(answer.tool_trace)
    missing = tuple(t for t in expected if t not in called)
    return replace(
        score, tool_selection=1.0 - len(missing) / len(expected), missing_tools=missing
    )


def _summarise(scores: Sequence[ItemScore]) -> KindSummary:
    statuses = [s.status for s in scores]
    tool_scores = [s.tool_selection for s in scores if s.tool_selection is not None]
    return KindSummary(
        run=len(statuses), correct=statuses.count("correct"),
        wrong=statuses.count("wrong"), failed=statuses.count("failed"),
        citations=sum(s.citations for s in scores),
        valid_citations=sum(s.valid_citations for s in scores),
        supported=sum(s.supported for s in scores),
        completed=sum(s.completed for s in scores),
        tool_selection_items=len(tool_scores),
        tool_selection_total=sum(tool_scores),
    )


def _score_item(item: BenchmarkItem, answer: EndpointAnswer) -> ItemScore:
    gold_pages = {(e.doc_key, e.page) for e in item.gold.evidence}
    valid = sum((c.doc_key, c.page) in gold_pages for c in answer.citations)

    # An unanswerable item has no gold evidence, so no citation can be valid
    # and completion cannot demand one: a correct refusal is the whole task.
    needs_citation = item.gold.answerable

    def score(status: ItemStatus, reason: str = "") -> ItemScore:
        return ItemScore(
            item_id=item.id, kind=item.kind, status=status, reason=reason,
            citations=len(answer.citations), valid_citations=valid,
            supported=valid > 0,
            completed=status == "correct" and (valid > 0 or not needs_citation),
        )

    if answer.error is not None or answer.answer is None:
        return score("failed", answer.error or "no answer returned")
    if not item.gold.answerable:
        asserted = _asserted_figures(answer.answer)
        if asserted:
            return score("wrong", f"asserted a figure the Reports do not hold: {asserted[0].text}")
        if not _NOT_FOUND.search(answer.answer):
            return score("wrong", "no statement that the information was not found")
        return score("correct")
    # Each gold value must be matched by a DIFFERENT headline figure. Gold of
    # one unit kind (rupees, percent, ratio) draws on its own headline
    # figures, so a margin's inputs never crowd out the margin itself.
    missing: list[GoldValue] = []
    for unit_kind in dict.fromkeys(_unit_kind(g.unit) for g in item.gold.values):
        golds = [g for g in item.gold.values if _unit_kind(g.unit) == unit_kind]
        unclaimed = _headline_figures(answer.answer, unit_kind)[: len(golds)]
        for gold in golds:
            hit = next((f for f in unclaimed if _matches(f, gold)), None)
            if hit is None:
                missing.append(gold)
            else:
                unclaimed.remove(hit)
    missing.sort(key=item.gold.values.index)
    if missing:
        return score("wrong", "; ".join(f"{g.value:g} {g.unit} not stated" for g in missing))
    return score("correct")
