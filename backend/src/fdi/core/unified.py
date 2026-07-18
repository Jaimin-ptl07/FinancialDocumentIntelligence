import contextlib
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
    TABLE_CELL = "table_cell"


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


def _table_cell_paragraph_indices(ocr: Mapping[str, object]) -> set[int]:
    """Collect the OCR paragraph indices that are table cells.

    Walks ``ocr["tables"][].cells[].elements``, parsing every
    ``"/paragraphs/{n}"`` reference into its integer index. Malformed entries
    (wrong shape, non-``/paragraphs/N`` strings, non-integer suffixes) are
    skipped rather than raising -- this is best-effort tagging, not
    validation.

    Args:
        ocr: OCR artifact, optionally with a ``tables`` list.

    Returns:
        Set of paragraph indices referenced by any table cell.
    """
    out: set[int] = set()
    tables = ocr.get("tables")
    if not isinstance(tables, list):
        return out
    for t in tables:
        if not isinstance(t, Mapping):
            continue
        cells = t.get("cells")
        if not isinstance(cells, list):
            continue
        for c in cells:
            if not isinstance(c, Mapping):
                continue
            els = c.get("elements")
            if not isinstance(els, list):
                continue
            for e in els:
                if isinstance(e, str) and e.startswith("/paragraphs/"):
                    with contextlib.suppress(ValueError):
                        out.add(int(e.rsplit("/", 1)[1]))
    return out


def normalize_document(ocr: Mapping[str, object]) -> dict[str, object]:
    """Transform OCR artifact to unified schema.

    Maps OCR output to a stable unified schema with normalized roles
    and simplified structure. Paragraphs that are also referenced as table
    cells (via ``tables[].cells[].elements``) are re-roled to
    ``Role.TABLE_CELL`` instead of their Azure-derived role -- the block is
    kept (not dropped), so ``blocks`` stays 1:1 with ``ocr["paragraphs"]``.

    Args:
        ocr: OCR artifact with content, pages, paragraphs, and tables.

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

    # Paragraph indices that are actually table cells (Phase 1 de-pollution)
    table_cell_indices = _table_cell_paragraph_indices(ocr)

    # Extract and transform paragraphs to blocks
    blocks: list[dict[str, object]] = []
    paragraphs_raw = ocr.get("paragraphs")
    if isinstance(paragraphs_raw, list):
        for i, para in enumerate(paragraphs_raw):
            if isinstance(para, dict):
                text = para.get("content")

                if i in table_cell_indices:
                    role_value: str = Role.TABLE_CELL.value
                else:
                    role_raw = para.get("role")
                    # Convert role to str or None for canonical_role
                    role_str = role_raw if isinstance(role_raw, str) else None
                    role_value = canonical_role(role_str).value

                block: dict[str, object] = {
                    "role": role_value,
                    "text": text,
                }
                blocks.append(block)

    result: dict[str, object] = {
        "schema_version": 2,
        "content": content,
        "page_count": page_count,
        "blocks": blocks,
    }

    return result
