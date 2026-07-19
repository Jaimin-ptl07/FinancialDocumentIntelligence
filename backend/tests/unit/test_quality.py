import pytest

from fdi.core.quality import (
    AST_VALIDATORS,
    RECONCILIATION_VALIDATORS,
    STRUCTURE_VALIDATORS,
    ast_has_sections,
    balance_sheet_balances,
    cash_flow_ties,
    profit_and_loss_foots,
    sections_have_content,
    tables_are_rectangular,
    title_present,
    validate,
    validate_all,
)

_CLEAN_AST = {
    "schema_version": 1,
    "title": "ACME LTD Annual Report",
    "sections": [
        {"heading": "Directors' Report", "paragraphs": ["The board is pleased."]},
    ],
}


# --- ast_has_sections ---

def test_ast_has_sections_errors_on_empty_sections() -> None:
    issues = ast_has_sections({"sections": []})
    assert issues == [
        {"severity": "error", "code": "ast.no_sections", "message": "AST has no sections."}
    ]


def test_ast_has_sections_clean_on_non_empty_sections() -> None:
    assert ast_has_sections(_CLEAN_AST) == []


def test_ast_has_sections_treats_missing_key_as_empty() -> None:
    assert ast_has_sections({})[0]["code"] == "ast.no_sections"


# --- sections_have_content ---

def test_sections_have_content_warns_on_fully_empty_section() -> None:
    ast = {"sections": [{"heading": None, "paragraphs": []}]}
    issues = sections_have_content(ast)
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert issues[0]["code"] == "ast.empty_section"


def test_sections_have_content_heading_alone_is_not_empty() -> None:
    ast = {"sections": [{"heading": "Auditor's Report", "paragraphs": []}]}
    assert sections_have_content(ast) == []


def test_sections_have_content_preamble_with_paragraphs_is_not_empty() -> None:
    ast = {"sections": [{"heading": None, "paragraphs": ["Preamble text."]}]}
    assert sections_have_content(ast) == []


def test_sections_have_content_reports_one_warning_per_offending_section() -> None:
    ast = {"sections": [
        {"heading": None, "paragraphs": []},
        {"heading": "Real Section", "paragraphs": ["Content."]},
        {"heading": None, "paragraphs": []},
    ]}
    assert len(sections_have_content(ast)) == 2


# --- title_present ---

def test_title_present_warns_on_missing_title() -> None:
    issues = title_present({"title": None})
    assert issues == [
        {"severity": "warning", "code": "ast.missing_title", "message": "Document has no title."}
    ]


def test_title_present_clean_on_real_title() -> None:
    assert title_present(_CLEAN_AST) == []


# --- tables_are_rectangular ---

def test_tables_are_rectangular_warns_on_torn_grid() -> None:
    structure = {"tables": [
        {"column_count": 2, "cells": [{"column_index": 0}, {"column_index": 2}]},
    ]}
    issues = tables_are_rectangular(structure)
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert issues[0]["code"] == "structure.torn_table"


def test_tables_are_rectangular_clean_on_well_formed_table() -> None:
    structure = {"tables": [
        {"column_count": 2, "cells": [{"column_index": 0}, {"column_index": 1}]},
    ]}
    assert tables_are_rectangular(structure) == []


def test_tables_are_rectangular_flags_only_the_torn_table() -> None:
    structure = {"tables": [
        {"column_count": 2, "cells": [{"column_index": 0}, {"column_index": 1}]},
        {"column_count": 1, "cells": [{"column_index": 0}, {"column_index": 1}]},
    ]}
    issues = tables_are_rectangular(structure)
    assert len(issues) == 1


# --- validate ---

def test_validate_clean_ast_is_ok_with_no_issues() -> None:
    report = validate(_CLEAN_AST, AST_VALIDATORS)
    assert report == {
        "schema_version": 1,
        "issues": [],
        "error_count": 0,
        "warning_count": 0,
        "ok": True,
    }


def test_validate_counts_error_and_gates_ok_false() -> None:
    report = validate({"sections": [], "title": "T"}, AST_VALIDATORS)
    assert report["error_count"] == 1
    assert report["ok"] is False


