# SP-3 — Eval Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the eval-harness framework — datasets, a pure `Metric`, a candidate runner, a results registry — and prove it end to end on a toy dataset + a trivial candidate.

**Architecture:** A `Candidate` is `predict(given) -> output` with `name`/`version`. A `Metric` is pure `score(predicted, expected) -> [0,1]`. `run_eval(candidate, dataset, metric)` (pure) produces per-case scores + a mean aggregate. `eval_service` loads a dataset artifact, runs a candidate, and persists an `eval_run` + `eval_case_result` rows; a leaderboard ranks candidates by aggregate. Reuses SP-2's `ArtifactStore`, canonical JSON, registry pattern, and import contract.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async + Alembic, MinIO, Postgres, pytest + testcontainers, `uv`.

## Global Constraints

- Run tooling as `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive = true`. `core` is pure (no I/O/config/clock/RNG). Metrics + eval value types live in `core`.
- Determinism: given the same candidate+dataset+metric, case scores + aggregate are byte-identical; ids/timestamps live in Postgres rows. Evals are observations, intentionally NOT content-addressed/deduped (a new run row each time).
- Naming: avoid shadowing builtins — the case input field is `given` (not `input`); modules are `fdi.pipeline.evaluation` (not `eval`), `fdi.core.metrics`, `fdi.core.eval_types`.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.
- New `status`/domain columns get an explicit `CHECK` where they have a closed set; prove any CHECK bites with an illegal insert expecting `IntegrityError` (the SP-1/SP-2 lesson).
- Integration tests: reuse the per-file `session_factory` (from `migrated_engine`) and `store` (from `minio_client`, with `ensure_bucket()`) fixtures; the migrated DB is session-scoped, so **mint unique ids (uuid) per test** — never hardcode PKs (the SP-2 cross-test collision).

**Canonical commands:** fast `pytest -m "not integration and not e2e" -q`; integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `Metric` protocol + `exact_match` (core, pure)

**Files:** Create `backend/src/fdi/core/metrics.py`; Test `backend/tests/unit/test_metrics.py`.

**Interfaces:** Produces `class Metric(Protocol)` with `name: str` and `score(self, predicted: object, expected: object) -> float`; and `exact_match(predicted: object, expected: object) -> float` (1.0 if equal else 0.0). Also `class ExactMatch` implementing `Metric` (`name="exact_match"`, `score` delegates to `exact_match`) so a metric can be passed as an object.

- [ ] **Step 1: Write the failing test**
```python
# backend/tests/unit/test_metrics.py
from fdi.core.metrics import ExactMatch, exact_match


def test_exact_match_scores() -> None:
    assert exact_match("a", "a") == 1.0
    assert exact_match("a", "b") == 0.0
    assert exact_match({"x": 1}, {"x": 1}) == 1.0


def test_exact_match_metric_object() -> None:
    m = ExactMatch()
    assert m.name == "exact_match"
    assert m.score(1, 1) == 1.0 and m.score(1, 2) == 0.0
```

