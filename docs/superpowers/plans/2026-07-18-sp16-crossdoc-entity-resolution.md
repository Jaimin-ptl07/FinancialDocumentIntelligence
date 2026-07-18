# SP-16 Cross-Document Entity Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the corpus-level cross-document layer: cluster documents into real-world entities (companies) by embedding similarity, fold each entity's per-document `knowledge` records into an entity record (version timeline, cross-doc concept refs, quality trend), and surface it at `POST /entities/resolve`.

**Architecture:** A pure, deterministic core (`fdi.core.crossdoc`) does mean-pool vectorization, guarded cosine, single-linkage connected-components clustering, and entity folding — unit-testable with hand-crafted vectors, no BGE/Milvus/Docker. A pipeline-layer orchestrator (`fdi.pipeline.crossdoc.resolve_entities`, mirroring `intake`/`eval_service`) enumerates the corpus, reads each document's already-cached `embedding`+`knowledge` artifacts via the existing `latest_run`→`get_json` path, and calls the core. A synchronous route (`POST /entities/resolve`, mirroring `POST /search`) delivers it on-demand. No new Stage — entity resolution is corpus-level by design.

**Tech Stack:** Python 3.13, `uv`, FastAPI, Pydantic v2, SQLAlchemy async, pytest + testcontainers. Reuses SP-13b BGE vectors and SP-15 knowledge records already in the artifact store.

## Global Constraints

- Layered import contract `api | workers → pipeline → platform → core` (exhaustive), CI-enforced via `PYTHONUTF8=1 lint-imports --config .importlinter`; must stay "1 kept, 0 broken". `fdi.core.crossdoc` is PURE: no I/O, no config, no clock, no RNG, and no import of `fdi.core.knowledge`/`fdi.core.embeddings` (read inputs as plain `Mapping`/`Sequence`).
- Determinism (Global Constraint 6): no wall-clock, no RNG, no reliance on `set`/`dict` iteration order in returned values — sort before returning. `entity_id` = the lexicographically smallest member `doc_id`.
- Inert-config discipline: every `FDI_` field must be mirrored across ALL THREE of `backend/src/fdi/platform/config.py`, `docker/compose.yml` (`x-backend-env`), `.env.example`, or `_reject_unknown_fdi_env_vars` crashes boot / the field goes inert.
- Per-task gates (all must pass before commit): `uv run --directory backend ruff check .` · `uv run --directory backend mypy --strict src` · `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter` · relevant pytest. Line length ≤ 100 (ruff).
- TDD: failing test → run (see it fail) → minimal implementation → run (see it pass) → commit. Never raises anywhere in `fdi.core.crossdoc`: malformed/partial input yields a smaller/emptier result.
- Default threshold 0.85; `POST /entities/resolve` body may override per-request; validated `0.0 ≤ threshold ≤ 1.0`.

---

### Task 1: `fdi.core.crossdoc` — `document_vector` + `cosine` (geometry primitives)

**Files:**
- Create: `backend/src/fdi/core/crossdoc.py`
- Test: `backend/tests/unit/test_crossdoc_vectors.py`

**Interfaces:**
- Consumes: nothing (pure; reads an SP-13 `embedding` artifact shape `{"chunks":[{"vector":[float,...]}]}` as a plain `Mapping`).
- Produces:
  - `document_vector(embedding: Mapping[str, object]) -> list[float]` — L2-normalized element-wise mean of the chunk vectors; `[]` on empty/malformed/zero-norm.
  - `cosine(a: Sequence[float], b: Sequence[float]) -> float` — cosine similarity; `0.0` on length-mismatch/empty/zero-norm.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_crossdoc_vectors.py
import math

import pytest

from fdi.core.crossdoc import cosine, document_vector


def test_document_vector_is_normalized_mean_of_chunk_vectors() -> None:
    emb = {"chunks": [{"vector": [3.0, 0.0]}, {"vector": [0.0, 4.0]}]}
    # mean = [1.5, 2.0], norm = 2.5 -> [0.6, 0.8]
    assert document_vector(emb) == pytest.approx([0.6, 0.8])


def test_document_vector_empty_or_malformed_returns_empty() -> None:
    assert document_vector({}) == []
    assert document_vector({"chunks": []}) == []
    assert document_vector({"chunks": "nope"}) == []
    assert document_vector({"chunks": [{"vector": []}]}) == []
    assert document_vector({"chunks": [{"vector": [0.0, 0.0]}]}) == []  # zero norm
    assert document_vector({"chunks": ["not-a-mapping"]}) == []


def test_document_vector_skips_mismatched_dimension_chunks() -> None:
    # first valid vector sets dim=2; the 3-d vector is skipped, not zero-padded
    emb = {"chunks": [{"vector": [3.0, 0.0]}, {"vector": [1.0, 1.0, 1.0]}]}
    assert document_vector(emb) == pytest.approx([1.0, 0.0])


