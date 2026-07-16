"""Pure validation of artifact bodies. No I/O — belongs in fdi.core."""

import math
from typing import Final


class NonFiniteFloatError(ValueError):
    """A float in an artifact body is NaN or +/-Inf, so the body is not valid JSON."""


_PRIMITIVES: Final = (str, bool, int, type(None))


def validate_finite(obj: object, _path: str = "$") -> None:
    """Raise NonFiniteFloatError at the first non-finite float, else return None.

    bool is checked before int/float since bool is a subclass of int; it is always
    finite. int is exact. Only float can be NaN/Inf.
    """
    if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int)):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise NonFiniteFloatError(f"{_path} is non-finite: {obj!r}")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            validate_finite(v, f"{_path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            validate_finite(v, f"{_path}[{i}]")
        return
    # Anything else (bytes, sets, custom objects) is not JSON-serialisable anyway.
    raise NonFiniteFloatError(f"{_path} is not a JSON value: {type(obj).__name__}")
