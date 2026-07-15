# SP-1 — Project Setup & Walking Skeleton (Design Spec)

- **Status:** Approved 2026-07-15. Not yet implemented.
- **Sub-project:** SP-1 of 18 — see `docs/architecture/ingestion-roadmap.md`
- **Depends on:** nothing (greenfield)
- **Blocks:** SP-2 … SP-18 (all of them)
- **Target repo:** `D:\FinancialDocumentIntelligence` (currently empty; not yet a git repository)

---

## 1. Purpose

Establish the repository, the container topology, and — critically — **prove every integration seam
the remaining 17 sub-projects depend on**, before anything real is built on top of them.

SP-1 deliberately performs **no document processing**. Its deliverable is a *proven skeleton*: a
trivial job that round-trips API → RabbitMQ → Celery worker → Postgres → S3 → Redis → back,
observable end-to-end through a single correlation ID, with CI enforcing the structural rules that
keep the later phases tractable.

**The alternative that was rejected:** a thin scaffold (repo + Compose + `/health`) that leaves every
seam unproven. Each unproven seam — Celery↔RabbitMQ, async SQLAlchemy, S3, migrations — would then
surface as a surprise *inside* SP-2, entangled with the genuinely hard work of designing the stage
framework. Proving them here costs ~3 extra days and buys a clean SP-2.

---

## 2. Target Environment (measured 2026-07-15)

| | |
|---|---|
| RAM | 19.8 GB |
| CPU | Intel i5-1035G1 — 4 physical / 8 logical cores, 1.0 GHz base (15 W) |
| Disk | **D: 29.1 GB free** (repo lives here) · C: 40.2 GB · **F: 202.3 GB** |
| Installed | Docker 28.3.2 + Compose v2.39.1 · uv 0.8.12 · git 2.33.0 · Python 3.11.3 · Node v21 |

These numbers are load-bearing, not background. The full stack running at once totals ≈18.5 GB of
19.8 GB, leaving nothing for an IDE or browser — which is why §6 (profiles) and §7
(`--concurrency=2`) exist, and why volumes must not live on D:.

**Pre-work required before implementation:**

1. Docker daemon is **not running** — Docker Desktop must be started.
2. Python 3.13 — installed is 3.11.3; provision with `uv python install 3.13`.
3. Node **v21 is end-of-life** — install Node 24 LTS.
4. `pnpm` is absent — enable via corepack.
5. `curl` is broken machine-wide: a stale `CURL_CA_BUNDLE` env var points at a nonexistent
   PostgreSQL cert bundle (`C:\Program Files\PostgreSQL\15\ssl\certs\ca-bundle.crt`), so every
   HTTPS call via curl fails with `error setting certificate verify locations`. Unset it.

---

## 3. Decisions & Rationale

Each decision records *why*, so a future session can tell a considered choice from an accident.

### 3.1 Single repo · one backend package · two entrypoints

`backend/` is one Python package (`fdi`) with one `pyproject.toml`. The API and the Celery worker
are **two entrypoints into the same image**.

*Why:* they share ~90% of their code (models, repositories, artifact store, config). One image means
one dependency resolution, one build, one CI matrix. Cost: the API image carries Celery and vice
versa — a few MB, judged irrelevant. Splitting into a uv workspace remains possible later; the §3.2
boundary is what makes that cheap.

### 3.2 The `core` / `platform` purity boundary — enforced in CI

An `import-linter` **layered contract** runs as a CI gate:

```
api, workers  →  pipeline  →  platform  →  core
```

`core` may import nothing from `fdi` outside `core`: no settings singleton, no clock, no RNG, no I/O.
Violations fail the build.

*Why this is the most important structural rule in the project:* all twelve phases' algorithms live
in `core`. Purity is what makes them **testable offline against frozen fixtures** (no OCR cost in
tests), **deterministic** (byte-identical re-runs), and **safely re-runnable** — which is exactly
what the incremental-reprocessing requirement depends on. Convention will not hold this line across
18 sub-projects and many sessions; a CI gate will.

### 3.3 Async at the edge; `asyncio.run()` once per task

The I/O layer (repositories, artifact store) is written **once, as async**. FastAPI awaits it
natively. Celery tasks are **sync `def`** and call `asyncio.run(_impl(...))` exactly once, wrapping
all of that task's I/O in a single coroutine. The pure CPU transform stays a plain sync function
called from inside that coroutine.

*Why not "full async" inside workers:* async multiplexes *many concurrent I/O waits*. A stage has
~3–6 I/O operations wrapped around **minutes** of CPU. There is nothing to multiplex, so async
concurrency in the worker buys **zero** throughput.