- [ ] **Step 2: Run to verify it fails** — `uv run --directory backend pytest tests/unit/test_metrics.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement**
```python
# backend/src/fdi/core/metrics.py
"""Pure evaluation metrics. score() returns [0,1]; 1.0 is perfect. No I/O — core."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Metric(Protocol):
    name: str
    def score(self, predicted: object, expected: object) -> float: ...


def exact_match(predicted: object, expected: object) -> float:
    return 1.0 if predicted == expected else 0.0


class ExactMatch:
    name = "exact_match"
    def score(self, predicted: object, expected: object) -> float:
        return exact_match(predicted, expected)
```

- [ ] **Step 4: Run to verify it passes** — `uv run --directory backend pytest tests/unit/test_metrics.py -q` → PASS.

- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/metrics.py backend/tests/unit/test_metrics.py && git commit -m "feat(core): Metric protocol and exact_match"`

---

### Task 2: Eval value types (core, pure)

**Files:** Create `backend/src/fdi/core/eval_types.py`; Test `backend/tests/unit/test_eval_types.py`.

**Interfaces:** Produces frozen dataclasses `EvalCase(case_id: str, given: object, expected: object)`, `Dataset(name: str, version: int, cases: tuple[EvalCase, ...])`, `CaseResult(case_id: str, score: float)`, `EvalOutcome(aggregate: float, results: tuple[CaseResult, ...])`. Plus `dataset_to_dict(Dataset) -> dict` and `dataset_from_dict(dict) -> Dataset` (canonical, order-preserving) for artifact storage.

- [ ] **Step 1: Write the failing test**
```python
# backend/tests/unit/test_eval_types.py
from fdi.core.eval_types import Dataset, EvalCase, dataset_from_dict, dataset_to_dict


def test_dataset_roundtrips_through_dict() -> None:
    ds = Dataset(name="toy", version=1, cases=(
        EvalCase("c1", given="a", expected="A"),
        EvalCase("c2", given="b", expected="B"),
    ))
    d = dataset_to_dict(ds)
    assert d == {"name": "toy", "version": 1, "cases": [
        {"case_id": "c1", "given": "a", "expected": "A"},
        {"case_id": "c2", "given": "b", "expected": "B"},
    ]}
    assert dataset_from_dict(d) == ds
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**
```python
# backend/src/fdi/core/eval_types.py
"""Pure value objects for evaluation. No I/O — core."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    given: object
    expected: object


@dataclass(frozen=True)
class Dataset:
    name: str
    version: int
    cases: tuple[EvalCase, ...]


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    score: float


@dataclass(frozen=True)
class EvalOutcome:
    aggregate: float
    results: tuple[CaseResult, ...]


def dataset_to_dict(ds: Dataset) -> dict[str, object]:
    return {
        "name": ds.name,
        "version": ds.version,
        "cases": [
            {"case_id": c.case_id, "given": c.given, "expected": c.expected}
            for c in ds.cases
        ],
    }


def dataset_from_dict(d: dict[str, object]) -> Dataset:
    cases = tuple(
        EvalCase(case_id=str(c["case_id"]), given=c["given"], expected=c["expected"])
        for c in d["cases"]  # type: ignore[union-attr]
    )
    return Dataset(name=str(d["name"]), version=int(d["version"]), cases=cases)  # type: ignore[call-overload]
```
(If `mypy --strict` objects to `d["cases"]` iteration typing, refine with explicit `cast`s from `typing` rather than broad ignores.)

- [ ] **Step 4: Run to verify it passes** — PASS.

- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/eval_types.py backend/tests/unit/test_eval_types.py && git commit -m "feat(core): eval value types + dataset dict round-trip"`

---

### Task 3: `Candidate` protocol, registry, and `run_eval` (pipeline, pure)

**Files:** Create `backend/src/fdi/pipeline/evaluation.py`; Test `backend/tests/unit/test_run_eval.py`.

**Interfaces:** Produces `class Candidate(Protocol)` (`name: str`, `version: int`, `predict(self, given: object) -> object`); `class CandidateRegistry` (`register`, `get`, `all` — duplicate-name raises, mirror `StageRegistry`) + module singleton `CANDIDATES`; and `run_eval(candidate: Candidate, dataset: Dataset, metric: Metric) -> EvalOutcome` — per-case `metric.score(candidate.predict(c.given), c.expected)`, asserts each score in `[0,1]` (raise `ValueError` otherwise), aggregate = mean (0.0 for empty), propagates a candidate exception.

