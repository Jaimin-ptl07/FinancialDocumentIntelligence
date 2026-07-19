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


# --- Reconciliation validators (Phase 4) ---
#
# Read `ontology["concept_values"]` (fdi.core.concept_values.map_concept_values,
# folded additively into the `ontology` artifact by OntologyStage v2) and check
# that a statement's own arithmetic holds together: BS balances, P&L foots, CF
# ties. Always "warning" severity, never "error" -- a reconciliation miss is
# very often an extraction/matching artifact (an ambiguous concept match, a
# column-alignment heuristic that guessed wrong), not proof the source
# document itself is wrong. See design spec §2 decisions 6-9.

_ABS_TOL = 0.05
_REL_TOL = 0.001  # 0.1% -- see design spec §2 decision 6


def _reconciles(a: float, b: float) -> bool:
    return abs(a - b) <= max(_ABS_TOL, _REL_TOL * max(abs(a), abs(b)))


def _norm_header(h: object) -> str:
    return h.strip().lower() if isinstance(h, str) else ""


def _numeric_entries(concept_values: list[object], concept: str) -> list[Mapping[str, object]]:
    out: list[Mapping[str, object]] = []
    for e in concept_values:
        if not isinstance(e, Mapping) or e.get("concept") != concept:
            continue
        v = e.get("value")
        if isinstance(v, int | float) and not isinstance(v, bool):
            out.append(e)
    return out


def _period_pairs(
    a_entries: list[Mapping[str, object]], b_entries: list[Mapping[str, object]]
) -> list[tuple[Mapping[str, object], Mapping[str, object]]]:
    """Design spec §2 decision 7: exact `column_header` match first
    (non-blank only); positional `column_index` fallback if that yields
    nothing at all for the whole candidate set."""
    exact = [
        (a, b) for a in a_entries for b in b_entries
        if _norm_header(a.get("column_header")) == _norm_header(b.get("column_header"))
        and _norm_header(a.get("column_header"))
    ]
    if exact:
        return exact
    return [
        (a, b) for a in a_entries for b in b_entries
        if a.get("column_index") == b.get("column_index")
    ]


def _closest(
    pairs: list[tuple[Mapping[str, object], Mapping[str, object]]],
) -> tuple[Mapping[str, object], Mapping[str, object]] | None:
    """Design spec §2 decision 7: the pair minimizing |a.value - b.value| --
    doubles as an implicit disambiguator among multiple candidate rows for a
    concept (a false-positive row's unrelated value will not be closest)."""
    if not pairs:
        return None
    return min(
        pairs,
        key=lambda ab: abs(cast(float, ab[0]["value"]) - cast(float, ab[1]["value"])),
    )


def _combine_pairs(
    pairs: list[tuple[Mapping[str, object], Mapping[str, object]]],
    combine: Callable[[float, float], float],
) -> list[Mapping[str, object]]:
    """Turn period-aligned `(a, b)` pairs into synthetic entries carrying a
    combined `value` and `a`'s period identity (`column_header`/
    `column_index`), so the combination can itself be period-aligned against
    a third concept's entries via `_period_pairs`. `_a`/`_b` keep the source
    entries around for citation in a warning message."""
    out: list[Mapping[str, object]] = []
    for a, b in pairs:
        out.append({
            "value": combine(cast(float, a["value"]), cast(float, b["value"])),
            "column_header": a.get("column_header"),
            "column_index": a.get("column_index"),
            "_a": a,
            "_b": b,
        })
    return out


def _cite(e: Mapping[str, object]) -> str:
    return (
        f"{e['value']} (table {e['table_index']} row {e['row_label']!r}, "
        f"period {e['column_header']!r})"
    )


