"""Pure coverage report + XBRL export (M4)."""

from xml.etree import ElementTree as ET

from fdi.core.coverage import coverage_report
from fdi.core.xbrl import calculation_arcs, export_instance

_STATEMENTS = [{
    "sheet": "S", "statement": "assets",
    "rows": [
        {"tag": "A", "origin": "extracted", "excluded": False, "is_section_header": False},
        {"tag": "B", "origin": "extracted", "excluded": False, "is_section_header": False},
        {"tag": "SUB", "origin": "derived", "excluded": False, "is_section_header": False},
        {"tag": "GONE", "origin": "extracted", "excluded": True, "is_section_header": False},
        {"tag": None, "origin": "section", "excluded": False, "is_section_header": True},
    ],
}]


def test_coverage_classifies_found_derived_missing():
    facts = {
        "A": {"value": 10.0, "origin": "extracted"},
        "SUB": {"value": 10.0, "origin": "derived"},
        # B absent, SUB present
    }
    rep = coverage_report(_STATEMENTS, facts)
    t = rep["totals"]
    assert t["expected"] == 3  # A, B, SUB (GONE excluded, section header skipped)
    assert t["found_extracted"] == 1  # A
    assert t["derived_ok"] == 1  # SUB
    assert t["missing_extracted"] == 1  # B
    assert "B" in rep["missing_extracted"]
    assert 0 <= t["completeness_pct"] <= 100


def test_xbrl_instance_well_formed_and_contains_facts():
    facts = {
        "RFSP": {"value": 1234.5, "origin": "extracted"},
        "TOTRE/SOPL": {"value": 99.0, "origin": "derived"},  # slash -> must still be valid XML
        "EMPTY": {"value": None, "origin": "extracted"},  # skipped
    }
    xml = export_instance(facts, period="2024-03-31", entity="urn:fdi:acme")
    root = ET.fromstring(xml)  # parses -> well-formed
    concepts = {el.get("concept") for el in root if el.get("concept")}
    assert "RFSP" in concepts
    assert "TOTRE/SOPL" in concepts  # preserved in attribute despite slash
    assert "EMPTY" not in concepts  # no value -> not exported
    # numeric facts carry context + unit
    rfsp = next(el for el in root if el.get("concept") == "RFSP")
    assert rfsp.get("contextRef") == "C"
    assert rfsp.get("unitRef") == "U-INR"
    assert rfsp.text == "1234.50"


def test_calculation_arcs_from_derived():
    derived = {
        "SUB": {"status": "ok", "derivation": {"method": "sum", "inputs": ["A", "B"],
                                               "explanation": "SUB = sum of A, B"}},
        "BAD": {"status": "unresolved", "derivation": {"method": "none", "inputs": []}},
    }
    arcs = calculation_arcs(derived)
    assert len(arcs) == 1
    assert arcs[0]["parent"] == "SUB"
    assert arcs[0]["children"] == ["A", "B"]
