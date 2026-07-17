import enum
from collections.abc import Mapping


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


def normalize_document(ocr: Mapping[str, object]) -> dict[str, object]:
    """Transform OCR artifact to unified schema.

    Maps OCR output to a stable unified schema with normalized roles
    and simplified structure.

    Args:
        ocr: OCR artifact with content, pages, and paragraphs.

    Returns:
        Unified schema dict with schema_version, content, page_count, blocks.

    Raises:
        ValueError: If 'content' key is missing.
    """
    # Validate required content field
    if "content" not in ocr:
        raise ValueError("OCR artifact has no content")

    content = ocr["content"]

    # Extract page count safely
    pages_raw = ocr.get("pages")
    page_count = 0
    if isinstance(pages_raw, list):
        page_count = len(pages_raw)

    # Extract and transform paragraphs to blocks
    blocks: list[dict[str, object]] = []
    paragraphs_raw = ocr.get("paragraphs")
    if isinstance(paragraphs_raw, list):
        for para in paragraphs_raw:
            if isinstance(para, dict):
                role_raw = para.get("role")
                # Convert role to str or None for canonical_role
                role_str = role_raw if isinstance(role_raw, str) else None
                text = para.get("content")

                # Map role using canonical_role and get the string value
                canonical = canonical_role(role_str)
                block: dict[str, object] = {
                    "role": canonical.value,
                    "text": text,
                }
                blocks.append(block)

    result: dict[str, object] = {
        "schema_version": 1,
        "content": content,
        "page_count": page_count,
        "blocks": blocks,
    }

    return result
