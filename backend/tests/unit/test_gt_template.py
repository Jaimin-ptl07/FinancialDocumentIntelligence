"""The template must start every tag as `not_reported` -- the human MOVES the
disclosed ones into `values`, so the disjointness invariant holds by
construction and nobody hand-types 402 tags."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from gt_template import build_template  # noqa: E402

from fdi.core.scoring import parse_groundtruth  # noqa: E402

_SCHEMA = {
    "modules": [
        {
            "module_id": "profit_and_loss",
            "retrieval_units": [
                {
                    "unit_id": "RU_PL",
                    "fields": [
                        {"tag": "RFOGRS", "field_name": "Gross revenue", "origin": "extracted"},
                        {"tag": "TOTFC", "field_name": "Finance cost", "origin": "extracted"},
                        {"tag": "PBTR", "field_name": "Profit before tax", "origin": "derived"},
                        {
                            "tag": "TOTOTING", "field_name": "Other income",
                            "origin": "extracted",
                            "fields": [
                                {"tag": "INTOI", "field_name": "Interest income",
                                 "origin": "extracted"},
                            ],
                        },
                    ],
                }
            ],
        },
        {"module_id": "assets", "retrieval_units": [
            {"unit_id": "RU_BS", "fields": [
                {"tag": "GRSPNE", "field_name": "Plant", "origin": "extracted"}]}]},
    ]
}


def test_every_extractable_tag_of_the_module_starts_as_not_reported():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    assert tpl["values"] == {}
    assert tpl["not_reported"] == ["INTOI", "RFOGRS", "TOTFC", "TOTOTING"]


def test_derived_tags_are_excluded_they_are_computed_not_read():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    assert "PBTR" not in tpl["not_reported"]


def test_nested_group_children_are_reached():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    assert "INTOI" in tpl["not_reported"]


def test_only_the_named_module_is_included():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    assert "GRSPNE" not in tpl["not_reported"]


def test_the_emitted_template_parses_as_valid_ground_truth():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="profit_and_loss",
    )
    gt = parse_groundtruth(tpl)
    assert gt is not None
    assert gt.issuer == "TCS"
    assert gt.values == {}


def test_unknown_module_yields_an_empty_but_valid_template():
    tpl = build_template(
        _SCHEMA, issuer="TCS", fiscal_year="FY2025-26",
        basis="consolidated", statement="nonexistent",
    )
    assert tpl["not_reported"] == []
    assert parse_groundtruth(tpl) is not None


def test_malformed_schema_does_not_raise():
    tpl = build_template(
        {}, issuer="X", fiscal_year="FY", basis="consolidated", statement="pl",
    )
    assert tpl["not_reported"] == []
