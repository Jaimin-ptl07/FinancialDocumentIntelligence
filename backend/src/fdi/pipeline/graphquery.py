"""Graph retrieval orchestrator: neighbors/document_structure/reading_order,
each returning GraphStore's raw node/edge shapes enriched with a citation
(doc_id, doc_key, version, heading, page, bounding_regions). A pipeline-layer
service -- imports platform (GraphStore + repository + artifact store) + core
(none directly; the ordering already happened inside GraphStore) -- the same
shape as fdi.pipeline.crossdoc.resolve_entities and fdi.pipeline.retrieval.
resolve_chunk, called by an API route.

Kept separate from fdi.pipeline.retrieval (RT-1's chunk-resolution seam,
which has no reason to import GraphStore) rather than folded into it -- see
design spec §3.5.

Node ids are namespaced `{doc_id}:{bare_id}` (GraphLoadStage, RT-5); the
`provenance` artifact (SP-12) is unchanged and keys on the BARE id --
`_split` recovers `(doc_id, bare_id)` by splitting on the first ':'.

Pre-check (verified against fdi/pipeline/intake.py): `intake_document`
generates `doc_id` via `str(uuid.uuid4())`, which never contains ':' -- so
splitting a namespaced node id on the FIRST ':' always correctly separates
doc_id from bare_id, even though a bare id itself could theoretically
contain further structure (it doesn't today: ids are `doc`/`s{i}`/
`s{i}p{j}`)."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.pipeline.graphstore import GraphStore
from fdi.platform.db.models import Document
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


def _split(node_id: str) -> tuple[str, str] | None:
    if ":" not in node_id:
        return None
    doc_id, bare_id = node_id.split(":", 1)
    return doc_id, bare_id


async def _read_provenance(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str
) -> dict[str, dict[str, object]]:
    """bare node_id -> {"page", "bounding_regions"}, read ONCE per doc_id."""
    run = await repo.latest_run(doc_id, "provenance")
    if run is None or run.artifact_key is None:
        return {}
    body = cast(Mapping[str, object], await store.get_json(run.artifact_key))
    node_prov = body.get("node_provenance")
    out: dict[str, dict[str, object]] = {}
    if isinstance(node_prov, list):
        for rec in node_prov:
            if isinstance(rec, Mapping) and isinstance(rec.get("node_id"), str):
                out[rec["node_id"]] = {
                    "page": rec.get("page"), "bounding_regions": rec.get("bounding_regions"),
                }
    return out


def _cite(
    node: Mapping[str, object], doc: Document | None, prov: Mapping[str, dict[str, object]]
) -> dict[str, object]:
    node_id = node.get("id")
    split = _split(node_id) if isinstance(node_id, str) else None
    entry: dict[str, object] = prov.get(split[1], {}) if split is not None else {}
    return {
        **node,
        "citation": {
            "doc_id": doc.id if doc is not None else None,
            "doc_key": doc.doc_key if doc is not None else None,
            "version": doc.version if doc is not None else None,
            "heading": node.get("label"),
            "page": entry.get("page"),
            "bounding_regions": entry.get("bounding_regions"),
        },
    }


async def neighbors(
    *, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, node_id: str
) -> dict[str, object]:
    split = _split(node_id)
    if split is None:
        return {"schema_version": 1, "node_id": node_id, "neighbors": []}
    doc_id, _bare_id = split

    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    prov = await _read_provenance(repo, store, doc_id)

    raw = graph_store.neighbors(node_id)
    return {
        "schema_version": 1,
        "node_id": node_id,
        "neighbors": [
            {**hit, "node": _cite(cast(Mapping[str, object], hit["node"]), doc, prov)}
            for hit in raw
        ],
    }


async def document_structure(
    *, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, doc_id: str
) -> dict[str, object]:
    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    prov = await _read_provenance(repo, store, doc_id)

    raw = graph_store.document_structure(doc_id)
    document_node = raw.get("document")
    document = (
        _cite(cast(Mapping[str, object], document_node), doc, prov)
        if document_node is not None
        else None
    )
    sections_raw = raw.get("sections")
    sections = (
        [s for s in sections_raw if isinstance(s, Mapping)]
        if isinstance(sections_raw, list)
        else []
    )
    cited_sections = []
    for s in sections:
        paras_raw = s.get("paragraphs")
        paras = (
            [p for p in paras_raw if isinstance(p, Mapping)]
            if isinstance(paras_raw, list)
            else []
        )
        cited_sections.append({
            **_cite(s, doc, prov),
            "paragraphs": [_cite(p, doc, prov) for p in paras],
        })

    return {
        "schema_version": 1, "doc_id": doc_id, "document": document, "sections": cited_sections,
    }


async def reading_order(
    *, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, doc_id: str
) -> dict[str, object]:
    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    prov = await _read_provenance(repo, store, doc_id)

    raw = graph_store.reading_order(doc_id)
    return {
        "schema_version": 1,
        "doc_id": doc_id,
        "sections": [_cite(s, doc, prov) for s in raw],
    }
