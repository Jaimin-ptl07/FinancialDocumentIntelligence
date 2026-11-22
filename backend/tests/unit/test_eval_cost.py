"""Latency percentiles, spend and cost per completed task, through
`summarise_cost` and `estimate_spend` only. Expected values are worked by
hand from the nearest-rank percentile definition, not recomputed."""

from dataclasses import replace

import pytest

from fdi.core.eval_cost import ItemCost, ItemUsage, estimate_spend, summarise_cost


def _item(item_id: str, kind: str, latency_s: float, cost_inr: float | None = None,
          tokens: tuple[int, int] = (0, 0)) -> ItemCost:
    usage = None if cost_inr is None else ItemUsage(
        input_tokens=tokens[0], output_tokens=tokens[1], cost_inr=cost_inr
    )
    return ItemCost(item_id=item_id, kind=kind, latency_s=latency_s, usage=usage)


def test_p50_and_p95_latency_are_nearest_rank_per_kind() -> None:
    # 20 fact lookups taking 1..20 s: P50 is the 10th, P95 the 19th.
    lookups = [_item(f"f{n}", "fact_lookup", float(n)) for n in range(20, 0, -1)]
    calcs = [_item("c1", "calculation", 4.0), _item("c2", "calculation", 9.0)]

    summary = summarise_cost([*lookups, *calcs], completed=set())

    f = summary.latency_by_kind["fact_lookup"]
    assert (f.count, f.p50_s, f.p95_s) == (20, 10.0, 19.0)
    c = summary.latency_by_kind["calculation"]
    assert (c.count, c.p50_s, c.p95_s) == (2, 4.0, 9.0)
    assert (summary.latency_overall.count, summary.latency_overall.p95_s) == (22, 19.0)


def test_a_single_item_is_its_own_p50_and_p95() -> None:
    s = summarise_cost([_item("a", "research", 2.5)], completed=set())
    assert (s.latency_by_kind["research"].p50_s, s.latency_by_kind["research"].p95_s) == (2.5, 2.5)


def test_cost_per_completed_task_charges_every_item_run_to_the_completed_ones() -> None:
    # Three items cost 0.30 + 0.50 + 0.20 = 1.00 INR; two completed. The miss
    # still spent money, so each completed task carries 0.50.
    items = [
        _item("a", "fact_lookup", 1.0, 0.30, (1_000, 50)),
        _item("b", "fact_lookup", 1.0, 0.50, (2_000, 70)),
        _item("c", "fact_lookup", 1.0, 0.20, (500, 30)),
    ]
    s = summarise_cost(items, completed={"a", "c"})
    assert s.cost_inr == pytest.approx(1.00)
    assert (s.input_tokens, s.output_tokens) == (3_500, 150)
    assert s.completed == 2
    assert s.cost_per_completed_task_inr == pytest.approx(0.50)


def test_tokens_spend_and_cost_per_completed_task_are_broken_down_by_kind() -> None:
    # Lookups: 0.30 + 0.50 = 0.80 INR, one completed -> 0.80 each.
    # Unanswerable: 0.10 priced + one unread, none completed -> no per-task cost.
    items = [
        _item("f1", "fact_lookup", 1.0, 0.30, (1_000, 50)),
        _item("f2", "fact_lookup", 1.0, 0.50, (2_000, 70)),
        _item("u1", "unanswerable", 1.0, 0.10, (400, 20)),
        _item("u2", "unanswerable", 1.0, None),
    ]
    s = summarise_cost(items, completed={"f2"})

    f = s.spend_by_kind["fact_lookup"]
    assert (f.input_tokens, f.output_tokens, f.items_without_usage, f.completed) == (
        3_000, 120, 0, 1)
    assert f.cost_inr == pytest.approx(0.80)
    assert f.cost_per_completed_task_inr == pytest.approx(0.80)
    u = s.spend_by_kind["unanswerable"]
    assert (u.input_tokens, u.output_tokens, u.items_without_usage, u.completed) == (
        400, 20, 1, 0)
    assert u.cost_inr == pytest.approx(0.10)
    assert u.cost_per_completed_task_inr is None


def test_nothing_completed_has_no_cost_per_completed_task() -> None:
    s = summarise_cost([_item("a", "fact_lookup", 1.0, 0.30)], completed=set())
    assert s.completed == 0
    assert s.cost_per_completed_task_inr is None


