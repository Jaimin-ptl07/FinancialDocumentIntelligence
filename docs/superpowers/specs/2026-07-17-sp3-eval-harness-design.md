# SP-3 — Eval Harness & Ground Truth

- **Status:** Approved (autonomously, per standing mandate) 2026-07-17. Not yet implemented.
- **Depends on:** SP-2 (stage/DAG framework, content-addressed artifacts, registry) — complete.
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 ("Why SP-3 exists"), §4 (build order: SP-3 before the heavy layers).

## 1. Purpose

The project goal is *"the best algorithm at each step for most accurate functioning."* Research yields
**candidates** (e.g. XY-cut vs LayoutReader vs topological sort for reading order); it cannot say which
wins **on Indian annual reports** — only measurement does. SP-3 builds that measurement machinery
**before** the heavy layers (SP-5 OCR, SP-7 structural, …), so every later layer picks its winner by
score, not reputation. One annotated document with ground truth outweighs a week of literature review.

SP-3 is scoped as a **thin vertical slice**: the eval *framework* — ground-truth datasets, a pure
`Metric` protocol, a candidate runner, and a results registry that makes candidates rankable — proven
end to end on a toy dataset and a trivial candidate. Real metrics (OCR CER/WER, table cell-F1, reading-
order Kendall-τ) and real datasets arrive with the layers that produce candidates; SP-3 ships the
harness they plug into, not those metrics.

## 2. Scope decisions (made autonomously; documented for review)

1. **Thin-slice framework, not real metrics.** Build datasets + `Metric` protocol + runner + results
   registry; prove it with a toy dataset, a trivial `UppercaseCandidate`, and an `exact_match` metric.
   Per-layer metrics (CER/WER, cell-F1, τ) are added by the layers that need them (SP-5, SP-7, …).
2. **A candidate is a callable `predict(input) -> output`, not necessarily a full `Stage`.** Eval must
   compare algorithm *variants* cheaply, before any of them is promoted to a pipeline `Stage`. A
   candidate carries a `name` + `version` (same discipline as `Stage`), but needs no DB/DAG wiring.
   (A real stage can be wrapped as a candidate trivially.)
3. **Ground truth is versioned, content-addressed data.** A dataset is a named, versioned set of
   `(case_id, input, expected)` cases, stored as one canonical-JSON artifact in the object store under
   `ground-truth/{dataset}/v{version}.json`, and registered in Postgres so runs reference it by id.
   Human-authored, so it is an *input*, never regenerated.
4. **Metrics are pure and live in `fdi.core`.** A metric is `(predicted, expected) -> float` in `[0,1]`
   (1 = perfect), deterministic, no I/O — like the SP-2 NaN validator. Ships `exact_match`; richer
   metrics are added beside it.
5. **Eval runs are recorded and comparable.** An `eval_run` row (candidate name+version, dataset id,
   metric name, aggregate score, created_at) plus one `eval_case_result` row per case (case_id, score).
   Aggregate = mean case score (documented; a metric may later define its own aggregation). This is what
   "measurement picks the winner" means operationally: rank candidates by aggregate on a dataset+metric.
6. **Determinism (Global Constraint 6) holds.** Given the same candidate, dataset, and metric, every
   case score and the aggregate are byte-identical; ids/timestamps live in Postgres rows. Re-running an
   identical eval is allowed (a new run row) — evals are *observations over time*, not cached artifacts,
   so unlike stage artifacts they are intentionally NOT content-addressed/deduplicated.

## 3. Architecture

### 3.1 Layering (honours the CI-enforced `api | workers → pipeline → platform → core` contract)

- **`fdi.core.metrics`** — the `Metric` protocol + `exact_match` (pure; no I/O).
- **`fdi.core.eval_types`** — pure value objects: `EvalCase(case_id, input, expected)`,
  `Dataset(name, version, cases)`, `CaseResult(case_id, score)`, `EvalOutcome(aggregate, results)`.
- **`fdi.pipeline.eval`** — the `Candidate` protocol and `run_eval(candidate, dataset, metric) -> EvalOutcome`
  (pure orchestration over the above; no I/O, so it is unit-testable without Docker).
- **`fdi.platform.storage`** — dataset put/get by content-addressed `ground-truth/...` key (reuses
  `ArtifactStore`).
- **`fdi.platform.db`** — `eval_run` + `eval_case_result` tables + `EvalRepository` (persistence and the
  leaderboard query).
- **`fdi.pipeline.eval_service`** — glues store + repo + `run_eval`: load a dataset artifact, run a
  candidate, persist the run + case results, return the run id. (Pipeline may import platform.)
