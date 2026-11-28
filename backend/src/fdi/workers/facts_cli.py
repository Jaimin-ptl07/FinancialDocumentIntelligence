"""`fdi-facts` -- maintain the Financial fact projection (S5, ADR-0006).

    fdi-facts seed-concepts [--path FILE]
    fdi-facts project DOC_ID [--force]
    fdi-facts backfill [--force] [--dry-run]
    fdi-facts rebuild --yes
    fdi-facts export DOC_ID

The Observation and Financial-fact tables are a deterministic projection of
each Report's graph artifacts plus its Invitees' reviewed Extracted values, so
they can always be regenerated:

* `seed-concepts` adds every Concept of the FDI standard schema document that
  is not stored yet (projection does this itself the first time).
* `project` projects one Report version and re-derives its Company's facts.
* `backfill` projects every Report version filed under a Company. A Report
  whose inputs have not changed since its last projection is `unchanged` and
  costs one key comparison; `--force` re-projects anyway.
* `export` prints one Report version's stored values as JSON -- the input
  `scripts/score_pipeline.py --facts` scores, to set the service's score
  against the artifact's (`--reasoning`).
* `rebuild` drops every Observation, fact, validation result and projection
  record and backfills from the artifacts -- the "drop and regenerate"
  guarantee, exercised.

Runs in-process (no broker): `docker compose exec api fdi-facts backfill`.
Nothing here calls a model or Azure. Prints one line per Report to STDOUT;
exit 0 unless the database is unreachable.
"""

import argparse
import asyncio
import json
import sys

import sqlalchemy as sa
from minio import Minio

from fdi.pipeline.financial_facts import (
    FinancialFactService,
    project_document,
    read_json_document,
)
from fdi.pipeline.schema_store import load_schema
from fdi.platform.config import Settings, get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.db.models import (
    Document,
    FinancialFactRow,
    FinancialObservationRow,
    FinancialProjection,
    FinancialValidationRow,
)
from fdi.platform.storage.artifact_store import ArtifactStore


async def _store(settings: Settings) -> ArtifactStore:
    client = Minio(settings.s3_endpoint, access_key=settings.s3_access_key,
                   secret_key=settings.s3_secret_key, secure=settings.s3_secure)
    store = ArtifactStore(client, bucket=settings.s3_bucket)
    await store.ensure_bucket()
    return store


async def seed_concepts(settings: Settings, path: str | None) -> int:
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as s:
            service = FinancialFactService(session=s, store=await _store(settings))
            source = path or settings.standard_schema_path
            added = await service.seed_concepts(await asyncio.to_thread(read_json_document, source))
            await s.commit()
    finally:
        await engine.dispose()
    print(f"concepts added\t{added}")
    return added


async def project(
    settings: Settings, doc_ids: list[str] | None, *, force: bool, dry_run: bool = False,
) -> list[str]:
    engine = make_engine(settings.database_url)
    projected: list[str] = []
    try:
        session_factory = make_session_factory(engine)
        store = await _store(settings)
        schema, _ = await load_schema(store=store, seed_path=settings.extraction_schema_path)
        if doc_ids is None:
            async with session_factory() as s:
                doc_ids = list((await s.execute(
                    sa.select(Document.id).where(Document.company_id.is_not(None))
                    .order_by(Document.created_at, Document.id)
                )).scalars())
        for doc_id in doc_ids:
            if dry_run:
                print(f"{doc_id}\twould project")
                continue
            outcome = await project_document(
                session_factory=session_factory, store=store, doc_id=doc_id,
                formulas_schema=schema, standard_schema_path=settings.standard_schema_path,
                force=force,
            )
            detail = outcome.reason or f"{outcome.observations} observations"
            print(f"{doc_id}\t{outcome.status}\t{detail}")
            if outcome.status == "projected":
                projected.append(doc_id)
    finally:
        await engine.dispose()
    return projected


async def rebuild(settings: Settings) -> list[str]:
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as s:
            for model in (FinancialFactRow, FinancialValidationRow, FinancialObservationRow,
                          FinancialProjection):
                await s.execute(sa.delete(model))
            await s.commit()
    finally:
        await engine.dispose()
    print("dropped\tevery Observation, fact, validation result and projection record")
    return await project(settings, None, force=True)


async def export(settings: Settings, doc_id: str) -> dict[str, object]:
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as s:
            service = FinancialFactService(session=s, store=await _store(settings))
            body: dict[str, object] = {
                "doc_id": doc_id, "bases": await service.report_values(doc_id)}
    finally:
        await engine.dispose()
    print(json.dumps(body, indent=2, sort_keys=True))
    return body


async def amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="fdi-facts", description="Maintain the Financial fact projection (S5).")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-concepts", help="store the standard schema's Concepts")
    seed.add_argument("--path", help="a standard schema document (default: the configured one)")
    one = sub.add_parser("project", help="project one Report version")
    one.add_argument("doc_id")
    one.add_argument("--force", action="store_true", help="re-project unchanged inputs")
    every = sub.add_parser("backfill", help="project every filed Report version")
    every.add_argument("--force", action="store_true", help="re-project unchanged inputs")
    every.add_argument("--dry-run", action="store_true", help="list, project nothing")
    dump = sub.add_parser("export", help="print one Report version's stored values")
    dump.add_argument("doc_id")
    drop = sub.add_parser("rebuild", help="drop the projection and backfill it")
    drop.add_argument("--yes", action="store_true", required=True,
                      help="confirm dropping every stored Observation and fact")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "seed-concepts":
        await seed_concepts(settings, args.path)
    elif args.command == "project":
        await project(settings, [args.doc_id], force=args.force)
    elif args.command == "backfill":
        await project(settings, None, force=args.force, dry_run=args.dry_run)
    elif args.command == "export":
        await export(settings, args.doc_id)
    else:
        await rebuild(settings)


def main() -> None:
    asyncio.run(amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
