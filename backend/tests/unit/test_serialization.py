"""canonical_json underpins Global Constraint 6 (determinism): artifact bodies
must be a pure function of their input."""
from fdi.core.serialization import canonical_json


def test_key_order_does_not_change_output() -> None:
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b


def test_output_is_compact_and_sorted() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_unicode_is_preserved_not_escaped() -> None:
    assert canonical_json({"co": "Reliance ₹"}) == '{"co":"Reliance ₹"}'


def test_repeated_calls_are_byte_identical() -> None:
    payload = {"z": [3, 1, 2], "a": {"nested": True}, "n": None}
    assert canonical_json(payload) == canonical_json(payload)
