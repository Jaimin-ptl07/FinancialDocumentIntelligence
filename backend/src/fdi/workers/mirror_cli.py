"""`fdi-mirror` -- the deploy-day data-mirror tool (productionization T10).

The Gated demo ships with the dev corpus rather than re-paying Azure to
re-extract it: Postgres is dumped and restored, the artifact bucket is synced,
and this tool does the two things neither copy can do on its own
(docs/operations/deploy-day-runbook.md):

    fdi-mirror check             every recorded artifact is in the store?
    fdi-mirror replay [--dry-run]  rebuild Milvus + Neo4j from the mirror

`check` is the stage-run/artifact consistency check. Every `done` stage_run row
names the object its stage wrote, and every document's source PDF sits at its
checksum's key; a row whose object did not make it across is a stage the new
host will fail to read from (a dependency read raises, a preview 404s) long
after anyone would think to blame the mirror. Exit 1 lists each gap.

`replay` exists because a restored database plus a synced bucket is NOT a
rebuilt graph/vector tier. `index` and `graph_load` are the only stages whose
point is a side effect -- the Milvus upsert and the Neo4j load -- and their
artifact is a small receipt. `run_stage` treats an existing artifact as a cache
hit and skips the body, so re-dispatching them onto the mirrored bucket would
"succeed" in milliseconds and write nothing. Replay deletes each document's
latest receipt first, then publishes the stage, so the worker sees a miss and
performs the write. Only those two receipts are touched: every other artifact
is the corpus itself, and deleting it would re-pay Azure for it.

Lives in `workers`, not beside `fdi-admin` in `platform`: replay publishes the
worker's own stage task, and `platform` may not import upward. Run it in the
api or worker container, where the FDI_ environment and broker live:

    docker compose -f docker/compose.prod.yml exec api fdi-mirror check

Exit codes: 0 consistent / replay dispatched; 1 `check` found gaps. Unlike
`fdi-admin`'s errors, the `MISSING` lines go to STDOUT: they are the report the
command exists to produce (pipe it, grep it), not a diagnostic. Replay is
idempotent -- if it stops partway, the receipts it already deleted show up in
`check` as MISSING, and running it again re-dispatches every one of them.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass

from minio import Minio

from fdi.pipeline.intake import document_key
from fdi.platform.config import Settings, get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.obs.logging import new_correlation_id
from fdi.platform.storage.artifact_store import ArtifactStore

# The stages whose artifact is a receipt for a write into Milvus (`index`) or
# Neo4j (`graph_load`). Neither store is mirrored -- both are rebuilt from the
# artifacts that are.
SIDE_EFFECT_STAGES = ("index", "graph_load")


@dataclass(frozen=True)
class Gap:
    doc_id: str
    stage_or_source: str  # a stage name, or "source" for the uploaded file itself
    key: str


def _store(settings: Settings) -> ArtifactStore:
    client = Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )
    return ArtifactStore(client, settings.s3_bucket)


async def _check(settings: Settings) -> list[Gap]:
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as s:
            repo = RegistryRepository(s)
            documents = await repo.list_documents()
            runs = await repo.completed_runs()
    finally:
        await engine.dispose()

    store = _store(settings)
    expected = [
        Gap(d.id, "source", document_key(d.checksum)) for d in documents if d.checksum
    ] + [Gap(r.doc_id, r.stage_name, r.artifact_key) for r in runs if r.artifact_key]
    # Two runs can share a key (a cache hit records the key it found), so stat
    # each object once.
    present = {key: await store.exists(key) for key in {g.key for g in expected}}
    print(f"checked {len(documents)} documents, {len(present)} objects")
    return [g for g in expected if not present[g.key]]


async def _replay(settings: Settings, *, dry_run: bool) -> list[tuple[str, str, str]]:
    engine = make_engine(settings.database_url)
    plan: list[tuple[str, str, str]] = []
    try:
        async with make_session_factory(engine)() as s:
            repo = RegistryRepository(s)
            for doc in await repo.list_documents():
                for stage in SIDE_EFFECT_STAGES:
                    run = await repo.latest_completed_run(doc.id, stage)
                    if run is not None and run.artifact_key is not None:
                        plan.append((doc.id, stage, run.artifact_key))
    finally:
        await engine.dispose()

    for doc_id, stage, _ in plan:
        print(f"{doc_id}\t{stage}")
    if dry_run or not plan:
        return plan

    # Imported here, not at module top: importing the task module builds the
    # Celery app (and Settings) at import time, and `check` needs neither.
    from fdi.workers.tasks import run_stage_task

    store = _store(settings)
    correlation_id = new_correlation_id()
    for doc_id, stage, key in plan:
        await store.delete(key)
        run_stage_task.apply_async(
            args=[doc_id, stage], kwargs={"correlation_id": correlation_id}
        )
    print(f"correlation_id {correlation_id}")
    return plan


async def amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="fdi-mirror",
        description="Verify and finish a deploy-day data mirror (T10 runbook).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "check", help="Verify every recorded artifact and source file is in the store."
    )
    replay = sub.add_parser(
        "replay", help="Rebuild Milvus and Neo4j by re-running index and graph_load."
    )
    replay.add_argument(
        "--dry-run", action="store_true", help="List what would be replayed; change nothing."
    )
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "check":
        gaps = await _check(settings)
        for g in gaps:
            print(f"MISSING\t{g.doc_id}\t{g.stage_or_source}\t{g.key}")
        print(f"{len(gaps)} missing")
        if gaps:
            raise SystemExit(1)
    elif args.command == "replay":
        plan = await _replay(settings, dry_run=args.dry_run)
        verb = "would replay" if args.dry_run else "dispatched"
        print(f"{verb} {len(plan)} stage runs")


def main(argv: list[str] | None = None) -> None:
    """Console-script shim; tests drive the loop-free `amain` directly."""
    asyncio.run(amain(argv if argv is not None else sys.argv[1:]))