- [ ] **Step 1: Write the failing test**
```python
# backend/tests/unit/test_run_eval.py
import pytest

from fdi.core.eval_types import Dataset, EvalCase
from fdi.core.metrics import ExactMatch
from fdi.pipeline.evaluation import CandidateRegistry, run_eval


class _Upper:
    name = "upper"
    version = 1
    def predict(self, given: object) -> object:
        return str(given).upper()


def _toy() -> Dataset:
    return Dataset("toy", 1, (
        EvalCase("c1", "a", "A"),   # correct
        EvalCase("c2", "b", "X"),   # wrong
    ))


def test_run_eval_scores_and_aggregates() -> None:
    out = run_eval(_Upper(), _toy(), ExactMatch())
    assert [r.score for r in out.results] == [1.0, 0.0]
    assert out.aggregate == 0.5


def test_empty_dataset_aggregates_zero() -> None:
    assert run_eval(_Upper(), Dataset("e", 1, ()), ExactMatch()).aggregate == 0.0


def test_out_of_range_metric_raises() -> None:
    class _Bad:
        name = "bad"
        def score(self, predicted: object, expected: object) -> float:
            return 2.0
    with pytest.raises(ValueError, match="out of range"):
        run_eval(_Upper(), _toy(), _Bad())


def test_candidate_exception_propagates() -> None:
    class _Boom:
        name = "boom"; version = 1
        def predict(self, given: object) -> object:
            raise RuntimeError("kaboom")
    with pytest.raises(RuntimeError, match="kaboom"):
        run_eval(_Boom(), _toy(), ExactMatch())


def test_registry_rejects_duplicate() -> None:
    r = CandidateRegistry()
    r.register(_Upper())
    with pytest.raises(ValueError, match="already registered"):
        r.register(_Upper())
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**
```python
# backend/src/fdi/pipeline/evaluation.py
"""The eval runner + candidate registry. Pure orchestration over core types."""

from typing import Protocol, runtime_checkable

from fdi.core.eval_types import CaseResult, Dataset, EvalOutcome
from fdi.core.metrics import Metric


@runtime_checkable
class Candidate(Protocol):
    name: str
    version: int
    def predict(self, given: object) -> object: ...


class CandidateRegistry:
    def __init__(self) -> None:
        self._c: dict[str, Candidate] = {}
    def register(self, candidate: Candidate) -> None:
        if candidate.name in self._c:
            raise ValueError(f"candidate {candidate.name!r} already registered")
        self._c[candidate.name] = candidate
    def get(self, name: str) -> Candidate:
        return self._c[name]
    def all(self) -> dict[str, Candidate]:
        return dict(self._c)


CANDIDATES = CandidateRegistry()


def run_eval(candidate: Candidate, dataset: Dataset, metric: Metric) -> EvalOutcome:
    results: list[CaseResult] = []
    for case in dataset.cases:
        score = metric.score(candidate.predict(case.given), case.expected)
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"metric {metric.name!r} score out of range: {score}")
        results.append(CaseResult(case.case_id, score))
    aggregate = sum(r.score for r in results) / len(results) if results else 0.0
    return EvalOutcome(aggregate=aggregate, results=tuple(results))
```

- [ ] **Step 4: Run to verify it passes** — PASS (5 tests).

- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/evaluation.py backend/tests/unit/test_run_eval.py && git commit -m "feat(pipeline): Candidate protocol, registry, pure run_eval"`

---

### Task 4: Dataset storage via `ArtifactStore` (pipeline)

**Files:** Create `backend/src/fdi/pipeline/dataset_store.py`; Test `backend/tests/integration/test_dataset_store.py`.

**Interfaces:** Consumes `ArtifactStore` (SP-1/2), `dataset_to_dict`/`dataset_from_dict` (Task 2). Produces `dataset_key(name: str, version: int) -> str` → `f"ground-truth/{name}/v{version}.json"`; `async def put_dataset(store: ArtifactStore, ds: Dataset) -> str` (canonical JSON at the key, returns key); `async def get_dataset(store: ArtifactStore, name: str, version: int) -> Dataset`.

- [ ] **Step 1: Write the failing test**
```python
# backend/tests/integration/test_dataset_store.py
from fdi.core.eval_types import Dataset, EvalCase
from fdi.pipeline.dataset_store import dataset_key, get_dataset, put_dataset
# reuse the local `store` fixture pattern from test_artifact_store.py (minio_client + ensure_bucket)


async def test_dataset_roundtrips_through_store(store) -> None:
    ds = Dataset("toy", 1, (EvalCase("c1", "a", "A"),))
    key = await put_dataset(store, ds)
    assert key == dataset_key("toy", 1) == "ground-truth/toy/v1.json"
    assert await get_dataset(store, "toy", 1) == ds
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**
```python
# backend/src/fdi/pipeline/dataset_store.py
"""Ground-truth datasets are canonical-JSON artifacts in the object store."""

