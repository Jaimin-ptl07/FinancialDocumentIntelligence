"""RT-6 orchestrator: read a document's `structure` artifact on demand and
expose its canonicalized table grids (get_tables) and concept->row matches
(concept_lookup) via the pure fdi.core.tables functions. Pipeline layer (does
I/O) -- imports platform (repo + store) + core, the same shape as
fdi.pipeline.retrieval.resolve_chunk. No new stage, no new artifact (spec
decision 1): the `structure` artifact already exists per document.

concept_lookup does NOT read the `ontology` artifact (spec decision 2) --
only `structure`, plus fdi.core.ontology's synonym DATA reused as a pure
library via fdi.core.tables.find_concept_rows. A document that has no
`structure` artifact yet returns None from both functions (caller maps this
to 404); a document that HAS `structure` but no matching row/table returns
[] -- a real, distinct, non-error outcome.
"""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.ontology import CanonicalConcept
from fdi.core.tables import filter_tables, find_concept_rows
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def _read_structure(
    session: AsyncSession, store: ArtifactStore, doc_id: str
) -> Mapping[str, object] | None:
    repo = RegistryRepository(session)
    run = await repo.latest_completed_run(doc_id, "structure")
    if run is None or run.artifact_key is None:
        return None
    return cast(Mapping[str, object], await store.get_json(run.artifact_key))


def _tables_of(structure: Mapping[str, object]) -> list[Mapping[str, object]]:
    tables_raw = structure.get("tables")
    if not isinstance(tables_raw, list):
        return []
    return [t for t in tables_raw if isinstance(t, Mapping)]


async def get_tables(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, keyword: str | None = None
) -> list[dict[str, object]] | None:
    """A document's canonicalized table grids, each tagged with its
    `table_index` (position within `structure["tables"]`), optionally
    filtered by `keyword` (case-insensitive substring against any column
    header). None if the document has no `structure` artifact yet."""
    structure = await _read_structure(session, store, doc_id)
    if structure is None:
        return None
    tagged: list[Mapping[str, object]] = [
        {"table_index": i, **t} for i, t in enumerate(_tables_of(structure))
    ]
    return [dict(t) for t in filter_tables(tagged, keyword)]


async def concept_lookup(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, concept: CanonicalConcept
) -> list[dict[str, object]] | None:
    """Every table row across the document whose label matches `concept`'s
    ontology synonyms (fdi.core.tables.find_concept_rows), each tagged with
    its source `table_index`. None if the document has no `structure`
    artifact yet; [] if it has one but nothing matched (a real answer, not
    a missing-artifact signal)."""
    structure = await _read_structure(session, store, doc_id)
    if structure is None:
        return None
    matches: list[dict[str, object]] = []
    for i, table in enumerate(_tables_of(structure)):
        grid = table.get("grid")
        if not isinstance(grid, Mapping):
            continue
        for row in find_concept_rows(grid, concept):
            matches.append({"table_index": i, **row})
    return matches
