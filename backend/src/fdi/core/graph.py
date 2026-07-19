import sys
from collections.abc import Mapping, Sequence


def _as_source_index(value: object) -> int | None:
    """Degrade anything but a real (non-bool) `int` to `None`. Never raises."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _int_list(value: object) -> list[int]:
    """`value` filtered to real (non-bool) ints, or `[]` for anything else."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]


def _figure_ref_list(value: object) -> list[Mapping[str, object]]:
    """`value` filtered to Mapping entries, or `[]` for anything else."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, Mapping)]


def _raw_to_structure_index(tables: Sequence[object]) -> dict[int, int]:
    """{raw_ocr_table_index: structure_position}; `structure_position` is the
    table's own `enumerate(structure["tables"])` position -- the SAME index
    space `core.retrieval._table_chunks` already uses for its shipped `t{i}`
    chunk ids (positions are counted, not re-packed, past a malformed
    non-Mapping entry, so a later real table's position never shifts).

    Reads an OPTIONAL `source_indices` list per table (only present on a
    Mapping entry); a table with no such key (or a non-Mapping entry)
    defaults to `[i]` (identity) -- true for 100% of tables today (audited
    0/261 merges). Malformed entries skipped; a raw index appearing in two
    tables' `source_indices` is last-write-wins (defensive;
    `merge_cross_page_tables` partitions its input so this should not occur).
    """
    out: dict[int, int] = {}
    for pos, t in enumerate(tables):
        if not isinstance(t, Mapping):
            out[pos] = pos
            continue
        raw = t.get("source_indices")
        if not isinstance(raw, list):
            out[pos] = pos
            continue
        for entry in raw:
            if isinstance(entry, int) and not isinstance(entry, bool):
                out[entry] = pos
    return out


def _table_label(table: Mapping[str, object], i: int) -> str:
    """table["caption"]["content"] if non-blank, else f"Table {i}" -- SAME
    fallback `retrieval.py`'s `_table_context` already uses for table chunks."""
    caption = table.get("caption")
    if isinstance(caption, Mapping):
        content = caption.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return f"Table {i}"


def build_graph(ast: Mapping[str, object], structure: Mapping[str, object]) -> dict[str, object]:
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

    A second, separate pass (appended after the document/section/paragraph
    logic above, which stays byte-for-byte unchanged) additively emits one
    `table` node (id `t{structureIndex}`, matching the already-shipped
    `chunks` artifact's `t{i}` convention exactly) per raw table index in a
    section's `table_refs` (Phase 3b's `fdi.core.ast.attach_table_refs`),
    resolved through an optional-key raw->structure bridge that defaults to
    identity, and one `figure` node (id `f{rawIndex}`, figures are never
    reordered/merged) per entry in `figure_refs` -- each with a `CONTAINS`
    edge from its owning section. No `READING_ORDER` edge ever touches a
    `t*`/`f*` node. `source_index` is always `None` for these two node types
    (they have no position in `ocr["paragraphs"]`). First-section-wins dedup
    if the same raw/structure index is (in principle) referenced from more
    than one section.

    Args:
        ast: AST dict with optional title and sections (as produced by
            `fdi.core.ast.build_ast`/`attach_table_refs`).
        structure: `structure` artifact dict (as produced by
            `fdi.core.structure.merge_cross_page_tables` plus grid
            canonicalization) -- read only for `structure["tables"]`.

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

    structure_tables_raw = structure.get("tables")
    structure_tables: list[object] = (
        structure_tables_raw if isinstance(structure_tables_raw, list) else []
    )
    bridge = _raw_to_structure_index(structure_tables)
    emitted_tables: set[int] = set()
    emitted_figures: set[int] = set()

    for i, section in enumerate(sections):
        sid = f"s{i}"
        for raw_idx in _int_list(section.get("table_refs")):
            t_idx = bridge.get(raw_idx)
            if t_idx is None or t_idx >= len(structure_tables) or t_idx in emitted_tables:
                continue
            table_entry = structure_tables[t_idx]
            if not isinstance(table_entry, Mapping):
                continue
            emitted_tables.add(t_idx)
            tid = f"t{t_idx}"
            nodes.append({
                "id": tid, "type": "table",
                "label": _table_label(table_entry, t_idx), "source_index": None,
            })
            edges.append({"source": sid, "target": tid, "type": "CONTAINS"})

        for fig in _figure_ref_list(section.get("figure_refs")):
            k = fig.get("index")
            if not isinstance(k, int) or isinstance(k, bool) or k in emitted_figures:
                continue
            emitted_figures.add(k)
            caption = fig.get("caption")
            label = caption if isinstance(caption, str) and caption.strip() else f"Figure {k}"
            fid = f"f{k}"
            nodes.append({"id": fid, "type": "figure", "label": label, "source_index": None})
            edges.append({"source": sid, "target": fid, "type": "CONTAINS"})

    return {"schema_version": 3, "nodes": nodes, "edges": edges}


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


def numeric_id_sort_key(doc_id: str, node_id: object) -> tuple[int, str]:
    """Sort key for a table/figure node id BY ITS INTEGER SUFFIX -- NOT a
    lexicographic string sort (`"t10" < "t2"` lexicographically, the exact
    bug class `order_reading_chain`'s own docstring already warns about for
    section ids, here applied to tables/figures). Strips a leading
    `"{doc_id}:"` namespace prefix (if present -- both a namespaced,
    post-`GraphLoadStage` id like `"d1:t10"` and a bare pre-namespace id like
    `"t10"` are accepted) and the node's own one-letter type prefix, then
    parses the remaining digits. Anything that doesn't parse this way sorts
    after every real int (stable, deterministic), tie-broken by the original
    id string. Never raises."""
    id_str = node_id if isinstance(node_id, str) else ""
    prefix = f"{doc_id}:"
    bare = id_str[len(prefix):] if id_str.startswith(prefix) else id_str
    suffix = bare[1:] if bare else ""
    try:
        n = int(suffix)
    except ValueError:
        n = sys.maxsize
    return (n, id_str)
