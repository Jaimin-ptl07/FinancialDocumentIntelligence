import pytest

from fdi.core.eval_types import Dataset, EvalCase
from fdi.core.metrics import ExactMatch
from fdi.pipeline.evaluation import CandidateRegistry, run_eval


class _Upper:
    name = "upper"
    version = 1
    def predict(self, given: object) -> object:
        return str(given).upper()


def _toy() -> Dataset:
    return Dataset("toy", 1, (
        EvalCase("c1", "a", "A"),   # correct
        EvalCase("c2", "b", "X"),   # wrong
    ))


def test_run_eval_scores_and_aggregates() -> None:
    out = run_eval(_Upper(), _toy(), ExactMatch())
    assert [r.score for r in out.results] == [1.0, 0.0]
    assert out.aggregate == 0.5


def test_empty_dataset_aggregates_zero() -> None:
    assert run_eval(_Upper(), Dataset("e", 1, ()), ExactMatch()).aggregate == 0.0


def test_out_of_range_metric_raises() -> None:
    class _Bad:
        name = "bad"
        def score(self, predicted: object, expected: object) -> float:
            return 2.0
    with pytest.raises(ValueError, match="out of range"):
        run_eval(_Upper(), _toy(), _Bad())


def test_candidate_exception_propagates() -> None:
    class _Boom:
        name = "boom"
        version = 1
        def predict(self, given: object) -> object:
            raise RuntimeError("kaboom")
    with pytest.raises(RuntimeError, match="kaboom"):
        run_eval(_Boom(), _toy(), ExactMatch())


def test_registry_rejects_duplicate() -> None:
    r = CandidateRegistry()
    r.register(_Upper())
    with pytest.raises(ValueError, match="already registered"):
        r.register(_Upper())
