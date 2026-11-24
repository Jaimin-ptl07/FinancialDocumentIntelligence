"""`fdi-companies` -- file already-ingested Reports under their Companies.

    fdi-companies backfill

Reports ingested before the issuer stage existed have no Company. This runs
the issuer stage for each of them and resolves it against the company
registry -- the same path a new upload takes after its issuer stage, so there
is one way a Report gets its Company (ADR-0003). It runs the stage in-process
rather than publishing it: the issuer stage reads two small artifacts and
calls no external service, and running inline lets the command report each
Report's outcome as it happens. Idempotent; safe to re-run.

Lives in `workers`, beside `fdi-mirror`, for the same reason: it drives
pipeline stages, and `platform` may not import upward. Run it where the FDI_
environment lives:

    docker compose exec worker fdi-companies backfill

Prints one line per Report (doc id, CIN, Fiscal year and link status, each
"-" when absent, then the reason) to STDOUT. Exit 0
always: a Report left unfiled is an outcome to read, not a command failure.
"""

import argparse
import asyncio
import sys

from minio import Minio

from fdi.pipeline.companies import backfill_companies
from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore


async def amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="fdi-companies",
        description="File already-ingested Reports under their Companies (ADR-0003).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", help="Run the issuer stage and file every ingested Report.")
    parser.parse_args(argv)

    settings = get_settings()
    store = ArtifactStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        settings.s3_bucket,
    )
    engine = make_engine(settings.database_url)
    try:
        links = await backfill_companies(
            session_factory=make_session_factory(engine), store=store
        )
    finally:
        await engine.dispose()

    for link in links:
        print(
            f"{link.doc_id}\t{link.cin or '-'}\t{link.fiscal_year or '-'}\t"
            f"{link.link_status or '-'}\t{link.reason}"
        )
    filed = sum(1 for link in links if link.company_id is not None)
    print(f"filed {filed} of {len(links)} Reports")


def main(argv: list[str] | None = None) -> None:
    """Console-script shim; tests drive the loop-free `amain` directly."""
    asyncio.run(amain(argv if argv is not None else sys.argv[1:]))
