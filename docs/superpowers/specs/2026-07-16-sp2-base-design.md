# SP-2 — The Base: stage/DAG framework, content-addressed artifacts, registry

- **Status:** Approved 2026-07-16. Not yet implemented.
- **Depends on:** SP-1 (walking skeleton) — complete on `feature/sp-1-project-setup`.
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 ("Why SP-2 is the highest-leverage sub-project").

## 1. Purpose

SP-2 delivers the foundation every later phase (OCR, normalize, AST, graph, …) plugs into: a
**stage/DAG framework** whose artifacts are **content-addressed and version-stamped**, so any single
stage can be improved and its downstream regenerated without re-running the stages above it — most
importantly without re-paying for OCR. Incremental reprocessing and artifact versioning are
*properties of this framework*, not features that can be bolted on later; if SP-2 does not deliver
them, no later sub-project can retrofit them.

Per the roadmap's flag, SP-2 is scoped as a **thin vertical slice**: build the minimum framework and
prove it end-to-end by re-expressing the existing trivial `ping-job` as the first real stage. The
rich normalized-document schema, document intake/dedup, auto-cascade reprocessing, and multi-input
DAG topology are explicitly deferred to the sub-projects that first need them.

## 2. Scope decisions (settled in brainstorming)

1. **Thin vertical slice.** Build the framework (Stage abstraction, content-addressed + version-
   stamped artifact model, registry, DAG resolver) and prove it by reframing `ping-job` as one
   stage. Defer the rich unified schema to SP-6.
2. **Explicit per-stage integer version.** Each `Stage` declares `version: int`, bumped by the
   developer when a change alters the stage's output. Precise and intentional; the known cost is
   discipline (forget to bump → a stale artifact is served), mitigated by a review checklist and,
   later, SP-3 eval drift.
3. **Detect + report staleness; explicit re-run.** The registry records each artifact's input hash
   and stage version, so the framework can *compute* what is stale and expose it. Re-running is an
   explicit API/CLI operation ("reprocess doc X from stage Y down"); SP-2 does **not** auto-cascade.
4. **Task-per-stage, chained via the broker.** Each stage is its own Celery task; on completion it
   asks the DAG resolver what is next and enqueues those successors. The resolver is plain Python and
   independently unit-testable; the DAG topology is not entangled with Celery's signature graph.
5. **Reject non-finite floats.** The schema/validation layer rejects `NaN`/`Inf` before
   serialization, so every artifact is valid JSON and a financial `0/0` is an explicit per-stage
   decision rather than a silent `null`.
6. **Generalize `Job` → `document` + `stage_run`.** SP-1's `Job` (one ping request) is superseded by
   a `document` with a pipeline of `stage_run`s. `/ping-job` becomes "start a one-stage pipeline for
   a new document." This changes the ping artifact key to the content-addressed path, so SP-1's
   e2e/round-trip key assertion is updated.

## 3. Architecture

### 3.1 Layering (honours the SP-1 import contract)

The CI-enforced contract is `api | workers → pipeline → platform → core`, `exhaustive = true`.
SP-2's pieces are placed, not appended to a list:

- **`fdi.core`** — the artifact-body **validator** (pure: rejects `NaN`/`Inf`, no I/O). Keeps
  `canonical_json` a thin, branch-free pass-through; validation happens *before* it.
- **`fdi.platform.storage`** — `ArtifactStore` extended with a content-addressed key builder and an
  `exists(key)` check. No rewrite of the SP-1 put/get/presign surface.
- **`fdi.platform.db`** — the `document` and `stage_run` tables, models, and a repository.
- **`fdi.pipeline`** — the `Stage` protocol, the stage registry, the DAG resolver, and the
  `run_stage` orchestration. (`fdi.pipeline.dispatch` already lives here from SP-1.)
- **`fdi.workers`** — the generic `run_stage` Celery task binding.
- **`fdi.api`** — `/ping-job` (start a pipeline), `/jobs/{id}` (aggregate status), plus a staleness /
  reprocess surface.

### 3.2 Artifact identity

An artifact is identified by `(doc_id, stage_name, stage_version, input_hash)` and stored at:

```
artifacts/{doc_id}/{stage}/v{version}/{input_hash}.json
```

- `input_hash = sha256(canonical bytes of the stage's input)` — the upstream artifact body for a
  derived stage, or the source document's checksum for a root stage. Same input + same stage version
  → same key → the framework finds the artifact already present and **skips recompute**. The cache,
  and the entire reprocessing story, fall out of the key.
- Addressing is **scoped to `doc_id`** (it is in the path), not a global content-addressed store.
  Two byte-identical source documents therefore produce two artifact trees; global cross-document
  dedup is intake's concern and is deferred to SP-4. Within one document, the `input_hash` still
  gives the cache and the staleness math their meaning.
- A **root stage** (`deps == ()`) has no upstream artifact, so its input is the document's source:
  `input_hash = sha256(document.checksum)`. Until SP-4 populates `checksum`, a root stage over a
  synthetic document (see the ping reframe) hashes a defined constant — the empty input — so its key
  is stable across re-runs of that document while remaining distinct per `doc_id` via the path.
- The body is a deterministic function of input (SP-1 `canonical_json`). Ids and timestamps live only
  in Postgres rows, never in the artifact body (Global Constraint 6 / determinism).

### 3.3 Registry (Postgres)

```
document(id, created_at, checksum NULLABLE)
    -- checksum stays null until SP-4 owns real intake/dedup.

stage_run(id, doc_id FK, stage_name, stage_version, input_hash,
          artifact_key, status, error, correlation_id, created_at, updated_at)
    -- one row per stage execution; answers "current artifact for (doc, stage)",
    -- records lineage, and is where staleness is computed.
```