*Why not sync clients in workers* (the more conservative option): it requires two I/O
implementations maintained across 18 sub-projects. One implementation was judged worth more than a
pooling optimisation that is provably noise at these task durations — per-task pool setup is
milliseconds against minutes.

*Why `asyncio.run()` per task and not `async_to_sync` per call:* confirmed in asgiref's source,
every `async_to_sync(...)` call spawns a **fresh event loop in a fresh thread** via `asyncio.run()` —
per *call*, not per task. SQLAlchemy's asyncio docs name the resulting error exactly:
`got Future attached to a different loop`. Connection pooling becomes structurally impossible.

*Why not a persistent loop per worker process:* it buys real pooling but costs a background thread,
`run_coroutine_threadsafe` plumbing, and a family of shutdown bugs. Deferred until profiling proves
loop setup matters.

### 3.4 S3 API everywhere; MinIO is local-dev only

Application code targets the **generic S3 API** through the maintained Apache-2.0 `minio` Python SDK.
MinIO the *server* appears only in local Compose and never in production; cloud uses real S3 or Azure
Blob.

*Why:* MinIO's community server repository is **archived** — its README reads "THIS REPOSITORY IS NO
LONGER MAINTAINED", and its final release (2025-10-15) was itself a CVE fix for privilege
escalation. An unmaintained storage layer with no patch pipeline is unacceptable in production, but
acceptable on an isolated dev container holding throwaway data. Confining it to local dev makes the
eventual swap a Compose change with **zero application-code impact**.

### 3.5 Compose profiles — pay for a service at its phase

Every service is *defined* from day one so the topology is real and reviewable, but only the needed
subset *runs*. See §6.

*Why:* the full stack is ≈18.5 GB on a 19.8 GB machine. Profiles keep the daily loop to ≈5 GB of
containers (≈11 GB total with Windows and the WSL2 VM), which leaves real headroom.

### 3.6 Python 3.13 · Node 24 LTS

*Why 3.13 and not 3.14:* Celery 5.6.3 declares classifiers for 3.9–3.13 and **not** 3.14. Its
changelog claims initial 3.14 support, but support is undeclared and lightly tested — Celery is now
the Python-version laggard in this stack.
*Why not 3.12:* already security-only.
*Why Node 24:* the current LTS ("Krypton"). The installed v21 is EOL.

---

## 4. Repository Layout

```
D:\FinancialDocumentIntelligence\
├── backend/
│   ├── pyproject.toml                  uv · Python 3.13 · ruff · mypy --strict · pytest
│   ├── .importlinter                   the §3.2 layered contract
│   ├── src/fdi/
│   │   ├── core/            ← PURE. no I/O, no config, no clock, no RNG
│   │   │   └── schema/         unified document model            (SP-2 fills)
│   │   ├── platform/        ← ALL I/O lives here
│   │   │   ├── config.py       pydantic-settings, env-only
│   │   │   ├── db/             async SQLAlchemy 2.0 · models · repositories
│   │   │   ├── storage/        S3 artifact store (async)
│   │   │   ├── clients/        Azure DI · LLM · embeddings       (SP-5+)
│   │   │   └── obs/            structlog · correlation IDs
│   │   ├── pipeline/        ← stage framework + stage impls      (SP-2 fills)
│   │   ├── api/                FastAPI (async)
│   │   └── workers/            Celery (prefork)
│   ├── alembic/
│   └── tests/
│       ├── unit/               pure, no containers
│       ├── integration/        session-scoped testcontainers
│       └── e2e/                the §8 round-trip
├── frontend/                            Next.js 16 · Node 24 · pnpm
├── docker/
│   ├── compose.yml                      profiles: core │ graph │ vector │ ui
│   ├── backend.Dockerfile               ONE image → api + worker entrypoints
│   ├── frontend.Dockerfile
│   └── rabbitmq.conf                    consumer_timeout override (§7)
├── docs/
│   ├── architecture/ingestion-roadmap.md
│   └── superpowers/{specs,plans}/
├── .github/workflows/ci.yml
├── .env.example
└── .gitignore
```

---

## 5. Container Topology

