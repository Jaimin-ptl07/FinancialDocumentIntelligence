"""Score what hybrid retrieval returned against the benchmark's gold evidence.

One entry point, `score_retrieval(benchmark, retrieved) -> RetrievalReport`:
the retrieval-only counterpart of `answer_scoring.score_run`. No answer is
generated, so no LLM is involved anywhere; what is measured is whether the
pages an answer would have to cite were put in front of the answerer at all.

A HIT IS A GOLD LOCATION
------------------------
A retrieved hit counts when its (`doc_key`, page) is one of the item's gold
evidence locations -- the same key a citation must carry to be valid. A hit
missing either half can never count. Ranks are 1-based, in the order
retrieval returned the hits.

RECALL@K
--------
Recall@K is the fraction of items run whose top K hits include AT LEAST ONE
gold evidence location (the "hit rate" reading of recall, not the fraction
of each item's gold locations recovered). Why: the completion rule needs one
citation on one gold location, not all of them, so one gold page in context
is what makes an item answerable; and most items (every `fact_lookup`) have
exactly one gold location, where the two readings agree anyway. It also
keeps Recall@K and MRR about the same event -- the first gold hit.

An item whose gold spans several Reports (`cross_company`: TCS vs RIL) is
answerable only with a gold page of EACH Report in context, so its "first
gold hit" is the rank at which the hits first cover every gold Report.
Finding one Company's page and not the other's is a miss, not half a hit.

MRR
---
The mean, over items run, of 1 / that rank; 0 for an item the returned
hits never cover.

WHAT IS COUNTED
---------------
Only items with gold evidence can be scored; the rest are SKIPPED and their
number reported, never silently dropped. A scored item whose retrieval call
failed is run and failed: it counts against every metric with a reciprocal
rank of 0, the same way a failed answer counts against accuracy.

Pure (`fdi.core` law): no I/O, no LLM, deterministic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fdi.core.benchmark import Benchmark, BenchmarkItem


@dataclass(frozen=True)
class RetrievalHit:
    """One retrieved chunk's location; either half may be unknown."""

    doc_key: str | None
    page: int | None


@dataclass(frozen=True)
class RetrievedHits:
    """What retrieval returned for one item, best first, or why it failed."""

    item_id: str
    hits: tuple[RetrievalHit, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ItemRetrieval:
    item_id: str
    kind: str
    hits: int
    # 1-based rank at which the hits first hold a gold page of every gold
    # Report (for most items: the first gold hit); None when they never do.
    first_gold_rank: int | None
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def recalled_at(self, k: int) -> bool:
        return self.first_gold_rank is not None and self.first_gold_rank <= k

    @property
    def recalled_at_5(self) -> bool:
        return self.recalled_at(5)

    @property
    def recalled_at_10(self) -> bool:
        return self.recalled_at(10)

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.first_gold_rank if self.first_gold_rank is not None else 0.0


@dataclass(frozen=True)
class RetrievalSummary:
    """Recall and MRR over a set of scored items: one kind's, or the whole
    run's. Every rate is over items RUN, so a failed call counts against it."""

    run: int
    failed: int
    recalled_at_5: int
    recalled_at_10: int
    reciprocal_rank_sum: float

    @property
    def recall_at_5(self) -> float:
        return self.recalled_at_5 / self.run if self.run else 0.0

    @property
    def recall_at_10(self) -> float:
        return self.recalled_at_10 / self.run if self.run else 0.0

    @property
    def mrr(self) -> float:
        return self.reciprocal_rank_sum / self.run if self.run else 0.0


@dataclass(frozen=True)
class RetrievalReport:
    """`skipped` counts the benchmark's items with no gold evidence, which
    retrieval cannot be scored on; `benchmark_size` keeps a limited run from
    being read as the whole benchmark."""

    benchmark_version: str
    benchmark_size: int
    skipped: int
    items: tuple[ItemRetrieval, ...]
    by_kind: dict[str, RetrievalSummary]
    overall: RetrievalSummary


def scorable(item: BenchmarkItem) -> bool:
    """Whether retrieval can be scored on `item`: it has gold evidence."""
    return bool(item.gold.evidence)


def score_retrieval(
    benchmark: Benchmark, retrieved: Sequence[RetrievedHits]
) -> RetrievalReport:
    """Score every scorable benchmark item that was retrieved for, in
    benchmark order. Results for ids the benchmark does not hold, or for
    items without gold evidence, are ignored."""
    by_id = {r.item_id: r for r in retrieved}
    items = tuple(
        _score_item(item, by_id[item.id])
        for item in benchmark.items if scorable(item) and item.id in by_id
    )
    kinds = sorted({i.kind for i in items})
    return RetrievalReport(
        benchmark_version=benchmark.version,
        benchmark_size=len(benchmark.items),
        skipped=sum(not scorable(item) for item in benchmark.items),
        items=items,
        by_kind={k: _summarise([i for i in items if i.kind == k]) for k in kinds},
        overall=_summarise(items),
    )


def _score_item(item: BenchmarkItem, retrieved: RetrievedHits) -> ItemRetrieval:
    if retrieved.error is not None:
        return ItemRetrieval(item_id=item.id, kind=item.kind, hits=0, first_gold_rank=None,
                             error=retrieved.error)
    gold = {(e.doc_key, e.page) for e in item.gold.evidence}
    uncovered = {e.doc_key for e in item.gold.evidence}
    rank: int | None = None
    for n, hit in enumerate(retrieved.hits, start=1):
        if (hit.doc_key, hit.page) in gold:
            uncovered.discard(hit.doc_key)
            if not uncovered:
                rank = n
                break
    return ItemRetrieval(item_id=item.id, kind=item.kind, hits=len(retrieved.hits),
                         first_gold_rank=rank)


def _summarise(items: Sequence[ItemRetrieval]) -> RetrievalSummary:
    return RetrievalSummary(
        run=len(items),
        failed=sum(i.failed for i in items),
        recalled_at_5=sum(i.recalled_at_5 for i in items),
        recalled_at_10=sum(i.recalled_at_10 for i in items),
        reciprocal_rank_sum=sum(i.reciprocal_rank for i in items),
    )
