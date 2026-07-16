"""Pure evaluation metrics. score() returns [0,1]; 1.0 is perfect. No I/O — core."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Metric(Protocol):
    name: str
    def score(self, predicted: object, expected: object) -> float: ...


def exact_match(predicted: object, expected: object) -> float:
    return 1.0 if predicted == expected else 0.0


class ExactMatch:
    name = "exact_match"
    def score(self, predicted: object, expected: object) -> float:
        return exact_match(predicted, expected)
