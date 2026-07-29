"""The reasoned v4.1 schema parses, validates, and derives from its baked-in
formula inputs (not just sr_no ranges)."""

import json
import pathlib

from fdi.core.derivation import derive_facts
from fdi.core.extractor import parse_field_specs
from fdi.core.factsheet import document_factsheet, rows_from_schema
from fdi.core.schema import validate_extraction_schema

_V41 = (
    pathlib.Path(__file__).resolve().parents[3]
    / "docs" / "architecture" / "field-extraction-schema.v4_1.json"
)


def test_derive_uses_explicit_schema_inputs_over_sr_no():
    # A derived row that carries explicit inputs must use them, even with no sr_no.
    statements = [{
        "statement": "x", "rows": [
            {"tag": "A", "origin": "extracted", "block_id": "B"},
            {"tag": "B2", "origin": "extracted", "block_id": "B"},
            {"tag": "SUB", "origin": "derived", "block_id": "B",
             "inputs": ["A", "B2"], "method": "sum"},
            {"tag": "NET", "origin": "derived", "block_id": "B",
             "inputs": ["SUB", "A"], "method": "diff"},
        ],
    }]
    values = {"A": {"value": 10.0, "confidence": 0.9},
              "B2": {"value": 4.0, "confidence": 0.9}}
    out = derive_facts(statements, values)
    assert out["SUB"]["value"] == 14.0
    assert out["NET"]["value"] == 4.0  # SUB(14) - A(10)


def test_v41_schema_present_and_valid():
    if not _V41.exists():
        return  # generated artifact; skip if not built
    schema = json.loads(_V41.read_text("utf-8"))
    assert schema["version"].startswith("4.1")
    assert validate_extraction_schema(schema) == []
    assert len(schema["modules"]) == 4
    specs = parse_field_specs(schema)
    assert len(specs) >= 380  # ~400 extracted fields
    assert all(s.tag for s in specs)


def test_v41_derives_many_subtotals_end_to_end():
    if not _V41.exists():
        return
    schema = json.loads(_V41.read_text("utf-8"))
    statements = rows_from_schema(schema)
    records = [{"tag": r["tag"], "origin": "extracted", "value": 1.0, "confidence": 0.9}
               for st in statements for r in st["rows"] if r["origin"] == "extracted"]
    fs = document_factsheet(schema, records)
    # Far more than the ~6 the old sr_no-only parser managed across all sheets.
    assert fs["summary"]["derived_ok"] >= 50
    # a few known subtotals compute to the expected sums
    assert fs["facts"]["GRSTOTTA"]["value"] == 7.0   # 7 tangible-asset lines
    assert fs["facts"]["TOTEQ"]["value"] == 8.0      # 8 share-capital lines


def test_v41_tree_from_schema_matches_the_four_sheets():
    if not _V41.exists():
        return
    schema = json.loads(_V41.read_text("utf-8"))
    statements = rows_from_schema(schema)
    records = [{"tag": r["tag"], "origin": "extracted", "value": 1.0, "confidence": 0.9,
                "source": {"page": 1, "snippet": "", "evidence_ids": []}}
               for st in statements for r in st["rows"] if r["origin"] == "extracted"]
    fs = document_factsheet(schema, records)
    trees = fs["statements"]
    assert [t["statement"] for t in trees] == [
        "profit_and_loss", "assets", "liabilities", "equity",
    ]
    # every node's path is unique within the whole document
    paths = []

    def collect(nodes):
        for n in nodes:
            paths.append(n["path"])
            collect(n["children"])

    for t in trees:
        collect(t["tree"])
    assert len(paths) == len(set(paths))
    # a known derived subtotal carries its resolved value into the tree, nested
    def find(nodes, tag):
        for n in nodes:
            if n["tag"] == tag:
                return n
            found = find(n["children"], tag)
            if found:
                return found
        return None

    assets_tree = trees[1]["tree"]
    grstotta = find(assets_tree, "GRSTOTTA")
    assert grstotta is not None
    assert grstotta["value"] == 7.0
    assert grstotta["origin"] == "derived"
