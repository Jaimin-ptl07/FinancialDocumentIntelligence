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


def test_a_reused_taxonomy_code_yields_two_addressable_concepts():
    schema = build_schema(_norm_doc())
    # WCL appears twice in the sheet's Liabilities. This used to assert that
    # BOTH nodes kept the tag `WCL` and were told apart by field_path alone --
    # which was the bug: nothing downstream is keyed by field_path, so the
    # second concept was unreachable. Both must now be addressable BY TAG.
    leaves = _all_leaves(schema)
    wcl = [lf for lf in leaves if str(lf.get("tag", "")).startswith("WCL")
           and lf.get("field_name", "").startswith(("Working capital", "Buyers"))]
    assert len(wcl) >= 2
    assert len({lf["tag"] for lf in wcl}) == len(wcl)
    assert len({lf["field_path"] for lf in wcl}) == len(wcl)


def test_no_tag_is_defined_twice_anywhere_in_the_schema():
    tags = [lf["tag"] for lf in _all_leaves(build_schema(_norm_doc())) if lf.get("tag")]
    assert len(tags) == len(set(tags))


def test_all_field_paths_globally_unique():
    schema = build_schema(_norm_doc())
    paths = [lf["field_path"] for lf in _all_leaves(schema)]
    assert len(paths) == len(set(paths))


def test_a_tag_the_taxonomy_reuses_is_made_unique():
    """REGRESSION. The sheet uses `WCL` twice -- working-capital loans (children
    WCLI/WCLB/WCLO) and Buyers Credit (children STBYCRD/LTBYCRD). Only
    `field_path` was disambiguated, so both concepts shared one tag and every
    tag-keyed structure downstream (by_tag, all_concepts, for_tag, XBRL) kept
    the first and silently discarded the second: it could never be extracted."""
    schema = build_schema({"statements": [{"statement": "liabilities", "rows": [
        {"block_id": "B-3", "tag": "WCL", "particulars": "Working capital loans",
         "sr_no": "1.2", "origin": "extracted"},
        {"block_id": "B-3", "tag": "WCL", "particulars": "Buyers Credit",
         "sr_no": "1.3", "origin": "extracted"},
    ]}]})
    fields = schema["modules"][0]["retrieval_units"][0]["fields"]
    tags = [f["tag"] for f in fields]

    assert tags == ["WCL", "WCL2"]
    assert len(tags) == len(set(tags))
    # the customer's own code is never lost
    assert "taxonomy_tag" not in fields[0]
    assert fields[1]["taxonomy_tag"] == "WCL"


def test_a_tag_used_once_is_left_exactly_as_the_customer_wrote_it():
    schema = build_schema({"statements": [{"statement": "assets", "rows": [
        {"block_id": "B-1", "tag": "INVT", "particulars": "Inventories",
         "sr_no": "4.1", "origin": "extracted"},
    ]}]})
    node = schema["modules"][0]["retrieval_units"][0]["fields"][0]
    assert node["tag"] == "INVT" and "taxonomy_tag" not in node
