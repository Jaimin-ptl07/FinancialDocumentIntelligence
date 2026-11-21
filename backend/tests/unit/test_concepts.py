"""Concept index: the bounded candidate set a report line may map onto."""

import json
import pathlib

from fdi.core.concepts import (
    MAX_CANDIDATES,
    all_concepts,
    build_concept_index,
    normalize,
)

_V42 = (
    pathlib.Path(__file__).resolve().parents[3]
    / "docs" / "architecture" / "field-extraction-schema.v4_2.json"
)


def _schema(fields: list[dict[str, object]]) -> dict[str, object]:
    return {
        "modules": [
            {
                "statement": "assets",
                "retrieval_units": [{"unit_id": "AS", "fields": fields}],
            }
        ]
    }


def _leaf(tag: str, name: str, **kw: object) -> dict[str, object]:
    return {"tag": tag, "field_name": name, "origin": "extracted",
            "field_path": f"assets.{tag}", **kw}


def test_group_exposes_its_tagged_children_as_candidates():
    schema = _schema([
        {
            "field_name": "Fixed assets/PPE Gross Block - Closing",
            "note_aliases": ["property plant and equipment"],
            "fields": [_leaf("GRSLAND", "Land"), _leaf("GRSBLDG", "Buildings")],
        }
    ])
    index = build_concept_index(schema)

    group = index.for_label("Fixed assets/PPE Gross Block - Closing")
    assert group is not None
    assert [c.tag for c in group.candidates] == ["GRSLAND", "GRSBLDG"]
    assert group.statement == "assets"


def test_report_side_alias_reaches_the_same_group():
    """The whole point: the issuer's words, not ours, are what we get handed."""
    schema = _schema([
        {
            "field_name": "Fixed assets/PPE Gross Block - Closing",
            "note_aliases": ["property plant and equipment"],
            "fields": [_leaf("GRSLAND", "Land"), _leaf("GRSBLDG", "Buildings")],
        }
    ])
    index = build_concept_index(schema)

    assert index.for_label("Property, plant and equipment") is index.for_label(
        "Fixed assets/PPE Gross Block - Closing"
    )
    assert index.for_label("nothing like this") is None


def test_intermediate_parents_are_candidates_too():
    """Issuers report at different granularity: one lists "Raw materials",
    another splits it into Imported/Indigenous. Both must be mappable."""
    schema = _schema([
        {
            "field_name": "Inventories",
            "fields": [
                {
                    "tag": "RM", "field_name": "Raw materials", "origin": "derived",
                    "field_path": "assets.RM",
                    "fields": [
                        _leaf("INVTRMI", "Imported"),
                        _leaf("INVTRMD", "Indigenous"),
                    ],
                }
            ],
        }
    ])
    group = build_concept_index(schema).for_label("Inventories")

    assert group is not None
    tags = [c.tag for c in group.candidates]
    assert tags == ["RM", "INVTRMI", "INVTRMD"]
    assert next(c for c in group.candidates if c.tag == "RM").depth == 1
    assert next(c for c in group.candidates if c.tag == "INVTRMI").depth == 2


def test_for_tag_finds_the_siblings_a_tag_competes_with():
    schema = _schema([
        {
            "field_name": "Cash, Bank balance and Bank depo.",
            "fields": [_leaf("CB", "Cash balance"), _leaf("BB", "Bank balance")],
        }
    ])
    group = build_concept_index(schema).for_tag("BB")

    assert group is not None
    assert {c.tag for c in group.candidates} == {"CB", "BB"}
    assert build_concept_index(schema).for_tag("NOSUCH") is None


def test_oversized_group_is_capped_and_reports_what_it_dropped():
    kids = [_leaf(f"T{i}", f"Item {i}") for i in range(MAX_CANDIDATES + 5)]
    schema = _schema([{"field_name": "Other expenses", "fields": kids}])
    group = build_concept_index(schema).for_label("Other expenses")

    assert group is not None
    assert len(group.candidates) == MAX_CANDIDATES
    assert group.truncated == 5  # never silent


def test_describe_renders_a_mapping_prompt_line():
    schema = _schema([
        {
            "field_name": "Cash, Bank balance and Bank depo.",
            "fields": [
                _leaf("CB", "Cash balance", note_aliases=["cash and cash equivalents"]),
                _leaf("BB", "Bank balance"),
            ],
        }
    ])
    group = build_concept_index(schema).for_label("Cash, Bank balance and Bank depo.")

    assert group is not None
    assert group.describe() == [
        "CB: Cash balance (also reported as: cash and cash equivalents)",
        "BB: Bank balance",
    ]


def test_malformed_input_degrades_to_empty_and_never_raises():
    assert build_concept_index({}).groups == ()
    assert build_concept_index({"modules": "nope"}).groups == ()
    assert build_concept_index({"modules": [None, 7]}).groups == ()
    assert build_concept_index(_schema([None, 3])).groups == ()  # type: ignore[list-item]
    assert build_concept_index({}).for_label(None) is None
    assert normalize(None) == ""