def balance_sheet_balances(ontology: Mapping[str, object]) -> list[Issue]:
    """Primary: TOTAL_ASSETS vs. TOTAL_EQUITY_AND_LIABILITIES (matches the
    real Indian-Schedule-III document's actual single grand-total row).
    Fallback, only when TOTAL_EQUITY_AND_LIABILITIES has zero matches
    anywhere: TOTAL_ASSETS vs. EQUITY + TOTAL_LIABILITIES summed per aligned
    period first, then compared to assets -- for a differently-formatted
    statement that reports liabilities and equity as two separate grand-total
    lines. Design spec §2 decision 8."""
    cv = ontology.get("concept_values")
    concept_values = cv if isinstance(cv, list) else []
    assets = _numeric_entries(concept_values, "TOTAL_ASSETS")
    tel = _numeric_entries(concept_values, "TOTAL_EQUITY_AND_LIABILITIES")
    pair = _closest(_period_pairs(assets, tel))
    if pair is not None:
        a, b = pair
        if _reconciles(cast(float, a["value"]), cast(float, b["value"])):
            return []
        diff = abs(cast(float, a["value"]) - cast(float, b["value"]))
        return [_issue(
            "warning", "quality.balance_sheet_imbalance",
            f"Total assets {_cite(a)} does not equal total equity and liabilities "
            f"{_cite(b)}; diff={diff:.2f}.",
        )]

    # Fallback: EQUITY + TOTAL_LIABILITIES summed per aligned period.
    equity = _numeric_entries(concept_values, "EQUITY")
    liab = _numeric_entries(concept_values, "TOTAL_LIABILITIES")
    combined = _combine_pairs(_period_pairs(equity, liab), lambda eq, li: eq + li)
    fallback_pair = _closest(_period_pairs(assets, combined))
    if fallback_pair is None:
        return []
    a, computed = fallback_pair
    if _reconciles(cast(float, a["value"]), cast(float, computed["value"])):
        return []
    e = cast(Mapping[str, object], computed["_a"])
    liab_entry = cast(Mapping[str, object], computed["_b"])
    diff = abs(cast(float, a["value"]) - cast(float, computed["value"]))
    return [_issue(
        "warning", "quality.balance_sheet_imbalance",
        f"Total assets {_cite(a)} does not equal equity + total liabilities "
        f"(equity {_cite(e)} + total liabilities {_cite(liab_entry)} = {computed['value']}); "
        f"diff={diff:.2f}.",
    )]


def profit_and_loss_foots(ontology: Mapping[str, object]) -> list[Issue]:
    """total_income - total_expenses (OPERATING_EXPENSE) vs profit_before_tax,
    aligned by period the same way as balance_sheet_balances, extended to a
    3-way closest-triple selection minimizing |(income - expense) - pbt|."""
    cv = ontology.get("concept_values")
    concept_values = cv if isinstance(cv, list) else []
    income = _numeric_entries(concept_values, "TOTAL_INCOME")
    expense = _numeric_entries(concept_values, "OPERATING_EXPENSE")
    pbt = _numeric_entries(concept_values, "PROFIT_BEFORE_TAX")
    combined = _combine_pairs(_period_pairs(income, expense), lambda i, x: i - x)
    triple = _closest(_period_pairs(combined, pbt))
    if triple is None:
        return []
    computed, p = triple
    if _reconciles(cast(float, computed["value"]), cast(float, p["value"])):
        return []
    a = cast(Mapping[str, object], computed["_a"])
    b = cast(Mapping[str, object], computed["_b"])
    diff = abs(cast(float, computed["value"]) - cast(float, p["value"]))
    return [_issue(
        "warning", "quality.profit_and_loss_does_not_foot",
        f"Total income {_cite(a)} minus total expenses {_cite(b)} = {computed['value']}, "
        f"which does not equal profit before tax {_cite(p)}; diff={diff:.2f}.",
    )]


def cash_flow_ties(ontology: Mapping[str, object]) -> list[Issue]:
    """opening_cash_balance + net_change_in_cash vs closing_cash_balance;
    same 3-way pattern as profit_and_loss_foots. Documented: on the real doc
    this validator will have fewer NET_CHANGE_IN_CASH matches than the other
    two concepts (design spec §2 decision 2's normalize_label
    parenthetical-stripping gap) until that synonym set is tuned -- a
    missing match means this validator emits nothing for that document, not
    a false pass or false warning."""
    cv = ontology.get("concept_values")
    concept_values = cv if isinstance(cv, list) else []
    opening = _numeric_entries(concept_values, "OPENING_CASH_BALANCE")
    change = _numeric_entries(concept_values, "NET_CHANGE_IN_CASH")
    closing = _numeric_entries(concept_values, "CLOSING_CASH_BALANCE")
    combined = _combine_pairs(_period_pairs(opening, change), lambda o, c: o + c)
    triple = _closest(_period_pairs(combined, closing))
    if triple is None:
        return []
    computed, cl = triple
    if _reconciles(cast(float, computed["value"]), cast(float, cl["value"])):
        return []
    a = cast(Mapping[str, object], computed["_a"])
    b = cast(Mapping[str, object], computed["_b"])
    diff = abs(cast(float, computed["value"]) - cast(float, cl["value"]))
    return [_issue(
        "warning", "quality.cash_flow_does_not_tie",
        f"Opening cash balance {_cite(a)} plus net change in cash {_cite(b)} = "
        f"{computed['value']}, which does not equal closing cash balance {_cite(cl)}; "
        f"diff={diff:.2f}.",
    )]


RECONCILIATION_VALIDATORS: tuple[Validator, ...] = (
    balance_sheet_balances, profit_and_loss_foots, cash_flow_ties,
)


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