def test_validate_warning_only_still_ok_true() -> None:
    ast = {"title": None, "sections": [{"heading": "H", "paragraphs": ["p"]}]}
    report = validate(ast, AST_VALIDATORS)
    assert report["warning_count"] == 1
    assert report["error_count"] == 0
    assert report["ok"] is True


def test_validate_raises_on_unknown_severity() -> None:
    def _bogus(_artifact: object) -> list[dict[str, object]]:
        return [{"severity": "critical", "code": "x.bogus", "message": "m"}]

    with pytest.raises(ValueError, match="critical"):
        validate(_CLEAN_AST, (_bogus,))


# --- validate_all ---

_CLEAN_STRUCTURE = {
    "tables": [{"column_count": 2, "cells": [{"column_index": 0}, {"column_index": 1}]}],
}

_TORN_STRUCTURE = {
    "tables": [{"column_count": 2, "cells": [{"column_index": 0}, {"column_index": 2}]}],
}


def test_validate_all_merges_two_clean_artifacts_into_one_ok_report() -> None:
    report = validate_all(
        [(_CLEAN_AST, AST_VALIDATORS), (_CLEAN_STRUCTURE, STRUCTURE_VALIDATORS)]
    )
    assert report == {
        "schema_version": 1,
        "issues": [],
        "error_count": 0,
        "warning_count": 0,
        "ok": True,
    }


def test_validate_all_unions_counts_ast_issues_before_structure_issues() -> None:
    sparse_ast = {"sections": [], "title": None}
    report = validate_all(
        [(sparse_ast, AST_VALIDATORS), (_TORN_STRUCTURE, STRUCTURE_VALIDATORS)]
    )
    codes = [issue["code"] for issue in report["issues"]]
    assert codes == ["ast.no_sections", "ast.missing_title", "structure.torn_table"]
    assert report["error_count"] == 1
    assert report["warning_count"] == 2
    assert report["ok"] is False


def test_validate_all_single_pair_matches_validate() -> None:
    assert validate_all([(_CLEAN_AST, AST_VALIDATORS)]) == validate(_CLEAN_AST, AST_VALIDATORS)

    sparse_ast = {"sections": [], "title": "T"}
    assert validate_all([(sparse_ast, AST_VALIDATORS)]) == validate(sparse_ast, AST_VALIDATORS)


def test_validate_all_raises_on_unknown_severity_from_either_pair() -> None:
    def _bogus(_artifact: object) -> list[dict[str, object]]:
        return [{"severity": "critical", "code": "x.bogus", "message": "m"}]

    with pytest.raises(ValueError, match="critical"):
        validate_all([(_CLEAN_AST, AST_VALIDATORS), (_CLEAN_STRUCTURE, (_bogus,))])


def test_validate_all_empty_pairs_is_ok_with_no_issues() -> None:
    assert validate_all([]) == {
        "schema_version": 1,
        "issues": [],
        "error_count": 0,
        "warning_count": 0,
        "ok": True,
    }


# --- reconciliation validators ---


def _cv(
    concept: str,
    value: float,
    *,
    column_header: str = "FY24",
    column_index: int = 0,
    table_index: int = 0,
    row_label: str | None = None,
    page: int | None = 1,
    raw: str | None = None,
) -> dict[str, object]:
    return {
        "concept": concept,
        "row_label": row_label if row_label is not None else concept.lower(),
        "value": value,
        "raw": raw if raw is not None else str(value),
        "column_header": column_header,
        "column_index": column_index,
        "page": page,
        "table_index": table_index,
    }


def _ontology(concept_values: list[dict[str, object]]) -> dict[str, object]:
    return {"schema_version": 2, "mappings": [], "concept_values": concept_values}


# --- balance_sheet_balances ---


def test_balance_sheet_balances_clean_on_equal_values_same_period() -> None:
    ontology = _ontology([
        _cv("TOTAL_ASSETS", 16427.37, row_label="Total Assets", table_index=0),
        _cv(
            "TOTAL_EQUITY_AND_LIABILITIES", 16427.37,
            row_label="Total Equity and Liabilities", table_index=1,
        ),
    ])
    assert balance_sheet_balances(ontology) == []