def test_real_v42_schema_yields_bounded_candidate_sets():
    if not _V42.exists():
        return  # generated artifact
    index = build_concept_index(json.loads(_V42.read_text("utf-8")))

    assert len(index.groups) >= 30
    assert all(len(g.candidates) <= MAX_CANDIDATES for g in index.groups)
    # the PPE block is reachable by the issuer's own wording
    ppe = index.for_label("Property, plant and equipment", statement="assets")
    assert ppe is not None
    assert any(c.tag == "GRSBLDG" for c in ppe.candidates)
    # the narrowest owning group is the PPE block, not "Non-current assets"
    siblings = index.for_tag("GRSBLDG")
    assert siblings is not None
    assert len(siblings.candidates) < 12
    assert any(c.tag == "GRSLAND" for c in siblings.candidates)


def test_definition_disambiguates_a_catch_all_sibling():
    """Names alone cannot decide where an issuer's "Computer equipment" goes;
    the definition states what a bucket includes AND excludes."""
    schema = _schema([
        {
            "field_name": "Fixed assets/PPE Gross Block - Closing",
            "fields": [
                _leaf("GRSPNE", "Plant and equipment",
                      definition="Machinery and operating equipment, including "
                                 "computer hardware. Excludes land and buildings."),
                _leaf("GRSOTTA", "Other tangible assets"),
            ],
        }
    ])
    group = build_concept_index(schema).for_label(
        "Fixed assets/PPE Gross Block - Closing"
    )

    assert group is not None
    plant = next(c for c in group.candidates if c.tag == "GRSPNE")
    assert "Excludes land and buildings" in plant.describe()
    # a concept with no recorded ambiguity carries no definition, and that is fine
    assert next(c for c in group.candidates if c.tag == "GRSOTTA").definition == ""


def test_all_concepts_includes_the_top_level_parents_groups_leave_out():
    """REGRESSION. A group holds a parent's DESCENDANTS, so a root concept is a
    group *key* but sits in no group's candidate list. Mapping the face against
    group candidates alone left 15 of TCS's 28 face rows unmapped -- including
    'Revenue from operations' and every waterfall total."""
    schema = _schema([
        {"tag": "RFOGRS", "field_name": "Gross Revenue from Operations",
         "origin": "derived", "field_path": "pl.RFOGRS",
         "fields": [_leaf("RFSP", "Sale of Products"),
                    _leaf("RFSS", "Sale of Services")]},
    ])

    tags = {c.tag for c in all_concepts(schema)}
    assert tags == {"RFOGRS", "RFSP", "RFSS"}

    # the parent is precisely what the group-candidate route omits
    group_tags = {c.tag for g in build_concept_index(schema).groups for c in g.candidates}
    assert "RFOGRS" not in group_tags


def test_all_concepts_dedupes_and_survives_junk():
    assert all_concepts({}) == ()
    assert all_concepts({"modules": "no"}) == ()
    assert all_concepts({"modules": [None, {"retrieval_units": [None, 3]}]}) == ()


def test_real_v43_pl_exposes_every_tagged_concept():
    path = _V42.parent / "field-extraction-schema.v4_3-pl.json"
    if not path.exists():
        return
    concepts = all_concepts(json.loads(path.read_text("utf-8")))
    tags = [c.tag for c in concepts]
    assert len(tags) == len(set(tags))          # deduped
    assert "RFOGRS" in tags and "PBMI" in tags  # top-level parents present


def test_real_v42_carries_definitions_for_ambiguous_concepts():
    if not _V42.exists():
        return
    index = build_concept_index(json.loads(_V42.read_text("utf-8")))
    defined = [c for g in index.groups for c in g.candidates if c.definition]
    assert defined, "expected curated definitions in the generated schema"


def test_aliases_are_read_under_either_schema_generation():
    """v4.3 names this `note_aliases`; the v3.2 seed `load_schema` still serves
    by default names it `aliases` -- 190 of them. Reading only one key left the
    FREE lexical tier permanently silent against the other generation, sending
    every label to the paid tier."""
    from fdi.core.concepts import all_concepts

    v43 = {"modules": [{"module_id": "pl", "statement": "pl", "retrieval_units": [
        {"unit_id": "RU", "fields": [
            {"tag": "TCEXP", "field_name": "Travel", "origin": "extracted",
             "note_aliases": ["travel expenses"]}]}]}]}
    v32 = {"modules": [{"module_id": "pl", "statement": "pl", "retrieval_units": [
        {"unit_id": "RU", "fields": [
            {"tag": "TCEXP", "field_name": "Travel", "origin": "extracted",
             "aliases": ["travel expenses"]}]}]}]}

    assert all_concepts(v43)[0].aliases == ("travel expenses",)
    assert all_concepts(v32)[0].aliases == ("travel expenses",)


def test_the_v4_3_alias_key_wins_when_a_node_carries_both():
    from fdi.core.concepts import all_concepts

    both = {"modules": [{"module_id": "pl", "statement": "pl", "retrieval_units": [
        {"unit_id": "RU", "fields": [
            {"tag": "TCEXP", "field_name": "Travel", "origin": "extracted",
             "note_aliases": ["new"], "aliases": ["old"]}]}]}]}
    assert all_concepts(both)[0].aliases == ("new",)


def test_a_malformed_alias_list_degrades_to_empty_never_raises():
    from fdi.core.concepts import all_concepts

    bad = {"modules": [{"module_id": "pl", "statement": "pl", "retrieval_units": [
        {"unit_id": "RU", "fields": [
            {"tag": "TCEXP", "field_name": "Travel", "origin": "extracted",
             "note_aliases": "not a list", "aliases": [1, 2, None]}]}]}]}
    assert all_concepts(bad)[0].aliases == ()
