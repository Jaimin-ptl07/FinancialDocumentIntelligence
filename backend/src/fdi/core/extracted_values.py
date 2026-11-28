"""How an Extracted value is judged and shown (S3) -- the pure half.

A stored read says only what the model returned: a value (`extracted`), an
explicit "not in this Report" (`not_found`), or no answer at all because the
call failed (`failed`). Everything an Invitee sees on top of that is computed
here, when the value is shown, and is never stored (ADR-0004):

* `validate_value` -- the problems a value has against its Field's data type,
  allowed values and validation rules;
* `value_status` -- the status shown: a review (accepted / corrected) wins,
  otherwise failed / not_found, otherwise `needs_review` when the value has a
  problem or its confidence is below the schema's threshold, else `extracted`;
* `compute_derived` -- derived Fields, from the EFFECTIVE values of their
  inputs (a correction carries into every total built on it), computed only
  when every input is present.

Pure (`fdi.core` law): no I/O, deterministic, never raises on any input.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from fdi.core.extraction_schema import NUMERIC_DATA_TYPES, FieldDefinition

READ_STATUSES: Final = frozenset({"extracted", "not_found", "failed"})
REVIEW_ACTIONS: Final = frozenset({"accepted", "corrected"})
VALUE_STATUSES: Final = frozenset({
    "extracted", "not_found", "failed", "needs_review", "accepted", "corrected", "derived",
})
_PERIODS: Final = ("current", "prior")


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v))


def _numbers(value: object) -> list[float] | None:
    """The numbers inside a numeric value -- a scalar or a {current, prior}
    pair (nulls allowed) -- or None when it is not numeric-shaped."""
    if _is_number(value):
        return [float(value)]  # type: ignore[arg-type]
    if isinstance(value, Mapping) and value and set(value) <= set(_PERIODS):
        out: list[float] = []
        for v in value.values():
            if v is None:
                continue
            if not _is_number(v):
                return None
            out.append(float(v))
        return out
    return None


def validate_value(value: object, f: FieldDefinition) -> list[str]:
    """Problems `value` has as a value of `f`. A null value has none -- it is
    `not_found`, judged by `value_status`."""
    if value is None:
        return []
    problems: list[str] = []
    rules = f.validation
    if f.data_type in NUMERIC_DATA_TYPES:
        nums = _numbers(value)
        if nums is None:
            return [f"expected a number, got {type(value).__name__}"]
        if f.data_type == "integer" and any(n != int(n) for n in nums):
            problems.append("expected a whole number")
        lo, hi = rules.get("min"), rules.get("max")
        for n in nums:
            if _is_number(lo) and n < float(lo):  # type: ignore[arg-type]
                problems.append(f"{n:g} is below the minimum {lo}")
            if _is_number(hi) and n > float(hi):  # type: ignore[arg-type]
                problems.append(f"{n:g} is above the maximum {hi}")
            if rules.get("non_negative") is True and n < 0:
                problems.append(f"{n:g} is negative")
        return problems
    if f.data_type == "boolean":
        return [] if isinstance(value, bool) else ["expected true or false"]
    if f.data_type == "list":
        return [] if isinstance(value, list) else ["expected a list"]
    if not isinstance(value, str):
        return [f"expected text, got {type(value).__name__}"]
    rule_values = rules.get("allowed_values")
    allowed = list(f.allowed_values) or (
        [v for v in rule_values if isinstance(v, str)] if isinstance(rule_values, list) else []
    )
    if allowed and value not in allowed:
        problems.append(f"{value!r} is not one of the allowed values")
    pattern = rules.get("pattern")
    if isinstance(pattern, str):
        try:
            if re.fullmatch(pattern, value) is None:
                problems.append("does not match the Field's pattern")
        except re.error:
            problems.append("the Field's pattern is not a valid regular expression")
    return problems


def value_status(
    *,
    read_status: str,
    review_action: str | None,
    problems: Sequence[str],
    confidence: float | None,
    threshold: float,
    required: bool,
) -> str:
    if review_action in REVIEW_ACTIONS:
        return str(review_action)
    if read_status == "failed":
        return "failed"
    if read_status == "not_found":
        return "needs_review" if required else "not_found"
    if problems or confidence is None or confidence < threshold:
        return "needs_review"
    return "extracted"


@dataclass(frozen=True)
class DerivedValue:
    value: object
    inputs: tuple[str, ...]
    missing: tuple[str, ...]


def _combine(method: str, terms: Sequence[tuple[object, float]]) -> object:
    """Σ weight·value (`sum`) or first − rest (`diff`), per period when any
    term is a {current, prior} pair (a scalar counts as the current period).
    None when a needed period is missing."""
    signed = [
        (value, weight if (method == "sum" or i == 0) else -weight)
        for i, (value, weight) in enumerate(terms)
    ]
    if all(_is_number(v) for v, _w in signed):
        return math.fsum(float(v) * w for v, w in signed)  # type: ignore[arg-type]
    out: dict[str, float | None] = {}
    for period in _PERIODS:
        total = 0.0
        for v, w in signed:
            part = v.get(period) if isinstance(v, Mapping) else (v if period == "current" else None)
            if not _is_number(part):
                total = math.nan
                break
            total += float(part) * w  # type: ignore[arg-type]
        out[period] = None if math.isnan(total) else total
    return out if out["current"] is not None else None


def compute_derived(
    fields: Sequence[FieldDefinition], values_by_tag: Mapping[str, object]
) -> dict[str, DerivedValue]:
    """Every derived Field's value by Field id. `values_by_tag` holds the
    effective value of each non-derived tagged Field (None when absent).
    A derived input may itself be derived; a cycle or a missing input leaves
    the Field uncomputed (value None, `missing` naming what was absent)."""
    by_tag = {f.tag: f for f in fields if f.tag}
    memo: dict[str, object] = {}
    out: dict[str, DerivedValue] = {}
    visiting: set[str] = set()

    def value_of(tag: str) -> object:
        f = by_tag.get(tag)
        if f is None:
            return None
        if f.origin != "derived":
            return values_by_tag.get(tag)
        return resolve(f).value

    def resolve(f: FieldDefinition) -> DerivedValue:
        if f.field_id in out:
            return out[f.field_id]
        if f.field_id in visiting or f.derivation is None or not f.derivation.inputs:
            result = DerivedValue(value=None, inputs=(), missing=())
            out[f.field_id] = result
            return result
        visiting.add(f.field_id)
        terms: list[tuple[object, float]] = []
        missing: list[str] = []
        for tag, weight in f.derivation.inputs:
            v = memo[tag] if tag in memo else value_of(tag)
            memo[tag] = v
            if v is None or _numbers(v) is None:
                missing.append(tag)
            terms.append((v, weight))
        visiting.discard(f.field_id)
        value = None if missing else _combine(f.derivation.method, terms)
        result = DerivedValue(
            value=value, inputs=tuple(t for t, _w in f.derivation.inputs),
            missing=tuple(missing),
        )
        out[f.field_id] = result
        return result

    for f in fields:
        if f.origin == "derived":
            resolve(f)
    return out