- **`fdi.api`** — `POST /eval` (run a registered candidate over a dataset), `GET /datasets/{name}/leaderboard`
  (ranked candidates for a metric). Optional for the thin slice; include a minimal read endpoint.

### 3.2 The `Metric` and `Candidate` contracts

```python
# fdi.core.metrics
class Metric(Protocol):
    name: str
    def score(self, predicted: object, expected: object) -> float: ...   # [0,1], pure

def exact_match(predicted: object, expected: object) -> float:
    return 1.0 if predicted == expected else 0.0
```

```python
# fdi.pipeline.eval
class Candidate(Protocol):
    name: str
    version: int
    def predict(self, input: object) -> object: ...
```

### 3.3 `run_eval` (pure)

```
run_eval(candidate, dataset, metric) -> EvalOutcome:
    results = [CaseResult(c.case_id, metric.score(candidate.predict(c.input), c.expected))
               for c in dataset.cases]
    aggregate = mean(r.score for r in results)   # 0.0 for an empty dataset
    return EvalOutcome(aggregate=aggregate, results=results)
```

### 3.4 Registry (Postgres)

```
eval_run(id, candidate_name, candidate_version, dataset_name, dataset_version,
         metric_name, aggregate, created_at)
eval_case_result(id, eval_run_id FK, case_id, score)
```
`EvalRepository`: `record_run(...) -> run_id`, `leaderboard(dataset_name, metric_name) -> list[(candidate, version, aggregate)]`
ranked by aggregate desc (latest run per candidate).

### 3.5 Dataset storage & registry

A dataset artifact is the canonical JSON of `Dataset(name, version, cases)`, stored at
`ground-truth/{name}/v{version}.json` via `ArtifactStore`. A `dataset` Postgres row (name, version,
artifact_key, created_at) registers it so an eval run references it by (name, version). Datasets are
authored/uploaded (out of scope to build an editor now); the thin slice seeds one toy dataset in a test
/ a small fixture helper.

## 4. Error handling

- A candidate that raises on a case: the run fails fast and records nothing partial (an eval with a
  broken candidate is not a 0.0 score — it is an error, surfaced with the case id). Kept simple: the
  runner propagates; the service does not persist a partial run.
- A metric returning outside `[0,1]`: the runner asserts the contract and raises (a metric bug must be
  loud, not silently skew a leaderboard).
- Unknown dataset / candidate at the API: 404.

## 5. Testing

- **Unit** (no Docker): `exact_match`; `run_eval` over a toy dataset with `UppercaseCandidate` (mixed
  right/wrong cases → known aggregate); empty dataset → aggregate 0.0; a metric out of range → raises;
  a candidate raising → propagates.
- **Integration** (testcontainers): `EvalRepository.record_run` + `leaderboard` (two candidates, ranking
  by aggregate); dataset artifact round-trip via `ArtifactStore`; `eval_service` end to end (seed dataset
  → run candidate → persisted run + case results → leaderboard reflects it).
- **API** (integration): `POST /eval` runs and returns a run id + aggregate; `GET /datasets/{name}/leaderboard`
  ranks candidates.

## 6. Explicitly deferred (YAGNI)

- Real metrics (CER/WER, table cell-F1, reading-order τ) → the layers that produce candidates (SP-5, SP-7…).
- A ground-truth authoring UI / annotation tooling → later; datasets are uploaded JSON for now.
- Statistical significance / confidence intervals across runs → when corpus-scale evals arrive (SP-13/14).
- Per-metric custom aggregation (beyond mean) → add when a metric needs it.

## 7. Done criteria

1. `exact_match` scores 1.0 on equal, 0.0 on unequal, and is pure.
2. `run_eval` returns per-case scores and the mean aggregate for a toy dataset; empty dataset → 0.0;
   an out-of-range metric raises; a raising candidate propagates.
3. A `Dataset` round-trips through `ArtifactStore` at `ground-truth/{name}/v{version}.json` (deterministic
   canonical JSON).
4. `EvalRepository` records a run + its case results and returns a leaderboard ranked by aggregate.
5. `eval_service` runs end to end against real Postgres + MinIO: seed dataset → run candidate → persisted
   run → leaderboard reflects it.
6. `POST /eval` returns a run id + aggregate; `GET /datasets/{name}/leaderboard` ranks candidates.
7. All four static gates green (`ruff`, `mypy --strict`, `lint-imports` unchanged contract, full suite);
   `core` stays pure (metrics + eval types are pure).
