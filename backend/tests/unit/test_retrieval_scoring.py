"""What Recall@5, Recall@10 and MRR are for a given ranked hit list."""

import pytest

from fdi.core.benchmark import Benchmark, BenchmarkItem, Evidence, Gold, GoldValue
from fdi.core.retrieval_scoring import RetrievalHit, RetrievedHits, score_retrieval

_GOLD = Evidence("tcs-2025-26", 362)


def _item(item_id: str, kind: str = "fact_lookup",
          evidence: tuple[Evidence, ...] = (_GOLD,)) -> BenchmarkItem:
    return BenchmarkItem(
        id=item_id, kind=kind, question=f"What was TCS's revenue? ({item_id})",
        gold=Gold(answerable=True, values=(GoldValue(271423.0, "INR crore", 0.5),),
                  evidence=evidence),
    )


def _off(page: int) -> RetrievalHit:
    return RetrievalHit(doc_key="tcs-2025-26", page=page)


def _gold_at(rank: int, total: int = 10) -> tuple[RetrievalHit, ...]:
    """`total` hits, the gold page at 1-based `rank`, every other one off it."""
    return tuple(RetrievalHit("tcs-2025-26", 362) if n == rank else _off(n)
                 for n in range(1, total + 1))


def test_a_gold_page_at_rank_1_is_recalled_at_5_and_10_with_reciprocal_rank_1() -> None:
    report = score_retrieval(Benchmark("v-test", (_item("q1"),)),
                             [RetrievedHits("q1", _gold_at(1))])
    item = report.items[0]
    assert (item.first_gold_rank, item.recalled_at_5, item.recalled_at_10) == (1, True, True)
    assert item.reciprocal_rank == 1.0
    assert (report.overall.recall_at_5, report.overall.recall_at_10, report.overall.mrr) == (
        1.0, 1.0, 1.0)


def test_a_gold_page_at_rank_7_is_recalled_at_10_but_not_at_5() -> None:
    report = score_retrieval(Benchmark("v-test", (_item("q1"),)),
                             [RetrievedHits("q1", _gold_at(7))])
    item = report.items[0]
    assert (item.first_gold_rank, item.recalled_at_5, item.recalled_at_10) == (7, False, True)
    assert report.overall.recall_at_5 == 0.0
    assert report.overall.recall_at_10 == 1.0
    assert report.overall.mrr == pytest.approx(1 / 7)


def test_an_absent_gold_page_scores_zero_everywhere() -> None:
    # Right page, wrong Report; right Report, no page: neither is the gold location.
    hits = (RetrievalHit("ril-2025-26", 362), RetrievalHit("tcs-2025-26", None), _off(9))
    report = score_retrieval(Benchmark("v-test", (_item("q1"),)), [RetrievedHits("q1", hits)])
    item = report.items[0]
    assert (item.first_gold_rank, item.recalled_at_10, item.reciprocal_rank) == (None, False, 0.0)
    assert (report.overall.recall_at_10, report.overall.mrr) == (0.0, 0.0)


def test_any_of_several_gold_locations_counts_and_the_first_one_sets_the_rank() -> None:
    item = _item("q1", evidence=(_GOLD, Evidence("tcs-2025-26", 3)))
    report = score_retrieval(Benchmark("v-test", (item,)),
                             [RetrievedHits("q1", (_off(9), _off(3), _off(362)))])
    assert report.items[0].first_gold_rank == 2


def test_a_multi_report_item_is_recalled_only_once_every_report_has_a_gold_hit() -> None:
    """TCS vs RIL: TCS's gold page at rank 2 and RIL's at rank 8 put both
    Companies in front of the answerer only by rank 8."""
    ril = Evidence("ril-2025-26", 145)
    item = _item("c1", kind="cross_company", evidence=(_GOLD, ril))
    hits = tuple(
        RetrievalHit("tcs-2025-26", 362) if n == 2
        else RetrievalHit("ril-2025-26", 145) if n == 8 else _off(n)
        for n in range(1, 11)
    )
    scored = score_retrieval(Benchmark("v-test", (item,)), [RetrievedHits("c1", hits)]).items[0]
    assert (scored.first_gold_rank, scored.recalled_at_5, scored.recalled_at_10) == (
        8, False, True)


def test_a_multi_report_item_missing_one_report_is_not_recalled() -> None:
    item = _item("c1", kind="cross_company",
                 evidence=(_GOLD, Evidence("ril-2025-26", 145)))
    scored = score_retrieval(Benchmark("v-test", (item,)),
                             [RetrievedHits("c1", _gold_at(1))]).items[0]
    assert (scored.first_gold_rank, scored.recalled_at_10, scored.reciprocal_rank) == (
        None, False, 0.0)


def test_a_failed_retrieval_call_is_run_and_counts_against_every_metric() -> None:
    bench = Benchmark("v-test", (_item("q1"), _item("q2")))
    report = score_retrieval(bench, [
        RetrievedHits("q1", _gold_at(1)),
        RetrievedHits("q2", error="POST /retrieve returned 503"),
    ])
    assert report.items[1].failed
    assert report.items[1].error == "POST /retrieve returned 503"
    o = report.overall
    assert (o.run, o.failed) == (2, 1)
    assert (o.recall_at_5, o.recall_at_10, o.mrr) == (0.5, 0.5, 0.5)


def test_items_without_gold_evidence_are_skipped_and_counted() -> None:
    unanswerable = BenchmarkItem(id="u1", kind="unanswerable", question="What is X?",
                                 gold=Gold(answerable=False))
    bench = Benchmark("v-test", (_item("q1"), unanswerable))
    report = score_retrieval(bench, [RetrievedHits("q1", _gold_at(1)),
                                     RetrievedHits("u1", _gold_at(1))])
    assert [i.item_id for i in report.items] == ["q1"]
    assert report.skipped == 1
    assert report.benchmark_size == 2
    assert "unanswerable" not in report.by_kind


def test_recall_and_mrr_are_reported_per_kind_and_overall() -> None:
    bench = Benchmark("v-test", (
        _item("f1"), _item("f2"), _item("c1", kind="cross_company"),
    ))
    report = score_retrieval(bench, [
        RetrievedHits("f1", _gold_at(1)),       # rr 1
        RetrievedHits("f2", _gold_at(7)),       # rr 1/7, @10 only
        RetrievedHits("c1", _gold_at(2)),       # rr 1/2
    ])
    fact, cross = report.by_kind["fact_lookup"], report.by_kind["cross_company"]
    assert (fact.run, fact.recall_at_5, fact.recall_at_10) == (2, 0.5, 1.0)
    assert fact.mrr == pytest.approx((1 + 1 / 7) / 2)
    assert (cross.run, cross.recall_at_5, cross.mrr) == (1, 1.0, 0.5)
    o = report.overall
    assert o.run == 3
    assert (o.recall_at_5, o.recall_at_10) == pytest.approx((2 / 3, 1.0))
    assert o.mrr == pytest.approx((1 + 1 / 7 + 1 / 2) / 3)


def test_items_not_retrieved_for_are_not_run() -> None:
    report = score_retrieval(Benchmark("v-test", (_item("q1"), _item("q2"))),
                             [RetrievedHits("q1", _gold_at(1))])
    assert report.overall.run == 1
