"""RT-7 orchestrator: turn a `doc_key`'s document versions into a filing
timeline (entity_timeline) or a cross-version concept comparison
(compare_concept_across_versions). Pipeline layer -- imports platform (repo +
store) + core, the same shape as fdi.pipeline.tables/fdi.pipeline.crossdoc. No
new stage, no new artifact, no clustering: `doc_key` IS the entity identity
here (design spec docs/superpowers/specs/2026-07-18-rt7-crossdoc-entity-
retrieval-design.md, decision 1) -- fdi.pipeline.crossdoc.resolve_entities
(corpus-wide, embedding-similarity clustering) is a SEPARATE, complementary
discovery step this module does not call. Both functions return None when
`doc_key` matches zero documents (the caller maps this to 404).
"""

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.entities import build_concept_comparison, build_entity_timeline
from fdi.core.ontology import CanonicalConcept
from fdi.pipeline.crossdoc import read_artifact
from fdi.pipeline.tables import concept_lookup
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def entity_timeline(
    *, session: AsyncSession, store: ArtifactStore, doc_key: str
) -> dict[str, object] | None:
    docs = await DocumentRepository(session).list_by_doc_key(doc_key)
    if not docs:
        return None
    reg_repo = RegistryRepository(session)
    members: list[Mapping[str, object]] = []
    for doc in docs:
        knowledge = await read_artifact(reg_repo, store, doc.id, "knowledge")
        members.append({
            "doc_id": doc.id,
            "doc_key": doc.doc_key,
            "version": doc.version,
            "knowledge": knowledge if knowledge is not None else {},
        })
    return build_entity_timeline(doc_key, members)


async def compare_concept_across_versions(
    *, session: AsyncSession, store: ArtifactStore, doc_key: str, concept: CanonicalConcept
) -> dict[str, object] | None:
    docs = await DocumentRepository(session).list_by_doc_key(doc_key)
    if not docs:
        return None
    member_lookups: list[Mapping[str, object]] = []
    for doc in docs:
        rows = await concept_lookup(session=session, store=store, doc_id=doc.id, concept=concept)
        member_lookups.append({
            "doc_id": doc.id, "doc_key": doc.doc_key, "version": doc.version, "rows": rows,
        })
    return build_concept_comparison(doc_key, concept.value, member_lookups)
