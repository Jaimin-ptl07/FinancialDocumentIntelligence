"""UppercaseCandidate: a trivial candidate that uppercases input."""

from fdi.core.eval_types import Dataset, EvalCase
from fdi.pipeline.evaluation import CANDIDATES


class UppercaseCandidate:
    name = "uppercase"
    version = 1

    def predict(self, given: object) -> object:
        return str(given).upper()


def toy_dataset() -> Dataset:
    """Small fixed dataset for testing and seeding."""
    return Dataset(
        name="toy",
        version=1,
        cases=(
            EvalCase("c1", "hello", "HELLO"),
            EvalCase("c2", "world", "WORLD"),
        ),
    )


CANDIDATES.register(UppercaseCandidate())
