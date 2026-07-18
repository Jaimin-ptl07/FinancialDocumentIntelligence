"""Provenance: trace every SP-9 graph node back to its source OCR paragraph's
page and bounding geometry. Pure, deterministic; no I/O.

Resolution is a DIRECT INDEX LOOKUP: a `section`/`paragraph` node carries its
origin OCR paragraph's position (`source_index`, threaded from `unified` ->
`ast` -> `graph` by Phase 2), so `ocr["paragraphs"][source_index]` is the
node's own paragraph -- never a text/label match, never ambiguous."""

from collections.abc import Mapping


def _paragraph_regions(
    ocr_paragraphs: list[object], index: object
) -> list[dict[str, object]] | None:
    """`ocr_paragraphs[index]["bounding_regions"]`, or `None` if `index` isn't
    a valid in-range (non-bool) `int`, the paragraph isn't a `Mapping`, or its
    `bounding_regions` is missing/malformed/empty. Never raises."""
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    if index < 0 or index >= len(ocr_paragraphs):
        return None
    para = ocr_paragraphs[index]
    if not isinstance(para, Mapping):
        return None
    regions = para.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return None
    resolved = [dict(r) for r in regions if isinstance(r, Mapping)]
    return resolved or None


def _page_of(regions: list[dict[str, object]]) -> int | None:
    """The first region's page_number, if present and an int."""
    if not regions:
        return None
    page = regions[0].get("page_number")
    return page if isinstance(page, int) else None


def _ocr_paragraphs(ocr: Mapping[str, object]) -> list[object]:
    paragraphs_raw = ocr.get("paragraphs")
    return paragraphs_raw if isinstance(paragraphs_raw, list) else []


def _first_paragraph_with_regions(ocr_paragraphs: list[object]) -> dict[str, object] | None:
    """The FIRST ocr paragraph (document order) with usable `bounding_regions`
    -- the `doc` node's fallback when it has no usable `source_index`. A
    positional scan, not a label match, so it's unaffected by the ambiguity
    bug and kept as-is (spec §2 decision 7)."""
    for i in range(len(ocr_paragraphs)):
        regions = _paragraph_regions(ocr_paragraphs, i)
        if regions is not None and _page_of(regions) is not None:
            return {"bounding_regions": regions, "page": _page_of(regions)}
    return None


def build_provenance(graph: Mapping[str, object], ocr: Mapping[str, object]) -> dict[str, object]:
    """Resolve every `graph` node to the page + bounding geometry of the OCR
    paragraph it was built from, via a direct index lookup into
    `ocr["paragraphs"][node["source_index"]]` -- O(1) per node, no text/label
    matching anywhere in this function.

    A node whose `source_index` is `None`, not a (non-bool) `int`, negative,
    out of range, or whose paragraph is not a `Mapping` or has empty/unusable
    `bounding_regions`, is OMITTED from `node_provenance` -- never emitted
    with a null/placeholder page (unchanged conservative policy).

    The `doc` node (`id == "doc"`) is index-first: if its `source_index`
    resolves to a paragraph with usable geometry, use it directly; otherwise
    fall back to the first OCR paragraph (document order) with any non-empty,
    page-bearing `bounding_regions` -- this fallback scan is positional, not a
    label match, so it predates and is unaffected by the ambiguity fix.
    """
    ocr_paragraphs = _ocr_paragraphs(ocr)

    nodes_raw = graph.get("nodes")
    nodes: list[Mapping[str, object]] = []
    if isinstance(nodes_raw, list):
        for entry in nodes_raw:
            if isinstance(entry, Mapping):
                nodes.append(entry)

    node_provenance: list[dict[str, object]] = []
    for node in nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue

        regions = _paragraph_regions(ocr_paragraphs, node.get("source_index"))
        page = _page_of(regions) if regions is not None else None

        if node_id == "doc" and page is None:
            fallback = _first_paragraph_with_regions(ocr_paragraphs)
            if fallback is not None:
                node_provenance.append({
                    "node_id": "doc",
                    "page": fallback["page"],
                    "bounding_regions": fallback["bounding_regions"],
                })
            continue

        if page is None or regions is None:
            continue
        node_provenance.append({"node_id": node_id, "page": page, "bounding_regions": regions})

    return {"schema_version": 1, "node_provenance": node_provenance}
