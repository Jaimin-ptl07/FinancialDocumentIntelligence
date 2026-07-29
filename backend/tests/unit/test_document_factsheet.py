"""End-to-end factsheet from the real v4 schema + synthetic extraction records."""

import json
import pathlib

from fdi.core.factsheet import document_factsheet, rows_from_schema

_SCHEMA = json.loads(
    (pathlib.Path(__file__).resolve().parents[3]
     / "docs" / "architecture" / "field-extraction-schema.v4.json").read_text("utf-8")
)


def test_rows_from_real_v4_schema():
    statements = rows_from_schema(_SCHEMA)
    assert len(statements) == 4
    all_tags = [r["tag"] for st in statements for r in st["rows"]]
    assert len(all_tags) >= 400  # ~508 tags
    assert all(r["origin"] in ("extracted", "derived")
               for st in statements for r in st["rows"])


def test_document_factsheet_end_to_end():
    # Feed every extracted tag a value so derivations can fire.
    statements = rows_from_schema(_SCHEMA)
    records = []
    for st in statements:
        for r in st["rows"]:
            if r["origin"] == "extracted":
                records.append({"tag": r["tag"], "origin": "extracted",
                                "value": 1.0, "confidence": 0.9})
    fs = document_factsheet(_SCHEMA, records)
    assert "facts" in fs and "ratios" in fs and "validations" in fs
    assert "coverage" in fs
    cov = fs["coverage"]
    assert cov["totals"]["expected"] >= 400
    assert cov["totals"]["found_extracted"] >= 400
    # some derived subtotals resolve
    assert fs["summary"]["derived_ok"] >= 5
    # bands present for every extracted fact
    assert fs["summary"]["bands"]["high"] >= 1


def test_derived_records_are_ignored_as_seed():
    schema = {"modules": [{"statement": "assets", "retrieval_units": [
        {"unit_id": "BS01", "fields": [
            {"tag": "A", "origin": "extracted", "field_name": "A", "sr_no": "1.1"},
            {"tag": "SUB", "origin": "derived", "field_name": "T (Total of 1.1 to 1.1)",
             "sr_no": None},
        ]},
    ]}]}
    # a stray 'derived' record must NOT seed the fact map; the engine derives it
    records = [
        {"tag": "A", "origin": "extracted", "value": 5.0, "confidence": 0.9},
        {"tag": "SUB", "origin": "derived", "value": 999.0, "confidence": 0.9},
    ]
    fs = document_factsheet(schema, records)
    assert fs["facts"]["SUB"]["value"] == 5.0  # derived from A, not the stray 999
    assert fs["facts"]["SUB"]["origin"] == "derived"
