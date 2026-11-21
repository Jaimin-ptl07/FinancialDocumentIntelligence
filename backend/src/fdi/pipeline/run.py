"""The framework around a Stage: hashing, the content-addressed cache, persistence,
and successor scheduling. A stage stays a pure transform; all of that lives here."""

import contextlib
import time
import uuid
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
from fdi.pipeline.usage import UsageAccumulator
from fdi.platform.db.models import Document
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def run_stage(
    *, doc_id: str, stage_name: str, run_id: str, correlation_id: str | None,
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore,
    registry: StageRegistry, resolver: DagResolver,
    services: Mapping[str, object] | None = None,
    azure_model: str = "",
) -> list[str]:
    stage = registry.get(stage_name)
    usage = UsageAccumulator()
    svcs: dict[str, object] = {**(services or {}), "usage": usage}

    async def read_artifact(d: str, upstream: str) -> object:
        async with session_factory() as s:
            run = await RegistryRepository(s).latest_completed_run(d, upstream)
        if run is None or run.artifact_key is None:
            # NOT an assert. A missing dependency artifact is a reachable
            # state -- an upstream stage that failed, or was never run -- so
            # it deserves a named error, not "impossible". The bare
            # `AssertionError()` this replaces reported no doc, no stage and
            # no dependency, and `python -O` would have removed the check
            # entirely, leaving `store.get_json(None)` to fail instead.
            raise ValueError(
                f"{stage_name} for document {d} needs {upstream}, "
                f"which has no completed run"
            )
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

        material = ""
        with contextlib.suppress(Exception):
            material = stage.key_material(svcs)
        if material:
            input_hash = derived_input_hash([input_hash, material])

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
    start = time.perf_counter()
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
        await _flush_llm_usage(
            usage, session_factory=session_factory, doc_id=doc_id,
            run_id=run_id, correlation_id=correlation_id,
        )
        async with session_factory() as s:
            await RegistryRepository(s).fail_run(run_id, repr(exc))
            await s.commit()
        raise
    duration_ms = int((time.perf_counter() - start) * 1000)
    # OCR bills 1 real Azure DI call per stage execution -- page count is
    # already present in the artifact body (fdi.core.ocr_schema.normalize_
    # ocr's "pages" list), so no change to AzureOcrClient/OcrStage is
    # needed. Costing feature (docs/superpowers/specs/2026-07-28-costing-
    # feature-design.md sec 3.3). `azure_model` arrives as a parameter,
    # not via get_settings() here: this function is the pure stage-
    # execution framework and is called directly (no Settings in scope)
    # by a large fraction of the integration suite, which fakes out
    # services instead of configuring a real app. A get_settings() call
    # here would require every one of those callers to also stub a full
    # Settings() environment just to bill a page count -- tasks.py is the
    # one real caller that already has Settings in scope; it supplies the
    # model name, and every other caller's default ("") is a legitimate
    # "unknown" for a purely descriptive metadata field.
    if stage_name == "ocr" and isinstance(body, Mapping):
        pages = body.get("pages")
        page_count = len(pages) if isinstance(pages, list) else 0
        async with session_factory() as s:
            await RegistryRepository(s).record_ocr_usage(
                str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
                correlation_id=correlation_id, page_count=page_count,
                azure_model=azure_model, duration_ms=duration_ms,
            )
            await s.commit()
    await _flush_llm_usage(
        usage, session_factory=session_factory, doc_id=doc_id,
        run_id=run_id, correlation_id=correlation_id,
    )
    async with session_factory() as s:
        await RegistryRepository(s).finish_run(run_id, output_hash(body), key)
        await s.commit()
    return resolver.successors(stage_name)


async def _flush_llm_usage(
    usage: UsageAccumulator,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    doc_id: str,
    run_id: str,
    correlation_id: str | None,
) -> None:
    """Persist every LLM call a stage reported to its UsageAccumulator, as
    one llm_usage_event row each, attributed to this stage execution's
    run_id. Called on BOTH the success and the failure path -- a stage
    that made 3 real calls before erroring on a 4th still billed for those
    3 (spec decision 4/9)."""
    if not usage.llm_calls:
        return
    async with session_factory() as s:
        repo = RegistryRepository(s)
        for call in usage.llm_calls:
            await repo.record_llm_usage(
                str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
                source=call.source, correlation_id=correlation_id,
                model=call.usage.model, input_tokens=call.usage.input_tokens,
                output_tokens=call.usage.output_tokens,
                duration_ms=call.usage.duration_ms,
            )
        await s.commit()
