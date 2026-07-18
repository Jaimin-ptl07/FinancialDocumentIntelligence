"""Corpus-level entity resolution orchestrator.

Enumerates every document, reads each one's already-cached `embedding` and
`knowledge` artifacts, clusters documents on embedding similarity, and folds each
cluster into an entity record. A pipeline-layer service that imports platform
(repository + store) and core -- the same shape as fdi.pipeline.intake and
fdi.pipeline.eval_service, called by an API route. pipeline -> platform -> core
is legal under the layers contract. Makes no new embedding call: the vectors are
the SP-13 per-section vectors already stored, mean-pooled by fdi.core.crossdoc.
"""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.crossdoc import cluster, consolidate_entity, document_vector
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def read_artifact(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str, stage: str
) -> Mapping[str, object] | None:
    """The latest `stage` artifact for `doc_id`, or None -- same read path as
    the GET /documents/{id}/artifacts/{stage} route.

    Public (RT-7's fdi.pipeline.entities reuses this for `knowledge`, the same
    "one document's store failure isolates, does not abort the whole scan"
    posture resolve_entities relies on for its corpus-wide loop).

    A store-level read failure (the recorded `artifact_key` no longer resolves
    against THIS store -- e.g. deleted, or from a differently-configured store)
    degrades to "no artifact" rather than aborting the whole corpus scan over
    one document. Same isolate-one-failure posture as `routes._probe`: a scan
    over many independently-produced artifacts should not let one bad object
    sink every other document's resolution."""
    run = await repo.latest_run(doc_id, stage)
    if run is None or run.artifact_key is None:
        return None
    try:
        return cast(Mapping[str, object], await store.get_json(run.artifact_key))
    except Exception:
        return None


async def resolve_entities(
    *, session: AsyncSession, store: ArtifactStore, threshold: float
) -> dict[str, object]:
    repo = RegistryRepository(session)
    documents = await repo.list_documents()

    vectors: dict[str, list[float]] = {}
    stash: dict[str, dict[str, object]] = {}
    unresolved: list[str] = []

    for doc in documents:
        embedding = await read_artifact(repo, store, doc.id, "embedding")
        vec = document_vector(embedding) if embedding is not None else []
        if not vec:
            unresolved.append(doc.id)
            continue
        knowledge = await read_artifact(repo, store, doc.id, "knowledge")
        vectors[doc.id] = vec
        stash[doc.id] = {
            "doc_id": doc.id,
            "doc_key": doc.doc_key,
            "version": doc.version,
            "knowledge": knowledge if knowledge is not None else {},
        }

    components = cluster(vectors, threshold)
    entities = [consolidate_entity([stash[d] for d in comp]) for comp in components]

    return {
        "schema_version": 1,
        "threshold": threshold,
        "entity_count": len(entities),
        "entities": entities,
        "unresolved": sorted(unresolved),
    }