def test_balance_sheet_balances_warns_on_genuine_mismatch() -> None:
    ontology = _ontology([
        _cv("TOTAL_ASSETS", 16427.37, row_label="Total Assets", table_index=0),
        _cv(
            "TOTAL_EQUITY_AND_LIABILITIES", 16327.37,
            row_label="Total Equity and Liabilities", table_index=1,
        ),
    ])
    issues = balance_sheet_balances(ontology)
    assert len(issues) == 1
    issue = issues[0]
    assert issue["severity"] == "warning"
    assert issue["code"] == "quality.balance_sheet_imbalance"
    message = issue["message"]
    assert isinstance(message, str)
    assert "16427.37" in message
    assert "16327.37" in message
    assert "100.00" in message
    assert "Total Assets" in message
    assert "Total Equity and Liabilities" in message


def test_balance_sheet_balances_within_absolute_tolerance_is_clean() -> None:
    ontology = _ontology([
        _cv("TOTAL_ASSETS", 100.00, row_label="Total Assets"),
        _cv(
            "TOTAL_EQUITY_AND_LIABILITIES", 100.02,
            row_label="Total Equity and Liabilities",
        ),
    ])
    assert balance_sheet_balances(ontology) == []


def test_balance_sheet_balances_relative_tolerance_wider_than_absolute_floor() -> None:
    # diff=50 on ~100,000 figures: 0.1% of 100,000 = 100 > 0.05 absolute floor,
    # and 50 <= 100 -- reconciles under the relative bound, not the absolute one.
    ontology = _ontology([
        _cv("TOTAL_ASSETS", 100000.0, row_label="Total Assets"),
        _cv(
            "TOTAL_EQUITY_AND_LIABILITIES", 100050.0,
            row_label="Total Equity and Liabilities",
        ),
    ])
    assert balance_sheet_balances(ontology) == []


def test_balance_sheet_balances_fallback_equity_plus_liabilities_reconciles() -> None:
    ontology = _ontology([
        _cv("TOTAL_ASSETS", 300.0, row_label="Total Assets"),
        _cv("EQUITY", 200.0, row_label="Equity"),
        _cv("TOTAL_LIABILITIES", 100.0, row_label="Total Liabilities"),
    ])
    assert balance_sheet_balances(ontology) == []


def test_balance_sheet_balances_fallback_equity_plus_liabilities_warns() -> None:
    ontology = _ontology([
        _cv("TOTAL_ASSETS", 300.0, row_label="Total Assets"),
        _cv("EQUITY", 150.0, row_label="Equity"),
        _cv("TOTAL_LIABILITIES", 100.0, row_label="Total Liabilities"),
    ])
    issues = balance_sheet_balances(ontology)
    assert len(issues) == 1
    message = issues[0]["message"]
    assert isinstance(message, str)
    assert "equity + total liabilities" in message.lower()


def test_balance_sheet_balances_disambiguates_via_closest_pairing() -> None:
    ontology = _ontology([
        _cv("TOTAL_ASSETS", 16427.37, row_label="Total Assets", table_index=0),
        _cv(
            "TOTAL_EQUITY_AND_LIABILITIES", 16427.37,
            row_label="Total Equity and Liabilities", table_index=1,
        ),
        _cv(
            "TOTAL_EQUITY_AND_LIABILITIES", 999.00,
            row_label="Some spurious duplicate row", table_index=2,
        ),
    ])
    assert balance_sheet_balances(ontology) == []


def test_balance_sheet_balances_no_data_returns_empty() -> None:
    assert balance_sheet_balances(_ontology([])) == []
    assert balance_sheet_balances({}) == []


# --- profit_and_loss_foots ---


