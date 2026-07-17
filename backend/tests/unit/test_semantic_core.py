from fdi.core.semantic import SectionType, build_snippet


def test_section_type_values() -> None:
    assert SectionType.BALANCE_SHEET == "BALANCE_SHEET"
    assert set(SectionType) == {
        SectionType.BALANCE_SHEET, SectionType.INCOME_STATEMENT, SectionType.CASH_FLOW,
        SectionType.NOTES, SectionType.MDNA, SectionType.AUDITORS_REPORT,
        SectionType.DIRECTORS_REPORT, SectionType.OTHER,
    }


def test_build_snippet_joins_and_truncates() -> None:
    assert build_snippet(["a", "b", "c"]) == "a\nb\nc"
    assert build_snippet(["x" * 10], max_chars=5) == "xxxxx"


def test_build_snippet_empty() -> None:
    assert build_snippet([]) == ""