from fdi.core.eval_types import Dataset, dataset_from_dict, dataset_to_dict
from fdi.platform.storage.artifact_store import ArtifactStore


def dataset_key(name: str, version: int) -> str:
    return f"ground-truth/{name}/v{version}.json"


async def put_dataset(store: ArtifactStore, ds: Dataset) -> str:
    key = dataset_key(ds.name, ds.version)
    await store.put_json(key, dataset_to_dict(ds))
    return key


async def get_dataset(store: ArtifactStore, name: str, version: int) -> Dataset:
    raw = await store.get_json(dataset_key(name, version))
    return dataset_from_dict(raw)
```

- [ ] **Step 4: Run to verify it passes** — add the `store` fixture (copy from `test_artifact_store.py`), then PASS.

- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/dataset_store.py backend/tests/integration/test_dataset_store.py && git commit -m "feat(pipeline): ground-truth dataset store"`

---

### Task 5: `eval_run` + `eval_case_result` tables + migration (platform db)

**Files:** Modify `backend/src/fdi/platform/db/models.py`; Create `backend/alembic/versions/0004_eval_tables.py`; Test `backend/tests/integration/test_migrations.py` (ADD).

**Interfaces:** Produces models `EvalRun(id, candidate_name, candidate_version, dataset_name, dataset_version, metric_name, aggregate: float, created_at)` and `EvalCaseResult(id, eval_run_id FK->eval_run.id, case_id, score: float)`. Head migration chains `down_revision = "0003"`, `revision = "0004"`.

- [ ] **Step 1: Write the failing test** (ADD to `test_migrations.py`, mirror the stage_run pattern — a raw INSERT proving the FK, and bump the head-version assertion to `"0004"`)
```python
async def test_eval_run_and_case_result_exist(migrated_engine) -> None:
    import sqlalchemy
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text(
            "INSERT INTO eval_run (id, candidate_name, candidate_version, dataset_name, "
            "dataset_version, metric_name, aggregate) VALUES "
            "('er1','upper',1,'toy',1,'exact_match',0.5)"))
        await conn.execute(sqlalchemy.text(
            "INSERT INTO eval_case_result (id, eval_run_id, case_id, score) "
            "VALUES ('cr1','er1','c1',1.0)"))
    # a case result whose run does not exist must violate the FK
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO eval_case_result (id, eval_run_id, case_id, score) "
                "VALUES ('cr2','NO_SUCH_RUN','c1',1.0)"))
```
Also update the existing head-version assertion in that file from `"0003"` to `"0004"`.

- [ ] **Step 2: Run to verify it fails** — missing tables / wrong head.

- [ ] **Step 3: Implement** — add the two models to `models.py` (match the SP-2 `Document`/`StageRun` idiom: `Base`, `Mapped[...]`, `DateTime(timezone=True)`, `server_default=func.now()`; `Float` for `aggregate`/`score`). Hand-write the migration (do NOT autogenerate):
```python
# 0004_eval_tables.py
revision = "0004"; down_revision = "0003"; branch_labels = None; depends_on = None
from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.create_table("eval_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("candidate_name", sa.String(128), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("dataset_name", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.Integer(), nullable=False),
        sa.Column("metric_name", sa.String(64), nullable=False),
        sa.Column("aggregate", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_eval_run_dataset_metric", "eval_run", ["dataset_name", "metric_name"])
    op.create_table("eval_case_result",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("eval_run_id", sa.String(36), sa.ForeignKey("eval_run.id"), nullable=False),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
    )
    op.create_index("ix_eval_case_result_run", "eval_case_result", ["eval_run_id"])

def downgrade() -> None:
    op.drop_table("eval_case_result")
    op.drop_table("eval_run")
```

