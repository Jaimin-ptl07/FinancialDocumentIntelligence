"""The framework around a Stage: hashing, the content-addressed cache, persistence,
and successor scheduling. A stage stays a pure transform; all of that lives here."""

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.schema import validate_finite
from fdi.pipeline.intake import document_key
from fdi.pipeline.keys import (
    artifact_key,
    derived_input_hash,
    output_hash,
    root_input_hash,
)
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.stage import StageContext
from fdi.platform.db.models import Document
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def run_stage(
    *, doc_id: str, stage_name: str, run_id: str, correlation_id: str | None,
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore,
    registry: StageRegistry, resolver: DagResolver,
    services: Mapping[str, object] | None = None,
) -> list[str]:
    stage = registry.get(stage_name)
    svcs = services if services is not None else {}

    async def read_artifact(d: str, upstream: str) -> object:
        async with session_factory() as s:
            run = await RegistryRepository(s).latest_run(d, upstream)
        assert run is not None and run.artifact_key is not None
        return await store.get_json(run.artifact_key)

    async def read_source() -> bytes:
        async with session_factory() as s:
            d = await RegistryRepository(s).get_document(doc_id)
        if d is None or d.checksum is None:
            raise ValueError(f"document {doc_id} has no source")
        return await store.get_bytes(document_key(d.checksum))

    # 1. input_hash from recorded upstream output_hashes (root: document checksum).
    async with session_factory() as s:
        repo = RegistryRepository(s)
        facts = await repo.runs_for_document(doc_id)
        if stage.deps:
            # A multi-dep stage is dispatched once per dependency edge (each
            # predecessor calls resolver.successors() independently on finish),
            # so this call can arrive after only SOME of its deps are done --
            # not a bug in the caller, just a race this stage must tolerate.
            # Skipping (no run, no successors) here is safe: whichever
            # dependency finishes LAST will re-dispatch this same stage, and
            # that call will see every dep in `facts` and proceed normally.
            if not all(d in facts for d in stage.deps):
                return []
            input_hash = derived_input_hash([facts[d].output_hash for d in stage.deps])
        else:
            doc = await s.get(Document, doc_id)
            checksum = doc.checksum.encode() if doc and doc.checksum else None
            input_hash = root_input_hash(checksum)

    key = artifact_key(doc_id, stage_name, stage.version, input_hash)

    async with session_factory() as s:
        await RegistryRepository(s).start_run(
            run_id, doc_id, stage_name, stage.version, input_hash, correlation_id
        )
        await s.commit()

    # 2. cache hit: record and skip the body.
    if await store.exists(key):
        existing = await store.get_json(key)
        async with session_factory() as s:
            await RegistryRepository(s).finish_run(run_id, output_hash(existing), key)
            await s.commit()
        return resolver.successors(stage_name)

    # 3. cache miss: run, validate, store, record.
    try:
        body = await stage.run(
            StageContext(
                doc_id=doc_id, read_artifact=read_artifact,
                read_source=read_source, services=svcs,
            )
        )
        validate_finite(body)
        await store.put_json(key, body)
    except Exception as exc:
        async with session_factory() as s:
            await RegistryRepository(s).fail_run(run_id, repr(exc))
            await s.commit()
        raise
    async with session_factory() as s:
        await RegistryRepository(s).finish_run(run_id, output_hash(body), key)
        await s.commit()
    return resolver.successors(stage_name)
