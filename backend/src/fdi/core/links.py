"""Surface figures (with captions) and footnotes from the enriched OCR artifact as a
linking artifact. Pure — no I/O. Cross-reference resolution (matching an in-text marker
like "see Note 5" to its target) is explicitly deferred — see the design doc §6."""

from collections.abc import Mapping
from typing import cast


def extract_links(ocr: Mapping[str, object]) -> dict[str, object]:
    """Extract figures (captions) and footnote paragraphs from the enriched OCR artifact.

    Args:
        ocr: The enriched OCR artifact (`normalize_ocr` schema_version >= 4 shape),
             with `figures: [{caption, bounding_regions}]` and
             `paragraphs: [{role, content, ...}]`.

    Returns:
        `{"schema_version": 1, "figures": [{"caption": str}], "footnotes": [{"text": str}]}`.
    """
    figures_raw = cast(list[Mapping[str, object]], ocr.get("figures") or [])
    figures = [{"caption": cast(str, f.get("caption", ""))} for f in figures_raw]

    paragraphs_raw = cast(list[Mapping[str, object]], ocr.get("paragraphs") or [])
    footnotes = [
        {"text": cast(str, p.get("content", ""))}
        for p in paragraphs_raw
        if p.get("role") == "footnote"
    ]

    return {"schema_version": 1, "figures": figures, "footnotes": footnotes}