def test_profit_and_loss_foots_clean_on_ground_truth_shaped_numbers() -> None:
    ontology = _ontology([
        _cv("TOTAL_INCOME", 20925.03, row_label="Total Income"),
        _cv("OPERATING_EXPENSE", 18795.89, row_label="Total Expenses"),
        _cv("PROFIT_BEFORE_TAX", 2129.14, row_label="Profit Before Tax"),
    ])
    assert profit_and_loss_foots(ontology) == []


def test_profit_and_loss_foots_warns_on_genuine_mismatch() -> None:
    ontology = _ontology([
        _cv("TOTAL_INCOME", 20925.03, row_label="Total Income"),
        _cv("OPERATING_EXPENSE", 18795.89, row_label="Total Expenses"),
        _cv("PROFIT_BEFORE_TAX", 1000.00, row_label="Profit Before Tax"),
    ])
    issues = profit_and_loss_foots(ontology)
    assert len(issues) == 1
    issue = issues[0]
    assert issue["severity"] == "warning"
    assert issue["code"] == "quality.profit_and_loss_does_not_foot"
    message = issue["message"]
    assert isinstance(message, str)
    assert "1129.14" in message  # diff = 2129.14 - 1000.00
    assert "1000.0" in message


def test_profit_and_loss_foots_no_data_returns_empty() -> None:
    assert profit_and_loss_foots(_ontology([])) == []
    assert profit_and_loss_foots({}) == []


def test_profit_and_loss_foots_missing_one_concept_emits_nothing() -> None:
    ontology = _ontology([
        _cv("TOTAL_INCOME", 20925.03, row_label="Total Income"),
        _cv("OPERATING_EXPENSE", 18795.89, row_label="Total Expenses"),
    ])
    assert profit_and_loss_foots(ontology) == []


# --- cash_flow_ties ---


def test_cash_flow_ties_within_tolerance_of_real_rounding_slack_is_clean() -> None:
    # Ground truth's own real-doc numbers: opening + net_change = 227.89 vs
    # closing 227.90 -- a 0.01 compounding-rounding artifact, within _ABS_TOL.
    ontology = _ontology([
        _cv("OPENING_CASH_BALANCE", 871.50, row_label="Opening Cash"),
        _cv("NET_CHANGE_IN_CASH", -643.61, row_label="Net Change in Cash"),
        _cv("CLOSING_CASH_BALANCE", 227.90, row_label="Closing Cash"),
    ])
    assert cash_flow_ties(ontology) == []


def test_cash_flow_ties_warns_on_genuine_mismatch() -> None:
    ontology = _ontology([
        _cv("OPENING_CASH_BALANCE", 871.50, row_label="Opening Cash"),
        _cv("NET_CHANGE_IN_CASH", -643.61, row_label="Net Change in Cash"),
        _cv("CLOSING_CASH_BALANCE", 500.00, row_label="Closing Cash"),
    ])
    issues = cash_flow_ties(ontology)
    assert len(issues) == 1
    issue = issues[0]
    assert issue["severity"] == "warning"
    assert issue["code"] == "quality.cash_flow_does_not_tie"
    message = issue["message"]
    assert isinstance(message, str)
    assert "500.0" in message


def test_cash_flow_ties_no_data_returns_empty() -> None:
    assert cash_flow_ties(_ontology([])) == []
    assert cash_flow_ties({}) == []


def test_reconciliation_validators_is_a_three_tuple() -> None:
    assert (
        balance_sheet_balances, profit_and_loss_foots, cash_flow_ties,
    ) == RECONCILIATION_VALIDATORS


def test_validate_all_composes_reconciliation_validators() -> None:
    ontology = _ontology([
        _cv("TOTAL_ASSETS", 100.0, row_label="Total Assets"),
        _cv(
            "TOTAL_EQUITY_AND_LIABILITIES", 50.0,
            row_label="Total Equity and Liabilities",
        ),
    ])
    report = validate_all([(ontology, RECONCILIATION_VALIDATORS)])
    codes = [issue["code"] for issue in report["issues"]]
    assert codes == ["quality.balance_sheet_imbalance"]
    assert report["warning_count"] == 1
    assert report["error_count"] == 0
    assert report["ok"] is True  # warnings never gate ok False
