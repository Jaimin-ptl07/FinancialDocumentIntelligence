"""`fdi-schemas` -- import the FDI standard Extraction schema (S3, ADR-0004).

    fdi-schemas import-standard [--path PATH]

The standard schema is imported from its document, never hand-edited in the
database. The API imports it once at start-up when the database has none;
publishing a NEW standard version (an edited v4.3 file, or a later one) is
this explicit command, so a deploy never changes the standard schema behind
anyone's back. Idempotent: an unchanged document publishes nothing, and each
Field keeps its identity (derived from its tag), so its cached reads survive.

Lives in `workers`, beside `fdi-companies`, because `platform` may not import
`pipeline`. Run it where the FDI_ environment lives:

    docker compose exec worker fdi-schemas import-standard

Exit 0 on success (including "unchanged"); 1 with a message on stderr when
the document cannot be read or is not a valid standard schema.
"""

import argparse
import asyncio
import sys

from fdi.core.extraction_schema import SchemaInputError
from fdi.pipeline.extraction_schemas import import_standard_schema_file
from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_engine, make_session_factory


async def amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="fdi-schemas",
        description="Manage the FDI standard Extraction schema (ADR-0004).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser(
        "import-standard", help="Publish the standard document as the next standard version."
    )
    imp.add_argument("--path", help="Defaults to FDI_STANDARD_SCHEMA_PATH.")
    args = parser.parse_args(argv)

    settings = get_settings()
    path = args.path or settings.standard_schema_path
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            try:
                version = await import_standard_schema_file(session, path, only_if_absent=False)
            except (OSError, ValueError) as exc:
                errors = exc.errors if isinstance(exc, SchemaInputError) else [str(exc)]
                print(f"could not import {path}:", file=sys.stderr)
                for error in errors[:50]:
                    print(f"  {error}", file=sys.stderr)
                raise SystemExit(1) from None
            await session.commit()
    finally:
        await engine.dispose()

    if version is None:
        print(f"standard schema unchanged ({path})")
    else:
        print(
            f"published standard schema version {version.summary.version}: "
            f"{version.summary.field_count} Fields from {path}"
        )


def main(argv: list[str] | None = None) -> None:
    """Console-script shim; tests drive the loop-free `amain` directly."""
    asyncio.run(amain(argv if argv is not None else sys.argv[1:]))