| Service | Image | Profile | Notes |
|---|---|---|---|
| `postgres` | `postgres:18.4` | core | registry + job state |
| `minio` | `minio/minio:RELEASE.2025-10-15T17-29-55Z` | core | **local dev only** (§3.4); the final upstream release — itself the privilege-escalation CVE fix, so this is the most-patched build that exists |
| `redis` | `redis:8.8.0` | core | Celery result backend |
| `rabbitmq` | `rabbitmq:4.3.2-management` | core | broker; custom `rabbitmq.conf` |
| `migrate` | backend image | core | **one-shot**, runs `alembic upgrade head`, exits |
| `api` | backend image | core | `uvicorn fdi.api.app:app` |
| `worker` | backend image | core | `celery -A fdi.workers.app worker --pool=prefork --concurrency=2` |
| `neo4j` | `neo4j:5.26.28` (LTS) | graph | defined now, **wired at SP-9** |
| `etcd` + `milvus` | `quay.io/coreos/etcd:v3.5.25` + `milvusdb/milvus:v2.6.20` | vector | defined now, **wired at SP-13** |
| `frontend` | `node:24` | ui | Next.js dev server |

**Every tag is pinned exactly, and that is deliberate — three floating tags in this stack are traps:**

- `neo4j:latest` shares a digest with the **CalVer `2026` line — a different lineage entirely**, not
  5.26 LTS. Non-LTS CalVer releases are supported only until the next monthly drops. `neo4j:5.26`
  also silently resolves to **community**; use `5.26-enterprise` if enterprise features are needed.
- `milvusdb/milvus:latest` was last pushed 2026-03-21 — months behind. Worse, the **master-branch**
  standalone compose points at **`v3.0-beta`**; copying the quickstart from master lands you on a
  pre-release. Use the `2.6` branch compose.
- `postgres:latest` tracks the newest major. PG **19 is beta only**; asyncpg 0.31.0 states support
  for PostgreSQL 9.5–**18**, so a float to 19 would leave the supported matrix.

**Startup ordering:** `api` and `worker` both declare
`depends_on: migrate: {condition: service_completed_successfully}`.

**Volumes:** named volumes bound under `F:\fdi-data\` — never D:.

**Approximate _container_ RAM by profile:** `core` ≈ 5 GB · `+graph` ≈ 7 GB · `+vector` ≈ 12 GB ·
`+ui` ≈ 13 GB. Windows itself plus the Docker/WSL2 VM adds roughly **6 GB on top**, which is how the
everything-at-once figure in §2 reaches ≈18.5 GB of 19.8 GB. Daily work on `core` sits near 11 GB
total, leaving real headroom.

---

## 6. Profiles

```
docker compose --profile core   up     postgres · minio · redis · rabbitmq · migrate · api · worker
docker compose --profile graph  up     + neo4j                    (from SP-9)
docker compose --profile vector up     + etcd · milvus            (from SP-13)
docker compose --profile ui     up     + frontend
```

---

## 7. Execution Model

Celery runs **prefork only**, at `--concurrency=2`.

*Why concurrency is below core count:* a 500-page report's parsed objects reach gigabytes, and
prefork's copy-on-write is defeated by CPython refcounting writing to object headers, so memory
multiplies per child. **Memory, not cores, is the binding constraint** on a 19.8 GB box. The value is
an environment variable so cloud can raise it.

Guards, each closing a specific documented failure mode:

| Guard | Failure prevented |
|---|---|
| `--pool=prefork` only | gevent/eventlet: CPU work never yields (monkey-patching only makes *I/O* yield), broker heartbeat starves → RabbitMQ redelivers → CPU re-burned. Celery's docs also state the gevent pool "does not implement soft time limits [and] will not enforce the hard time limit if the task is blocking" — a blocking CPU task there is un-timeout-able. |
| `asyncio.run()` **once per task** | `async_to_sync`'s fresh-loop-per-*call* → `got Future attached to a different loop`, pooling impossible |
| Never build loop/pool at module import | `fork()` copies the loop object but not its driving thread → coroutines hang **silently, with no error** |
| Engine created **inside** the task coroutine and disposed in `finally` — never at module level | A pool built at import is inherited across `fork()`, handing two interpreters one socket → **silent message corruption**, not a clean error. Note this is *not* solved by building pools in `worker_process_init`: that is the remedy for the sync-worker design (§3.3), and under `asyncio.run()`-per-task there is no event loop at `worker_process_init` for a pool to bind to. The remedy here is that **nothing exists at module level to be inherited**. |
| CI lint: no `async def` under `@app.task` | Celery returns an un-awaited coroutine, **reports success, and does nothing** |
| `consumer_timeout` raised above p99 in `rabbitmq.conf` | 30-minute default + `task_acks_late` → channel killed → requeue → **infinite redelivery loop re-burning CPU on the largest documents** |
| `task_acks_late=True` | task loss on worker death |
| `worker_prefetch_multiplier=1` | head-of-line blocking on long tasks |
| `worker_max_tasks_per_child` | unbounded memory drift |

Canonical task shape:

```python
@app.task                                    # sync def — NOT async def
def run_stage(doc_id: str) -> str:
    return asyncio.run(_run_stage(doc_id))   # exactly one loop, per task

