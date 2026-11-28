"""Extraction schemas as data (S3): parsing, rules, hashing, the standard
schema import -- the pure half (`fdi.core.extraction_schema`)."""

import json
from pathlib import Path

import pytest

from fdi.core.extraction_schema import (
    GroupDefinition,
    SchemaInputError,
    compile_standard_schema,
    definition_hash,
    diff_fields,
    field_from_mapping,
    field_to_mapping,
    resolve_group,
    standard_field_id,
    to_field_spec,
    validate_fields,
)

STANDARD = Path(__file__).resolve().parents[3] / "docs" / "architecture" / \
    "field-extraction-schema.v4_3.json"


def _field(field_id: str = "f1", **raw: object):
    return field_from_mapping({"name": "Capital expenditure", **raw}, field_id=field_id)


def test_a_field_round_trips_through_its_stored_shape() -> None:
    f = _field(description="Purchase of PPE", data_type="decimal", unit="INR crore",
               statement="cash_flow", tag="CAPEX", aliases=["capex"],
               validation={"non_negative": True}, required=True)
    again = field_from_mapping(field_to_mapping(f), field_id="f1")
    assert again == f


def test_blank_instructions_get_a_default_that_names_the_field() -> None:
    f = _field(statement="cash_flow")
    assert "Capital expenditure" in f.instructions
    assert "Cash Flow statement" in f.instructions


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(SchemaInputError) as exc:
        field_from_mapping({"name": "", "data_type": "money", "periods": ["next"],
                            "validation": {"min": "x", "colour": 1}}, field_id="f")
    joined = " | ".join(exc.value.errors)
    for fragment in ('"name"', '"data_type"', '"periods"', '"min"', '"colour"'):
        assert fragment in joined


def test_an_enum_needs_allowed_values() -> None:
    with pytest.raises(SchemaInputError):
        _field(data_type="enum")


def test_a_derivation_is_only_allowed_on_a_derived_field() -> None:
    with pytest.raises(SchemaInputError):
        _field(derivation={"method": "sum", "inputs": ["A"]})


def test_set_rules_catch_duplicates_dangling_inputs_and_cycles() -> None:
    fields = [
        _field("1", tag="A"),
        _field("2", tag="A"),
        field_from_mapping({"name": "T", "tag": "T", "origin": "derived",
                            "derivation": {"inputs": ["A", "MISSING"]}}, field_id="3"),
        field_from_mapping({"name": "X", "tag": "X", "origin": "derived",
                            "derivation": {"inputs": ["Y"]}}, field_id="4"),
        field_from_mapping({"name": "Y", "tag": "Y", "origin": "derived",
                            "derivation": {"inputs": ["X"]}}, field_id="5"),
        _field("6", group="nowhere"),
    ]
    errors = " | ".join(validate_fields(fields, []))
    assert "tag 'A' appears twice" in errors
    assert "'MISSING'" in errors
    assert "cycle" in errors
    assert "group 'nowhere'" in errors


def test_names_may_repeat_because_fields_are_answered_by_tag_or_id() -> None:
    assert validate_fields([_field("1", name="Others"), _field("2", name="Others")], []) == []


def test_the_definition_hash_ignores_judgement_but_not_reading() -> None:
    group = GroupDefinition("g", "G", "query")
    base = _field(group="g")
    same = _field(group="g", required=True, validation={"min": 0})
    reworded = _field(group="g", description="something else")
    assert definition_hash(base, group) == definition_hash(same, group)
    assert definition_hash(base, group) != definition_hash(reworded, group)
    assert definition_hash(base, group) != definition_hash(
        base, GroupDefinition("g", "G", "a different query")
    )


def test_automatic_groups_never_depend_on_sibling_fields() -> None:
    """Adding a Field must not restate any other Field's definition."""
    a = _field("1", statement="cash_flow")
    b = _field("2", name="Dividends paid", statement="cash_flow")
    assert resolve_group(a, {}) == resolve_group(b, {})  # same statement group
    assert resolve_group(a, {}).retrieval_query == "Cash Flow statement"
    loose = _field("3", name="Auditor name", aliases=["statutory auditor"])
    group = resolve_group(loose, {})
    assert group.key == "auto:field:3"
    assert "Auditor name" in group.retrieval_query


def test_a_field_spec_is_answered_under_its_tag_or_id() -> None:
    group = GroupDefinition("g", "G", "query", locator_keywords=("kw",))
    tagged = to_field_spec(_field("1", tag="CAPEX", aliases=["capex"]), group)
    untagged = to_field_spec(_field("2"), group)
    assert tagged.field_path == "CAPEX"
    assert untagged.field_path == "2"
    assert tagged.keywords == ("capex", "kw")
    assert tagged.unit_id == "g"


def test_the_diff_matches_fields_by_identity() -> None:
    old = [_field("1", instructions="Read it."), _field("2", name="Gone")]
    new = [_field("1", instructions="Read it.", description="changed"),
           _field("3", name="New")]
    diff = diff_fields(old, new)
    assert [f.field_id for f in diff.added] == ["3"]
    assert [f.field_id for f in diff.removed] == ["2"]
    assert [(c.field_id, c.changed) for c in diff.changed] == [("1", ("description",))]


# -- the standard schema ------------------------------------------------------


def test_the_standard_document_compiles_to_a_valid_schema() -> None:
    compiled = compile_standard_schema(json.loads(STANDARD.read_text(encoding="utf-8")))
    assert [g.key for g in compiled.groups] == ["PL", "AS", "LI", "EQ", "CF"]
    assert validate_fields(compiled.fields, compiled.groups) == []
    by_tag = {f.tag: f for f in compiled.fields if f.tag}
    # A printed total is READ; a derived node is COMPUTED.
    assert by_tag["TOTEXPR"].origin == "extracted"
    assert by_tag["RFOGRS"].origin == "derived"
    assert by_tag["RFOGRS"].derivation is not None
    assert [t for t, _ in by_tag["RFOGRS"].derivation.inputs] == [
        "RFSP", "RFSS", "RFCC", "RFTG", "OTOR",
    ]
    # An extracted parent's own formula is a cross-check, not a formula.
    assert by_tag["OTCI"].derivation is None
    assert "reconciliation_inputs" in by_tag["OTCI"].extra


def test_a_dangling_standard_formula_is_never_computed() -> None:
    compiled = compile_standard_schema(json.loads(STANDARD.read_text(encoding="utf-8")))
    eoi = next(f for f in compiled.fields if f.tag == "EOI")
    assert eoi.derivation is not None and eoi.derivation.inputs == ()
    assert eoi.extra["unresolved_inputs"] == ["AUTOPL014", "AUTOPL015", "AUTOPL016",
                                              "AUTOPL017"]


def test_standard_field_ids_are_stable_across_imports() -> None:
    doc = json.loads(STANDARD.read_text(encoding="utf-8"))
    first = compile_standard_schema(doc)
    second = compile_standard_schema(doc)
    assert [f.field_id for f in first.fields] == [f.field_id for f in second.fields]
    assert next(f for f in first.fields if f.tag == "PATR").field_id == standard_field_id("PATR")


def test_an_empty_standard_document_is_refused() -> None:
    with pytest.raises(SchemaInputError):
        compile_standard_schema({"modules": []})
