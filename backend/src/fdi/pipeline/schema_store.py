"""Where the field-extraction schema lives at runtime.

NOT the repo file. `backend/Dockerfile` COPYs
docs/architecture/field-extraction-schema.json into the image at BUILD time,
so a write to that path inside the container mutates a layer-owned file that
disappears on the next `docker compose up` -- and where the write does
succeed (the runtime user owns the copy), each replica ends up with a
different schema that no `git status` shows and no deploy reproduces. It is
also a git-tracked source file, so an HTTP endpoint that rewrites it makes
every deploy a merge conflict.

So the writable copy is an object in the artifact bucket, and the on-disk
file is the IMMUTABLE seed: read when the object is absent, never written,
never overwritten. Resolution is read-through, not read-and-seed -- a GET
that populated the bucket would make the first read of a fresh environment a
mutation, and two concurrent cold reads a race.

Pipeline layer (does I/O): imports platform (ArtifactStore) + core, same
shape as fdi.pipeline.tables. The route is the only reader; a PUT therefore
takes effect on the very next POST /extract/document, with no redeploy.
"""

import asyncio
import json
from collections.abc import Mapping

from fdi.platform.storage.artifact_store import ArtifactStore

SCHEMA_KEY = "schema/field-extraction-schema.json"


def _read_seed(seed_path: str) -> object:
    """Blocking file read, run off the event loop via asyncio.to_thread by
    the caller -- same posture as ArtifactStore's sync-minio-SDK calls."""
    with open(seed_path, encoding="utf-8") as f:
        return json.load(f)


async def load_schema(
    *, store: ArtifactStore, seed_path: str
) -> tuple[dict[str, object], str]:
    """(the current schema document, where it came from).

    "store" when the bucket holds an edited copy, "seed" when it falls back
    to the on-disk file. A stored object that is not a JSON object (corrupt
    or hand-written) falls back to the seed rather than serving a shape
    nothing can parse. An unreadable seed yields ({}, "seed") -- callers
    (parse_field_specs, validate_extraction_schema) are total on {}.
    """
    if await store.exists(SCHEMA_KEY):
        stored = await store.get_json(SCHEMA_KEY)
        if isinstance(stored, dict):
            return stored, "store"

    try:
        seed = await asyncio.to_thread(_read_seed, seed_path)
    except (OSError, ValueError):
        return {}, "seed"
    return (seed, "seed") if isinstance(seed, dict) else ({}, "seed")


async def save_schema(*, store: ArtifactStore, doc: Mapping[str, object]) -> None:
    """Persist `doc` as THE current schema. put_json canonicalizes, so two
    identical saves produce byte-identical objects."""
    await store.put_json(SCHEMA_KEY, dict(doc))
