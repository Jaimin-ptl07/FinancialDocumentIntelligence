"""The v4.3 composition law: a parent's derivation.inputs ARE its children."""

import json
import pathlib

import pytest

from fdi.core.concepts import all_concepts
from fdi.core.schema import validate_composition

_DOCS = pathlib.Path(__file__).resolve().parents[3] / "docs" / "architecture"


def _schema(fields: list[dict[str, object]]) -> dict[str, object]:
    return {"modules": [{"statement": "profit_and_loss",
                         "retrieval_units": [{"unit_id": "PL", "fields": fields}]}]}


def _leaf(tag: str) -> dict[str, object]:
    return {"tag": tag, "field_name": tag, "origin": "extracted", "data_type": "decimal"}


def test_a_parent_whose_inputs_are_exactly_its_children_holds():
    assert validate_composition(_schema([
        {"tag": "RFOGRS", "field_name": "Gross Revenue", "origin": "derived",
         "derivation": {"method": "sum", "inputs": ["RFSP", "RFSS"]},
         "fields": [_leaf("RFSP"), _leaf("RFSS")]},
    ])) == []


def test_a_child_the_parent_forgets_to_sum_is_an_error():
    errors = validate_composition(_schema([
        {"tag": "RFOGRS", "field_name": "Gross Revenue", "origin": "derived",
         "derivation": {"method": "sum", "inputs": ["RFSP"]},
         "fields": [_leaf("RFSP"), _leaf("RFSS")]},
    ]))
    assert len(errors) == 1
    assert "children not summed: ['RFSS']" in errors[0]


def test_an_input_that_is_not_a_child_is_an_error():
    """REGRESSION for the AUTOPL001 shape: a concept written as a SIBLING of the
    three concepts it was meant to compose. The formula read correctly; the tree
    did not hold it."""
    errors = validate_composition(_schema([
        {"tag": "AUTOPL001", "field_name": "Raw material consumed", "origin": "derived",
         "derivation": {"method": "sum", "inputs": ["RMIM", "RMID", "RME"]},
         "fields": [_leaf("RMIM")]},
        _leaf("RMID"), _leaf("RME"),
    ]))
    assert len(errors) == 1
    assert "inputs that are not children: ['RME', 'RMID']" in errors[0]


def test_a_derivation_with_no_children_is_an_error():
    """REGRESSION. `EXCHEXP` shipped as origin:derived with `inputs: []` because
    its roll-up was never confirmed. That is not a composition -- and a derived
    concept with nothing to derive from is permanently null."""
    errors = validate_composition(_schema([
        {"tag": "EXCHEXP", "field_name": "Exchange difference", "origin": "derived",
         "derivation": {"method": "sum", "inputs": []}},
    ]))
    assert len(errors) == 1
    assert "no child concepts to compose" in errors[0]


def test_untagged_presentation_groups_are_transparent():
    """A grouping node with no tag is layout, not meaning."""
    assert validate_composition(_schema([
        {"tag": "TOTEXP", "field_name": "Expenses", "origin": "derived",
         "derivation": {"method": "sum", "inputs": ["A", "B"]},
         "fields": [{"field_name": "presentation only",
                     "fields": [_leaf("A"), _leaf("B")]}]},
    ])) == []


def test_waterfall_and_reconciliation_inputs_are_peers_not_children():
    """Only `derivation` is bound to children. PBMI sums PATR/OTCI/TOTPNLDOP,
    which are its PEERS on the face -- constraining it to children would be
    wrong."""
    assert validate_composition(_schema([
        {"tag": "PBMI", "field_name": "Profit before minority interest",
         "origin": "derived", "reported": True,
         "waterfall": {"method": "sum",
                       "inputs": [{"tag": "PATR", "weight": 1},
                                  {"tag": "OTCI", "weight": 1}]}},
        _leaf("PATR"), _leaf("OTCI"),
    ])) == []


def test_a_concept_may_carry_only_one_kind_of_formula():
    errors = validate_composition(_schema([
        {"tag": "X", "field_name": "X", "origin": "derived",
         "derivation": {"method": "sum", "inputs": ["A"]},
         "waterfall": {"method": "sum", "inputs": ["B"]},
         "fields": [_leaf("A")]},
    ]))
    assert any("one formula" in e for e in errors)


def test_a_tag_defined_twice_is_an_error():
    errors = validate_composition(_schema([_leaf("RFSP"), _leaf("RFSP")]))
    assert any("already defined" in e for e in errors)


def test_validation_is_total_on_junk():
    for junk in (None, [], "text", {}, {"modules": "no"},
                 {"modules": [None, {"retrieval_units": [None, 7]}]}):
        assert validate_composition(junk) == []


_MODULES = ["v4_3-pl", "v4_3-assets", "v4_3-liabilities", "v4_3-equity"]


@pytest.mark.parametrize("name", _MODULES)
def test_every_authored_v43_module_holds_the_law(name):
    """The schemas shipped in docs/ must satisfy their own stated invariant."""
    path = _DOCS / f"field-extraction-schema.{name}.json"
    if not path.exists():
        return
    errors = validate_composition(json.loads(path.read_text("utf-8")))
    assert errors == [], f"{name}: {len(errors)} violation(s): {errors[:3]}"


@pytest.mark.parametrize(("name", "statement"), [
    ("v4_3-assets", "assets"),
    ("v4_3-liabilities", "liabilities"),
    ("v4_3-equity", "equity"),
])
def test_an_authored_module_covers_its_whole_taxonomy_sheet(name, statement):
    """Restructuring must not silently drop concepts: every tag the taxonomy
    defines has to survive into v4.3. `WCL` is the one deliberate exception --
    the sheet uses it for two different concepts, and the second is carried as
    WCL2 with the original preserved in `taxonomy_tag`."""
    authored = _DOCS / f"field-extraction-schema.{name}.json"
    generated = _DOCS / "field-extraction-schema.v4_2.json"
    if not (authored.exists() and generated.exists()):
        return

    concepts = all_concepts(json.loads(authored.read_text("utf-8")))
    mine = {c.tag for c in concepts}
    module = next(m for m in json.loads(generated.read_text("utf-8"))["modules"]
                  if m["statement"] == statement)
    taxonomy: set[str] = set()

    def walk(fields):
        for f in fields:
            if f.get("tag"):
                taxonomy.add(f["tag"])
            walk(f.get("fields", []))

    for unit in module["retrieval_units"]:
        walk(unit["fields"])

    assert taxonomy - mine == set(), f"{name} dropped: {sorted(taxonomy - mine)}"


def test_the_renamed_buyers_credit_concept_keeps_the_customers_code():
    path = _DOCS / "field-extraction-schema.v4_3-liabilities.json"
    if not path.exists():
        return
    doc = json.loads(path.read_text("utf-8"))

    found: list[dict] = []

    def walk(fields):
        for f in fields:
            if f.get("tag") == "WCL2":
                found.append(f)
            walk(f.get("fields", []))

    for module in doc["modules"]:
        for unit in module["retrieval_units"]:
            walk(unit["fields"])

    assert len(found) == 1
    assert found[0]["taxonomy_tag"] == "WCL"
    assert "Buyers Credit" in found[0]["field_name"]
