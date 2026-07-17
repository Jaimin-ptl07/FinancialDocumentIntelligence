import pytest

from fdi.core.unified import Role, canonical_role


@pytest.mark.parametrize(("azure", "expected"), [
    ("title", Role.TITLE),
    ("sectionHeading", Role.HEADING),
    ("pageHeader", Role.HEADER),
    ("pageFooter", Role.FOOTER),
    ("pageNumber", Role.PAGE_NUMBER),
    ("footnote", Role.FOOTNOTE),
    (None, Role.BODY),
    ("somethingUnknown", Role.BODY),
])
def test_canonical_role(azure: str | None, expected: Role) -> None:
    assert canonical_role(azure) == expected


def test_role_values_are_snake_case_strings() -> None:
    assert Role.PAGE_NUMBER.value == "page_number" and Role.HEADING == "heading"
