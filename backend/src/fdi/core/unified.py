import enum


class Role(enum.StrEnum):
    """Canonical role enum for document elements."""

    TITLE = "title"
    HEADING = "heading"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    FOOTNOTE = "footnote"
    BODY = "body"


def canonical_role(azure_role: str | None) -> Role:
    """Map Azure OCR role to canonical Role enum.

    Args:
        azure_role: Role string from Azure OCR, or None.

    Returns:
        Canonical Role enum. Unknown or None values map to BODY.
    """
    if azure_role is None:
        return Role.BODY

    mapping = {
        "title": Role.TITLE,
        "sectionHeading": Role.HEADING,
        "pageHeader": Role.HEADER,
        "pageFooter": Role.FOOTER,
        "pageNumber": Role.PAGE_NUMBER,
        "footnote": Role.FOOTNOTE,
    }
    return mapping.get(azure_role, Role.BODY)
