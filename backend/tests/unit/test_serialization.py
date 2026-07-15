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


def test_nested_dict_keys_are_also_sorted() -> None:
    # Artifact bodies nest (a section's line items, a table's rows). Sorting
    # only the top level would still let inner dict insertion order leak into
    # the bytes, so this pins the recursive behavior explicitly.
    assert (
        canonical_json({"outer_b": 1, "outer_a": {"inner_b": 2, "inner_a": 1}})
        == '{"outer_a":{"inner_a":1,"inner_b":2},"outer_b":1}'
    )