def test_document_vector_rejects_non_numeric_and_bool() -> None:
    assert document_vector({"chunks": [{"vector": [1.0, "x"]}]}) == []
    assert document_vector({"chunks": [{"vector": [True, False]}]}) == []


def test_cosine_basic_cases() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_guards() -> None:
    assert cosine([1.0], [1.0, 2.0]) == 0.0  # length mismatch
    assert cosine([], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero norm
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_crossdoc_vectors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.crossdoc'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/fdi/core/crossdoc.py
"""Cross-document entity resolution primitives (pure; no I/O, config, clock, RNG).

Documents are grouped into real-world entities (companies) by clustering their
whole-document embedding vectors: single-linkage connected components over the
"cosine >= threshold" graph. A document's vector is the SP-13 per-section chunk
vectors mean-pooled and L2-normalized. Every input is read as a plain
Mapping/Sequence -- no import of fdi.core.knowledge or fdi.core.embeddings, the
same "read defensively, never import the producer's types" posture consolidate/
build_graph take. Never raises: malformed/partial input yields a smaller/emptier
result, never an exception.
"""

import math
from collections.abc import Mapping, Sequence


def _as_float_vector(value: object) -> list[float]:
    """A Sequence of real (non-bool) numbers -> list[float]; anything else -> []."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    out: list[float] = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return []
        out.append(float(x))
    return out


def document_vector(embedding: Mapping[str, object]) -> list[float]:
    chunks = embedding.get("chunks")
    if not isinstance(chunks, list):
        return []
    vectors: list[list[float]] = []
    dim: int | None = None
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        vec = _as_float_vector(chunk.get("vector"))
        if not vec:
            continue
        if dim is None:
            dim = len(vec)
        if len(vec) != dim:
            continue
        vectors.append(vec)
    if not vectors or dim is None:
        return []
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(c * c for c in mean))
    if norm == 0.0:
        return []
    return [c / norm for c in mean]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_crossdoc_vectors.py -v`
Expected: PASS (all cases).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/crossdoc.py backend/tests/unit/test_crossdoc_vectors.py
git commit -m "feat(core): crossdoc document_vector + cosine (pure geometry primitives)"
```

---

### Task 2: `fdi.core.crossdoc` — `cluster` + `consolidate_entity`

**Files:**
- Modify: `backend/src/fdi/core/crossdoc.py` (append two functions)
- Test: `backend/tests/unit/test_crossdoc_cluster.py`

**Interfaces:**
- Consumes: `cosine` from Task 1.
- Produces:
  - `cluster(vectors: Mapping[str, Sequence[float]], threshold: float) -> list[list[str]]` — single-linkage connected components over the `cosine >= threshold` graph; each component a sorted `doc_id` list, the list sorted by each component's min `doc_id`; order-independent/deterministic.
  - `consolidate_entity(members: list[Mapping[str, object]]) -> dict[str, object]` — folds members (`{doc_id, doc_key, version, knowledge}`) into the entity record `{schema_version, entity_id, member_count, members, concepts, concept_presence, quality_trend}`; version-ordered; `entity_id` = min member `doc_id`; degrades on missing `knowledge`; never raises.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_crossdoc_cluster.py
from fdi.core.crossdoc import cluster, consolidate_entity


def _k(concepts: list[str], ok: bool = True, errors: int = 0, warnings: int = 0,
       section_count: int = 0, concept_count: int | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "section_count": section_count,
        "concepts": concepts,
        "concept_count": len(concepts) if concept_count is None else concept_count,
        "quality": {"ok": ok, "error_count": errors, "warning_count": warnings},
    }


def test_cluster_two_groups_and_a_singleton() -> None:
    vectors = {
        "a": [1.0, 0.0],
        "b": [0.99, 0.14],   # ~cos 0.99 with a
        "c": [0.0, 1.0],     # orthogonal to a/b -> its own entity
    }
    assert cluster(vectors, 0.85) == [["a", "b"], ["c"]]


def test_cluster_single_linkage_chains_transitively() -> None:
    # a~b and b~c but a NOT ~c directly; single-linkage still merges all three.
    vectors = {
        "a": [1.0, 0.0],
        "b": [0.72, 0.69],   # ~cos 0.72 with a, ~0.72 with c
        "c": [0.0, 1.0],
    }
    assert cluster(vectors, 0.70) == [["a", "b", "c"]]
    # at a stricter threshold a and c split off from b's reach
    assert cluster(vectors, 0.99) == [["a"], ["b"], ["c"]]


def test_cluster_is_order_independent() -> None:
    base = {"a": [1.0, 0.0], "b": [0.99, 0.14], "c": [0.0, 1.0]}
    reordered = {"c": base["c"], "b": base["b"], "a": base["a"]}
    assert cluster(base, 0.85) == cluster(reordered, 0.85)


def test_cluster_empty() -> None:
    assert cluster({}, 0.85) == []


def test_consolidate_entity_folds_members_version_ordered() -> None:
    members = [
        {"doc_id": "d2", "doc_key": "acme", "version": 2,
         "knowledge": _k(["MARGIN", "REVENUE"], section_count=13)},
        {"doc_id": "d1", "doc_key": "acme", "version": 1,
         "knowledge": _k(["REVENUE"], warnings=1, section_count=12)},
    ]
    entity = consolidate_entity(members)
    assert entity["schema_version"] == 1
    assert entity["entity_id"] == "d1"           # min doc_id
    assert entity["member_count"] == 2
    assert [m["version"] for m in entity["members"]] == [1, 2]   # version-ordered
    assert entity["concepts"] == ["MARGIN", "REVENUE"]           # sorted union
    assert entity["concept_presence"] == {"MARGIN": [2], "REVENUE": [1, 2]}
    assert entity["quality_trend"] == [
        {"version": 1, "ok": True, "error_count": 0, "warning_count": 1},
        {"version": 2, "ok": True, "error_count": 0, "warning_count": 0},
    ]


def test_consolidate_entity_version_none_sorts_last_and_uses_doc_id_presence() -> None:
    members = [
        {"doc_id": "z", "doc_key": None, "version": None, "knowledge": _k(["TAX"])},
        {"doc_id": "a", "doc_key": "k", "version": 1, "knowledge": _k(["TAX"])},
    ]
    entity = consolidate_entity(members)
    assert [m["doc_id"] for m in entity["members"]] == ["a", "z"]   # None ordered last
    assert entity["concept_presence"] == {"TAX": [1, "z"]}          # doc_id for the None-version


def test_consolidate_entity_missing_knowledge_degrades() -> None:
    members = [{"doc_id": "a", "doc_key": "k", "version": 1}]  # no knowledge key
    entity = consolidate_entity(members)
    assert entity["members"] == [
        {"doc_id": "a", "doc_key": "k", "version": 1,
         "section_count": 0, "concept_count": 0, "quality_ok": False}
    ]
    assert entity["concepts"] == []
    assert entity["concept_presence"] == {}
    assert entity["quality_trend"] == [
        {"version": 1, "ok": False, "error_count": 0, "warning_count": 0}
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_crossdoc_cluster.py -v`
Expected: FAIL — `ImportError: cannot import name 'cluster'`.

- [ ] **Step 3: Write minimal implementation** (append to `backend/src/fdi/core/crossdoc.py`)

```python
def cluster(vectors: Mapping[str, Sequence[float]], threshold: float) -> list[list[str]]:
    """Single-linkage connected components over the cosine>=threshold graph.

    Deterministic: output is sorted, not incidentally ordered. Union-find attaches
    the larger root under the smaller so each component's root is its min doc_id.
    """
    ids = sorted(vectors)
    parent: dict[str, str] = {i: i for i in ids}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path-compress
            parent[x], x = root, parent[x]
        return root

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if cosine(vectors[ids[i]], vectors[ids[j]]) >= threshold:
                union(ids[i], ids[j])

    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def consolidate_entity(members: list[Mapping[str, object]]) -> dict[str, object]:
    """Fold clustered members ({doc_id, doc_key, version, knowledge}) into the
    entity record. Version-ordered (None last, doc_id tie-break); entity_id = min
    doc_id; degrades on missing/malformed knowledge; never raises."""

    def sort_key(m: Mapping[str, object]) -> tuple[int, int, str]:
        doc_id = m.get("doc_id")
        doc_id_s = doc_id if isinstance(doc_id, str) else ""
        v = _int_or_none(m.get("version"))
        return (0, v, doc_id_s) if v is not None else (1, 0, doc_id_s)

    ordered = sorted(members, key=sort_key)
    doc_ids = [m.get("doc_id") for m in ordered if isinstance(m.get("doc_id"), str)]
    entity_id = min(doc_ids) if doc_ids else ""

    member_views: list[dict[str, object]] = []
    concepts_set: set[str] = set()
    concept_presence: dict[str, list[object]] = {}
    quality_trend: list[dict[str, object]] = []

    for m in ordered:
        doc_id = m.get("doc_id")
        doc_id_s = doc_id if isinstance(doc_id, str) else ""
        doc_key = m.get("doc_key")
        version = _int_or_none(m.get("version"))
        knowledge = m.get("knowledge")
        k: Mapping[str, object] = knowledge if isinstance(knowledge, Mapping) else {}
        quality = k.get("quality")
        q: Mapping[str, object] = quality if isinstance(quality, Mapping) else {}
        ok = q.get("ok")
        ok_b = ok if isinstance(ok, bool) else False
        presence_key: object = version if version is not None else doc_id_s

        member_views.append({
            "doc_id": doc_id_s,
            "doc_key": doc_key if isinstance(doc_key, str) else None,
            "version": version,
            "section_count": _int_or_none(k.get("section_count")) or 0,
            "concept_count": _int_or_none(k.get("concept_count")) or 0,
            "quality_ok": ok_b,
        })

        concepts_raw = k.get("concepts")
        if isinstance(concepts_raw, list):
            for c in concepts_raw:
                if isinstance(c, str):
                    concepts_set.add(c)
                    concept_presence.setdefault(c, []).append(presence_key)

        quality_trend.append({
            "version": version,
            "ok": ok_b,
            "error_count": _int_or_none(q.get("error_count")) or 0,
            "warning_count": _int_or_none(q.get("warning_count")) or 0,
        })

    return {
        "schema_version": 1,
        "entity_id": entity_id,
        "member_count": len(ordered),
        "members": member_views,
        "concepts": sorted(concepts_set),
        "concept_presence": {c: concept_presence[c] for c in sorted(concept_presence)},
        "quality_trend": quality_trend,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_crossdoc_cluster.py -v`
Expected: PASS. Also run `tests/unit/test_crossdoc_vectors.py` to confirm no regression.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/crossdoc.py backend/tests/unit/test_crossdoc_cluster.py
git commit -m "feat(core): crossdoc cluster (single-linkage) + consolidate_entity"
```

---

### Task 3: Config — `FDI_ENTITY_SIM_THRESHOLD` (0.85) across the inert-config trio

**Files:**
- Modify: `backend/src/fdi/platform/config.py` (add field after the milvus block, ~line 98)
- Modify: `docker/compose.yml` (`x-backend-env`, after `FDI_MILVUS_COLLECTION`, ~line 46)
- Modify: `.env.example` (near the Milvus vars)
- Modify: `backend/tests/unit/test_config.py` (assert the default)

**Interfaces:**
- Produces: `Settings.entity_sim_threshold: float = 0.85` reachable as `settings.entity_sim_threshold`.

- [ ] **Step 1: Write the failing test** (append to `backend/tests/unit/test_config.py`)

```python
def test_entity_resolution_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.entity_sim_threshold == 0.85
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_config.py::test_entity_resolution_settings_default -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'entity_sim_threshold'`.

- [ ] **Step 3: Add the field + mirror across the trio**

In `backend/src/fdi/platform/config.py`, after the `milvus_collection` line (~98):

```python
    # Cross-document entity resolution (SP-16). Cosine-similarity threshold for
    # single-linkage clustering of whole-document embedding vectors: two documents
    # join the same entity when their vectors are cosine-similar at or above this.
    # 0.85 balances recall (a company's filings across years cluster together)
    # against precision (distinct companies stay apart); tune against real data via
    # this var without a redeploy, or per-request via POST /entities/resolve's body.
    entity_sim_threshold: float = 0.85
```

In `docker/compose.yml`, in `x-backend-env` after the `FDI_MILVUS_COLLECTION` line (~46):

```yaml
  FDI_ENTITY_SIM_THRESHOLD: ${FDI_ENTITY_SIM_THRESHOLD}
```

In `.env.example`, near the Milvus block, add:

```bash
# Cross-document entity resolution (SP-16): cosine threshold for clustering
# document embedding vectors into entities. Override per-request via the
# POST /entities/resolve body; this is the default.
FDI_ENTITY_SIM_THRESHOLD=0.85
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_config.py -v`
Expected: PASS (new test + all existing config tests, incl. `test_unknown_setting_is_rejected`).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/platform/config.py docker/compose.yml .env.example backend/tests/unit/test_config.py
git commit -m "feat(config): FDI_ENTITY_SIM_THRESHOLD (0.85) across the inert-config trio"
```

---

### Task 4: Repository — `RegistryRepository.list_documents()`

**Files:**
- Modify: `backend/src/fdi/platform/db/repository.py` (add method to `RegistryRepository`, after `latest_runs`, ~line 97)
- Test: `backend/tests/integration/test_registry_list_documents.py`

**Interfaces:**
- Consumes: `Document` model (already imported in `repository.py`), `sqlalchemy` (already imported).
- Produces: `RegistryRepository.list_documents() -> list[Document]` — every `Document` row, ordered by `id` (deterministic).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_registry_list_documents.py
import pytest

from fdi.platform.db.repository import RegistryRepository

pytestmark = pytest.mark.integration


async def test_list_documents_returns_all_ordered_by_id(session_factory) -> None:
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.create_document("d-b")
        await repo.create_document("d-a")
        await repo.create_document("d-c")
        await s.commit()
    async with session_factory() as s:
        docs = await RegistryRepository(s).list_documents()
    assert [d.id for d in docs] == ["d-a", "d-b", "d-c"]


async def test_list_documents_empty(session_factory) -> None:
    async with session_factory() as s:
        docs = await RegistryRepository(s).list_documents()
    assert docs == []
```

> **Note on the `session_factory` fixture:** use the same integration DB fixture the existing `backend/tests/integration/` suite uses (grep `tests/integration/` and `tests/conftest.py` for the fixture that yields an `async_sessionmaker` against the testcontainers Postgres — e.g. what `test_run.py`/`test_intake.py` consume). Match its exact name/signature; if it is named differently (e.g. `session_factory`, `db_session_factory`), use that name verbatim in both tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_registry_list_documents.py -v`
Expected: FAIL — `AttributeError: 'RegistryRepository' object has no attribute 'list_documents'`.

- [ ] **Step 3: Add the method** to `RegistryRepository` (after `latest_runs`, ~line 97)

```python
    async def list_documents(self) -> list[Document]:
        """Every document in the corpus, ordered by id (deterministic).

        Corpus-scale note (SP-16): this materializes all documents; at 5000+ docs
        a streamed/paginated variant is the scale path, deferred with on-demand
        entity resolution (spec §6)."""
        rows = (
            await self._s.execute(sqlalchemy.select(Document).order_by(Document.id))
        ).scalars().all()
        return list(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_registry_list_documents.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/platform/db/repository.py backend/tests/integration/test_registry_list_documents.py
git commit -m "feat(platform): RegistryRepository.list_documents() -- enumerate the corpus"
```

---

### Task 5: Orchestrator — `fdi.pipeline.crossdoc.resolve_entities`

**Files:**
- Create: `backend/src/fdi/pipeline/crossdoc.py`
- Test: `backend/tests/integration/test_resolve_entities.py`

**Interfaces:**
- Consumes: `document_vector`, `cluster`, `consolidate_entity` (Task 1/2); `RegistryRepository` incl. `list_documents` (Task 4) + `latest_run` (existing); `ArtifactStore.get_json` (existing).
- Produces: `resolve_entities(*, session: AsyncSession, store: ArtifactStore, threshold: float) -> dict[str, object]` — `{schema_version, threshold, entity_count, entities, unresolved}`.

- [ ] **Step 1: Write the failing test**

This test drives the full pipeline for three docs, then resolves. Reuse the integration harness the existing `tests/integration/test_knowledge_stage.py` uses (a `FakeOcrClient` + `FakeSemanticClient` + `run_stage` over a LOCAL `StageRegistry`, real testcontainers Postgres+MinIO). Set the `FakeEmbeddingClient` so docs A and B embed to near-identical vectors and C to a distinct one.

```python
# backend/tests/integration/test_resolve_entities.py
import pytest

from fdi.pipeline.crossdoc import resolve_entities

pytestmark = pytest.mark.integration


async def test_resolve_entities_two_entities_from_three_docs(
    session_factory, store, resolved_corpus
) -> None:
    """`resolved_corpus` ingests + fully processes 3 docs: A,B similar, C distinct
    (see conftest note). Default threshold 0.85 -> {A,B} and {C}."""
    async with session_factory() as s:
        result = await resolve_entities(session=s, store=store, threshold=0.85)

    assert result["schema_version"] == 1
    assert result["entity_count"] == 2
    entities = result["entities"]
    members = [sorted(m["doc_id"] for m in e["members"]) for e in entities]
    assert resolved_corpus["a"] in members[0] and resolved_corpus["b"] in members[0]
    assert members[1] == [resolved_corpus["c"]]
    assert result["unresolved"] == []


async def test_resolve_entities_strict_threshold_splits(
    session_factory, store, resolved_corpus
) -> None:
    async with session_factory() as s:
        result = await resolve_entities(session=s, store=store, threshold=0.999)
    assert result["entity_count"] == 3  # A,B no longer merge


async def test_resolve_entities_unembedded_doc_is_unresolved(
    session_factory, store, resolved_corpus, unembedded_doc_id
) -> None:
    async with session_factory() as s:
        result = await resolve_entities(session=s, store=store, threshold=0.85)
    assert unembedded_doc_id in result["unresolved"]
    for e in result["entities"]:
        assert unembedded_doc_id not in [m["doc_id"] for m in e["members"]]


async def test_resolve_entities_empty_corpus(session_factory, store) -> None:
    async with session_factory() as s:
        result = await resolve_entities(session=s, store=store, threshold=0.85)
    assert result == {
        "schema_version": 1, "threshold": 0.85,
        "entity_count": 0, "entities": [], "unresolved": [],
    }
```

> **Fixture note (write these in this test module or the integration `conftest.py`):**
> - `store` — the `ArtifactStore` bound to the testcontainers MinIO (same one `test_knowledge_stage.py` builds; reuse its construction).
> - `resolved_corpus` — intakes 3 PDFs via `intake_document`, then runs `run_stage("ocr", ...)` (auto-enqueues the whole DAG through `embedding` + `knowledge`) for each, with a `FakeEmbeddingClient` whose `embed(texts)` returns vectors making A≈B (cosine ≥ 0.85) and C distinct (< 0.85). Return `{"a": doc_a.id, "b": doc_b.id, "c": doc_c.id}`. The simplest deterministic fake: map each document to a fixed base vector by a per-doc seed injected via the fake (e.g. A and B share a base direction plus tiny noise, C orthogonal).
> - `unembedded_doc_id` — a 4th `intake_document` with NO stage run (or only `ocr`), so it has no `embedding` artifact.
> - `test_resolve_entities_empty_corpus` runs against a fresh DB with no documents — ensure it does not depend on `resolved_corpus`.
> If the existing harness injects the embedding client differently (grep `FakeEmbeddingClient` usage in `tests/integration/test_embedding_stage.py`), follow that exact injection path.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_resolve_entities.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.pipeline.crossdoc'`.

- [ ] **Step 3: Write the orchestrator**

```python
# backend/src/fdi/pipeline/crossdoc.py
"""Corpus-level entity resolution orchestrator.

Enumerates every document, reads each one's already-cached `embedding` and
`knowledge` artifacts, clusters documents on embedding similarity, and folds each
cluster into an entity record. A pipeline-layer service that imports platform
(repository + store) and core -- the same shape as fdi.pipeline.intake and
fdi.pipeline.eval_service, called by an API route. pipeline -> platform -> core
is legal under the layers contract. Makes no new embedding call: the vectors are
the SP-13 per-section vectors already stored, mean-pooled by fdi.core.crossdoc.
"""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.crossdoc import cluster, consolidate_entity, document_vector
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def _read_artifact(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str, stage: str
) -> Mapping[str, object] | None:
    """The latest `stage` artifact for `doc_id`, or None -- same read path as
    the GET /documents/{id}/artifacts/{stage} route."""
    run = await repo.latest_run(doc_id, stage)
    if run is None or run.artifact_key is None:
        return None
    return cast(Mapping[str, object], await store.get_json(run.artifact_key))


async def resolve_entities(
    *, session: AsyncSession, store: ArtifactStore, threshold: float
) -> dict[str, object]:
    repo = RegistryRepository(session)
    documents = await repo.list_documents()

    vectors: dict[str, list[float]] = {}
    stash: dict[str, dict[str, object]] = {}
    unresolved: list[str] = []

    for doc in documents:
        embedding = await _read_artifact(repo, store, doc.id, "embedding")
        vec = document_vector(embedding) if embedding is not None else []
        if not vec:
            unresolved.append(doc.id)
            continue
        knowledge = await _read_artifact(repo, store, doc.id, "knowledge")
        vectors[doc.id] = vec
        stash[doc.id] = {
            "doc_id": doc.id,
            "doc_key": doc.doc_key,
            "version": doc.version,
            "knowledge": knowledge if knowledge is not None else {},
        }

    components = cluster(vectors, threshold)
    entities = [consolidate_entity([stash[d] for d in comp]) for comp in components]

    return {
        "schema_version": 1,
        "threshold": threshold,
        "entity_count": len(entities),
        "entities": entities,
        "unresolved": sorted(unresolved),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_resolve_entities.py -v`
Expected: PASS (2 entities at 0.85; 3 at 0.999; unembedded doc in `unresolved`; empty corpus zeroed).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/crossdoc.py backend/tests/integration/test_resolve_entities.py
git commit -m "feat(pipeline): resolve_entities orchestrator -- cluster corpus into entities"
```

---

### Task 6: API — `POST /entities/resolve` (schemas + threshold dep + lifespan + route)

**Files:**
- Modify: `backend/src/fdi/api/schemas.py` (append the entity views + request)
- Modify: `backend/src/fdi/api/deps.py` (add `get_entity_threshold` + `EntityThresholdDep`)
- Modify: `backend/src/fdi/api/app.py` (lifespan: seed `app.state.entity_threshold`, after line 74)
- Modify: `backend/src/fdi/api/routes.py` (imports + the route)
- Test: `backend/tests/integration/test_api.py` (add entity-resolve cases; reuse the app/client fixture there)

**Interfaces:**
- Consumes: `resolve_entities` (Task 5); `SessionDep`, `StoreDep` (existing); `Settings.entity_sim_threshold` (Task 3).
- Produces: `POST /entities/resolve` → `EntitiesView`; `EntityResolveRequest{threshold: float|None}`; `EntityThresholdDep`.

- [ ] **Step 1: Write the failing test** (add to `backend/tests/integration/test_api.py`)

Reuse the existing test app/client fixture in that file (grep the module for how `POST /search` or `POST /documents` is exercised — same `client`/`app` fixture, same testcontainers wiring). Build the same 3-doc corpus as Task 5 (or reuse a shared fixture).

```python
async def test_entities_resolve_returns_entities(client, resolved_corpus_via_api) -> None:
    resp = await client.post("/entities/resolve", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["entity_count"] == 2
    assert body["threshold"] == 0.85  # configured default


async def test_entities_resolve_threshold_override(client, resolved_corpus_via_api) -> None:
    resp = await client.post("/entities/resolve", json={"threshold": 0.999})
    assert resp.status_code == 200
    assert resp.json()["entity_count"] == 3


async def test_entities_resolve_rejects_out_of_range_threshold(client) -> None:
    resp = await client.post("/entities/resolve", json={"threshold": 1.5})
    assert resp.status_code == 422


async def test_entities_resolve_empty_corpus(client_empty_db) -> None:
    resp = await client_empty_db.post("/entities/resolve", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["entity_count"] == 0 and body["entities"] == []
```

> **Fixture note:** `resolved_corpus_via_api` = the 3-doc processed corpus reachable through the test app's DB/store (reuse the `resolved_corpus` construction from Task 5, bound to the app fixture's session_factory/store — factor it into the integration `conftest.py` if cleaner). `client_empty_db` = the app client against a fresh empty DB (many suites already have a clean-DB client fixture; reuse it, else assert `entity_count == 0` on the same client before any corpus is created). Match existing fixture names in `test_api.py` verbatim.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k entities_resolve -v`
Expected: FAIL — 404 (route not registered) / import errors.

- [ ] **Step 3: Implement schemas, dep, lifespan, route**

In `backend/src/fdi/api/schemas.py`, ensure `Field` is imported (`from pydantic import BaseModel, Field`) and append:

```python
class EntityMemberView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    section_count: int
    concept_count: int
    quality_ok: bool


class QualityTrendPoint(BaseModel):
    version: int | None
    ok: bool
    error_count: int
    warning_count: int


class EntityView(BaseModel):
    schema_version: int
    entity_id: str
    member_count: int
    members: list[EntityMemberView]
    concepts: list[str]
    # presence value is a member version (int) or, for a version-less member, its doc_id (str)
    concept_presence: dict[str, list[int | str]]
    quality_trend: list[QualityTrendPoint]


class EntityResolveRequest(BaseModel):
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class EntitiesView(BaseModel):
    schema_version: int
    threshold: float
    entity_count: int
    entities: list[EntityView]
    unresolved: list[str]
```

In `backend/src/fdi/api/deps.py`, after `get_vector_store` (~line with `EmbedderDep`/`VectorStoreDep`), add the function and, in the `*Dep` block at the bottom, the annotation:

```python
def get_entity_threshold(request: Request) -> float:
    threshold: float = request.app.state.entity_threshold
    return threshold
```

```python
EntityThresholdDep = Annotated[float, Depends(get_entity_threshold)]
```

In `backend/src/fdi/api/app.py` lifespan, after line 74 (`app.state.vector_store = ...`):

```python
    app.state.entity_threshold = settings.entity_sim_threshold
```

In `backend/src/fdi/api/routes.py`: add `EntityThresholdDep` to the `fdi.api.deps` import block; add `EntitiesView, EntityResolveRequest` to the `fdi.api.schemas` import block; add `from fdi.pipeline.crossdoc import resolve_entities` next to the existing `from fdi.pipeline.eval_service import run_evaluation`; then add the route (near `/search`):

```python
@router.post("/entities/resolve", response_model=EntitiesView)
async def resolve_entities_route(
    body: EntityResolveRequest,
    session: SessionDep,
    store: StoreDep,
    default_threshold: EntityThresholdDep,
) -> EntitiesView:
    """Corpus-level cross-document entity resolution (on-demand, task-free like
    /search). Clusters documents on embedding similarity and returns entity
    records; body `threshold` overrides the configured default for this call."""
    threshold = body.threshold if body.threshold is not None else default_threshold
    result = await resolve_entities(session=session, store=store, threshold=threshold)
    return EntitiesView.model_validate(result)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k entities_resolve -v`
Expected: PASS (200 with 2 entities + default threshold echoed; override → 3; 1.5 → 422; empty → 0).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/schemas.py backend/src/fdi/api/deps.py backend/src/fdi/api/app.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(api): POST /entities/resolve -- on-demand cross-document entity resolution"
```

---

### Task 7: Done-criteria audit, spec status, roadmap (ingestion layer complete)

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-sp16-crossdoc-entity-resolution-design.md` (Status → Implemented)
- Modify: `docs/architecture/ingestion-roadmap.md` (SP-16 row → ✅; status line → ingestion layer complete; add a "What SP-16 shipped" handoff)

**Interfaces:** none (docs + verification only).

- [ ] **Step 1: Full static gates**

Run:
```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
```
Expected: ruff "All checks passed!"; mypy "Success: no issues found"; lint-imports "Contracts: 1 kept, 0 broken." (Confirm `fdi.core.crossdoc` did NOT import `fdi.core.knowledge`/`fdi.core.embeddings` — grep to be sure.)

- [ ] **Step 2: Full non-e2e suite**

Run: `uv run --directory backend pytest -q`
Expected: green (e2e + azure + neo4j + milvus + bge skip). Note the passed count (should be prior 317 + the SP-16 unit/integration tests added).

- [ ] **Step 3: Audit spec §7 done criteria** — map each of the 5 criteria to what shipped (Task 1/2 → criterion 1; Task 4/5 → criterion 2; Task 3/6 → criteria 3–4; Tasks 1–6 gates → criterion 5). Confirm no new Stage was registered (grep `stages/__init__.py` — unchanged) and entity resolution is corpus-level per decision 8.

- [ ] **Step 4: Spec status → Implemented** — set the spec's Status line to `Implemented 2026-07-18` (only after Steps 1–3 are actually green — do not pre-declare).

- [ ] **Step 5: Roadmap** — flip the SP-16 table row to `✅ done (thin: embedding-similarity clustering, on-demand API; Milvus-ANN scale, materialized persistence, content-name resolution, cross-doc Neo4j merge, numeric roll-ups deferred)`; update the top status line to record SP-16 done and the **ingestion layer roadmap complete** (SP-1…SP-18 all landed); add a short "What SP-16 shipped" handoff (the `entities/resolve` API + `fdi.core.crossdoc`, the deferred scale/persistence/graph-merge follow-ons).

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: SP-16 done-criteria audit, spec status, roadmap (ingestion layer complete)"
```

---

## Self-Review

**Spec coverage:**
- §2 decision 1 (embedding cluster key) → Tasks 1,2,5. Decision 2 (mean-pool vector) → Task 1 `document_vector`. Decision 3 (single-linkage) → Task 2 `cluster` + determinism test. Decision 4 (on-demand API) → Task 6 route. Decision 5 (threshold config + override) → Tasks 3,6. Decision 6 (version ordering) → Task 2 `consolidate_entity`. Decision 7 (record shape) → Task 2 + Task 6 schemas. Decision 8 (layering, no new Stage) → Tasks 5,6 + Task 7 audit. Decision 9 (unresolved) → Task 5 + tests. Decision 10 (missing knowledge degrades) → Task 2 + Task 5.
- §3.1 core → Tasks 1,2. §3.2 orchestrator → Task 5. §3.3 API → Task 6. §3.4 config → Task 3.
- §4 error handling → covered by Task 1/2 unit tests (malformed embedding/knowledge, singleton, dimension-mismatch via cosine guard) + Task 6 (422 out-of-range, empty corpus).
- §5 testing → Tasks 1,2 (unit), 5,6 (integration), 3 (config default).
- §6 deferred → not built (asserted in Task 7 audit).
- §7 done criteria → Task 7.

**Placeholder scan:** No "TBD/TODO"; every code step shows complete code. The fixture-name notes in Tasks 4–6 are explicit instructions to reuse a NAMED existing fixture (grep-and-match), not deferred work — the existing integration suite (`test_knowledge_stage.py`, `test_embedding_stage.py`, `test_api.py`) already provides the OCR/semantic/embedding fakes and testcontainers harness this plan composes; the executor must bind to those verbatim rather than invent new infra.

**Type consistency:** `document_vector(Mapping)->list[float]`, `cosine(Sequence,Sequence)->float`, `cluster(Mapping[str,Sequence[float]],float)->list[list[str]]`, `consolidate_entity(list[Mapping])->dict`, `resolve_entities(*,session,store,threshold)->dict` — names/signatures identical across Tasks 1,2,5,6. `concept_presence` typed `dict[str, list[int | str]]` in the schema matches the core's `list[object]` returns (int version or str doc_id) — the Pydantic model narrows it; `EntityView` fields match `consolidate_entity`'s keys exactly (`schema_version, entity_id, member_count, members, concepts, concept_presence, quality_trend`) and `EntityMemberView` matches the member dict (`doc_id, doc_key, version, section_count, concept_count, quality_ok`). `EntitiesView` matches `resolve_entities`'s return keys (`schema_version, threshold, entity_count, entities, unresolved`).

**One risk flagged for the executor:** `EntityView.concept_presence: dict[str, list[int | str]]` — Pydantic v2 may coerce an `int` presence key into `str` under `list[int | str]` union ordering (it prefers the left member, `int`, so ints stay ints and doc_id strings stay strings — correct). If a coercion surprise appears in Task 6, assert against the raw `resolve_entities` dict in the pipeline test (Task 5, already does) and keep the API test focused on counts/threshold rather than exact presence typing.
