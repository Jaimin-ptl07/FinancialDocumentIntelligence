"""The eval runner + candidate registry. Pure orchestration over core types."""

from typing import Protocol, runtime_checkable

from fdi.core.eval_types import CaseResult, Dataset, EvalOutcome
from fdi.core.metrics import Metric


@runtime_checkable
class Candidate(Protocol):
    name: str
    version: int
    def predict(self, given: object) -> object: ...


class CandidateRegistry:
    def __init__(self) -> None:
        self._c: dict[str, Candidate] = {}
    def register(self, candidate: Candidate) -> None:
        if candidate.name in self._c:
            raise ValueError(f"candidate {candidate.name!r} already registered")
        self._c[candidate.name] = candidate
    def get(self, name: str) -> Candidate:
        return self._c[name]
    def all(self) -> dict[str, Candidate]:
        return dict(self._c)


CANDIDATES = CandidateRegistry()


def run_eval(candidate: Candidate, dataset: Dataset, metric: Metric) -> EvalOutcome:
    results: list[CaseResult] = []
    for case in dataset.cases:
        score = metric.score(candidate.predict(case.given), case.expected)
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"metric {metric.name!r} score out of range: {score}")
        results.append(CaseResult(case.case_id, score))
    aggregate = sum(r.score for r in results) / len(results) if results else 0.0
    return EvalOutcome(aggregate=aggregate, results=tuple(results))
