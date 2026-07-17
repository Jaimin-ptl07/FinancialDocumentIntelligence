"""Quality validation: pure validators over a document's own artifacts, checking
for internal defects (not correctness against ground truth -- see fdi.core.metrics
/ SP-3 for that comparison). No I/O; deterministic; validators compose via
`validate`."""

from collections.abc import Callable, Mapping, Sequence
from typing import cast

Issue = dict[str, object]
Validator = Callable[[Mapping[str, object]], list[Issue]]

_KNOWN_SEVERITIES = {"error", "warning"}


def _issue(severity: str, code: str, message: str) -> Issue:
    return {"severity": severity, "code": code, "message": message}


def ast_has_sections(ast: Mapping[str, object]) -> list[Issue]:
    sections = cast(list[object], ast.get("sections", []))
    if not sections:
        return [_issue("error", "ast.no_sections", "AST has no sections.")]
    return []


def sections_have_content(ast: Mapping[str, object]) -> list[Issue]:
    sections = cast(list[Mapping[str, object]], ast.get("sections", []))
    issues: list[Issue] = []
    for index, section in enumerate(sections):
        paragraphs = cast(list[object], section.get("paragraphs", []))
        if section.get("heading") is None and not paragraphs:
            issues.append(_issue(
                "warning",
                "ast.empty_section",
                f"Section at index {index} has no heading and no paragraphs.",
            ))
    return issues


def title_present(ast: Mapping[str, object]) -> list[Issue]:
    if ast.get("title") is None:
        return [_issue("warning", "ast.missing_title", "Document has no title.")]
    return []


def tables_are_rectangular(structure: Mapping[str, object]) -> list[Issue]:
    tables = cast(list[Mapping[str, object]], structure.get("tables", []))
    issues: list[Issue] = []
    for index, table in enumerate(tables):
        column_count = cast(int, table.get("column_count", 0))
        cells = cast(list[Mapping[str, object]], table.get("cells", []))
        torn = any(cast(int, cell.get("column_index", 0)) >= column_count for cell in cells)
        if torn:
            issues.append(_issue(
                "warning",
                "structure.torn_table",
                f"Table at index {index} has a cell referencing a column_index "
                f">= its column_count ({column_count}).",
            ))
    return issues


AST_VALIDATORS: tuple[Validator, ...] = (ast_has_sections, sections_have_content, title_present)
STRUCTURE_VALIDATORS: tuple[Validator, ...] = (tables_are_rectangular,)


def validate_all(
    pairs: Sequence[tuple[Mapping[str, object], Sequence[Validator]]],
) -> dict[str, object]:
    issues: list[Issue] = [
        issue
        for artifact, validators in pairs
        for validator in validators
        for issue in validator(artifact)
    ]
    for issue in issues:
        if issue["severity"] not in _KNOWN_SEVERITIES:
            raise ValueError(f"unknown issue severity: {issue['severity']!r}")
    error_count = sum(1 for issue in issues if issue["severity"] == "error")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    return {
        "schema_version": 1,
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
        "ok": error_count == 0,
    }


def validate(artifact: Mapping[str, object], validators: Sequence[Validator]) -> dict[str, object]:
    """Single-artifact convenience wrapper: `validate_all` generalized to one pair."""
    return validate_all([(artifact, validators)])
