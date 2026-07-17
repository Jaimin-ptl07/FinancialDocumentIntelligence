"""Normalization of Azure Document Intelligence AnalyzeResult to stable subset."""

from collections.abc import Mapping
from typing import cast


def _normalize_bounding_regions(raw: object) -> list[dict[str, object]]:
    """Map Azure `boundingRegions[].pageNumber/polygon` to snake_case."""
    regions_data: list[dict[str, object]] = []
    if isinstance(raw, list):
        for region in raw:
            if isinstance(region, dict):
                regions_data.append(
                    {
                        "page_number": region.get("pageNumber"),
                        "polygon": region.get("polygon"),
                    }
                )
    return regions_data


def _normalize_spans(raw: object) -> list[dict[str, object]]:
    """Map Azure `spans[].offset/length` to snake_case."""
    spans_data: list[dict[str, object]] = []
    if isinstance(raw, list):
        for span in raw:
            if isinstance(span, dict):
                spans_data.append(
                    {
                        "offset": span.get("offset"),
                        "length": span.get("length"),
                    }
                )
    return spans_data


def normalize_ocr(raw: Mapping[str, object]) -> dict[str, object]:
    """Map an AnalyzeResult-shaped dict to a stable subset.

    Transforms camelCase keys from Azure Document Intelligence
    into snake_case and extracts only necessary fields, preserving
    the structural payload (bounding regions, spans, cells, sections)
    needed by downstream stages.

    Args:
        raw: A mapping with AnalyzeResult-like structure
             (content, pages, paragraphs, tables, sections).

    Returns:
        A normalized dict with schema_version, content, pages,
        paragraphs, tables, and sections in snake_case.

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
                    "bounding_regions": _normalize_bounding_regions(para.get("boundingRegions")),
                    "spans": _normalize_spans(para.get("spans")),
                }
                paragraphs_data.append(para_dict)

    # Extract and normalize tables
    tables_data: list[dict[str, object]] = []
    tables_raw = raw.get("tables")
    if isinstance(tables_raw, list):
        for table in tables_raw:
            if isinstance(table, dict):
                # Extract cells list safely
                cells_data: list[dict[str, object]] = []
                cells = table.get("cells")
                if isinstance(cells, list):
                    for cell in cells:
                        if isinstance(cell, dict):
                            row_span = cell.get("rowSpan")
                            column_span = cell.get("columnSpan")
                            cell_content = cell.get("content")
                            cell_dict: dict[str, object] = {
                                "row_index": cell.get("rowIndex"),
                                "column_index": cell.get("columnIndex"),
                                "row_span": row_span if row_span is not None else 1,
                                "column_span": column_span if column_span is not None else 1,
                                "kind": cell.get("kind"),
                                "content": cell_content if cell_content is not None else "",
                            }
                            cells_data.append(cell_dict)

                table_dict: dict[str, object] = {
                    "row_count": table.get("rowCount"),
                    "column_count": table.get("columnCount"),
                    "bounding_regions": _normalize_bounding_regions(table.get("boundingRegions")),
                    "cells": cells_data,
                    "spans": _normalize_spans(table.get("spans")),
                }
                tables_data.append(table_dict)

    # Extract and normalize sections
    sections_data: list[dict[str, object]] = []
    sections_raw = raw.get("sections")
    if isinstance(sections_raw, list):
        for section in sections_raw:
            if isinstance(section, dict):
                elements = section.get("elements")
                elements_list = cast(list[object], elements) if isinstance(elements, list) else []
                sections_data.append({"elements": elements_list})

    # Extract and normalize figures
    figures_data: list[dict[str, object]] = []
    figures_raw = raw.get("figures")
    if isinstance(figures_raw, list):
        for fig in figures_raw:
            if isinstance(fig, dict):
                caption_raw = fig.get("caption")
                caption_content = ""
                if isinstance(caption_raw, dict):
                    content_val = caption_raw.get("content")
                    caption_content = content_val if isinstance(content_val, str) else ""
                figures_data.append(
                    {
                        "caption": caption_content,
                        "bounding_regions": _normalize_bounding_regions(fig.get("boundingRegions")),
                    }
                )

    # Build result
    result: dict[str, object] = {
        "schema_version": 4,
        "content": content,
        "pages": pages_data,
        "paragraphs": paragraphs_data,
        "tables": tables_data,
        "sections": sections_data,
        "figures": figures_data,
    }

    return result
