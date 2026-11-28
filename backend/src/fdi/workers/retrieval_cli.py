"""`fdi-retrieval` -- maintain the vector index the Retrieval service reads (S4).

    fdi-retrieval reindex [--dry-run]

Vector schema v3 (ADR-0005) adds filterable `basis` and `kind` scalars, which
Milvus cannot add to a live collection: v3 lives in a NEW collection
(`FDI_MILVUS_COLLECTION`, default `fdi_chunks_v3`), and every Report's rows have
to be written into it. `reindex` publishes the `index` stage for every Report
whose `index` inputs (`embedding`, `chunks`, `basis`) are complete. Index stage
v4 is a cache miss for every Report indexed by v3, so the worker performs the
write; `embedding` stays cached, so nothing is re-embedded and nothing calls
Azure. A Report whose inputs are not complete yet is skipped -- its own chain
indexes it when it gets there.

Idempotent: once a Report is indexed by v4, re-running publishes a cache hit
that writes nothing. Run it where the worker's broker lives:

    docker compose exec api fdi-retrieval reindex

Prints one line per Report (doc id, then `dispatched`/`would dispatch` or the
missing inputs) to STDOUT. Exit 0 always: a skipped Report is an outcome to
read, not a failure.
"""

import argparse
import asyncio
import sys

from fdi.pipeline.stages.index import IndexStage
from fdi.platform.config import Settings, get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.obs.logging import new_correlation_id


async def _plan(settings: Settings) -> list[tuple[str, list[str]]]:
    """(doc id, missing index inputs) for every Report, oldest first."""
    engine = make_engine(settings.database_url)
    plan: list[tuple[str, list[str]]] = []
    try:
        async with make_session_factory(engine)() as s:
            repo = RegistryRepository(s)
            for doc in await repo.list_documents():
                missing = [
                    dep for dep in IndexStage.deps
                    if await repo.latest_completed_run(doc.id, dep) is None
                ]
                plan.append((doc.id, missing))
    finally:
        await engine.dispose()
    return plan


async def reindex(settings: Settings, *, dry_run: bool) -> list[str]:
    plan = await _plan(settings)
    ready = [doc_id for doc_id, missing in plan if not missing]
    verb = "would dispatch" if dry_run else "dispatched"
    for doc_id, missing in plan:
        print(f"{doc_id}\t{verb}" if not missing else f"{doc_id}\tskipped: no {', '.join(missing)}")
    if dry_run or not ready:
        return ready
    # Imported here: importing the task module builds the Celery app.
    from fdi.workers.tasks import run_stage_task

    correlation_id = new_correlation_id()
    for doc_id in ready:
        run_stage_task.apply_async(
            args=[doc_id, IndexStage.name], kwargs={"correlation_id": correlation_id}
        )
    print(f"collection {settings.milvus_collection}\tcorrelation_id {correlation_id}")
    return ready


async def amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="fdi-retrieval",
        description="Maintain the vector index the Retrieval service reads (S4).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser(
        "reindex", help="Write every Report's rows into the configured v3 collection."
    )
    cmd.add_argument("--dry-run", action="store_true", help="List the plan; change nothing.")
    args = parser.parse_args(argv)
    if args.command == "reindex":
        await reindex(get_settings(), dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> None:
    asyncio.run(amain(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
