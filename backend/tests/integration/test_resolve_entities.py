"""fdi.pipeline.crossdoc.resolve_entities against real Postgres + MinIO.

Drives three documents (A, B, C) through the full ocr->...->knowledge chain
plus embedding (the same testcontainers harness test_knowledge_stage.py /
test_embedding_stage.py use), with a call-ordered fake embedding client that
assigns A and B near-identical vectors (cosine ~0.99) and C an orthogonal one
-- the exact numbers used in tests/unit/test_crossdoc_cluster.py's
`test_cluster_two_groups_and_a_singleton`.

**Why assertions are membership-based, not exact `entity_count` equality:**
`postgres_url`/`minio_container` in `tests/integration/conftest.py` are
SESSION-scoped -- one Postgres + one MinIO for the ENTIRE `pytest` run, shared
by every file under `tests/integration`. `resolve_entities` enumerates *every*
`document` row (by design -- spec decision 8, no per-call scoping), so in a
full-suite run it also sees whatever earlier test files already inserted
(most have no embedding artifact and land in `unresolved`; two sibling files,
test_embedding_stage.py and test_index_stage.py, DO leave one real 8-dim
embedded document each in the same `test-artifacts` bucket this file uses).
An exact `entity_count`/`unresolved == []` assertion would therefore be
order-dependent across the suite, not a property of this feature. Instead
each test asks "did MY documents cluster the way the algorithm says they
should", which is unaffected by whatever else is in the shared corpus (cosine
between my 2-dim vectors and any stray N-dim vector is guardedly 0.0 -- core
§4 -- so stray docs can only ever add unrelated entities, never merge into or
split mine). This mirrors the existing convention in
test_repository.py/test_document_repository.py of fresh uuids over an
assumed-clean table, one level up (whole-corpus vs. single-row scope).
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.crossdoc import resolve_entities
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.embedding import EmbeddingStage
from fdi.pipeline.stages.knowledge import KnowledgeStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.ontology import OntologyStage
from fdi.pipeline.stages.quality import QualityStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.pipeline.stages.structure import StructureStage
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


class _SequencedEmbeddingClient:
    """Deterministic, call-ORDERED stand-in: the Nth call to `embed()` returns
    the Nth hand-picked vector, regardless of input text. Lets a test assign
    an exact vector per document (one `embed()` call per document's
    EmbeddingStage run) without depending on OCR/AST text content at all --
    the "seed injected via the fake" the plan describes."""

    model = "fake-sequenced"

    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        vector = self._vectors[self.calls]
        self.calls += 1
        return [vector for _ in texts]


def _build_registry() -> tuple[StageRegistry, DagResolver]:
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(SemanticStage())
    reg.register(OntologyStage())
    reg.register(QualityStage())
    reg.register(KnowledgeStage())
    reg.register(EmbeddingStage())
    return reg, DagResolver(reg.all())


async def _process_document(
    *,
    doc_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    reg: StageRegistry,
    resolver: DagResolver,
    embedder: _SequencedEmbeddingClient,
) -> None:
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD Annual Report"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Total Assets"},
                {"role": "sectionHeading", "content": "Directors' Report"},
                {"content": "The board is pleased with results."},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient(
        {
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Directors' Report": SectionType.DIRECTORS_REPORT.value,
        }
    )
    common = {
        "doc_id": doc_id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic",
        run_id=str(uuid.uuid4()),
        services={"semantic": fake_semantic},
        **common,
    )
    await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="quality", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="knowledge", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding",
        run_id=str(uuid.uuid4()),
        services={"embedder": embedder},
        **common,
    )


@pytest.fixture
async def resolved_corpus(
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore
) -> dict[str, str]:
    """Three fully-processed documents: A, B near-identical (cosine ~0.99),
    C orthogonal to both -- the same numbers as
    test_crossdoc_cluster.py::test_cluster_two_groups_and_a_singleton."""
    embedder = _SequencedEmbeddingClient([[1.0, 0.0], [0.99, 0.14], [0.0, 1.0]])
    reg, resolver = _build_registry()
    ids: dict[str, str] = {}
    for key in ("a", "b", "c"):
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
            filename=f"{key}.pdf",
            doc_key=str(uuid.uuid4()),
            content_type="application/pdf",
            session_factory=session_factory,
            store=store,
        )
        await _process_document(
            doc_id=doc.id,
            session_factory=session_factory,
            store=store,
            reg=reg,
            resolver=resolver,
            embedder=embedder,
        )
        ids[key] = doc.id
    return ids


@pytest.fixture
async def unembedded_doc_id(
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore
) -> str:
    """Intaken but never run through any stage -- no `embedding` artifact."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="unembedded.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    return doc.id


