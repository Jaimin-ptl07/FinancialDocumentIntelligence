"""Latency percentiles, spend and cost per completed task, through
`summarise_cost` and `estimate_spend` only. Expected values are worked by
hand from the nearest-rank percentile definition, not recomputed."""

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