async def _run_stage(doc_id: str) -> str:
    engine = make_engine(...)                # built INSIDE the loop, never at import
    try:
        raw = await store.get(doc_id, "in.json")     # I/O
        out = transform(raw)                          # CPU — pure, sync; blocks this loop (fine,
                                                      #   nothing else is on it)
        await store.put(doc_id, "out.json", out)     # I/O
        await mark_done(engine, doc_id)              # I/O
        return out.key
    finally:
        await engine.dispose()               # loop dies with the task; so must the pool
```

The engine lives exactly as long as the loop that owns it. Per-task connection setup costs
milliseconds against minutes of CPU — the same reasoning that made §3.3's choice affordable.

---

## 8. The Walking Skeleton

```
POST /ping-job
  api    │ validate → INSERT job(status=queued)
         │ publish(RMQ, headers={correlation_id})
         │ 202 {job_id}
         ▼
  worker │ consume → asyncio.run(_ping_job(job_id))
         │   UPDATE job status=running                    ← async SQLAlchemy seam
         │   PUT s3://artifacts/{job_id}/ping.json        ← S3 seam
         │   UPDATE job status=done, artifact_key=...
         │   return → Redis                               ← result-backend seam
         ▼
GET /jobs/{job_id} → 200 {status:"done", artifact_url}
```

The payload is trivial by design. The **seams** are the deliverable.

`correlation_id` is minted at the API (or honored from an inbound `X-Request-ID`), rides RabbitMQ
message headers, and binds into structlog's context inside the worker — so one grep retrieves the
full causal chain across both processes.

---

## 9. Configuration & Secrets

`pydantic-settings` reading **environment variables only**. No host paths in application code — the
`F:` binding lives in `compose.yml`. `.env.example` is committed; `.env` is gitignored.

Secrets are env vars for now; a secrets manager slots in at the cloud move without touching
application code. This is the 12-factor line that makes the cloud migration a config change rather
than a redesign.

---

## 10. Error Handling

| Concern | Approach |
|---|---|
| API errors | RFC 9457 `application/problem+json` via FastAPI exception handlers |
| Task failure | job row → `status=failed` with error detail; `autoretry_for` transient errors only |
| Poison messages | bounded retries → dead-letter queue; never infinite redelivery |
| Liveness | `GET /health` — process is alive |
| Readiness | `GET /ready` — Postgres + S3 + RabbitMQ reachable |
| Migrations | dedicated one-shot `migrate` service; api/worker gate on its completion |

**On migrations:** running `alembic upgrade head` from the API on boot works with one replica and
**corrupts under two**, racing on the version table. Getting this wrong now would surface as a
mystery in cloud, where replicas are the default.

**Accepted trade-off:** `/ready` probing three backends makes startup ordering strict and can flap on
a laptop where containers settle slowly. Correct for cloud, mildly annoying locally.

---

## 11. Testing

| Layer | Approach |
|---|---|
| **Unit** | pure functions, zero containers, fast enough to run on save. Where `core` is tested — forever, offline. |
| **Integration** | session-scoped **testcontainers**: Postgres, RabbitMQ, MinIO, Redis. |
| **E2E** | the §8 round-trip, asserted end to end. |

**CI gates:** `ruff` · `mypy --strict` · `import-linter` · `pytest`.

**Accepted trade-off:** testcontainers costs ~30–60s of session startup on a 15 W CPU. Testing
against an already-running Compose stack would be faster but creates two paths that drift. One path
was chosen over inner-loop speed.

---

## 12. Done Criteria

Verifiable, not vibes. SP-1 is complete when all of these hold:

1. `docker compose --profile core up` → the six long-running services (`postgres`, `minio`, `redis`,
   `rabbitmq`, `api`, `worker`) all report healthy. `migrate` is one-shot and exits 0.
2. `migrate` runs to completion before `api` and `worker` start.
3. `POST /ping-job` → 202; worker consumes; the S3 object exists; the job row reads `done`.
4. `GET /jobs/{id}` → 200 with a working `artifact_url`.
5. One `correlation_id` is greppable across both api **and** worker log lines.
6. The Next.js page renders live job status from the real API.
7. CI is green on all four gates.
8. RAM under `--profile core` is measured and recorded (budget ≈ 5 GB).
9. `import-linter` passes (trivially true at SP-1 — the gate's existence is the deliverable).
10. A determinism test asserts the ping artifact's **body** is byte-identical across repeated runs
    for the same input — establishing the pattern SP-2's stage framework will enforce corpus-wide.
    Job IDs and timestamps belong in the Postgres row, never in the artifact body.

---

## 13. Out of Scope for SP-1

Explicitly deferred to the sub-project that needs them: authentication/API keys ·
OpenTelemetry/Prometheus/Grafana · Flower (2.0.1, last released 2023-08-13 — ~3 years stale) ·
any real pipeline stage · Neo4j and Milvus **wiring** (their containers are defined, not used) ·
real OCR · the unified document schema (SP-2) · the stage/DAG framework (SP-2).

---

## 14. Version Pins (verified 2026-07-15)

| Component | Version | Note |
|---|---|---|
| Python | **3.13** | not 3.14 — Celery doesn't declare it; not 3.12 — security-only |
| Celery | 5.6.3 (2026-03-26) | Celery 6 has **not** shipped; milestone open since 2020, now due 2027-01-31 |
| Kombu | 5.6.2 | |
| FastAPI | 0.139.0 | |
| SQLAlchemy | 2.0.51 | |
| Pydantic | 2.13.4 | |
| asyncpg | 0.31.0 | 3.13/3.14 classifiers present since 0.31.0 |
| Alembic | 1.18.5 | |
| asgiref | 3.12.1 | |
| Node | **24 LTS** ("Krypton") | installed v21 is EOL |
| Next.js | 16.2.x | |
| PostgreSQL | **18.4** | 18 is GA (19 is beta only). asyncpg 0.31.0 states support for 9.5–**18**. |
| RabbitMQ | **4.3.2** | 4.2's **community** support ends **2026-07-31 — 16 days from now, not yet passed**. Its 2030 LTS date is *commercial* only and buys nothing without a Broadcom licence. 4.3 community runs to 2026-11-30. |
| Redis | 8.8.0 | tri-licensed RSALv2 / SSPLv1 / **AGPLv3** — AGPLv3 may be *elected* (added as a third option 2025-05-01, not swapped in) |
| MinIO server | pinned, local-dev only | archived upstream (§3.4) |
| minio Python SDK | 7.2.20 | Apache-2.0, maintained, generic S3 client |
| Neo4j | 5.26.28 (LTS) | final 5.x; hotfixes to 2028-06-06. No CalVer release carries LTS yet. |
| Milvus | v2.6.20 | companions: `etcd v3.5.25`, `minio RELEASE.2024-05-28T17-19-04Z` |
| pymilvus | **`>=2.6,<3`** | ⚠️ `pip install pymilvus` resolves to **3.0.0** (for Milvus 3.0 *beta*) — wrong |

**PG18 behavioural change to know:** PostgreSQL 18 made `VIRTUAL` the default for generated columns,
and SQLAlchemy renders no keyword on PG18+ so the server picks its default. A generated column that
was `STORED` on PG17 becomes `VIRTUAL` on PG18 unless declared explicitly. Greenfield, so no
migration pain now — but relevant the first time SP-2 or SP-4 reaches for one.

**PG18 volume trap (for the future, not now):** the PG18 image moved `PGDATA` to
`/var/lib/postgresql/18/docker` and the `VOLUME` to `/var/lib/postgresql`. A volume created under an
older major makes the container refuse to start. Greenfield avoids this entirely — record it so the
eventual 18→19 bump is planned, not discovered.

---

## 15. Deferred Decisions

Recorded so they are not silently lost:

- **OCR provider is NOT settled.** The architecture assumes Azure Document Intelligence, but provider
  choice sets the quality ceiling for every downstream phase — each weakness there becomes work in
  Phase 4. To be decided at **SP-5** with evidence from SP-3's eval harness, unless an existing
  subscription or committed spend makes it a hard constraint.
- **Neo4j 5.26 LTS vs. CalVer 2026.x** — revisit at SP-9.
- **Persistent event loop per worker** (§3.3) — revisit only if profiling shows loop setup matters.
- **Splitting `core` into its own distributable package** — the §3.2 boundary keeps this cheap;
  revisit only if a second consumer appears.
- **RabbitMQ quorum queues** — the HA path since 4.x dropped classic mirrored queues, but they
  require disabling global QoS, which breaks task priorities. Revisit at the cloud move.
