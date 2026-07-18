from collections.abc import Mapping


def _source_index(block: Mapping[str, object]) -> int | None:
    """`block.get("source_index")`, degraded to `None` on anything but a
    real (non-bool) `int` -- absent, non-int, or a hand-built test fixture
    predating Phase 2 all degrade the same way. Never raises."""
    value = block.get("source_index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def build_ast(unified: Mapping[str, object]) -> dict[str, object]:
    """Transform unified document to naive title/sections/paragraphs tree.

    Walks the unified blocks by role and builds a simple AST with an optional
    title and a list of sections (each with an optional heading and paragraphs).
    Sections are created on heading role; paragraphs accumulate until the next
    heading or end of document. Non-heading paragraph content before the first
    heading becomes a preamble section with heading=None.

    The first `title`-role block becomes `ast["title"]`; every subsequent
    `title`-role block starts a new section instead (heading = its text),
    exactly like a `heading` block -- Azure emits multiple `title`-role
    paragraphs per document, and only the first is the document title.
    `table_cell`-role blocks (paragraphs that are also table cells) are
    excluded entirely -- they're duplicated table content, not prose.

    Args:
        unified: Unified schema dict with blocks.

    Every block's `source_index` (its position in the original
    `ocr["paragraphs"]`, set by `fdi.core.unified.normalize_document`) is
    carried through: `title_source_index` for the title block, per-section
    `heading_source_index` for the block that started it, and per-section
    `paragraph_source_indices` (parallel array to `paragraphs`, same length/
    order). A block missing/with a non-int `source_index` degrades every
    derived index to `None` -- never raises.

    Returns:
        AST dict with schema_version, title (str | None),
        title_source_index (int | None), and sections. Each section has
        heading (str | None), heading_source_index (int | None), paragraphs
        (list of str), and paragraph_source_indices (list of int | None,
        parallel to paragraphs).
    """
    title: str | None = None
    title_source_index: int | None = None
    sections: list[dict[str, object]] = []
    current: dict[str, object] | None = None

    # Extract blocks safely
    blocks_raw = unified.get("blocks")
    blocks: list[dict[str, object]] = []
    if isinstance(blocks_raw, list):
        for block in blocks_raw:
            if isinstance(block, dict):
                blocks.append(block)

    # Walk blocks by role
    for block in blocks:
        role_raw = block.get("role")
        text_raw = block.get("text")

        # Ensure role and text are strings
        role = role_raw if isinstance(role_raw, str) else None
        text = text_raw if isinstance(text_raw, str) else ""

        # Process by role
        if role == "title" and title is None:
            # First title becomes the document title
            title = text
            title_source_index = _source_index(block)
        elif role in {"header", "footer", "page_number", "table_cell"}:
            # Skip noise roles and duplicated table-cell fragments
            pass
        elif role == "heading" or role == "title":
            # Create new section for heading, or for a non-first title
            current = {
                "heading": text,
                "heading_source_index": _source_index(block),
                "paragraphs": [],
                "paragraph_source_indices": [],
            }
            sections.append(current)
        else:
            # Handle body, footnote, and unknown roles as paragraphs
            if current is None:
                # Create preamble section if needed
                current = {
                    "heading": None,
                    "heading_source_index": None,
                    "paragraphs": [],
                    "paragraph_source_indices": [],
                }
                sections.append(current)
            # Append text to current section's paragraphs
            paragraphs = current.get("paragraphs")
            if isinstance(paragraphs, list):
                paragraphs.append(text)
            indices = current.get("paragraph_source_indices")
            if isinstance(indices, list):
                indices.append(_source_index(block))

    return {
        "schema_version": 2,
        "title": title,
        "title_source_index": title_source_index,
        "sections": sections,
    }