def _entity_containing(entities: list[dict[str, object]], doc_id: str) -> dict[str, object]:
    return next(e for e in entities if doc_id in _member_ids(e))


def _member_ids(entity: dict[str, object]) -> list[str]:
    members = entity["members"]
    assert isinstance(members, list)
    return [str(m["doc_id"]) for m in members]


async def test_resolve_entities_two_entities_from_three_docs(
    session_factory, store, resolved_corpus
) -> None:
    async with session_factory() as s:
        result = await resolve_entities(session=s, store=store, threshold=0.85)

    assert result["schema_version"] == 1
    assert result["threshold"] == 0.85
    entities = result["entities"]
    a, b, c = resolved_corpus["a"], resolved_corpus["b"], resolved_corpus["c"]

    entity_a = _entity_containing(entities, a)
    entity_c = _entity_containing(entities, c)
    assert b in _member_ids(entity_a)
    assert c not in _member_ids(entity_a)
    assert entity_a["entity_id"] != entity_c["entity_id"]
    assert a not in _member_ids(entity_c)
    assert b not in _member_ids(entity_c)
    # our three documents were all placed -- none dropped to unresolved
    assert a not in result["unresolved"]
    assert b not in result["unresolved"]
    assert c not in result["unresolved"]


async def test_resolve_entities_strict_threshold_splits(
    session_factory, store, resolved_corpus
) -> None:
    async with session_factory() as s:
        result = await resolve_entities(session=s, store=store, threshold=0.999)

    entities = result["entities"]
    a, b, c = resolved_corpus["a"], resolved_corpus["b"], resolved_corpus["c"]
    entity_ids = {
        _entity_containing(entities, a)["entity_id"],
        _entity_containing(entities, b)["entity_id"],
        _entity_containing(entities, c)["entity_id"],
    }
    assert len(entity_ids) == 3  # A, B no longer merge at this threshold


async def test_resolve_entities_unembedded_doc_is_unresolved(
    session_factory, store, resolved_corpus, unembedded_doc_id
) -> None:
    async with session_factory() as s:
        result = await resolve_entities(session=s, store=store, threshold=0.85)

    assert unembedded_doc_id in result["unresolved"]
    for e in result["entities"]:
        assert unembedded_doc_id not in _member_ids(e)


async def test_resolve_entities_returns_a_valid_shape_for_the_current_corpus(
    session_factory, store
) -> None:
    """`resolve_entities` never raises and echoes the requested threshold,
    regardless of what the shared corpus already contains.

    A literal *empty* corpus cannot be asserted here: the module docstring
    above explains why (session-scoped Postgres/MinIO shared by every
    `tests/integration` file). This test instead pins the one thing that IS
    guaranteed independent of that shared state -- the function completes and
    returns the documented shape -- which is also the meaningful part of the
    spec's "empty corpus does not raise / does not 503" requirement.
    """
    async with session_factory() as s:
        result = await resolve_entities(session=s, store=store, threshold=0.5)

    assert result["schema_version"] == 1
    assert result["threshold"] == 0.5
    assert isinstance(result["entity_count"], int)
    assert isinstance(result["entities"], list)
    assert isinstance(result["unresolved"], list)
    assert result["entity_count"] == len(result["entities"])