- [ ] **Step 4: Run to verify it passes** — PASS.

- [ ] **Step 5: Commit** — `git add backend/src/fdi/platform/db/models.py backend/alembic/versions backend/tests/integration/test_migrations.py && git commit -m "feat(db): eval_run + eval_case_result tables"`

---

### Task 6: `EvalRepository` (platform db)

**Files:** Modify `backend/src/fdi/platform/db/repository.py`; Test `backend/tests/integration/test_eval_repository.py`.

**Interfaces:** Consumes `EvalOutcome` (core), the models (Task 5). Produces `class EvalRepository`: `async def record_run(self, run_id, candidate_name, candidate_version, dataset_name, dataset_version, metric_name, outcome: EvalOutcome) -> None` (inserts the run + one case_result per `outcome.results`); `async def leaderboard(self, dataset_name, metric_name) -> list[tuple[str, int, float]]` → `(candidate_name, candidate_version, aggregate)` ranked by aggregate desc (latest run per candidate — use the max `created_at` per candidate_name).

- [ ] **Step 1: Write the failing test** (unique ids per test)
```python
# backend/tests/integration/test_eval_repository.py
import uuid
from fdi.core.eval_types import CaseResult, EvalOutcome
from fdi.platform.db.repository import EvalRepository
# reuse session_factory fixture (migrated_engine)


async def test_record_and_leaderboard(session_factory) -> None:
    async with session_factory() as s:
        repo = EvalRepository(s)
        out_lo = EvalOutcome(0.4, (CaseResult("c1", 0.4),))
        out_hi = EvalOutcome(0.9, (CaseResult("c1", 0.9),))
        await repo.record_run(str(uuid.uuid4()), "lo", 1, "toy", 1, "exact_match", out_lo)
        await repo.record_run(str(uuid.uuid4()), "hi", 1, "toy", 1, "exact_match", out_hi)
        await s.commit()
        board = await repo.leaderboard("toy", "exact_match")
    assert board[0][0] == "hi" and board[0][2] == 0.9
    assert board[1][0] == "lo"
```

- [ ] **Step 2: Run to verify it fails** — `ImportError`.

- [ ] **Step 3: Implement** — `record_run` adds an `EvalRun` + `EvalCaseResult` rows; `leaderboard` selects the latest run per candidate for the (dataset, metric) and orders by aggregate desc. Use a subquery on `max(created_at)` grouped by `candidate_name`, or fetch all and reduce in Python (dataset is small at SP-3 scale — document the choice).

- [ ] **Step 4: Run to verify it passes** — PASS.

- [ ] **Step 5: Commit** — `git add backend/src/fdi/platform/db/repository.py backend/tests/integration/test_eval_repository.py && git commit -m "feat(db): EvalRepository record_run + leaderboard"`

---

### Task 7: `UppercaseCandidate` + toy dataset seed (pipeline)

**Files:** Create `backend/src/fdi/pipeline/candidates/__init__.py`, `backend/src/fdi/pipeline/candidates/uppercase.py`; Test `backend/tests/unit/test_uppercase_candidate.py`.

**Interfaces:** Produces `class UppercaseCandidate` (`name="uppercase"`, `version=1`, `predict(given) -> str(given).upper()`), self-registered into `CANDIDATES` on import of the package; and `toy_dataset() -> Dataset` (a small fixed dataset used by tests/seed). `candidates/__init__.py` imports `uppercase` for the registration side effect.

- [ ] **Step 1: Write the failing test**
```python
# backend/tests/unit/test_uppercase_candidate.py
from fdi.pipeline.candidates.uppercase import UppercaseCandidate, toy_dataset


def test_uppercase_predicts() -> None:
    assert UppercaseCandidate().predict("abc") == "ABC"
    assert UppercaseCandidate().name == "uppercase"


def test_toy_dataset_shape() -> None:
    ds = toy_dataset()
    assert ds.name == "toy" and len(ds.cases) >= 2
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — `UppercaseCandidate` + `toy_dataset()` (e.g. cases `("hello"→"HELLO", "world"→"WORLD")`), self-register in `uppercase.py` (`CANDIDATES.register(UppercaseCandidate())`), `candidates/__init__.py` does `from fdi.pipeline.candidates import uppercase as uppercase  # noqa: F401`.

