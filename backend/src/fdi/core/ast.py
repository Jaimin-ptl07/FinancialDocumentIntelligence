from collections.abc import Mapping, Sequence
from typing import cast


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


def _heading_index_map(sections: Sequence[Mapping[str, object]]) -> dict[int, int]:
    """`raw_paragraph_index -> ast_section_index`, from each section's
    `heading_source_index` (Phase 2). A section with no/non-int
    `heading_source_index` (e.g. a preamble) contributes no entry."""
    out: dict[int, int] = {}
    for i, s in enumerate(sections):
        hsi = s.get("heading_source_index")
        if isinstance(hsi, int) and not isinstance(hsi, bool):
            out[hsi] = i
    return out


def _parse_ref(el: object) -> tuple[str, int] | None:
    """Parse a `"/kind/N"` Azure element ref into `(kind, N)`. Anything else
    (non-str, wrong shape, non-int suffix, unknown kind) -> None."""
    if not isinstance(el, str):
        return None
    parts = el.strip("/").split("/")
    if len(parts) != 2:
        return None
    kind, num_str = parts
    if kind not in {"paragraphs", "tables", "figures", "sections"}:
        return None
    try:
        num = int(num_str)
    except ValueError:
        return None
    return kind, num


def _flatten(ocr_sections: list[Mapping[str, object]]) -> list[tuple[str, int]]:
    """DFS pre-order over `ocr_sections[0]`'s elements, following `"/sections/N"`
    refs recursively; a section index already on the CURRENT DFS path is not
    re-entered (cycle guard -- a malformed/cyclic artifact degrades to "that
    branch stops early," never an infinite loop). Malformed elements entries
    (non-string, wrong prefix/shape, non-int suffix, out-of-range section
    index) are skipped. Returns [(kind, index), ...] for "paragraphs"/
    "tables"/"figures" refs only ("/sections/N" is walked, not itself
    yielded)."""
    out: list[tuple[str, int]] = []

    def walk(idx: int, path: frozenset[int]) -> None:
        if idx < 0 or idx >= len(ocr_sections):
            return
        section = ocr_sections[idx]
        if not isinstance(section, Mapping):
            return
        elements = section.get("elements")
        if not isinstance(elements, list):
            return
        for el in elements:
            parsed = _parse_ref(el)
            if parsed is None:
                continue
            kind, num = parsed
            if kind == "sections":
                if num not in path:
                    walk(num, path | {num})
                # else: cycle -- refuse to re-enter, branch stops early
            else:
                out.append((kind, num))

    if ocr_sections:
        walk(0, frozenset({0}))
    return out


def _append_ref(sections: list[dict[str, object]], idx: int, key: str, value: object) -> None:
    lst = sections[idx].get(key)
    if isinstance(lst, list):
        lst.append(value)


def attach_table_refs(ast: Mapping[str, object], ocr: Mapping[str, object]) -> dict[str, object]:
    """Additively populate every ast section with `table_refs: list[int]` (raw
    `ocr["tables"]` indices) and `figure_refs: list[{"index": int, "caption":
    str}]`, derived from Azure's authoritative `ocr["sections"]` elements
    hierarchy (root section's `elements`, recursing "/sections/N" refs,
    cycle-guarded).

    Section-boundary tracking is index equality against each existing AST
    section's `heading_source_index` (Phase 2) -- NOT a re-derivation of
    build_ast's role rule. A table/figure ref seen before any section has
    opened attaches to the preamble section (created at index 0 if absent,
    mirroring build_ast's own preamble handling). Never raises; malformed
    elements entries, dangling/cyclic "/sections/N" refs, and out-of-range
    paragraph/table/figure indices are skipped.
    """
    sections_raw = ast.get("sections")
    sections: list[dict[str, object]] = []
    if isinstance(sections_raw, list):
        for entry in sections_raw:
            if isinstance(entry, Mapping):
                sections.append(dict(entry))

    for s in sections:
        s["table_refs"] = []
        s["figure_refs"] = []

    heading_map = _heading_index_map(sections)

    ocr_sections_raw = ocr.get("sections")
    ocr_sections = (
        cast(list[Mapping[str, object]], ocr_sections_raw)
        if isinstance(ocr_sections_raw, list)
        else []
    )

    ocr_tables_raw = ocr.get("tables")
    n_tables = len(ocr_tables_raw) if isinstance(ocr_tables_raw, list) else 0

    ocr_figures_raw = ocr.get("figures")
    ocr_figures = (
        cast(list[Mapping[str, object]], ocr_figures_raw)
        if isinstance(ocr_figures_raw, list)
        else []
    )

    preamble_idx: int | None = None

    def ensure_preamble() -> int:
        nonlocal preamble_idx
        if preamble_idx is not None:
            return preamble_idx
        if sections and sections[0].get("heading") is None:
            preamble_idx = 0
            return preamble_idx
        synthetic: dict[str, object] = {
            "heading": None,
            "heading_source_index": None,
            "paragraphs": [],
            "paragraph_source_indices": [],
            "table_refs": [],
            "figure_refs": [],
        }
        sections.insert(0, synthetic)
        for k in list(heading_map):
            heading_map[k] += 1
        preamble_idx = 0
        return preamble_idx

    current: int | None = None
    for kind, idx in _flatten(ocr_sections):
        if kind == "paragraphs":
            mapped = heading_map.get(idx)
            if mapped is not None:
                current = mapped
        elif kind == "tables":
            if not (0 <= idx < n_tables):
                continue
            target = current if current is not None else ensure_preamble()
            if current is None:
                current = target
            _append_ref(sections, target, "table_refs", idx)
        elif kind == "figures":
            if not (0 <= idx < len(ocr_figures)):
                continue
            target = current if current is not None else ensure_preamble()
            if current is None:
                current = target
            caption_raw = ocr_figures[idx].get("caption")
            caption = caption_raw if isinstance(caption_raw, str) else ""
            _append_ref(sections, target, "figure_refs", {"index": idx, "caption": caption})

    return {**ast, "sections": sections}
