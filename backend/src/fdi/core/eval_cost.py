"""What an eval run cost: latency percentiles, tokens, spend, and cost per
completed task (design doc §16.2), plus the pre-run spend estimate.

Two entry points:

- `summarise_cost(items, completed) -> CostSummary` -- per-item wall-clock
  latency and usage in, P50/P95 latency per item kind and the run's totals
  out. `completed` is the set of item ids that completed their task; the
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
    usage -- None when the usage could not be read."""

    item_id: str
    kind: str
    latency_s: float
    usage: ItemUsage | None


@dataclass(frozen=True)
class LatencySummary:
    """Nearest-rank P50/P95 over `count` latencies; None when count is 0."""

    count: int
    p50_s: float | None
    p95_s: float | None


@dataclass(frozen=True)
class CostSummary:
    latency_by_kind: dict[str, LatencySummary]
    latency_overall: LatencySummary
    input_tokens: int
    output_tokens: int
    cost_inr: float
    items_without_usage: int
    completed: int
    cost_per_completed_task_inr: float | None


@dataclass(frozen=True)
class SpendEstimate:
    """`items` x the average of `sample_size` recent per-item costs. Both
    figures are None when there is no recent cost to average."""

    items: int
    sample_size: int
    average_cost_inr: float | None
    total_cost_inr: float | None


def summarise_cost(items: Sequence[ItemCost], completed: Collection[str]) -> CostSummary:
    kinds: dict[str, list[float]] = {}
    for item in items:
        kinds.setdefault(item.kind, []).append(item.latency_s)
    priced = [item.usage for item in items if item.usage is not None]
    cost = sum(u.cost_inr for u in priced)
    done = sum(1 for item in items if item.item_id in completed)
    return CostSummary(
        latency_by_kind={kind: _latency(values) for kind, values in kinds.items()},
        latency_overall=_latency([item.latency_s for item in items]),
        input_tokens=sum(u.input_tokens for u in priced),
        output_tokens=sum(u.output_tokens for u in priced),
        cost_inr=cost,
        items_without_usage=len(items) - len(priced),
        completed=done,
        cost_per_completed_task_inr=cost / done if done else None,
    )


def estimate_spend(items: int, recent_item_costs: Sequence[float]) -> SpendEstimate:
    if not recent_item_costs:
        return SpendEstimate(items=items, sample_size=0, average_cost_inr=None,
                             total_cost_inr=None)
    average = sum(recent_item_costs) / len(recent_item_costs)
    return SpendEstimate(items=items, sample_size=len(recent_item_costs),
                         average_cost_inr=average, total_cost_inr=items * average)


def _latency(values: Sequence[float]) -> LatencySummary:
    ordered = sorted(values)
    return LatencySummary(count=len(ordered), p50_s=_nearest_rank(ordered, 50),
                          p95_s=_nearest_rank(ordered, 95))


def _nearest_rank(ordered: Sequence[float], percentile: int) -> float | None:
    if not ordered:
        return None
    return ordered[max(1, math.ceil(percentile / 100 * len(ordered))) - 1]
