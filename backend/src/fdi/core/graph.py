from collections.abc import Mapping, Sequence


def _as_source_index(value: object) -> int | None:
    """Degrade anything but a real (non-bool) `int` to `None`. Never raises."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def build_graph(ast: Mapping[str, object]) -> dict[str, object]:
    """Build a typed CONTAINS/READING_ORDER node/edge graph over an AST.

    Walks the AST's title/sections/paragraphs tree and emits one `document`
    node, one `section` node per section, and one `paragraph` node per
    paragraph, with `CONTAINS` hierarchy edges (document->section,
    section->paragraph) and `READING_ORDER` edges between consecutive
    siblings (sections, and paragraphs within the same section).

    Pure, deterministic: node ids are positional (`doc`, `s{i}`, `s{i}p{j}`),
    not UUIDs, so a re-run over the same AST produces a byte-identical graph.

    Every node also carries `source_index` (int | None), read from the AST's
    `title_source_index`/`heading_source_index`/`paragraph_source_indices`
    (positionally, by the same index used to build the node id) -- a
    non-`int`/missing value degrades to `None`, never raises. Paragraph node
    labels are the full paragraph text (no truncation).

    Args:
        ast: AST dict with optional title and sections (as produced by
            `fdi.core.ast.build_ast`).

    Returns:
        Graph dict with schema_version, nodes ({id, type, label,
        source_index}), and edges ({source, target, type}).
    """
    title_raw = ast.get("title")
    title = title_raw if isinstance(title_raw, str) else ""
    title_source_index = _as_source_index(ast.get("title_source_index"))

    nodes: list[dict[str, object]] = [
        {"id": "doc", "type": "document", "label": title, "source_index": title_source_index}
    ]
    edges: list[dict[str, object]] = []

    sections_raw = ast.get("sections")
    sections: list[Mapping[str, object]] = []
    if isinstance(sections_raw, list):
        for entry in sections_raw:
            if isinstance(entry, Mapping):
                sections.append(entry)

    prev_section_id: str | None = None
    for i, section in enumerate(sections):
        sid = f"s{i}"
        heading_raw = section.get("heading")
        heading = heading_raw if isinstance(heading_raw, str) else ""
        heading_source_index = _as_source_index(section.get("heading_source_index"))
        nodes.append({
            "id": sid, "type": "section", "label": heading,
            "source_index": heading_source_index,
        })
        edges.append({"source": "doc", "target": sid, "type": "CONTAINS"})
        if prev_section_id is not None:
            edges.append({"source": prev_section_id, "target": sid, "type": "READING_ORDER"})
        prev_section_id = sid

        paragraphs_raw = section.get("paragraphs")
        paragraphs: list[str] = []
        if isinstance(paragraphs_raw, list):
            for para in paragraphs_raw:
                if isinstance(para, str):
                    paragraphs.append(para)

        indices_raw = section.get("paragraph_source_indices")
        paragraph_source_indices: list[object] = (
            indices_raw if isinstance(indices_raw, list) else []
        )

        prev_para_id: str | None = None
        for j, para in enumerate(paragraphs):
            pid = f"{sid}p{j}"
            para_source_index = (
                _as_source_index(paragraph_source_indices[j])
                if j < len(paragraph_source_indices)
                else None
            )
            nodes.append({
                "id": pid, "type": "paragraph", "label": para,
                "source_index": para_source_index,
            })
            edges.append({"source": sid, "target": pid, "type": "CONTAINS"})
            if prev_para_id is not None:
                edges.append({"source": prev_para_id, "target": pid, "type": "READING_ORDER"})
            prev_para_id = pid

    return {"schema_version": 2, "nodes": nodes, "edges": edges}


def order_reading_chain(
    nodes: Sequence[Mapping[str, object]], edges: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """Order `nodes` by walking their READING_ORDER edges from the chain head
    (a node among `nodes` with no incoming READING_ORDER edge from another
    node in `nodes`) to the tail.

    Pure, defensive: an edge whose type isn't READING_ORDER, or whose source/
    target isn't in `nodes`, is ignored. Multiple disconnected chains are each
    walked and concatenated, heads sorted by id for determinism. A cycle or a
    dangling `next` stops that chain's walk (visited-set guard) rather than
    looping forever; any node never reached this way is appended in id order
    at the end -- never silently dropped. Never raises.
    """
    node_by_id: dict[str, dict[str, object]] = {}
    for n in nodes:
        node_id = n.get("id")
        if isinstance(node_id, str):
            node_by_id[node_id] = dict(n)

    next_of: dict[str, str] = {}
    has_incoming: set[str] = set()
    for e in edges:
        if e.get("type") != "READING_ORDER":
            continue
        source, target = e.get("source"), e.get("target")
        if (
            isinstance(source, str)
            and isinstance(target, str)
            and source in node_by_id
            and target in node_by_id
        ):
            next_of[source] = target
            has_incoming.add(target)

    heads = sorted(nid for nid in node_by_id if nid not in has_incoming)
    ordered: list[dict[str, object]] = []
    visited: set[str] = set()
    for head in heads:
        cur: str | None = head
        while cur is not None and cur not in visited:
            visited.add(cur)
            ordered.append(node_by_id[cur])
            cur = next_of.get(cur)

    for nid in sorted(node_by_id):
        if nid not in visited:
            ordered.append(node_by_id[nid])
            visited.add(nid)

    return ordered
