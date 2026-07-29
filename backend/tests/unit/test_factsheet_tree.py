"""tree_from_schema: the nested (not flattened) walk of a v4/v4.1 schema, with
facts attached by tag -- what the taxonomy viewer page renders directly."""

from fdi.core.factsheet import build_factsheet, document_factsheet, tree_from_schema

_SCHEMA = {
    "modules": [
        {
            "key": "module_assets",
            "name": "Assets",
            "statement": "assets",
            "retrieval_units": [
                {
                    "unit_id": "AS",
                    "fields": [
                        {
                            "field_name": "Raw materials",
                            "tag": "RM",
                            "origin": "derived",
                            "role": "line",
                            "derivation": {"method": "sum", "inputs": ["IMP", "IND"]},
                            "fields": [
                                {
                                    "field_name": "Raw materials - Imported",
                                    "tag": "IMP",
                                    "origin": "extracted",
                                    "role": "line",
                                    "field_path": "assets.IMP",
                                },
                                {
                                    "field_name": "Raw materials - Indigenous",
                                    "tag": "IND",
                                    "origin": "extracted",
                                    "role": "line",
                                    "field_path": "assets.IND",
                                },
                            ],
                        },
                        {
                            "field_name": "Not extracted this run",
                            "tag": "MISSING",
                            "origin": "extracted",
                            "role": "line",
                            "field_path": "assets.MISSING",
                        },
                    ],
                }
            ],
        }
    ]
}


def test_tree_preserves_nesting_and_attaches_facts_by_tag():
    facts = {
        "IMP": {"value": 3.0, "confidence": 0.9, "origin": "extracted",
                 "band": "high", "page": 12},
        "IND": {"value": 4.0, "confidence": 0.8, "origin": "extracted",
                 "band": "high", "page": 12},
        "RM": {"value": 7.0, "confidence": 0.8, "origin": "derived", "band": "high"},
    }
    tree = tree_from_schema(_SCHEMA, facts)
    assert len(tree) == 1
    module = tree[0]
    assert module["key"] == "module_assets"
    assert module["name"] == "Assets"
    assert module["statement"] == "assets"
    assert len(module["tree"]) == 2  # RM (with children), MISSING

    rm = module["tree"][0]
    assert rm["tag"] == "RM"
    assert rm["value"] == 7.0
    assert rm["origin"] == "derived"
    assert rm["page"] is None  # a derived node has no single source page
    assert len(rm["children"]) == 2
    imp, ind = rm["children"]
    assert imp["tag"] == "IMP"
    assert imp["value"] == 3.0
    assert imp["page"] == 12
    assert ind["tag"] == "IND"
    assert ind["value"] == 4.0


def test_a_tag_with_no_fact_renders_as_none_not_missing_from_the_tree():
    tree = tree_from_schema(_SCHEMA, {})
    module = tree[0]
    missing_node = module["tree"][1]
    assert missing_node["tag"] == "MISSING"
    assert missing_node["value"] is None
    assert missing_node["confidence"] is None
    assert missing_node["band"] is None


def test_paths_are_unique_and_stable_for_every_node_including_parents():
    tree = tree_from_schema(_SCHEMA, {})
    module = tree[0]
    rm = module["tree"][0]
    assert rm["path"] == "assets.AS.0"
    assert rm["children"][0]["path"] == "assets.AS.0.0"
    assert rm["children"][1]["path"] == "assets.AS.0.1"
    assert module["tree"][1]["path"] == "assets.AS.1"


def test_document_factsheet_carries_page_through_to_the_tree():
    records = [
        {"tag": "IMP", "origin": "extracted", "value": 3.0, "confidence": 0.9,
         "source": {"page": 12, "snippet": "x", "evidence_ids": []}},
        {"tag": "IND", "origin": "extracted", "value": 4.0, "confidence": 0.8,
         "source": {"page": 12, "snippet": "x", "evidence_ids": []}},
    ]
    fs = document_factsheet(_SCHEMA, records)
    assert "statements" in fs
    rm = fs["statements"][0]["tree"][0]
    assert rm["value"] == 7.0  # derived from IMP + IND
    imp = rm["children"][0]
    assert imp["page"] == 12


def test_build_factsheet_alone_has_no_statements_key():
    # tree_from_schema needs the schema, which build_factsheet (unlike
    # document_factsheet) never receives -- statements is assembled one
    # level up.
    fs = build_factsheet(
        [{"statement": "assets", "rows": [{"tag": "IMP", "origin": "extracted"}]}],
        {"IMP": {"value": 1.0, "confidence": 0.9}},
    )
    assert "statements" not in fs
