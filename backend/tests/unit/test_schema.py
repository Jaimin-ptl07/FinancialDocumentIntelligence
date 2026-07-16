"""The artifact-body validator: every stored body must be valid JSON.

json.dumps(allow_nan=True) — the default — emits a bare `NaN`/`Infinity` token,
which is deterministic but NOT valid JSON, so a strict downstream parser rejects
it. For a pipeline extracting financial ratios (0/0 is live), that is a real
interop hazard. canonical_json stays a branch-free pass-through; this validator
runs BEFORE it and rejects non-finite floats loudly, naming where.
"""
import math

import pytest

from fdi.core.schema import NonFiniteFloatError, validate_finite


def test_finite_body_passes() -> None:
    validate_finite({"a": 1, "b": [1.5, -2.0], "c": {"d": "x", "e": None, "f": True}})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_scalar(bad: float) -> None:
    with pytest.raises(NonFiniteFloatError):
        validate_finite(bad)


def test_names_the_path() -> None:
    with pytest.raises(NonFiniteFloatError, match=r"\$\.rows\[1\]\.ratio"):
        validate_finite({"rows": [{"ratio": 1.0}, {"ratio": math.nan}]})