`status` is constrained to the same closed set as SP-1's job status (`queued`/`running`/`done`/
`failed`) via an explicit `CHECK`, tested by inserting an illegal value and expecting `IntegrityError`
(the SP-1 lesson: a decorative enum with nothing watching it is not a constraint).

### 3.4 The `Stage` abstraction (`fdi.pipeline`)

```python
class Stage(Protocol):
    name: str               # stable identifier, e.g. "normalize"
    version: int            # bump when the stage's output changes
    deps: tuple[str, ...]   # upstream stage names it consumes; () for a root stage
    async def run(self, ctx: StageContext) -> object:   # returns the output body
```

A stage is a near-pure transform: it reads its input(s) through `ctx` and returns an output object.
It does **not** touch keys, hashing, storage, or the DB — the framework owns all of that — so a stage
stays trivially unit-testable. Stages self-register into a `name → Stage` map at import.

### 3.5 The DAG resolver (`fdi.pipeline`, pure, no I/O)

Built from the registered stages' `deps`. It answers exactly three questions:

- `successors(stage)` — who consumes this output → what to enqueue next.
- `stale(doc_id, runs)` — which stages' recorded `input_hash`/`version` no longer match (the report).
- `plan(doc_id, from_stage)` — the subgraph to (re)run for an explicit reprocess.

Cycles are rejected at registration (fail fast). The thin slice proves it on a linear chain; the
resolver is written general but only what ping exercises is *tested* now.

### 3.6 Execution — one generic Celery task

```
run_stage(doc_id, stage_name):
    stage = registry[stage_name]
    input_hash = sha256(canonical bytes of upstream artifact(s) | source checksum)
    key = artifacts/{doc}/{stage}/v{stage.version}/{input_hash}.json
    if store.exists(key):                    # content-addressed cache hit
        record stage_run(cached); enqueue resolver.successors(); return
    out = asyncio.run(stage.run(ctx))        # canonical shape: engine built inside, disposed in finally
    validate(out)                            # reject NaN/Inf
    store.put_json(key, out)                 # canonical_json
    record stage_run(done, key)
    for s in resolver.successors(stage): enqueue run_stage(doc, s)
```

The correlation id rides the chain via the SP-1 machinery (bound before `asyncio.run`, cleared in
`finally`). Each stage is independently retryable. All SP-1 invariants hold: `core` pure, one
`asyncio.run()` per task with the engine built inside the coroutine, content-addressed + version-
stamped artifacts, deterministic bodies.

### 3.7 The ping reframe (what proves the slice)

Register `PingStage(name="ping", version=1, deps=())` returning `{"kind":"ping","schema_version":1}`.
`/ping-job` creates a `document` and enqueues `run_stage(doc, "ping")`; `/jobs/{id}` reports the
aggregated `stage_run` status and the presigned artifact URL. The full SP-1 round-trip stays green,
now flowing through the framework instead of a bespoke task — with the artifact at the content-
addressed key rather than `jobs/{id}/ping.json`.

## 4. Error handling

- A stage that raises: `run_stage` records `stage_run(failed, error=...)`, logs the cause first, and
  does not enqueue successors. Downstream simply never runs; nothing is left half-written because the
  artifact is only recorded after a successful `put_json`.
- Cache-hit path never runs the stage body, so a transient stage bug cannot corrupt an already-good
  artifact.
- Validation failure (`NaN`/`Inf`) is a stage failure, surfaced with the offending path.
- Cycle in the registered DAG is a startup error (fail fast at registration), not a runtime hang.

## 5. Testing

- **Unit** (fast, no Docker): resolver (topo order, cycle rejection, `successors`, staleness math),
  `input_hash` determinism, key builder, cache-hit skip, the `NaN`-reject validator, stage
  registration, the `stage_run` status `CHECK` (insert illegal → `IntegrityError`).
- **Integration** (testcontainers): `run_stage` against real Postgres + MinIO — cache-miss runs,
  stores, and records; cache-hit skips the body; a `version` bump flips `stale()` to true.
- **e2e**: the reframed ping pipeline round-trip (updated artifact-key assertion), still gated on a
  live `--profile core` stack and skipping honestly when it is down.

## 6. Explicitly deferred (YAGNI)

- Rich unified normalized-document schema → SP-6.
- Document intake, upload, checksum, dedup, versioning → SP-4.
- Auto-cascade reprocessing and scheduling → a later SP with a real multi-stage pipeline.
- Multi-input joins / fan-out DAG richness → when a real stage first needs it (the resolver is
  written general; only linear-chain behaviour is tested now).

## 7. Done criteria

1. A `Stage` can be registered with `name`, `version`, `deps`; a cycle is rejected at registration.
2. `run_stage` computes a content-addressed, version-stamped key; a second run with the same input
   and version is a store cache hit that does **not** re-execute the stage body (proven by a spy /
   side-effect counter).
3. Bumping a stage's `version` produces a new key and marks its downstream `stale()`.
4. The registry (`document` + `stage_run`) records every execution, enforces the status `CHECK`, and
   answers "current artifact for (doc, stage)".
5. The artifact-body validator rejects `NaN`/`Inf` and passes finite JSON unchanged; `canonical_json`
   stays a branch-free pass-through.
6. `/ping-job` runs through the framework: create `document` → `run_stage(doc, "ping")` → `done`, and
   `/jobs/{id}` returns a host-fetchable presigned `artifact_url` for the content-addressed key.
7. The SP-1 e2e round-trip passes against the reframed pipeline (key assertion updated).
8. All four static gates stay green: `ruff`, `mypy --strict`, `lint-imports` (contract unchanged),
   full test suite. `core` remains pure (the validator is pure).
