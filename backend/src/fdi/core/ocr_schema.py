"""Normalization of Azure Document Intelligence AnalyzeResult to stable subset."""

from collections.abc import Mapping


def normalize_ocr(raw: Mapping[str, object]) -> dict[str, object]:
    """Map an AnalyzeResult-shaped dict to a stable subset.

    Transforms camelCase keys from Azure Document Intelligence
    into snake_case and extracts only necessary fields.

    Args:
        raw: A mapping with AnalyzeResult-like structure
             (content, pages, paragraphs, tables).

    Returns:
        A normalized dict with schema_version, content, pages,
        paragraphs, and tables in snake_case.

    Raises:
        ValueError: If 'content' key is missing.
    """
    # Validate required content field
    if "content" not in raw:
        raise ValueError("AnalyzeResult has no content")

    content = raw["content"]

    # Extract and normalize pages
    pages_data: list[dict[str, object]] = []
    pages_raw = raw.get("pages")
    if isinstance(pages_raw, list):
        for page in pages_raw:
            if isinstance(page, dict):
                # Extract lines and words lists safely
                lines = page.get("lines")
                lines_list = lines if isinstance(lines, list) else []

                words = page.get("words")
                words_list = words if isinstance(words, list) else []

                page_dict: dict[str, object] = {
                    "page_number": page.get("pageNumber"),
                    "width": page.get("width"),
                    "height": page.get("height"),
                    "unit": page.get("unit"),
                    "line_count": len(lines_list),
                    "word_count": len(words_list),
                }
                pages_data.append(page_dict)

    # Extract and normalize paragraphs
    paragraphs_data: list[dict[str, object]] = []
    paragraphs_raw = raw.get("paragraphs")
    if isinstance(paragraphs_raw, list):
        for para in paragraphs_raw:
            if isinstance(para, dict):
                para_dict: dict[str, object] = {
                    "role": para.get("role"),  # Defaults to None if absent
                    "content": para.get("content"),
                }
                paragraphs_data.append(para_dict)

    # Extract and normalize tables
    tables_data: list[dict[str, object]] = []
    tables_raw = raw.get("tables")
    if isinstance(tables_raw, list):
        for table in tables_raw:
            if isinstance(table, dict):
                # Extract cells list safely
                cells = table.get("cells")
                cells_list = cells if isinstance(cells, list) else []

                table_dict: dict[str, object] = {
                    "row_count": table.get("rowCount"),
                    "column_count": table.get("columnCount"),
                    "cell_count": len(cells_list),
                }
                tables_data.append(table_dict)

    # Build result
    result: dict[str, object] = {
        "schema_version": 1,
        "content": content,
        "pages": pages_data,
        "paragraphs": paragraphs_data,
        "tables": tables_data,
    }

    return result
