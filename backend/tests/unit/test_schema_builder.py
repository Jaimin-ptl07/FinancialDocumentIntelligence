"""Pure build of the v4 field-extraction schema from the normalized taxonomy."""

import json
import pathlib

from fdi.core.extractor import parse_field_specs
from fdi.core.schema import validate_extraction_schema
from fdi.core.schema_builder import build_schema
from fdi.core.taxonomy import parse_taxonomy

_REAL = (
    pathlib.Path(__file__).resolve().parents[3]
    / "docs" / "architecture" / "taxonomy" / "taxonomy.raw.json"
)


def _norm_doc():
    return parse_taxonomy(json.loads(_REAL.read_text(encoding="utf-8")))


_MINI = {
    "schema_version": 1,
    "source": "mini",
    "statements": [
        {
            "sheet": "B-1-PROFIT & LOSS",
            "statement": "profit_and_loss",
            "rows": [
                {"block_id": "PL01", "tag": None, "sr_no": None,
                 "particulars": "Revenue From Operations", "flag": None, "origin": "section",
                 "is_new": False, "excluded": False, "is_section_header": True, "depth": None,
                 "redirect_dataset2": None, "redirect_dataset1": None},
                {"block_id": "PL01", "tag": "RFSP", "sr_no": "1.1",
                 "particulars": "Revenue from Sale of Products", "flag": None,
                 "origin": "extracted",
                 "is_new": False, "excluded": False, "is_section_header": False, "depth": 1,
                 "redirect_dataset2": None, "redirect_dataset1": None},
                {"block_id": "PL01", "tag": "RFOGRS", "sr_no": None,
                 "particulars": "Total Gross Revenue (Total of 1.1 to 1.5)", "flag": "derived",
                 "origin": "derived", "is_new": False, "excluded": False,
                 "is_section_header": False, "depth": None,
                 "redirect_dataset2": None, "redirect_dataset1": None},
                {"block_id": "PL01", "tag": "DROP", "sr_no": None, "particulars": "Excluded",
                 "flag": "excluded", "origin": "extracted", "is_new": False, "excluded": True,
                 "is_section_header": False, "depth": None,
                 "redirect_dataset2": None, "redirect_dataset1": None},
            ],
        }
    ],
    "warnings": [],
}


def test_valid_schema_shape():
    schema = build_schema(_MINI)
    assert schema["version"].startswith("4.")
    assert validate_extraction_schema(schema) == []


def test_excluded_rows_omitted():
    schema = build_schema(_MINI)
    dumped = json.dumps(schema)
    assert "DROP" not in dumped


def test_extracted_leaf_carries_tag_and_prompt():
    specs = parse_field_specs(build_schema(_MINI))
    tags = {s.tag for s in specs}
    assert "RFSP" in tags
    assert "RFOGRS" not in tags  # derived: not an extracted FieldSpec
    rfsp = next(s for s in specs if s.tag == "RFSP")
    assert rfsp.origin == "extracted"
    assert rfsp.extraction_prompt
    assert rfsp.field_path  # globally unique key present


def test_real_schema_builds_and_validates():
    schema = build_schema(_norm_doc())
    assert validate_extraction_schema(schema) == []
    specs = parse_field_specs(schema)
    # every extracted leaf has a non-empty tag and a unique field_path
    assert all(s.tag for s in specs)
    paths = [s.field_path for s in specs]
    assert len(paths) == len(set(paths))
    # ~429 extracted tags across the four statements (allow slack)
    assert 380 <= len(specs) <= 470
    # four statement modules
    assert len(schema["modules"]) == 4


def _walk_leaves(fields):
    for f in fields:
        if f.get("node_type") == "group":
            yield from _walk_leaves(f["fields"])
        else:
            yield f


def _all_leaves(schema):
    for m in schema["modules"]:
        for u in m["retrieval_units"]:
            yield from _walk_leaves(u["fields"])


def test_duplicate_tag_gets_unique_field_path():
    schema = build_schema(_norm_doc())
    # WCL appears twice in Liabilities (both derived); walk the tree, not the
    # extracted specs, since derived nodes are absent from parse_field_specs.
    wcl = [lf for lf in _all_leaves(schema) if lf.get("tag") == "WCL"]
    assert len(wcl) >= 2
    assert len({lf["field_path"] for lf in wcl}) == len(wcl)


def test_all_field_paths_globally_unique():
    schema = build_schema(_norm_doc())
    paths = [lf["field_path"] for lf in _all_leaves(schema)]
    assert len(paths) == len(set(paths))
