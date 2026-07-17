import pytest

from fdi.core.quality import (
    AST_VALIDATORS,
    ast_has_sections,
    sections_have_content,
    tables_are_rectangular,
    title_present,
    validate,
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
