"""Pure value objects for evaluation. No I/O — core."""

from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    given: object
    expected: object


@dataclass(frozen=True)
class Dataset:
    name: str
    version: int
    cases: tuple[EvalCase, ...]


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    score: float


@dataclass(frozen=True)
class EvalOutcome:
    aggregate: float
    results: tuple[CaseResult, ...]


def dataset_to_dict(ds: Dataset) -> dict[str, object]:
    return {
        "name": ds.name,
        "version": ds.version,
        "cases": [
            {"case_id": c.case_id, "given": c.given, "expected": c.expected}
            for c in ds.cases
        ],
    }


def dataset_from_dict(d: dict[str, object]) -> Dataset:
    cases = tuple(
        EvalCase(case_id=str(c["case_id"]), given=c["given"], expected=c["expected"])
        for c in cast(list[dict[str, object]], d["cases"])
    )
    return Dataset(name=str(d["name"]), version=int(cast(int, d["version"])), cases=cases)