- [ ] **Step 4: Run to verify it passes** — PASS.

- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/candidates backend/tests/unit/test_uppercase_candidate.py && git commit -m "feat(pipeline): UppercaseCandidate + toy dataset"`

---

### Task 8: `eval_service` end-to-end glue (pipeline)

**Files:** Create `backend/src/fdi/pipeline/eval_service.py`; Test `backend/tests/integration/test_eval_service.py`.

**Interfaces:** Consumes `get_dataset` (Task 4), `run_eval` + `CANDIDATES` (Task 3/7), `EvalRepository` (Task 6), `ExactMatch` (Task 1). Produces `async def run_evaluation(*, run_id, candidate_name, dataset_name, dataset_version, session_factory, store, candidates=CANDIDATES, metric=None) -> float` — loads the dataset artifact, resolves the candidate, `run_eval` with the metric (default `ExactMatch()`), persists via `EvalRepository.record_run`, returns the aggregate.

- [ ] **Step 1: Write the failing test** (integration; seed a dataset, run, assert leaderboard)
```python
# backend/tests/integration/test_eval_service.py
import uuid
from fdi.pipeline.candidates.uppercase import UppercaseCandidate, toy_dataset
from fdi.pipeline.dataset_store import put_dataset
from fdi.pipeline.eval_service import run_evaluation
from fdi.pipeline.evaluation import CandidateRegistry
from fdi.platform.db.repository import EvalRepository
# reuse session_factory + store fixtures


async def test_eval_service_persists_and_ranks(session_factory, store) -> None:
    await put_dataset(store, toy_dataset())
    reg = CandidateRegistry(); reg.register(UppercaseCandidate())
    agg = await run_evaluation(
        run_id=str(uuid.uuid4()), candidate_name="uppercase",
        dataset_name="toy", dataset_version=1,
        session_factory=session_factory, store=store, candidates=reg,
    )
    assert agg == 1.0  # uppercase matches the toy dataset exactly
    async with session_factory() as s:
        board = await EvalRepository(s).leaderboard("toy", "exact_match")
    assert board[0][0] == "uppercase" and board[0][2] == 1.0
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — load dataset, `candidate = candidates.get(candidate_name)`, `outcome = run_eval(candidate, ds, metric or ExactMatch())`, `async with session_factory() as s: await EvalRepository(s).record_run(run_id, candidate.name, candidate.version, ds.name, ds.version, (metric or ExactMatch()).name, outcome); await s.commit()`, return `outcome.aggregate`. Ensure the toy dataset's `expected` values are the uppercased inputs so the aggregate is 1.0.

- [ ] **Step 4: Run to verify it passes** — PASS.

- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/eval_service.py backend/tests/integration/test_eval_service.py && git commit -m "feat(pipeline): eval_service — run + persist + rank end to end"`

---

### Task 9: API — `POST /eval` and `GET /datasets/{name}/leaderboard`

**Files:** Modify `backend/src/fdi/api/routes.py`, `backend/src/fdi/api/schemas.py`, `backend/src/fdi/api/deps.py`, `backend/src/fdi/api/app.py`; Test `backend/tests/integration/test_api.py` (ADD).

**Interfaces:** `POST /eval` body `{candidate, dataset, dataset_version, metric?}` → 202/200 `{run_id, aggregate}`; runs `run_evaluation` with the app's `CANDIDATES` (import `fdi.pipeline.candidates` in `app.py` lifespan so the built-in candidate registers, and stash `app.state.candidates = CANDIDATES`). `GET /datasets/{name}/leaderboard?metric=exact_match` → `{leaderboard: [{candidate, version, aggregate}]}`. Schemas `EvalAccepted`, `LeaderboardView`. A `CandidatesDep` reads `app.state.candidates`.

- [ ] **Step 1: Write the failing test** (integration; seed dataset via store, POST /eval, GET leaderboard)
```python
async def test_eval_endpoint_runs_and_ranks(client, store) -> None:
    from fdi.pipeline.candidates.uppercase import toy_dataset
    from fdi.pipeline.dataset_store import put_dataset
    await put_dataset(store, toy_dataset())
    r = await client.post("/eval", json={"candidate": "uppercase", "dataset": "toy", "dataset_version": 1})
    assert r.status_code == 200
    assert r.json()["aggregate"] == 1.0
    board = (await client.get("/datasets/toy/leaderboard?metric=exact_match")).json()
    assert board["leaderboard"][0]["candidate"] == "uppercase"
