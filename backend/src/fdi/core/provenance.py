"""Provenance: trace every SP-9 graph node back to its source OCR paragraph's
page and bounding geometry. Pure, deterministic; no I/O."""

from collections.abc import Mapping

_Candidate = tuple[str, list[dict[str, object]]]


def _ocr_paragraph_candidates(ocr: Mapping[str, object]) -> list[_Candidate]:
    """(content, bounding_regions) pairs from the OCR artifact's paragraphs,
    in document order. A paragraph whose `content` isn't a `str` is dropped
    -- it cannot be matched against a node's `str` label. `bounding_regions`
    degrades to `[]` when absent/malformed (never raises)."""
    candidates: list[_Candidate] = []
    paragraphs_raw = ocr.get("paragraphs")
    if not isinstance(paragraphs_raw, list):
        return candidates
    for para in paragraphs_raw:
        if not isinstance(para, Mapping):
            continue
        content = para.get("content")
        if not isinstance(content, str):
            continue
        regions_raw = para.get("bounding_regions")
        regions: list[dict[str, object]] = []
        if isinstance(regions_raw, list):
            for region in regions_raw:
                if isinstance(region, Mapping):
                    regions.append(dict(region))
        candidates.append((content, regions))
    return candidates


def _page_of(regions: list[dict[str, object]]) -> int | None:
    """The first region's page_number, if present and an int."""
    if not regions:
        return None
    page = regions[0].get("page_number")
    return page if isinstance(page, int) else None


def build_provenance(graph: Mapping[str, object], ocr: Mapping[str, object]) -> dict[str, object]:
    """Resolve every `graph` node to the page + bounding geometry of the OCR
    paragraph it was built from.

    - a "paragraph" node: the FIRST ocr paragraph (document order) whose
      `content` startswith the node's (80-char-truncated) `label`.
    - a "section" node: the FIRST ocr paragraph whose `content` equals the
      node's (untruncated) `label`.
    - the "doc" node (`id == "doc"`): the page of the FIRST ocr paragraph
      with any non-empty `bounding_regions`.

    A node with no match, or whose matched paragraph has empty/unusable
    geometry, is OMITTED from `node_provenance` -- never emitted with a
    null/placeholder page. Ambiguous matches (shared prefixes/duplicate
    headings) resolve to the first in document order -- deterministic, a
    documented thin-slice limitation (spec §2 decision 3 / §6).
    """
    candidates = _ocr_paragraph_candidates(ocr)

    doc_entry: dict[str, object] | None = None
    for _content, regions in candidates:
        page = _page_of(regions)
        if page is not None:
            doc_entry = {"node_id": "doc", "page": page, "bounding_regions": regions}
            break

    nodes_raw = graph.get("nodes")
    nodes: list[Mapping[str, object]] = []
    if isinstance(nodes_raw, list):
        for entry in nodes_raw:
            if isinstance(entry, Mapping):
                nodes.append(entry)

    node_provenance: list[dict[str, object]] = []
    for node in nodes:
        node_id = node.get("id")
        if node_id == "doc":
            if doc_entry is not None:
                node_provenance.append(doc_entry)
            continue

        node_type = node.get("type")
        label_raw = node.get("label")
        label = label_raw if isinstance(label_raw, str) else ""

        match: _Candidate | None = None
        if node_type == "paragraph":
            match = next((c for c in candidates if c[0].startswith(label)), None)
        elif node_type == "section":
            match = next((c for c in candidates if c[0] == label), None)
        else:
            continue

        if match is None or not isinstance(node_id, str):
            continue
        _, regions = match
        page = _page_of(regions)
        if page is None:
            continue
        node_provenance.append({"node_id": node_id, "page": page, "bounding_regions": regions})

    return {"schema_version": 1, "node_provenance": node_provenance}
