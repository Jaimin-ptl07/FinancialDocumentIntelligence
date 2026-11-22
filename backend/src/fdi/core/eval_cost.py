"""What an eval run cost: latency percentiles, tokens, spend, and cost per
completed task (design doc §16.2), plus the pre-run spend estimate.

Two entry points:

- `summarise_cost(items, completed) -> CostSummary` -- per-item wall-clock
  latency and usage in, P50/P95 latency per item kind and the run's totals
  out, with tokens, spend and cost per completed task both per item kind
  and for the run. `completed` is the set of item ids that completed their task; the
  caller decides what "completed" means (the scorer's `ItemScore.completed`:
  correct AND validly cited).
- `estimate_spend(items, recent_item_costs) -> SpendEstimate` -- items x the
  average of recent per-item costs, printed before any billed call.

PERCENTILES are nearest-rank: the P-th percentile of n sorted latencies is
the ceil(P/100 x n)-th of them. Always an observed latency, never an
interpolation, so a P95 can be traced to the item that produced it.

COST PER COMPLETED TASK is the whole run's spend over the number of
completed items: an item that failed or answered wrongly still spent money,
and that money is the price of the tasks that did complete.

JUDGE SPEND. With the optional LLM judge on, an item also carries what
judging it cost (`ItemCost.judge_usage`). It is counted in its own
`judge_*` fields and in `total_cost_inr`, but NOT in `cost_inr` or cost per
completed task: those are what the answering endpoint spent, the price of a
task to an Invitee, and an eval's judge is no part of that price. The
estimate likewise adds judged items x the recent judge average, and has no
total when judged items have no judge history to price them by.

An item whose usage could not be read is counted in `items_without_usage`
and left out of the spend -- never priced as free without saying so.

Pure (`fdi.core` law): no I/O, no clock, deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ItemUsage:
    """The LLM usage billed for one item's answer, priced in INR."""

    input_tokens: int
    output_tokens: int
    cost_inr: float


@dataclass(frozen=True)
class ItemCost:
    """One item's wall-clock latency (submit to answer, in seconds) and its
    usage -- None when the usage could not be read. `judge_usage` is what
    the LLM judge spent on it; None when it was not judged."""

    item_id: str
    kind: str
    latency_s: float
    usage: ItemUsage | None
    judge_usage: ItemUsage | None = None


@dataclass(frozen=True)
class LatencySummary:
    """Nearest-rank P50/P95 over `count` latencies; None when count is 0."""

    count: int
    p50_s: float | None
    p95_s: float | None


@dataclass(frozen=True)
class SpendSummary:
    """Tokens and spend over one item kind's items, and that kind's cost per
    completed task (None when none of them completed)."""

    input_tokens: int
    output_tokens: int
    cost_inr: float
    items_without_usage: int
    completed: int
    cost_per_completed_task_inr: float | None
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    judge_cost_inr: float = 0.0

    @property
    def total_cost_inr(self) -> float:
        """Endpoint and judge spend together."""
        return self.cost_inr + self.judge_cost_inr


@dataclass(frozen=True)
class CostSummary:
    """The run's latency and spend, overall and per item kind."""

    latency_by_kind: dict[str, LatencySummary]
    spend_by_kind: dict[str, SpendSummary]
    latency_overall: LatencySummary
    input_tokens: int
    output_tokens: int
    cost_inr: float
    items_without_usage: int
    completed: int
    cost_per_completed_task_inr: float | None
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    judge_cost_inr: float = 0.0

    @property
    def total_cost_inr(self) -> float:
        """Endpoint and judge spend together."""
        return self.cost_inr + self.judge_cost_inr


@dataclass(frozen=True)
class SpendEstimate:
    """`items` x the average of `sample_size` recent per-item costs, plus
    `judge_items` x the average of `judge_sample_size` recent judge costs.
    An average and its product are None when there is no recent cost to
    average; `total_cost_inr` is None when either part it needs is."""

    items: int
    sample_size: int
    average_cost_inr: float | None
    total_cost_inr: float | None
    judge_items: int = 0
    judge_sample_size: int = 0
    judge_average_cost_inr: float | None = None
    judge_total_cost_inr: float | None = None


def summarise_cost(items: Sequence[ItemCost], completed: Collection[str]) -> CostSummary:
    kinds: dict[str, list[ItemCost]] = {}
    for item in items:
        kinds.setdefault(item.kind, []).append(item)
    overall = _spend(items, completed)
    return CostSummary(
        latency_by_kind={
            kind: _latency([i.latency_s for i in group]) for kind, group in kinds.items()
        },
        spend_by_kind={kind: _spend(group, completed) for kind, group in kinds.items()},
        latency_overall=_latency([item.latency_s for item in items]),
        input_tokens=overall.input_tokens,
        output_tokens=overall.output_tokens,
        cost_inr=overall.cost_inr,
        items_without_usage=overall.items_without_usage,
        completed=overall.completed,
        cost_per_completed_task_inr=overall.cost_per_completed_task_inr,
        judge_input_tokens=overall.judge_input_tokens,
        judge_output_tokens=overall.judge_output_tokens,
        judge_cost_inr=overall.judge_cost_inr,
    )


def _spend(items: Sequence[ItemCost], completed: Collection[str]) -> SpendSummary:
    priced = [item.usage for item in items if item.usage is not None]
    cost = sum(u.cost_inr for u in priced)
    done = sum(1 for item in items if item.item_id in completed)
    judged = [item.judge_usage for item in items if item.judge_usage is not None]
    return SpendSummary(
        input_tokens=sum(u.input_tokens for u in priced),
        output_tokens=sum(u.output_tokens for u in priced),
        cost_inr=cost,
        items_without_usage=len(items) - len(priced),
        completed=done,
        cost_per_completed_task_inr=cost / done if done else None,
        judge_input_tokens=sum(u.input_tokens for u in judged),
        judge_output_tokens=sum(u.output_tokens for u in judged),
        judge_cost_inr=sum(u.cost_inr for u in judged),
    )


def estimate_spend(
    items: int,
    recent_item_costs: Sequence[float],
    *,
    judged_items: int = 0,
    recent_judge_costs: Sequence[float] = (),
) -> SpendEstimate:
    average = _mean(recent_item_costs)
    judge_average = _mean(recent_judge_costs)
    answers = None if average is None else items * average
    judge = 0.0 if not judged_items else (
        None if judge_average is None else judged_items * judge_average
    )
    return SpendEstimate(
        items=items, sample_size=len(recent_item_costs), average_cost_inr=average,
        total_cost_inr=None if answers is None or judge is None else answers + judge,
        judge_items=judged_items, judge_sample_size=len(recent_judge_costs),
        judge_average_cost_inr=judge_average,
        judge_total_cost_inr=judge if judged_items else None,
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _latency(values: Sequence[float]) -> LatencySummary:
    ordered = sorted(values)
    return LatencySummary(count=len(ordered), p50_s=_nearest_rank(ordered, 50),
                          p95_s=_nearest_rank(ordered, 95))


def _nearest_rank(ordered: Sequence[float], percentile: int) -> float | None:
    if not ordered:
        return None
    return ordered[max(1, math.ceil(percentile / 100 * len(ordered))) - 1]