def test_completed_ids_outside_the_run_are_not_counted() -> None:
    s = summarise_cost([_item("a", "fact_lookup", 1.0, 0.40)], completed={"a", "elsewhere"})
    assert s.completed == 1
    assert s.cost_per_completed_task_inr == pytest.approx(0.40)


def test_an_item_whose_usage_was_never_read_is_counted_not_priced_as_free() -> None:
    items = [_item("a", "fact_lookup", 1.0, 0.40), _item("b", "fact_lookup", 1.0, None)]
    s = summarise_cost(items, completed={"a"})
    assert s.items_without_usage == 1
    assert s.cost_inr == pytest.approx(0.40)


def test_an_empty_run_summarises_to_zeros() -> None:
    s = summarise_cost([], completed=set())
    assert s.latency_by_kind == {}
    assert s.latency_overall.count == 0
    assert s.latency_overall.p50_s is None
    assert s.cost_inr == 0.0


def test_the_spend_estimate_is_items_times_the_recent_average() -> None:
    est = estimate_spend(10, [0.20, 0.40, 0.30])
    assert est.sample_size == 3
    assert est.average_cost_inr == pytest.approx(0.30)
    assert est.total_cost_inr == pytest.approx(3.00)


def test_with_no_recent_usage_there_is_no_estimate() -> None:
    est = estimate_spend(10, [])
    assert (est.items, est.sample_size) == (10, 0)
    assert est.average_cost_inr is None
    assert est.total_cost_inr is None


def _judged(item: ItemCost, cost_inr: float, tokens: tuple[int, int]) -> ItemCost:
    return replace(item, judge_usage=ItemUsage(
        input_tokens=tokens[0], output_tokens=tokens[1], cost_inr=cost_inr
    ))


def test_judge_spend_is_in_the_run_total_but_reported_apart() -> None:
    """Endpoint 0.30 + 0.50 INR; the judge 0.05 + 0.07 on two research
    items. The total is 0.92; cost per completed task stays the endpoint's
    0.80 over 2 -- the judge is the eval's spend, not the price of a task."""
    items = [
        _judged(_item("r1", "research", 1.0, 0.30, (1_000, 50)), 0.05, (2_000, 40)),
        _judged(_item("r2", "research", 1.0, 0.50, (1_500, 70)), 0.07, (2_500, 60)),
    ]
    s = summarise_cost(items, completed={"r1", "r2"})
    assert (s.judge_input_tokens, s.judge_output_tokens) == (4_500, 100)
    assert s.judge_cost_inr == pytest.approx(0.12)
    assert s.cost_inr == pytest.approx(0.80)
    assert s.total_cost_inr == pytest.approx(0.92)
    assert s.cost_per_completed_task_inr == pytest.approx(0.40)
    research = s.spend_by_kind["research"]
    assert research.judge_cost_inr == pytest.approx(0.12)
    assert research.total_cost_inr == pytest.approx(0.92)


def test_an_unjudged_run_has_no_judge_spend() -> None:
    s = summarise_cost([_item("a", "fact_lookup", 1.0, 0.25)], completed=set())
    assert (s.judge_input_tokens, s.judge_output_tokens, s.judge_cost_inr) == (0, 0, 0.0)
    assert s.total_cost_inr == pytest.approx(0.25)


def test_the_estimate_adds_judged_items_times_the_recent_judge_average() -> None:
    """10 answers x 0.30 = 3.00, plus 4 judged items x mean(0.04, 0.06) = 0.20."""
    est = estimate_spend(10, [0.20, 0.40, 0.30], judged_items=4,
                         recent_judge_costs=[0.04, 0.06])
    assert (est.judge_items, est.judge_sample_size) == (4, 2)
    assert est.judge_average_cost_inr == pytest.approx(0.05)
    assert est.judge_total_cost_inr == pytest.approx(0.20)
    assert est.total_cost_inr == pytest.approx(3.20)


def test_judged_items_with_no_recent_judge_usage_leave_no_estimate() -> None:
    """Pricing the judge as free would understate the run."""
    est = estimate_spend(10, [0.30], judged_items=4, recent_judge_costs=[])
    assert est.judge_total_cost_inr is None
    assert est.total_cost_inr is None