```
(The `client` app must share the container's Postgres+MinIO — it does, via `make_client`'s lifespan; the `store` fixture writes to the same MinIO. Confirm the app's `ArtifactStore` bucket matches the test `store` bucket, or seed via the app. If buckets differ, seed the dataset through an app-side helper instead — resolve during implementation and note it.)

- [ ] **Step 2: Run to verify it fails** — 404.

- [ ] **Step 3: Implement** — add the routes, schemas, `CandidatesDep`, and the `app.py` lifespan wiring (`import fdi.pipeline.candidates`; `app.state.candidates = CANDIDATES`). `run_evaluation` needs a `session_factory` + `store` — get them from `app.state` (a small route-side helper or pass `request.app.state`). Mint `run_id = str(uuid.uuid4())` in the route.

- [ ] **Step 4: Run to verify it passes** — PASS. Then full non-e2e suite green.

- [ ] **Step 5: Commit** — `git add backend/src/fdi/api backend/tests/integration/test_api.py && git commit -m "feat(api): POST /eval and dataset leaderboard"`

---

### Task 10: Audit + docs

**Files:** Modify the SP-3 spec status line; `docs/architecture/ingestion-roadmap.md` (SP-3 → done + handoff note).

- [ ] **Step 1: Static gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports --config .importlinter` (`1 kept, 0 broken`).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (unit + integration green; e2e skips).
- [ ] **Step 3: Audit spec §7 done criteria** — map each (1 exact_match, 2 run_eval, 3 dataset round-trip, 4 EvalRepository leaderboard, 5 eval_service e2e, 6 API, 7 gates) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-17.**
- [ ] **Step 5: Roadmap SP-3 → ✅ done** + a "what SP-3 shipped that the layers must know" note: a candidate is `predict(given)->out` with name/version; datasets live at `ground-truth/{name}/v{version}.json`; metrics are pure `score->[0,1]` in `core`; `run_evaluation` persists an `eval_run` + case results and `leaderboard(dataset, metric)` ranks candidates — every later layer adds its metric beside `exact_match` and its candidates into `CANDIDATES`, then measures.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-3 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → Tasks (thin slice all; candidate-as-callable T3/T7; content-addressed datasets T2/T4; pure metrics in core T1; eval registry T5/T6; determinism throughout). §3 architecture → T1(metrics) T2(types) T3(runner+registry) T4(dataset store) T5(tables) T6(repo) T7(candidate) T8(service) T9(api). §4 error handling → T3 (out-of-range raise, candidate propagation), T6/T9 (404). §5 testing → unit T1/2/3/7, integration T4/5/6/8, api T9. §7 done criteria → T10 audit.

**Type consistency:** `EvalCase.given`/`.expected`, `Dataset(name,version,cases)`, `CaseResult(case_id,score)`, `EvalOutcome(aggregate,results)` are defined in T2 and used identically in T3/6/8. `run_eval(candidate,dataset,metric)->EvalOutcome` (T3) matches its call in T8. `EvalRepository.record_run(...)`/`leaderboard(...)` (T6) match T8/T9. `CANDIDATES`/`CandidateRegistry` consistent T3/7/9. `dataset_key`/`put_dataset`/`get_dataset` (T4) consistent in T8/T9.

**Placeholder scan:** the two `# type: ignore` in T2 are flagged as "refine with cast if mypy objects" — a real instruction, not a blank. No TODO/TBD.
