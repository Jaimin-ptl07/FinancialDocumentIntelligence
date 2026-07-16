# SP-1 — Project Setup & Walking Skeleton: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove every integration seam that SP-2…SP-18 depend on — API → RabbitMQ → Celery → Postgres → S3 → Redis — with CI enforcing the structural rules, before any real document processing exists.

**Architecture:** One Python package `fdi` in `backend/`, built as one image with two entrypoints (uvicorn API, Celery prefork worker). A CI-enforced layered boundary keeps `fdi.core` pure. The I/O layer is written once as async; FastAPI awaits it, Celery tasks call `asyncio.run()` exactly once per task. Compose profiles keep the daily loop to ~5 GB of containers.

**Tech Stack:** Python 3.13 · uv 0.11.28 · FastAPI 0.139 · SQLAlchemy 2.0.51 (async) · asyncpg 0.31 · Alembic 1.18 · Celery 5.6.3 (prefork) · RabbitMQ 4.3.2 · Redis 8.8.0 · PostgreSQL 18.4 · MinIO (S3 API) · structlog 26.1 · import-linter 2.13 · testcontainers 4.14.2 · Next.js 16 · Node 24 LTS

**Spec:** `docs/superpowers/specs/2026-07-15-project-setup-design.md`
**Roadmap:** `docs/architecture/ingestion-roadmap.md`

---

## Global Constraints

These are non-negotiable. Every task must preserve them; a task that breaks one is wrong even if its tests pass.

1. **`fdi.core` is pure.** No I/O, no `Settings`, no clock, no RNG, no imports from any other `fdi` subpackage. Enforced by `lint-imports` (Task 2).
2. **Never `async def` under `@app.task`.** Celery returns the un-awaited coroutine and reports **SUCCESS while doing nothing**. Enforced by an AST test (Task 3).
3. **Never create an event loop, engine, or connection pool at module import.** `fork()` copies the loop object but not its driving thread → coroutines hang **silently**. Engines are created *inside* the task coroutine and disposed in `finally`.
4. **Bind the correlation ID BEFORE crossing `asyncio.run()`.** contextvars is copy-in / no-copy-out — anything bound *inside* the loop is lost to the caller. Verified behaviour, not theory.
5. **Prefork pool only.** `--concurrency=2` on this machine (memory-bound, not core-bound).
6. **Determinism.** Artifact *bodies* are a pure function of input. IDs and timestamps live in Postgres rows, never in artifact bodies.
7. **12-factor.** Config from environment only. No host paths in application code.

### Environment pre-work (do this before Task 1)

```powershell
# 1. Start Docker Desktop (daemon is not running)
# 2. Python 3.13 (installed is 3.11.3)
uv python install 3.13
# 3. Node 24 LTS (installed v21 is EOL) — install from nodejs.org, then:
corepack enable
# 4. curl is broken machine-wide: a stale CURL_CA_BUNDLE points at a nonexistent
#    PostgreSQL cert file, so every HTTPS call via curl fails.
[Environment]::SetEnvironmentVariable("CURL_CA_BUNDLE", $null, "User")
```

### Commands

All commands run from the **repo root** `D:\FinancialDocumentIntelligence` unless stated.

| Purpose | Command | Needs Docker? |
|---|---|---|
| Fast loop | `uv run --directory backend pytest -m "not integration" -q` | no |
| Full suite | `uv run --directory backend pytest -q` | **yes** |
| Lint | `uv run --directory backend ruff check .` | no |
| Types | `uv run --directory backend mypy --strict src` | no |
| Imports | `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter` | no |

> **`PYTHONUTF8=1` on the Imports row is load-bearing on Windows, not decoration.** import-linter
> renders a `:brick:` emoji through `rich`; on a console without VT support `rich` takes its legacy
> path and encodes to the ANSI code page (cp1252 here), which cannot represent it. The command then
> dies with `UnicodeEncodeError` and **exit 1** — the same exit code as a real contract violation.
> Measured on this box: with the flag, a deliberate `core → platform` import is reported precisely
> (`fdi.core.serialization -> fdi.platform.config (l.16)`); without it, a violated contract and a
> clean tree produce *identical* output. That is worse than a gate that does nothing, because it is
> an alarm that is always on — and the way to make an always-on alarm quiet is to stop believing it.
> `PYTHONIOENCODING=utf-8` and `TERM=dumb` also work; `NO_COLOR=1` does **not** (it suppresses
> colour, not the emoji). Linux/CI/Docker default to UTF-8 and are unaffected, which is precisely
> why this can only ever be caught locally. `tests/unit/test_import_contract.py` sets the flag
> itself, so the pytest gate is honest without the developer knowing any of this.

> **Bare `pytest -q` requires Docker** — integration tests are not deselected by default, and
> that is deliberate: hiding them behind an opt-in flag is how they stop being run locally and
> start failing only in CI. Use `-m "not integration"` for the inner loop when you know you
> haven't touched an I/O seam.
>
> **Budget timeouts off ~105s, not ~45s.** The container suite was measured green across four
> consecutive runs, but wall time ranged **41s → 105s** (a 2.4× spread, mostly RabbitMQ
> readiness). A timeout tuned to the fast case would flake intermittently and look like a bug in
> the code under test.

> ### `--directory`, never `--project`. This is not cosmetic.
>
> `--project` selects which project's **venv** to use. It does **not** change the working
> directory — and pytest and mypy discover their config from the *filesystem*, not from uv.
> Using `--project` from the repo root silently discards all of `backend/pyproject.toml`.
> Measured, not theorised:
>
> | | `--project` (broken) | `--directory` (correct) |
> |---|---|---|
> | pytest rootdir | `D:\FinancialDocumentIntelligence` | `...\backend` |
> | pytest configfile | **none loaded** | `pyproject.toml` |
> | `asyncio_mode` | **`STRICT`** | `AUTO` |
> | mypy `Config File` | **`Default`** | `backend/pyproject.toml` |
>
> Consequences: every `async def test_...()` without an explicit `@pytest.mark.asyncio`
> marker fails with *"async def functions are not natively supported"* — that is the style
> used throughout this plan. And `ignore_missing_imports` never loads, so
> `from celery import Celery` fails the types gate. mypy's config search is **cwd-only** and
> is *not* fixed by passing a path argument; pytest's *is* fixed by a path argument, which is
> why the per-task commands below appear to work while the whole-suite gate does not.
>
> This failure is *silent and misattributed*: it first detonates at Task 4, whose own warning
> about a Windows testcontainers regression makes it very easy to misdiagnose.

> ### Editing this file: use UTF-8-aware tools only
>
> This plan contains em-dashes, arrows and emoji. **Do not** edit it with PowerShell
> `Get-Content -Raw` / `Set-Content`: on PS 5.1 `Get-Content` defaults to cp1252, silently
> destroying every glyph with no cp1252 mapping — irreversibly, since they become `?`. This
> already happened once and cost a full restore-and-replay from git. Use the Edit tool, or
> Python with an explicit `encoding="utf-8"`.

### Branch

```powershell
git checkout -b feature/sp-1-project-setup
```

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/pyproject.toml` | deps, ruff/mypy/pytest config |
| `backend/.importlinter` | the layered purity contract |
| `backend/src/fdi/core/serialization.py` | `canonical_json` — deterministic JSON. **Pure.** |
| `backend/src/fdi/platform/config.py` | `Settings`, `get_settings()` |
| `backend/src/fdi/platform/obs/logging.py` | structlog + correlation-ID contextvar |
| `backend/src/fdi/platform/db/models.py` | `Base`, `Job` |
| `backend/src/fdi/platform/db/engine.py` | engine/session factories (never module-level) |
| `backend/src/fdi/platform/db/repository.py` | `JobRepository` |
| `backend/src/fdi/platform/storage/artifact_store.py` | async S3 store over the sync `minio` SDK |
| `backend/src/fdi/workers/app.py` | Celery app + config |
| `backend/src/fdi/workers/tasks.py` | `ping_job` — the sync/async seam |
| `backend/src/fdi/api/app.py` | FastAPI app factory, lifespan |
| `backend/src/fdi/api/errors.py` | RFC 9457 handlers |
| `backend/src/fdi/api/routes.py` | `/health`, `/ready`, `/ping-job`, `/jobs/{id}` |
| `backend/alembic/` | migrations |
| `backend/Dockerfile` | one image, two entrypoints |
| `docker/compose.yml` | profiles: core / graph / vector / ui |
| `docker/rabbitmq.conf` | `consumer_timeout` |
| `frontend/` | Next.js 16 |
| `.github/workflows/ci.yml` | ruff · mypy · lint-imports · pytest |

---

## Task 1: Backend scaffold and quality gates

**Files:**
- Create: `backend/pyproject.toml`, `backend/README.md`, `backend/src/fdi/__init__.py`, `backend/tests/__init__.py`, `backend/tests/unit/__init__.py`

- [ ] **Step 1: Create the project skeleton**

```powershell
New-Item -ItemType Directory -Force backend/src/fdi, backend/tests/unit, backend/tests/integration, backend/tests/e2e | Out-Null
New-Item -ItemType File backend/src/fdi/__init__.py, backend/tests/__init__.py, backend/tests/unit/__init__.py, backend/tests/integration/__init__.py, backend/tests/e2e/__init__.py | Out-Null
```

- [ ] **Step 2: Write `backend/pyproject.toml`**

```toml
[project]
name = "fdi"
version = "0.1.0"
description = "Financial Document Intelligence — ingestion platform"
requires-python = ">=3.13,<3.14"
dependencies = [
    "fastapi>=0.139",
    "uvicorn[standard]>=0.35",
    "pydantic>=2.13",
    "pydantic-settings>=2.14",
    "sqlalchemy[asyncio]>=2.0.51",
    "asyncpg>=0.31",
    "alembic>=1.18",
    "celery>=5.6.3",
    "redis>=5.2",
    "minio>=7.2.20",
    "structlog>=26.1",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "httpx>=0.28",
    "ruff>=0.8",
    "mypy>=1.13",
    "import-linter>=2.13",
    "testcontainers[postgres,minio,rabbitmq,redis]==4.14.2",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fdi"]

[tool.ruff]
line-length = 100
target-version = "py313"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC", "C4", "SIM"]

[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls = ["fastapi.Depends", "fastapi.Query", "fastapi.Path", "fastapi.Body"]

[tool.mypy]
python_version = "3.13"
strict = true
ignore_missing_imports = true
mypy_path = "src"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = ["integration: needs Docker containers"]
```

> **Note:** `testcontainers`' `postgres` extra is **empty** (`postgres = []`) — it pulls no SQLAlchemy or asyncpg. Ours come from the main dependency list, which is why this works.

- [ ] **Step 3: Lock and sync**

Run: `uv sync --project backend`
Expected: creates `backend/uv.lock` and `backend/.venv`, resolves Python 3.13.

- [ ] **Step 4: Verify the gates run on an empty project**

Run: `uv run --directory backend ruff check .`
Expected: `All checks passed!`

Run: `uv run --directory backend pytest -q`
Expected: `no tests ran` (exit 5 — acceptable, no tests exist yet)

- [ ] **Step 5: Commit**

```powershell
git add backend .gitignore
git commit -m "chore(backend): scaffold uv project with ruff, mypy and pytest gates"
```

---

## Task 2: The layered purity contract

**Files:**
- Create: `backend/.importlinter`, `backend/src/fdi/{core,platform,pipeline,api,workers}/__init__.py`
- Test: `backend/tests/unit/test_import_contract.py`

- [ ] **Step 1: Create the package skeleton**

Empty `__init__.py` files satisfy import-linter's existence check — a layer must exist on disk, but may be empty.

```powershell
New-Item -ItemType Directory -Force backend/src/fdi/core, backend/src/fdi/platform, backend/src/fdi/pipeline, backend/src/fdi/api, backend/src/fdi/workers | Out-Null
New-Item -ItemType File backend/src/fdi/core/__init__.py, backend/src/fdi/platform/__init__.py, backend/src/fdi/pipeline/__init__.py, backend/src/fdi/api/__init__.py, backend/src/fdi/workers/__init__.py | Out-Null
```

- [ ] **Step 2: Write `backend/.importlinter`**

```ini
[importlinter]
root_package = fdi

[importlinter:contract:fdi-layers]
name = FDI layered architecture
type = layers
containers =
    fdi
layers =
    api | workers
    pipeline
    platform
    core
exhaustive = true
```

> **Two facts this depends on, both counter-intuitive:**
> - **`|` = independent siblings** (may NOT import each other). `:` would mean they *may*. The separator that reads like "or" is the strict one.
> - **`exhaustive = true` is mandatory.** A layers contract only constrains modules it *lists*. Without it, the day someone adds `fdi.utils`, `core` could import it and **CI would stay green** — a gate that looks enforced but leaks. `exhaustive` forces every child of `fdi` to be a declared layer or an explicit `exhaustive_ignores` entry. It requires `containers`.

- [ ] **Step 3: Write the failing test**

`backend/tests/unit/test_import_contract.py`:

```python
"""The layered contract is the project's most important structural rule.
This test runs it in-process so a violation fails pytest, not just CI."""
import os
import subprocess
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def test_import_contract_holds() -> None:
    # MUST be the `lint-imports` console script. Do NOT use
    # `python -m importlinter.cli` -- importlinter/cli.py has no
    # `if __name__ == "__main__"` guard, so `-m` merely imports the module and
    # exits 0 without running anything. Verified: pointing that form at a config
    # file that DOES NOT EXIST still returns exit 0 with no output, so this test
    # would pass forever while enforcing nothing.
    result = subprocess.run(
        ["lint-imports", "--config", str(BACKEND / ".importlinter")],
        cwd=BACKEND, capture_output=True, text=True,
        # Both load-bearing on Windows, same root cause: import-linter renders a
        # `:brick:` emoji via `rich`, which on a non-VT console encodes to the
        # ANSI code page (cp1252) and dies. PYTHONUTF8=1 fixes the child;
        # encoding="utf-8" stops OUR reader thread dying on the way back and
        # silently blanking result.stdout.
        encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}, timeout=30,
    )
    detail = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # A crashed linter and a real violation BOTH exit 1. Confirm the linter
    # reached a verdict before blaming the architecture for it.
    assert "Contracts:" in result.stdout, (
        f"lint-imports did not run to completion, so the contract was never "
        f"checked. This is a TOOLING failure, not an architecture violation.\n"
        f"{detail}"
    )
    assert result.returncode == 0, f"Layered architecture violated.\n{detail}"
```

- [ ] **Step 4: Run it**

Run: `uv run --directory backend pytest tests/unit/test_import_contract.py -v`
Expected: PASS (the packages are empty, so no imports can violate anything).

- [ ] **Step 5: Prove the gate actually bites**

Temporarily create `backend/src/fdi/core/_violation.py`:

```python
from fdi.platform import config  # noqa: F401  — core must never import platform
```

Run: `uv run --directory backend pytest tests/unit/test_import_contract.py -v`
Expected: **FAIL**, naming `fdi.core._violation -> fdi.platform.config`.

This step is the point of the task — a contract that has never been seen to fail is not known to work.

- [ ] **Step 6: Delete the violation**

```powershell
Remove-Item backend/src/fdi/core/_violation.py
```

Run: `uv run --directory backend pytest tests/unit/test_import_contract.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```powershell
git add backend
git commit -m "feat(backend): CI-enforced layered purity contract (exhaustive)"
```

---

## Task 3: The async-Celery-task guard

Celery's worst footgun: `async def` under `@app.task` does **not** error. It returns an un-awaited coroutine, reports **SUCCESS**, and does nothing. No amount of code review reliably catches this, so we make it a test.

**Files:**
- Test: `backend/tests/unit/test_no_async_celery_tasks.py`

- [ ] **Step 1: Write the failing test**

```python
"""Celery 5.6.3 has no native async task support (Celery 6 has not shipped).
Decorating `async def` with @app.task returns an un-awaited coroutine and
reports SUCCESS while doing nothing. This walks the AST so it cannot happen."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "fdi"


def _is_task_decorator(dec: ast.expr) -> bool:
    node = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(node, ast.Attribute):
        return node.attr in {"task", "shared_task"}
    if isinstance(node, ast.Name):
        return node.id in {"task", "shared_task"}
    return False


def test_no_async_def_under_celery_task() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                _is_task_decorator(d) for d in node.decorator_list
            ):
                offenders.append(f"{path}:{node.lineno} async def {node.name}")
    assert not offenders, (
        "async def under a Celery task decorator. Celery will report SUCCESS "
        "and do nothing.\nUse:\n"
        "    @app.task\n"
        "    def my_task(x): return asyncio.run(_my_task(x))\n\n"
        + "\n".join(offenders)
    )
```

- [ ] **Step 2: Run it**

Run: `uv run --directory backend pytest tests/unit/test_no_async_celery_tasks.py -v`
Expected: PASS (no tasks exist yet).

- [ ] **Step 3: Prove the guard bites**

Temporarily create `backend/src/fdi/workers/_violation.py`:

```python
from celery import shared_task


@shared_task
async def bad_task() -> None:
    pass
```

Run: `uv run --directory backend pytest tests/unit/test_no_async_celery_tasks.py -v`
Expected: **FAIL**, naming `_violation.py:5 async def bad_task`.

- [ ] **Step 4: Delete it and re-run**

```powershell
Remove-Item backend/src/fdi/workers/_violation.py
```

Run: `uv run --directory backend pytest tests/unit/test_no_async_celery_tasks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```powershell
git add backend
git commit -m "test(workers): AST guard against async def under @app.task"
```

### Known limits of this guard (measured, not assumed)

Two shapes defeat it. Both were confirmed empirically in the same run that correctly caught
all four canonical forms (`@shared_task`, `@app.task`, `@app.task(name=...)`,
`@shared_task(bind=True)`):

1. **Aliased decorators** — `task_alias = app.task` then `@task_alias`. The matcher keys on the
   literal names `task`/`shared_task`, so an alias is invisible to it.
2. **Class-based tasks** — `class BadTask(Task): async def run(self): ...`. Celery invokes `run()`
   by subclassing, so there is no decorator for an AST matcher to see at all. **This one is
   structural, not an oversight**: no decorator-based check can ever cover it.

Neither is fixed, deliberately — chasing them would over-build a guard against shapes this
project does not use. **The rule is: every stage uses the decorator form in §7.** If class-based
tasks are ever adopted, this guard silently stops protecting the thing it was built for, and
needs a second check keyed on `Task` subclasses with an async `run`. Recorded here so that
decision is made knowingly rather than discovered.

Also note the scan path is load-bearing: `SRC = parents[2] / "src" / "fdi"`. If it were wrong,
`rglob()` on a nonexistent directory returns **empty rather than raising**, so the test would
pass trivially forever while scanning nothing. Verified by breaking it on purpose. The
`assert files` line exists solely to make that failure impossible to mistake for a pass.

---

## Task 4: De-risk testcontainers on Windows ⚠️ EARLY BY DESIGN

**Everything in §11 of the spec rests on testcontainers working on this machine, and there is an open Windows port-mapping regression filed against exactly 4.14.2 (testcontainers-python#1015, a 4.12.0→4.14.2 regression).** This task proves it works *before* twenty tasks depend on it. If it fails, stop and escalate — the fallback is compose-based integration tests, which is a spec change, not a code change.

**Files:**
- Create: `backend/tests/integration/conftest.py`
- Test: `backend/tests/integration/test_containers_smoke.py`

- [ ] **Step 1: Write `backend/tests/integration/conftest.py`**

```python
"""Session-scoped containers for integration tests.

WINDOWS: TC_HOST is read at IMPORT time (it is a dataclass field default), so it
MUST be set before `testcontainers` is imported — setting it later silently does
nothing. See testcontainers-python#1009 and #407.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from urllib.parse import quote

if sys.platform == "win32":
    os.environ.setdefault("TC_HOST", "localhost")

import pytest
from minio import Minio
from testcontainers.minio import MinioContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.rabbitmq import RabbitMqContainer
from testcontainers.redis import RedisContainer

PG_IMAGE = "postgres:18.4"
# NOT RELEASE.2025-10-15T17-29-55Z — that release exists but was never published
# as a container image (MinIO stopped publishing free images ~2025-10-23).
MINIO_IMAGE = "minio/minio:RELEASE.2025-09-07T16-13-09Z"
RABBITMQ_IMAGE = "rabbitmq:4.3.2-management"
REDIS_IMAGE = "redis:8.8.0"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """postgresql+asyncpg://test:test@host:port/test

    The method-level `driver` overrides the constructor default (psycopg2).
    Readiness runs `psql` INSIDE the container, so no Python driver is needed
    to wait — `driver="asyncpg"` cannot break the wait strategy.
    """
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg.get_connection_url(driver="asyncpg")


@pytest.fixture(scope="session")
def minio_container() -> Iterator[MinioContainer]:
    with MinioContainer(MINIO_IMAGE) as mc:
        yield mc


@pytest.fixture(scope="session")
def minio_client(minio_container: MinioContainer) -> Minio:
    return minio_container.get_client()


@pytest.fixture(scope="session")
def minio_endpoint(minio_container: MinioContainer) -> str:
    """host:port, no scheme."""
    endpoint: str = minio_container.get_config()["endpoint"]
    return endpoint


@pytest.fixture(scope="session")
def amqp_url() -> Iterator[str]:
    """amqp://guest:guest@host:port/%2F

    RabbitMqContainer has NO url builder — assemble it. The default vhost '/'
    MUST be percent-encoded: the RabbitMQ URI spec says `vhost = segment`, so
    the common `...//` form is out of spec.
    """
    with RabbitMqContainer(RABBITMQ_IMAGE) as rabbit:
        p = rabbit.get_connection_params()
        user = quote(p.credentials.username, safe="")
        pwd = quote(p.credentials.password, safe="")
        vhost = quote(p.virtual_host, safe="")
        yield f"amqp://{user}:{pwd}@{p.host}:{p.port}/{vhost}"


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """redis://host:port/0 — RedisContainer has no get_connection_url()."""
    with RedisContainer(REDIS_IMAGE) as rc:
        host = rc.get_container_host_ip()
        port = rc.get_exposed_port(rc.port)
        yield f"redis://{host}:{port}/0"
```

- [ ] **Step 2: Write the smoke test**

`backend/tests/integration/test_containers_smoke.py`:

```python
"""Proves all four containers start and are reachable FROM THIS HOST.
If this fails on Windows, see testcontainers-python#1015 (open regression
against 4.14.2) before building anything else on these fixtures."""
import pytest
import sqlalchemy
from minio import Minio
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


async def test_postgres_reachable(postgres_url: str) -> None:
    assert postgres_url.startswith("postgresql+asyncpg://")
    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(sqlalchemy.text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


def test_minio_reachable(minio_client: Minio) -> None:
    minio_client.make_bucket("smoke")
    assert minio_client.bucket_exists("smoke")


def test_amqp_url_well_formed(amqp_url: str) -> None:
    assert amqp_url.startswith("amqp://")
    assert amqp_url.endswith("/%2F"), "default vhost must be percent-encoded"


def test_redis_url_well_formed(redis_url: str) -> None:
    assert redis_url.startswith("redis://")
    assert redis_url.endswith("/0")
```

- [ ] **Step 3: Run it — this is the gate**

Run: `uv run --directory backend pytest tests/integration/test_containers_smoke.py -v`
Expected: **4 passed** (first run pulls images — several minutes on this connection).

**If this fails with port-mapping or host-resolution errors: STOP.** Do not proceed to Task 5. Report the exact error. Fallback options, in order: (a) confirm `TC_HOST=localhost` is set before import, (b) pin `testcontainers==4.12.0` (pre-regression), (c) escalate to change spec §11 to compose-based integration tests.

- [ ] **Step 4: Commit**

```powershell
git add backend
git commit -m "test(integration): session-scoped testcontainers fixtures with Windows TC_HOST guard"
```

---

## Task 5: `core.serialization` — the first pure module

**Files:**
- Create: `backend/src/fdi/core/serialization.py`
- Test: `backend/tests/unit/test_serialization.py`

- [ ] **Step 1: Write the failing test**

```python
"""canonical_json underpins Global Constraint 6 (determinism): artifact bodies
must be a pure function of their input."""
from fdi.core.serialization import canonical_json


def test_key_order_does_not_change_output() -> None:
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b


def test_output_is_compact_and_sorted() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_unicode_is_preserved_not_escaped() -> None:
    assert canonical_json({"co": "Reliance ₹"}) == '{"co":"Reliance ₹"}'


def test_repeated_calls_are_byte_identical() -> None:
    payload = {"z": [3, 1, 2], "a": {"nested": True}, "n": None}
    assert canonical_json(payload) == canonical_json(payload)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_serialization.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.serialization'`

- [ ] **Step 3: Implement**

`backend/src/fdi/core/serialization.py`:

```python
"""Deterministic JSON. PURE — no I/O, no config, no clock, no RNG."""

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Serialize deterministically: identical input -> byte-identical output.

    - sort_keys: dict insertion order must not leak into bytes
    - separators: no incidental whitespace
    - ensure_ascii=False: emit real UTF-8 (annual reports carry ₹, é, etc.)
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_serialization.py -v`
Expected: 4 passed

- [ ] **Step 5: Verify the purity contract still holds**

Run: `uv run --directory backend lint-imports --config .importlinter`
Expected: `Contracts: 1 kept, 0 broken.`

- [ ] **Step 6: Commit**

```powershell
git add backend
git commit -m "feat(core): canonical_json for deterministic artifact bodies"
```

---

## Task 6: `platform.config` — settings from environment only

**Files:**
- Create: `backend/src/fdi/platform/config.py`
- Test: `backend/tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from pydantic import ValidationError

from fdi.platform.config import Settings

REQUIRED = {
    "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
    "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
    "FDI_RESULT_BACKEND": "redis://h:6379/0",
    "FDI_S3_ENDPOINT": "h:9000",
    "FDI_S3_ACCESS_KEY": "ak",
    "FDI_S3_SECRET_KEY": "sk",
}


def test_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.database_url == REQUIRED["FDI_DATABASE_URL"]
    assert s.s3_bucket == "artifacts"
    assert s.worker_concurrency == 2


def test_missing_required_setting_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in REQUIRED:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_unknown_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FDI_TYPO_SETTING", "x")
    with pytest.raises(ValidationError):
        Settings()


def test_does_not_read_dotenv_files(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """12-factor: Compose reads .env and injects env vars. The app reads env only."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FDI_DATABASE_URL=postgresql+asyncpg://leaked/db\n")
    for k in REQUIRED:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.platform.config'`

- [ ] **Step 3: Implement**

`backend/src/fdi/platform/config.py`:

```python
"""Configuration. Environment only — Compose reads .env and injects it."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FDI_",
        env_file=None,      # already the default; explicit so the intent is unmissable
        case_sensitive=False,
        extra="forbid",     # a typo'd env var must fail loudly, not be ignored
    )

    database_url: str
    broker_url: str
    result_backend: str

    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str = "artifacts"
    s3_secure: bool = False

    worker_concurrency: int = 2
    log_level: str = "INFO"
    artifact_url_ttl_seconds: int = 3600


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

> `Settings()` is never instantiated at import — only inside `get_settings()`. A module-level
> instantiation would read the environment at import time and break test isolation.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_config.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```powershell
git add backend
git commit -m "feat(platform): env-only settings with strict extra=forbid"
```

---

## Task 7: `platform.obs.logging` — structlog with correlation IDs

Two verified traps here. (1) The common `LoggerFactory` + `JSONRenderer` chain **double-prints tracebacks** — `BoundLogger.exception()` hardcodes `exc_info=True` and proxies to stdlib, which re-appends the traceback structlog already rendered. `ProcessorFormatter` fixes it *and* routes uvicorn/Celery's own logs through JSON. (2) contextvars is **copy-in, no-copy-out** — anything bound inside `asyncio.run()` is invisible to the caller.

**Files:**
- Create: `backend/src/fdi/platform/obs/__init__.py`, `backend/src/fdi/platform/obs/logging.py`
- Test: `backend/tests/unit/test_logging.py`
- Also created (deviation, see below): `backend/tests/conftest.py`

> **Added during execution: an autouse `clear_contextvars()` fixture in `backend/tests/conftest.py`.**
> Measured, not assumed: a `correlation_id` bound in `test_logging.py` is readable from a
> *different test file* — pytest shares one thread and one Context across the whole session.
> Nothing depends on the leak today, so this buys isolation rather than fixing a live bug. It is
> in because the failure it prevents is the expensive kind: once SP-2 asserts an ID is *absent* or
> holds a specific value, the result depends on which tests ran first, and that gets debugged as a
> logging bug rather than a fixture bug. It also matches production, where every Celery task binds
> a fresh ID from its message header before `asyncio.run()`.

- [ ] **Step 1: Write the failing test**

```python
import asyncio
import json

from fdi.platform.obs.logging import (
    bind_correlation_id,
    configure_logging,
    get_correlation_id,
    new_correlation_id,
)


def test_new_correlation_id_is_unique() -> None:
    assert new_correlation_id() != new_correlation_id()


def test_bind_then_get_roundtrips() -> None:
    bind_correlation_id("abc-123")
    assert get_correlation_id() == "abc-123"


def test_correlation_id_survives_asyncio_run() -> None:
    """Global Constraint 4. contextvars copies IN across asyncio.run(), so an ID
    bound BEFORE the call is visible inside it."""
    bind_correlation_id("before-loop")

    async def _inner() -> str:
        return get_correlation_id()

    assert asyncio.run(_inner()) == "before-loop"


def test_binding_inside_asyncio_run_does_not_escape() -> None:
    """The other half of Constraint 4, pinned so nobody 'fixes' it later.
    Mutations inside the loop die with the copied Context."""
    bind_correlation_id("outer")

    async def _inner() -> None:
        bind_correlation_id("inner-only")

    asyncio.run(_inner())
    assert get_correlation_id() == "outer"


def test_log_output_is_json_with_correlation_id(capsys) -> None:
    configure_logging("INFO")
    bind_correlation_id("cid-42")
    import structlog

    structlog.get_logger("test").info("hello", extra_field=7)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["correlation_id"] == "cid-42"
    assert payload["extra_field"] == 7
    assert payload["level"] == "info"


def test_exception_is_not_double_printed(capsys) -> None:
    """The LoggerFactory+JSONRenderer chain emits the JSON line AND a raw
    traceback. ProcessorFormatter must not."""
    configure_logging("INFO")
    import structlog

    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger("test").exception("failed")

    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1, f"expected one JSON line, got:\n{out}"
    payload = json.loads(out)
    assert "ValueError: boom" in payload["exception"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_logging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.platform.obs'`

- [ ] **Step 3: Implement**

`backend/src/fdi/platform/obs/__init__.py`: (empty file)

`backend/src/fdi/platform/obs/logging.py`:

```python
"""Structured logging with a correlation ID that survives process boundaries.

contextvars is copy-IN / no-copy-OUT. An ID bound before asyncio.run() is
visible inside the loop; one bound inside is lost. So Celery tasks MUST bind in
the sync wrapper, from the message header, before calling asyncio.run().
"""

import logging
import sys
import uuid

import structlog

_CORRELATION_ID = "correlation_id"


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def bind_correlation_id(value: str) -> None:
    structlog.contextvars.bind_contextvars(**{_CORRELATION_ID: value})


def get_correlation_id() -> str:
    value = structlog.contextvars.get_contextvars().get(_CORRELATION_ID, "")
    return str(value)


def clear_correlation_id() -> None:
    structlog.contextvars.unbind_contextvars(_CORRELATION_ID)


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib to emit one JSON line per event.

    ProcessorFormatter (not LoggerFactory+JSONRenderer) because the latter
    double-prints tracebacks: BoundLogger.exception() sets exc_info on the
    LogRecord and stdlib re-appends the traceback structlog already rendered.
    It also gives uvicorn's and Celery's own loggers the same JSON shape and
    correlation ID, via foreign_pre_chain.
    """
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,   # MUST be first
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[*shared_processors,
                    structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_logging.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```powershell
git add backend
git commit -m "feat(platform): structlog JSON logging with correlation-id contextvar"
```

---

## Task 8: `platform.db.models` — Base and Job

**Files:**
- Create: `backend/src/fdi/platform/db/__init__.py`, `backend/src/fdi/platform/db/models.py`
- Test: `backend/tests/unit/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
from fdi.platform.db.models import Base, Job, JobStatus


def test_job_table_name() -> None:
    assert Job.__tablename__ == "jobs"


def test_job_is_registered_on_metadata() -> None:
    assert "jobs" in Base.metadata.tables


def test_job_columns() -> None:
    cols = Base.metadata.tables["jobs"].columns
    assert {"id", "status", "artifact_key", "error",
            "correlation_id", "created_at", "updated_at"} <= set(cols.keys())
    assert cols["id"].primary_key
    assert cols["status"].index
    assert cols["artifact_key"].nullable
    assert not cols["status"].nullable


def test_job_status_values() -> None:
    assert {s.value for s in JobStatus} == {"queued", "running", "done", "failed"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.platform.db'`

- [ ] **Step 3: Implement**

`backend/src/fdi/platform/db/__init__.py`: (empty file)

`backend/src/fdi/platform/db/models.py`:

```python
"""ORM models. Timestamps are server-side: the DB clock is the single source of
truth, and this keeps wall-clock out of artifact bodies (Global Constraint 6)."""

import enum
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStatus(enum.StrEnum):
    """StrEnum, NOT `(str, enum.Enum)` -- that form fails ruff UP042, and for a
    real reason: it renders as `JobStatus.QUEUED` under f-strings and `%s` while
    comparing, joining and persisting as `queued`. It passes every obvious smoke
    test and corrupts only interpolated output -- a log line or an f-string query.
    StrEnum gives `queued` everywhere."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    artifact_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_models.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```powershell
git add backend
git commit -m "feat(platform): Job ORM model with server-side timestamps"
```

---

## Task 9: `platform.db.engine` — factories, never module-level

**Files:**
- Create: `backend/src/fdi/platform/db/engine.py`
- Test: `backend/tests/unit/test_engine.py`

- [ ] **Step 1: Write the failing test**

```python
import ast
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from fdi.platform.db.engine import make_engine, make_session_factory

ENGINE_SRC = Path(__file__).resolve().parents[2] / "src" / "fdi" / "platform" / "db" / "engine.py"


def test_make_engine_returns_async_engine() -> None:
    engine = make_engine("postgresql+asyncpg://u:p@h:5432/db")
    assert isinstance(engine, AsyncEngine)


def test_make_session_factory_is_callable() -> None:
    engine = make_engine("postgresql+asyncpg://u:p@h:5432/db")
    factory = make_session_factory(engine)
    assert callable(factory)


def test_no_module_level_engine() -> None:
    """Global Constraint 3. A pool built at import is inherited across fork(),
    handing two interpreters one socket — corrupting SILENTLY."""
    tree = ast.parse(ENGINE_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        assert not isinstance(node, ast.Assign), (
            f"module-level assignment at line {node.lineno} in engine.py"
        )
        assert not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call), (
            f"module-level call at line {node.lineno} in engine.py"
        )
```

> ### ⛔ The guard above is INERT. Do not ship it. It is kept here only as the specimen.
>
> Measured during execution, by writing each violating form into the real `engine.py` and running
> the real guard against it: **it catches 2 of 12 forms, and false-positives on 2 innocent ones.**
>
> | Form | Plan's guard | Shipped guard |
> |---|---|---|
> | `_e = create_async_engine(...)` | CAUGHT | CAUGHT |
> | `_e: AsyncEngine = create_async_engine(...)` | **MISSED** | CAUGHT |
> | bare `create_async_engine(...)` | CAUGHT | CAUGHT |
> | `_engines += [create_async_engine(...)]` | **MISSED** | CAUGHT |
> | `(_e := create_async_engine(...))` | **MISSED** | CAUGHT |
> | nested in `if` / `try` / `with` / `for` | **MISSED** | CAUGHT |
> | nested in `if TYPE_CHECKING:` | **MISSED** | CAUGHT |
> | `def f(e=create_async_engine(...))` | **MISSED** | CAUGHT |
> | `class C: engine = create_async_engine(...)` | **MISSED** | CAUGHT |
> | *innocent* `__all__ = [...]` | **FIRES** | allowed |
> | *innocent* `DEFAULT_POOL_SIZE = 5` | **FIRES** | allowed |
>
> **The miss that matters is `AnnAssign`.** This project runs `mypy --strict`, so the natural way to
> write a module-level engine *here* is `_engine: AsyncEngine = create_async_engine(url)` — a
> different AST node from `ast.Assign`. The guard had a hole shaped exactly like the codebase's own
> house style. Every nested form escaped for one reason: `tree.body` is flat and the guard never
> descends. `def`-defaults and class bodies build a pool at import **while looking deferred**.
>
> Three corrections went into the shipped version, each of which the specimen gets wrong:
>
> 1. **Match engine *construction*, not assignment-in-general.** The false positives are the more
>    dangerous direction: a guard that rejects `__all__` gets deleted by the first person it annoys,
>    and takes the real enforcement with it.
> 2. **Walk every node evaluated at import** — descending through `if`/`try`/`with`/`for` and class
>    bodies, stopping at function and lambda bodies (where an engine is *supposed* to be built), but
>    still checking their decorators and default args, which run at `def` time.
> 3. **Scan the whole package, not `engine.py`.** `engine.py` is the file *least* likely to offend —
>    it exports factories by construction. The accident happens in a **consumer** reaching for a
>    module-level `_engine` to avoid threading one through, and that consumer is what Celery forks.
>    Proven: a module-level engine planted in `fdi/workers/__init__.py` is caught, and is
>    structurally invisible to a file-scoped guard.
>
> **Known limits, stated rather than hidden:** aliased imports (`create_async_engine as cae`) evade
> name matching, and `if TYPE_CHECKING:` is flagged though it never executes. This stops the
> accident, not the adversary — which is the correct goal for this guard.
>
> **See `backend/tests/unit/test_engine.py` for the shipped version.** It carries the same
> `assert files` self-check as `test_no_async_celery_tasks.py`: a wrong `SRC` makes `rglob()` return
> empty rather than raise, and `assert not offenders` would then pass having read nothing, forever.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.platform.db.engine'`

- [ ] **Step 3: Implement**

`backend/src/fdi/platform/db/engine.py`:

```python
"""Engine/session factories.

NEVER build an engine at module import. fork() copies the object graph but not
the threads driving it, so an inherited pool hands two interpreters the same
socket and corrupts silently. Celery tasks build an engine inside their
asyncio.run() coroutine and dispose it in `finally`; FastAPI builds one in
lifespan. Both own it for exactly as long as their event loop lives.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(url, echo=echo, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_engine.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```powershell
git add backend
git commit -m "feat(platform): engine factories with a no-module-level-engine guard"
```

---

## Task 10: Alembic migration for `jobs`

**Files:**
- Create: `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`, `backend/alembic/versions/0001_create_jobs.py`
- Test: `backend/tests/integration/test_migrations.py`

- [ ] **Step 1: Scaffold Alembic**

Run: `uv run --directory backend alembic init -t async alembic`
Expected: creates `backend/alembic/` and `backend/alembic.ini`.

> **The path is `alembic`, not `backend/alembic`.** `--directory backend` already puts cwd at
> `backend/`, so the longer form creates `backend/backend/alembic`. With the correct form
> `alembic.ini` lands in `backend/` directly, the generated `script_location = %(here)s/alembic`
> is already right, and **no `Move-Item` step is needed** — the plan previously specified one to
> clean up after the wrong path.

- [ ] **Step 2: Point `alembic.ini` at the right script location**

In `backend/alembic.ini`, set:

```ini
[alembic]
script_location = %(here)s/alembic
prepend_sys_path = %(here)s/src
sqlalchemy.url =
```

Leave `sqlalchemy.url` empty — `env.py` reads it from `Settings` so there is exactly one source of config.

- [ ] **Step 3: Replace `backend/alembic/env.py`**

```python
"""Alembic environment. Reads the URL from Settings so config has one source."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from fdi.platform.config import get_settings
from fdi.platform.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` -> `%%` is load-bearing, not defensive coding. set_main_option writes into
# a configparser, which reads `%` as interpolation syntax. A password of `p@ss`
# is CORRECTLY encoded in a URL as `p%40ss` -- and that kills every alembic
# command at import with `ValueError: invalid interpolation syntax ... at
# position 24`, naming neither alembic nor the password. It fires at set() time,
# so nothing alembic-related even appears in the traceback.
# The app is unaffected: create_async_engine takes the URL raw. So this breaks
# ONLY migrations, ONLY in environments with a special character in the password
# -- i.e. it passes every local test and fails at deploy.
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 4: Write the migration**

`backend/alembic/versions/0001_create_jobs.py`:

```python
"""create jobs table

Revision ID: 0001
Revises:
Create Date: 2026-07-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("artifact_key", sa.String(512), nullable=True),
        sa.Column("error", sa.String(2048), nullable=True),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # The ONLY thing enforcing the status domain -- see the note below.
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')",
            name="ck_jobs_status",
        ),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_correlation_id", "jobs", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_correlation_id", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
```

> **Why an explicit `CHECK` and not `Enum(JobStatus)` — all three alternatives are traps, measured
> against SQLAlchemy 2.0.51 on this box.** The obvious "improvement" here is to swap `String(16)`
> for a real enum type. Do not.
>
> - **`sa.Enum(JobStatus)`** creates a native PG type whose accepted labels are
>   `['QUEUED', 'RUNNING', 'DONE', 'FAILED']` — the enum **member names, not their values**. The
>   app writes `"queued"` (the value, per `test_job_status_values`), so PG would reject every
>   correct write and accept only strings no other layer produces. Fixable with
>   `values_callable=lambda x: [i.value for i in x]`, which is exactly the kind of incantation
>   nobody adds until after the outage.
> - **`sa.Enum(JobStatus, native_enum=False)`** looks like the safe middle path and is the worst of
>   the three: `create_constraint` defaults to **`False`** in SQLAlchemy 2.x, so it emits a bare
>   `VARCHAR(7)` with **no constraint at all** — less enforcement than `String(16)`, wearing a name
>   that reads as enforced.
> - Native PG enums are also genuinely painful to evolve (`ALTER TYPE ... ADD VALUE` cannot run in
>   a transaction on older PG; removing a value means recreating the type).
>
> Storing the plain string and constraining it in the DB keeps the domain enforced where it cannot
> be bypassed, and keeps migrations ordinary DDL. `JobStatus` stays the single source of the four
> literals for Python callers.

> ### ⚠️ `alembic check` is BLIND to the CHECK constraint. The banana test is its only guard.
>
> Measured during execution, not assumed: with the `CheckConstraint` **deleted** from the
> migration, against a live PostgreSQL 18.4 —
>
> ```
> is ck_jobs_status in the DB?      (nothing)
> does 'banana' insert succeed?     INSERT 0 1
> does `alembic check` notice?      No new upgrade operations detected.   exit 0
> ```
>
> Autogenerate does not compare CHECK constraints in either direction, and the ORM never declares
> one, so there is nothing to diff. `alembic check` is strong evidence for **columns and indexes**
> and **zero** evidence for the constraint this task calls its point.
>
> So `test_status_check_constraint_rejects_unknown_values` is not belt-and-braces on top of
> autogenerate — **it is the entire enforcement**. Delete it and the status domain silently
> evaporates with every gate still green. That is the exact shape of the three inert gates this
> plan has already found, so it is called out here rather than trusted to be obvious.

- [ ] **Step 5: Write the test**

`backend/tests/integration/test_migrations.py`:

```python
"""Proves the migration applies to a real PostgreSQL 18.4 and matches the ORM."""
import pytest
import sqlalchemy

pytestmark = pytest.mark.integration


async def test_migration_creates_jobs_table(migrated_engine) -> None:
    async with migrated_engine.connect() as conn:
        rows = await conn.execute(sqlalchemy.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'jobs'"
        ))
        cols = {r[0] for r in rows}
    assert {"id", "status", "artifact_key", "error",
            "correlation_id", "created_at", "updated_at"} == cols


async def test_status_check_constraint_rejects_unknown_values(migrated_engine) -> None:
    """The CHECK is the only thing enforcing the status domain -- prove it bites.

    `Job.status` is String(16), not Enum(JobStatus), so nothing in Python stops
    status="banana": JobStatus IS a str, so mypy accepts the enum and any string
    alike. Without this constraint the four-value domain is a convention, and
    conventions rot silently. A gate never seen to fail is not known to work.
    """
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text(
            "INSERT INTO jobs (id, status) VALUES ('ck-ok', 'queued')"
        ))
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_jobs_status"):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO jobs (id, status) VALUES ('ck-bad', 'banana')"
            ))


async def test_alembic_version_is_stamped(migrated_engine) -> None:
    async with migrated_engine.connect() as conn:
        result = await conn.execute(sqlalchemy.text(
            "SELECT version_num FROM alembic_version"
        ))
        assert result.scalar_one() == "0001"
```

- [ ] **Step 6: Add the `migrated_engine` fixture to `backend/tests/integration/conftest.py`**

**Merge these imports into the file's existing top import block — do NOT append them.** Appending
puts imports below module-level code, which is ruff **E402** and fails the lint gate. Only the
fixture body goes at the bottom.

```python
from collections.abc import AsyncIterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def _alembic_applied(postgres_url: str) -> Iterator[None]:
    """Run migrations once per session against the container.

    All SIX Settings fields must be set, not just DATABASE_URL: env.py calls
    get_settings() at import, which validates the whole model, so `alembic
    history` fails without FDI_S3_SECRET_KEY.

    Use MonkeyPatch, NOT os.environ. Raw os.environ assignment does not restore,
    integration runs BEFORE unit, so all six leak into the unit phase along with
    a get_settings cache pinned to a dying container. test_config.py survives
    that only because it happens to monkeypatch every key it reads -- luck, not
    design, and luck that expires the first time a test asserts on an FDI_ var
    it did not set itself.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("FDI_DATABASE_URL", postgres_url)
        for key, value in {
            "FDI_BROKER_URL": "amqp://guest:guest@localhost:5672/%2F",
            "FDI_RESULT_BACKEND": "redis://localhost:6379/0",
            "FDI_S3_ENDPOINT": "localhost:9000",
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
        }.items():
            if key not in os.environ:
                monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        # NOTE: no set_main_option("script_location", ...) override here, on
        # purpose. Overriding it would mask a broken alembic.ini -- the suite
        # would pass while the ini nobody tests is wrong. Let %(here)s resolve.
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        command.upgrade(cfg, "head")
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
async def migrated_engine(
    postgres_url: str, _alembic_applied: None
) -> AsyncIterator[AsyncEngine]:
    """Function-scoped ON PURPOSE - do NOT make this session-scoped.

    pytest-asyncio 1.x runs each test on its own function-scoped event loop
    (asyncio_default_fixture_loop_scope defaults to None). A session-scoped
    async fixture would bind its pool to a different loop than the test using
    it, producing exactly the failure this project's spec warns about:
    `got Future attached to a different loop`.

    Function scope also mirrors production: every Celery task builds an engine
    inside its own asyncio.run() and disposes it in `finally`. The test should
    look like the thing it tests. The container stays session-scoped, so the
    cost is a connection handshake per test, not a container start.
    """
    engine = create_async_engine(postgres_url)
    yield engine
    await engine.dispose()
```

- [ ] **Step 7: Run to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_migrations.py -v` (needs Docker)
Expected: 3 passed — table shape, ORM/migration agreement, and the CHECK rejecting `'banana'`.

- [ ] **Step 8: Commit**

```powershell
git add backend
git commit -m "feat(platform): alembic async env and the jobs migration"
```

---

## Task 11: `platform.db.repository` — JobRepository

**Files:**
- Create: `backend/src/fdi/platform/db/repository.py`
- Test: `backend/tests/integration/test_repository.py`

> **`updated_at` is only as honest as this class. Measured during Task 8, against SQLAlchemy 2.0.51.**
> `onupdate=func.now()` is a SQLAlchemy-level default, not a DB trigger, so it fires only for writes
> this ORM issues. It **does** fire for ORM attribute mutation *and* for Core `update(Job)...`
> (that one surprised me — `func.now()` is a SQL expression rendered inline at compile time, so
> `session.execute(update(...))` gets `SET ..., updated_at=now()` too). It is **bypassed** by:
> raw `text("UPDATE jobs ...")`, **`ON CONFLICT DO UPDATE`** (the upsert emits no `updated_at` in
> its SET clause), and any call passing `updated_at` explicitly. A flush with no net change emits
> no UPDATE at all, so the timestamp simply does not advance.
>
> So `updated_at` means *"last write through SQLAlchemy in this process"* — trustworthy precisely
> while `JobRepository` is the only writer. **If this class ever upserts, `updated_at` silently
> stops advancing and every staleness check built on it quietly lies.** Either set it explicitly in
> the upsert's `SET`, or move the whole thing to a DB trigger. Decide it here, deliberately, not by
> reaching for `ON CONFLICT` when a race shows up in SP-2.
>
> **`now()` is transaction-start time — now VERIFIED against the live container, not just PG's docs.**
> Two `SELECT now()` 100ms apart inside one transaction return an identical value; `statement_timestamp()`
> and `clock_timestamp()` both differ. Consequences: rows created in one transaction share a
> `created_at`, so **`created_at` is not a total order** and anything sorting by it must tie-break.
> And any `updated_at` test must span transactions, or it fails for reasons unrelated to the code.

> ### ⚠️ Three real hazards this class ships with, all measured. Carry them into SP-2.
>
> None is wrong for a walking skeleton with one worker and no retries. All three become wrong the
> moment SP-2 adds retries to an at-least-once broker, so they are recorded rather than discovered.
>
> 1. **The status columns are independent facts.** Neither mutator clears the other's column.
>    Measured: `mark_done` then `mark_failed` leaves
>    `status='failed'` **holding a valid `artifact_key`** — a row claiming failure while carrying a
>    good artifact. `mark_running` on a finished job reopens it with both still set. Any consumer
>    doing `if status == "failed": show(error)` then shows a stale error next to a real result.
>    The fix is *not* a rigid state machine — forbidding `failed → done` would break legitimate
>    retries. It is (a) clear the counterpart column, (b) guard terminal transitions with a
>    `WHERE status = ...` so a late duplicate no-ops instead of clobbering.
> 2. **Concurrent `mark_done` is a silent lost update — no error at all.** Measured across two
>    sessions: both returned successfully, last writer won, `errors raised: NONE`. PG's READ
>    COMMITTED row-locks so B blocks until A commits, then B's `WHERE id = ...` still matches and
>    overwrites. Without `version_id_col`, SQLAlchemy sees `rowcount == 1` and is satisfied — no
>    `StaleDataError`. (Shown across connections in one process, not two OS processes; the mechanism
>    does not depend on the process boundary, but that specific pairing is unproven.)
> 3. **`error[:2048]` loses tracebacks quietly.** The units are correct — PG's `varchar(2048)` counts
>    *characters*, not bytes (2048 emoji = 8192 octets stores fine; 2049 raises), and Python slicing
>    counts codepoints, so they match exactly. But 5000 chars silently become 2048 with no marker and
>    no log, and tracebacks routinely exceed 2048. Note the slice is still the *better* of the two
>    obvious options: without it PG raises `StringDataRightTruncation`, which aborts the transaction,
>    so the job would never be marked failed at all — losing the whole failure rather than its tail.
>    That is a false dichotomy though: truncating **with** a `…[truncated N chars]` marker costs one
>    line and keeps the diagnostic honest. Do that in SP-2.

> **`correlation_id` has three representations of "absent", and the type system endorses the worst one.**
> `create(correlation_id: str)` is a stricter promise than the column (`Mapped[str | None]`, nullable —
> and `test_migrations.py` itself inserts a NULL-correlation row, so the nullability is real, not
> vestigial). The repository's strictness is right and matches spec §8 ("minted at the API"). The
> hazard is the obvious call site: **`get_correlation_id()` returns `""` when unbound, never `None`** —
> so `create(job_id, correlation_id=get_correlation_id())` writes an empty string that satisfies
> `str` while meaning nothing, and `ix_jobs_correlation_id` grows one entry shared by every
> context-less job. Three ways to say "no ID" (NULL, `""`, real), only one of which any type checker
> objects to. Fix when Task 16/17 wire the API: `nullable=False` plus a non-empty CHECK, keeping the
> strict signature — a Task 10 migration change, so deliberately not done here.

- [ ] **Step 1: Write the failing test**

```python
import uuid

import pytest

from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.models import JobStatus
from fdi.platform.db.repository import JobRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine):
    return make_session_factory(migrated_engine)


async def test_create_then_get(session_factory) -> None:
    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id="cid-1")
        await s.commit()

    async with session_factory() as s:
        job = await JobRepository(s).get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED.value
    assert job.correlation_id == "cid-1"
    assert job.artifact_key is None


async def test_get_missing_returns_none(session_factory) -> None:
    async with session_factory() as s:
        assert await JobRepository(s).get(str(uuid.uuid4())) is None


async def test_mark_done_sets_artifact_key(session_factory) -> None:
    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        repo = JobRepository(s)
        await repo.create(job_id, correlation_id="cid-2")
        await s.commit()
    async with session_factory() as s:
        await JobRepository(s).mark_done(job_id, artifact_key="k/ping.json")
        await s.commit()
    async with session_factory() as s:
        job = await JobRepository(s).get(job_id)
    assert job is not None
    assert job.status == JobStatus.DONE.value
    assert job.artifact_key == "k/ping.json"


async def test_mark_failed_records_error(session_factory) -> None:
    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id="cid-3")
        await s.commit()
    async with session_factory() as s:
        await JobRepository(s).mark_failed(job_id, error="boom")
        await s.commit()
    async with session_factory() as s:
        job = await JobRepository(s).get(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED.value
    assert job.error == "boom"


async def test_mark_done_on_missing_job_raises(session_factory) -> None:
    async with session_factory() as s:
        with pytest.raises(KeyError):
            await JobRepository(s).mark_done(str(uuid.uuid4()), artifact_key="x")


@pytest.mark.parametrize("mutator", ["mark_running", "mark_done", "mark_failed"])
async def test_updated_at_advances_and_created_at_does_not(session_factory, mutator) -> None:
    """The ONLY thing watching `onupdate`. Without this, all five tests above stay
    green with `onupdate=func.now()` deleted from the model -- measured, not feared.

    MUST span two transactions. PG's now() is TRANSACTION-START time (verified
    against the live container: two SELECTs 100ms apart in one transaction return
    an identical now(), while statement_timestamp() differs). `onupdate` renders
    now() into the UPDATE's SET clause, so a create+mutate sharing a transaction
    stamps both columns from one clock reading and this assertion fails for a
    reason that has nothing to do with the code.

    No sleep() needed: back-to-back transactions land ~5.3ms apart against PG's
    microsecond resolution. Do not add one to "fix flakiness" -- if this goes
    flaky, the cause is transaction scope, not timing.
    """
    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id="cid-ts")
        await s.commit()
    async with session_factory() as s:
        before = await JobRepository(s).get(job_id)
        assert before is not None
        created_at, updated_at = before.created_at, before.updated_at

    async with session_factory() as s:
        repo = JobRepository(s)
        kwargs = {"mark_done": {"artifact_key": "k/x.json"},
                  "mark_failed": {"error": "boom"}}.get(mutator, {})
        await getattr(repo, mutator)(job_id, **kwargs)
        await s.commit()

    async with session_factory() as s:
        after = await JobRepository(s).get(job_id)
    assert after is not None
    assert after.updated_at > updated_at, (
        "updated_at did not advance -- onupdate did not reach the UPDATE's SET clause"
    )
    assert after.created_at == created_at, "created_at must never move"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_repository.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.platform.db.repository'`

- [ ] **Step 3: Implement**

`backend/src/fdi/platform/db/repository.py`:

```python
"""Job persistence. The caller owns the transaction — commit is theirs, not ours."""

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.platform.db.models import Job, JobStatus


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, job_id: str, correlation_id: str) -> Job:
        job = Job(id=job_id, status=JobStatus.QUEUED.value, correlation_id=correlation_id)
        self._session.add(job)
        await self._session.flush()
        return job

    async def get(self, job_id: str) -> Job | None:
        return await self._session.get(Job, job_id)

    async def _require(self, job_id: str) -> Job:
        job = await self._session.get(Job, job_id)
        if job is None:
            raise KeyError(f"job {job_id} not found")
        return job

    async def mark_running(self, job_id: str) -> None:
        (await self._require(job_id)).status = JobStatus.RUNNING.value

    async def mark_done(self, job_id: str, artifact_key: str) -> None:
        job = await self._require(job_id)
        job.status = JobStatus.DONE.value
        job.artifact_key = artifact_key

    async def mark_failed(self, job_id: str, error: str) -> None:
        job = await self._require(job_id)
        job.status = JobStatus.FAILED.value
        job.error = error[:2048]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_repository.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```powershell
git add backend
git commit -m "feat(platform): JobRepository over async SQLAlchemy"
```

---

## Task 12: `platform.storage.artifact_store` — async S3

The `minio` SDK is synchronous. We wrap it in `asyncio.to_thread`, which gives a genuinely async interface over a maintained client — and contextvars *do* propagate into `to_thread`, so log lines from inside keep their correlation ID.

> **That contextvars claim is now TESTED, not just asserted** — it was stated twice here and watched
> by nothing, the same shape as the four inert gates already found. Verified by counterfactual, which
> is the only way to prove it: swapping `asyncio.to_thread(f)` → `loop.run_in_executor(None, f)` —
> the *same* thread offload, differing only in the context copy — turns the test RED
> (`'' != 'cid-...'`). So `to_thread` is **load-bearing, not stylistic**. The test also asserts the
> call really left the calling thread; without that, a plain `f()` would keep the ID trivially and
> the test would prove nothing.

> ### ⚠️ `len(body)` must count BYTES. This one is not hypothetical for *this* corpus.
>
> `canonical_json` uses `ensure_ascii=False`, so characters ≠ bytes for any non-ASCII input — and
> this platform ingests **Indian annual reports**, where `₹` is on every page. Measured:
>
> ```
> canonical_json({"amount": "₹1,00,000", "note": "café"})
>   -> 36 CHARACTERS but 39 BYTES
> put_object(length=len(canonical_json(obj)))  -> uploads 36 of 39 bytes
> truncated body -> b'...,"note":"caf\xc3'    <- severed mid-codepoint
> still valid JSON? -> NO: UnicodeDecodeError
> ```
>
> **Encode first, then measure.** `len(body)` where `body = canonical_json(obj).encode("utf-8")` is
> correct; `len(canonical_json(obj))` silently uploads a prefix. Pinned by a parametrized test whose
> `multi-byte-utf8` case goes RED under the wrong form while both ASCII cases stay green — which is
> exactly why a test with only ASCII fixtures would have blessed the bug.

> **Deliberately NOT tested, and why:** `get_raw`'s `finally: close(); release_conn()` cannot be
> distinguished from its absence on this code path. Measured over 25 sequential GETs:
> `full_no_cleanup=0, partial_no_cleanup=24, partial_with_cleanup=1` — `read()` drains to EOF and
> urllib3's `_error_catcher` already releases. A test asserting the cleanup prevents leaks **passes
> with the cleanup deleted**; one was written, caught doing exactly that, and removed rather than
> hedged. A false guarantee is worse than no guarantee. The cleanup stays — it is what stops the 24
> the day someone streams a large artifact instead of reading it whole.

**Files:**
- Create: `backend/src/fdi/platform/storage/__init__.py`, `backend/src/fdi/platform/storage/artifact_store.py`
- Test: `backend/tests/integration/test_artifact_store.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from minio import Minio

from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


async def test_ensure_bucket_is_idempotent(store: ArtifactStore) -> None:
    await store.ensure_bucket()
    await store.ensure_bucket()


async def test_put_then_get_roundtrips(store: ArtifactStore) -> None:
    key = await store.put_json("a/b/ping.json", {"hello": "world", "n": 1})
    assert key == "a/b/ping.json"
    assert await store.get_json("a/b/ping.json") == {"hello": "world", "n": 1}


async def test_put_json_body_is_canonical(store: ArtifactStore) -> None:
    """Global Constraint 6: the body is a pure function of the input, so key
    order in the source dict must not change the stored bytes."""
    await store.put_json("k1.json", {"b": 1, "a": 2})
    await store.put_json("k2.json", {"a": 2, "b": 1})
    assert await store.get_raw("k1.json") == await store.get_raw("k2.json")
    assert await store.get_raw("k1.json") == b'{"a":2,"b":1}'


async def test_get_missing_raises(store: ArtifactStore) -> None:
    from minio.error import S3Error

    with pytest.raises(S3Error):
        await store.get_json("does/not/exist.json")


async def test_presigned_url_is_returned(store: ArtifactStore) -> None:
    await store.put_json("p.json", {"x": 1})
    url = await store.presigned_get_url("p.json", ttl_seconds=60)
    assert url.startswith("http")
    assert "p.json" in url
```

> **⛔ That presigned test proves almost nothing — see the shipped version instead.**
> `startswith("http")` and `"p.json" in url` all pass for a malformed, unsigned, or unreachable URL.
> The shipped test **fetches** it and compares bytes, pins the netloc to the configured endpoint,
> asserts `X-Amz-Expires=60` (proving `ttl_seconds` actually reaches the signature), and GETs it
> **unsigned expecting 403** — without that control, a plain URL against a public bucket looks
> identical to a correctly signed one.
>
> **The hazard this test structurally CANNOT catch — carry it to Task 20.** testcontainers builds the
> client from the *host's* view (`get_container_host_ip()` + mapped port), so the presigned URL is
> host-reachable **by construction**:
>
> ```
> minio_endpoint: localhost:24775
> presigned URL : http://localhost:24775/test-artifacts/p.json?X-Amz-Algorithm=...  -> 200
> ```
>
> Under compose the API container gets `FDI_S3_ENDPOINT=minio:9000`, so presigning emits
> `http://minio:9000/...` — a hostname **the browser cannot resolve**, while every test stays green.
> A presigned URL embeds whatever endpoint its client was configured with, so the one value that
> makes the container work is the one that breaks the browser. **Presigning needs a separate public
> endpoint** (`FDI_S3_PUBLIC_ENDPOINT`), decided in Task 20 — not discovered when the UI shows a
> broken download link.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_artifact_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.platform.storage'`

- [ ] **Step 3: Implement**

`backend/src/fdi/platform/storage/__init__.py`: (empty file)

`backend/src/fdi/platform/storage/artifact_store.py`:

```python
"""S3 artifact store.

Targets the generic S3 API only — never a MinIO-specific feature. MinIO is the
local-dev container; production is S3/Azure Blob. That is what makes swapping it
a compose change rather than a code change.

The minio SDK is sync, so calls go through asyncio.to_thread. contextvars DO
propagate into to_thread, so correlation IDs survive.
"""

import asyncio
import io
import json
from datetime import timedelta
from typing import Any

from minio import Minio

from fdi.core.serialization import canonical_json


class ArtifactStore:
    def __init__(self, client: Minio, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def ensure_bucket(self) -> None:
        exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
        if not exists:
            await asyncio.to_thread(self._client.make_bucket, self._bucket)

    async def put_json(self, key: str, obj: Any) -> str:
        body = canonical_json(obj).encode("utf-8")
        await asyncio.to_thread(
            self._client.put_object,
            self._bucket,
            key,
            io.BytesIO(body),
            len(body),
            content_type="application/json",
        )
        return key

    async def get_raw(self, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(self._bucket, key)
            try:
                data: bytes = response.read()
                return data
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_get)

    async def get_json(self, key: str) -> Any:
        return json.loads(await self.get_raw(key))

    async def presigned_get_url(self, key: str, ttl_seconds: int) -> str:
        url: str = await asyncio.to_thread(
            self._client.presigned_get_object,
            self._bucket,
            key,
            timedelta(seconds=ttl_seconds),
        )
        return url
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_artifact_store.py -v`
Expected: 5 passed

- [ ] **Step 5: Verify the layered contract still holds**

Run: `uv run --directory backend lint-imports --config .importlinter`
Expected: `Contracts: 1 kept, 0 broken.` (`platform` importing `core` is allowed; the reverse is not.)

- [ ] **Step 6: Commit**

```powershell
git add backend
git commit -m "feat(platform): async S3 artifact store with canonical JSON bodies"
```

---

## Task 13: `workers.app` — the Celery application

**Files:**
- Create: `backend/src/fdi/workers/app.py`
- Test: `backend/tests/unit/test_celery_app.py`

> ### ⚠️ Task 6's validator made the whole suite hostage to the developer's shell.
>
> Rejecting unknown `FDI_` vars is right for the app and hostile to the tests. Measured against the
> **already-committed** `test_config.py`, before the fix:
>
> ```
> FDI_FOO=1 pytest -m "not integration"        -> 1 failed, 26 passed
>   ValidationError: 1 validation error for Settings   <- names Settings, not your shell
> FDI_WORKER_CONCURRENCY=9 pytest              -> assert 9 == 2
> ```
>
> A developer with any unrelated `FDI_*` exported fails tests they never touched, and the error
> points at the wrong thing entirely. **`monkeypatch.delenv` over a known list cannot fix this** —
> the offending name is *unknown by premise*, which is the whole point of the validator. Only
> clearing the prefix works, so it lives in `tests/conftest.py` as autouse `_isolate_fdi_env`
> rather than being re-solved per test file.
>
> It also clears the `get_settings` lru_cache on **teardown**: `monkeypatch` restores the
> environment, not the object built from it, so a `Settings` cached under one test's env would
> otherwise be served to later files.
>
> Verified it does not mask what it isolates: neutering the stray-var scan in `config.py` turns
> `test_unknown_setting_is_rejected` **red**, and the suite passes under
> `FDI_FOO=1 FDI_WORKER_CONCURRENCY=9 FDI_S3_BUCKET=nope`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

REQUIRED_ENV = {
    "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
    "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
    "FDI_RESULT_BACKEND": "redis://h:6379/0",
    "FDI_S3_ENDPOINT": "h:9000",
    "FDI_S3_ACCESS_KEY": "ak",
    "FDI_S3_SECRET_KEY": "sk",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    # tests/conftest.py's autouse _isolate_fdi_env has already cleared the whole
    # FDI_ prefix; this only sets what these tests need. See the callout below --
    # that clearing is load-bearing, not tidiness.
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    from fdi.platform.config import get_settings

    get_settings.cache_clear()


def test_celery_app_config() -> None:
    from fdi.workers.app import create_celery_app

    app = create_celery_app()

    # Task loss on worker death is unacceptable for multi-minute stages.
    assert app.conf.task_acks_late is True
    # Long tasks + prefetch>1 = head-of-line blocking.
    assert app.conf.worker_prefetch_multiplier == 1
    # Bounds memory drift; prefork COW is defeated by CPython refcounting.
    assert app.conf.worker_max_tasks_per_child == 50
    assert app.conf.task_reject_on_worker_lost is True
    # Explicit: silences a CPendingDeprecationWarning in Celery 6.
    assert app.conf.broker_connection_retry_on_startup is True
    assert app.conf.broker_url == REQUIRED_ENV["FDI_BROKER_URL"]
    assert app.conf.result_backend == REQUIRED_ENV["FDI_RESULT_BACKEND"]
    # Concurrency must come from config, not a CLI flag -- otherwise
    # FDI_WORKER_CONCURRENCY looks configurable while being silently overridden.
    assert app.conf.worker_concurrency == 2


def test_worker_concurrency_honours_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDI_WORKER_CONCURRENCY", "7")
    from fdi.platform.config import get_settings

    get_settings.cache_clear()
    from fdi.workers.app import create_celery_app

    assert create_celery_app().conf.worker_concurrency == 7
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_celery_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.workers.app'`

- [ ] **Step 3: Implement**

`backend/src/fdi/workers/app.py`:

```python
"""The Celery application.

Prefork only. gevent/eventlet cannot parallelize CPU-bound stages (monkey-
patching makes only I/O yield), starve the broker heartbeat into redelivery,
and per Celery's docs cannot enforce time limits on blocking tasks. The pool is
set on the command line so it is visible in `docker compose ps`.
"""

from celery import Celery

from fdi.platform.config import get_settings


def create_celery_app() -> Celery:
    settings = get_settings()
    app = Celery("fdi")
    app.conf.update(
        broker_url=settings.broker_url,
        result_backend=settings.result_backend,
        # Single source of truth for concurrency. The compose worker command
        # deliberately carries NO --concurrency flag: a CLI flag silently wins over
        # config, which would make FDI_WORKER_CONCURRENCY look configurable while
        # doing nothing.
        worker_concurrency=settings.worker_concurrency,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=50,
        broker_connection_retry_on_startup=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
    )
    # NO force=True. See the callout below -- it makes this module unimportable.
    app.autodiscover_tasks(["fdi.workers"])
    return app


app = create_celery_app()
```

> `app = create_celery_app()` at module level is required — `celery -A fdi.workers.app` needs it.
> This is safe: `Celery()` opens no sockets and creates no event loop. It is *connection pools and
> event loops* that must never exist at import (Global Constraint 3), not configuration objects.

> ### ⛔ `force=True` (as this plan originally specified) makes the worker unbootable.
>
> `force=True` discovers **immediately** rather than deferring, so `import fdi.workers.app` imports
> `fdi.workers.tasks` **while `app.py` is still executing** — and Task 14's `tasks.py` opens with
> `from fdi.workers.app import app` to get the decorator. Measured:
>
> ```
> ImportError: cannot import name 'app' from partially initialized module
> 'fdi.workers.app' (most likely due to a circular import)
> ```
>
> Not a silent skip, not a half-built app: `celery -A fdi.workers.app worker` **cannot boot**.
> **Task 14 was dead on arrival as written.**
>
> It is never swallowed, on two independent counts (both measured, not inferred):
> 1. `find_related_module` (`loaders/base.py:271`) catches **`ModuleNotFoundError`**. A circular
>    import raises plain **`ImportError`** — the *parent* class — so the handler never matches.
> 2. Even if it did, that handler only returns `None` when `e.name == module_name`. Here
>    `e.name == 'fdi.workers.app'`, not `'fdi.workers.tasks'` → it re-raises.
>
> **What `force` is actually for:** forcing discovery *now* instead of deferring it onto the
> `import_modules` signal — for callers needing `app.tasks` populated without worker bootstrap. It
> defaults to `False` *precisely because* the app module is normally still executing.
>
> **Deferring is not "never", which is the obvious worry** — verified rather than assumed: after
> `loader.import_default_modules()` (what worker boot calls), `'fdi.workers.tasks' in sys.modules`
> is `True` and `app.tasks` contains `fdi.ping_job`. So this does not trade a loud `ImportError` for
> a silent `Received unregistered task`. `test_task_discovery_is_deferred_not_forced` guards it, and
> was proven to fail when `force=True` is reintroduced.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_celery_app.py -v`
Expected: 3 passed — two specified here, plus the regression guard against `force=True`.

- [ ] **Step 5: Commit**

```powershell
git add backend
git commit -m "feat(workers): celery app with acks-late, prefetch=1 and child recycling"
```

---

## Task 14: `workers.tasks` — the sync/async seam

The most important twelve lines in SP-1. Every stage in SP-2…SP-18 follows this shape.

> ### ⛔ `if correlation_id: bind(...)` — as this plan originally specified — LEAKS IDs BETWEEN JOBS.
>
> Prefork reuses the worker process across tasks, and `bind_correlation_id` writes a contextvar that
> outlives the task. Job A binds `cid-1`; job B arrives with no ID, the `if` is false, **nothing
> rebinds, and B's log lines carry A's correlation ID.** Reproduced through Celery's own
> `build_tracer`/`trace_task` path, not a bare call:
>
> ```
> body-sees     -> 'cid-A'    job A, dispatched with correlation_id="cid-A"
> between-tasks -> 'cid-A'    nothing ever clears it
> body-sees     -> 'cid-A'    job B, dispatched with NO id -- reads A's
> ```
>
> **The framework saves nobody.** `celery/app/trace.py`, `celery/concurrency/prefork.py` and
> `billiard/pool.py` contain **zero** references to `contextvars` or `copy_context` — verified by
> grep, not assumed. Combined with `worker_max_tasks_per_child=50`, up to **50 jobs share one
> Context**. On a platform whose entire value is provenance, log lines attributed to the wrong
> document are not cosmetic.
>
> **The fix needs both halves; they close different holes.** Unconditional bind (minting when
> absent) stops *this* task inheriting a predecessor's ID. `finally: clear` stops *this* task's ID
> becoming the ambient default for the next one — including future SP-2…SP-18 stages whose author
> forgets to bind at all. Verified by mutation: reverting to the plan's version turns the leak tests
> red (`3 failed`); the shipped version is green (`7 passed`).
>
> **Known limit, not papered over:** minting can hide a dispatch bug. If the API is meant to always
> pass an ID, a minted one silently means it didn't. Minting was still chosen — the signature permits
> `correlation_id=""`, and unattributable log lines are the worse failure — but nothing currently
> detects that case. Worth a warning when SP-2 wires real dispatch.

**Files:**
- Create: `backend/src/fdi/workers/tasks.py`
- Test: `backend/tests/integration/test_ping_task.py`

- [ ] **Step 1: Write the failing test**

```python
import uuid

import pytest

from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.models import JobStatus
from fdi.platform.db.repository import JobRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine):
    return make_session_factory(migrated_engine)


async def test_ping_job_writes_artifact_and_marks_done(
    session_factory, minio_client, postgres_url, monkeypatch
) -> None:
    from fdi.workers.tasks import _ping_job

    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id="cid-ping")
        await s.commit()

    key = await _ping_job(job_id, store_bucket="test-artifacts", client=minio_client,
                          db_url=postgres_url)

    assert key == f"jobs/{job_id}/ping.json"
    async with session_factory() as s:
        job = await JobRepository(s).get(job_id)
    assert job is not None
    assert job.status == JobStatus.DONE.value
    assert job.artifact_key == key


async def test_ping_artifact_body_is_deterministic(
    session_factory, minio_client, postgres_url
) -> None:
    """Done criterion 10. The body must not contain the job id or a timestamp —
    those belong in the Postgres row."""
    from fdi.platform.storage.artifact_store import ArtifactStore
    from fdi.workers.tasks import _ping_job

    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for job_id in ids:
        async with session_factory() as s:
            await JobRepository(s).create(job_id, correlation_id="cid")
            await s.commit()
        await _ping_job(job_id, store_bucket="test-artifacts", client=minio_client,
                        db_url=postgres_url)

    store = ArtifactStore(minio_client, bucket="test-artifacts")
    bodies = [await store.get_raw(f"jobs/{i}/ping.json") for i in ids]
    assert bodies[0] == bodies[1], "artifact body must be a pure function of input"


def test_ping_job_task_is_not_a_coroutine_function() -> None:
    """Guarded by the AST test too, but pin the actual object: Celery would
    report SUCCESS on a coroutine while doing nothing."""
    import inspect

    from fdi.workers.tasks import ping_job

    assert not inspect.iscoroutinefunction(ping_job.run)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_ping_task.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.workers.tasks'`

- [ ] **Step 3: Implement**

`backend/src/fdi/workers/tasks.py`:

```python
"""Tasks. THE canonical shape for every stage in SP-2..SP-18.

Three rules, each closing a verified failure:

1. The task is `def`, never `async def`. Celery has no native async support;
   `async def` returns an un-awaited coroutine and reports SUCCESS.
2. The correlation ID is bound in the SYNC wrapper, BEFORE asyncio.run().
   contextvars is copy-in / no-copy-out: bind it inside the loop and the caller
   never sees it. Bind UNCONDITIONALLY and clear in `finally` -- prefork reuses
   the process, so a conditional bind leaks the previous job's ID into this one.
   Celery handles contextvars nowhere; verified by grep across trace.py,
   prefork.py and billiard/pool.py.
3. The engine is created INSIDE the coroutine and disposed in `finally`. A pool
   built at import is inherited across fork() and corrupts silently.
"""

import asyncio
from typing import Any

import structlog
from minio import Minio

from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.db.repository import JobRepository
from fdi.platform.obs.logging import bind_correlation_id
from fdi.platform.storage.artifact_store import ArtifactStore
from fdi.workers.app import app

log = structlog.get_logger(__name__)

PING_PAYLOAD: dict[str, Any] = {"kind": "ping", "schema_version": 1}


def _make_client(settings: Any) -> Minio:
    return Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )


# `type: ignore[untyped-decorator]` is NOT optional, and EVERY stage in SP-2..SP-18
# will need it. Celery 5.6.3 ships no `py.typed`, so under `ignore_missing_imports`
# the package resolves to Any, making `app.task(...)` an untyped decorator -- and
# `--strict` implies `disallow_untyped_decorators`. Without it the gate fails:
#   error: Untyped decorator makes function "ping_job" untyped [untyped-decorator]
# Scoped to the line on purpose: a module-wide override would also wave through a
# genuinely untyped decorator added later. `--strict` implies
# `warn_unused_ignores`, so if `celery-types` is ever added, this comment fails the
# gate rather than rotting silently.
@app.task(name="fdi.ping_job", bind=False)  # type: ignore[untyped-decorator]
def ping_job(job_id: str, correlation_id: str = "") -> str:
    """Sync wrapper. Binds the correlation ID BEFORE the loop, then runs once.

    Unconditional bind + `finally: clear`. `if correlation_id:` is a LEAK, not an
    optimisation -- this process has already run other jobs and their IDs are
    still bound. See the callout below.
    """
    bind_correlation_id(correlation_id or new_correlation_id())
    try:
        return asyncio.run(_ping_job(job_id))
    finally:
        clear_correlation_id()


async def _ping_job(
    job_id: str,
    *,
    store_bucket: str | None = None,
    client: Minio | None = None,
    db_url: str | None = None,
) -> str:
    """All of this task's I/O, in one coroutine, on one event loop.

    The keyword args exist so tests can inject container-backed dependencies;
    production passes none and reads Settings.
    """
    settings = get_settings()
    engine = make_engine(db_url or settings.database_url)
    try:
        session_factory = make_session_factory(engine)
        store = ArtifactStore(
            client or _make_client(settings),
            bucket=store_bucket or settings.s3_bucket,
        )

        async with session_factory() as session:
            await JobRepository(session).mark_running(job_id)
            await session.commit()
        log.info("ping_job.running", job_id=job_id)

        await store.ensure_bucket()
        # NOTE: the body carries no job id and no timestamp — Global Constraint 6.
        key = await store.put_json(f"jobs/{job_id}/ping.json", PING_PAYLOAD)

        async with session_factory() as session:
            await JobRepository(session).mark_done(job_id, artifact_key=key)
            await session.commit()
        log.info("ping_job.done", job_id=job_id, artifact_key=key)
        return key
    except Exception as exc:
        # Log the CAUSE first: everything after this point can fail, and if it
        # does, this line is the only surviving record of why the job died.
        log.exception("ping_job.failed", job_id=job_id)
        try:
            # A fresh factory, not `session_factory`: that name is bound inside the
            # `try` and would be unbound here had its own line raised.
            async with make_session_factory(engine)() as session:
                await JobRepository(session).mark_failed(job_id, error=repr(exc))
                await session.commit()
        except Exception:
            # Swallowed ON PURPOSE -- see the callout below. Letting this escape
            # replaces `exc` with whatever broke while recording it, and Celery
            # stores the MASK as the task's error. Logged, never lost.
            log.exception("ping_job.mark_failed_failed", job_id=job_id)
        # Bare `raise` re-raises `exc`, NOT the swallowed one: the inner handler has
        # exited, so the exception being handled is the outer one again. Verified.
        raise
    finally:
        await engine.dispose()
```

> **Why the bookkeeping is best-effort — the original `except` destroyed the real error.**
> If `mark_failed` itself raises, its exception *replaces* `exc`, and this is worse than the usual
> "`__context__` still has it" consolation: this app sets `result_serializer="json"`, so Celery's
> `prepare_exception` emits only `{exc_type, exc_message, exc_module}` — **no chain**. A client's
> `result.get()` re-raises the mask with `__context__` of **`None`**. The cause survives solely in
> the worker's traceback string: a human can grep it back, a program cannot.
>
> The nonexistent-job case is the nastiest shape: `mark_running` raises `KeyError`, the handler calls
> `mark_failed`, which raises `KeyError` **with an identical message**. The substitution is invisible
> to every obvious assertion — only `__context__` distinguishes them, and that is exactly what JSON
> drops.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_ping_task.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the AST guard against real task code**

Run: `uv run --directory backend pytest tests/unit/test_no_async_celery_tasks.py -v`
Expected: PASS — now meaningful, since a real `@app.task` exists.

- [ ] **Step 6: Commit**

```powershell
git add backend
git commit -m "feat(workers): ping_job — the canonical sync-wrapper/async-impl stage seam"
```

---

## Task 15: `api.errors` — RFC 9457 problem details

**Files:**
- Create: `backend/src/fdi/api/errors.py`
- Test: `backend/tests/unit/test_api_errors.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from fdi.api.errors import install_error_handlers


@pytest.fixture
def client() -> AsyncClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=404, detail="job not found")

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("unexpected")

    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                       base_url="http://test")


async def test_http_exception_is_problem_json(client: AsyncClient) -> None:
    r = await client.get("/boom")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 404
    assert body["detail"] == "job not found"
    assert body["title"] == "Not Found"


async def test_unhandled_exception_is_problem_json_without_leaking(
    client: AsyncClient,
) -> None:
    r = await client.get("/crash")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 500
    assert "unexpected" not in body["detail"], "internals must not leak to clients"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_api_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.api.errors'`

- [ ] **Step 3: Implement**

`backend/src/fdi/api/errors.py`:

```python
"""RFC 9457 problem+json error responses."""

import http

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

log = structlog.get_logger(__name__)

CONTENT_TYPE = "application/problem+json"


def _problem(status: int, detail: str, title: str | None = None) -> JSONResponse:
    if title is None:
        try:
            title = http.HTTPStatus(status).phrase
        except ValueError:
            title = "Error"
    return JSONResponse(
        status_code=status,
        media_type=CONTENT_TYPE,
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


async def _unhandled(_: Request, exc: Exception) -> Response:
    # The response must NOT depend on logging succeeding. See callout below.
    # Swallowing is safe rather than lossy: ServerErrorMiddleware re-raises `exc`
    # after this response is sent, so the server still logs the original cause.
    with contextlib.suppress(Exception):
        log.exception("api.unhandled_exception", error=repr(exc))
    return _problem(500, "An internal error occurred.")


def install_error_handlers(app: FastAPI) -> None:
    """Route every error class this app can emit through problem+json.

    Keyed on `starlette.exceptions.HTTPException`, which ALSO covers FastAPI's
    subclass (the MRO walk finds the parent). Keying on FastAPI's -- as this plan
    originally did -- leaves the router's own 404 and 405 to FastAPI's default
    handler. See the callout below.
    """
    app.add_exception_handler(StarletteHTTPException, _http_exc)
    app.add_exception_handler(RequestValidationError, _validation_exc)
    app.add_exception_handler(Exception, _unhandled)
```

> ### ⛔ The plan's version leaked four ways. The whole point of this module is that *every* error is problem+json.
>
> Measured against the plan's code verbatim, not reasoned:
>
> | Error path | Plan's version | Fixed |
> |---|---|---|
> | `fastapi.HTTPException` | `application/problem+json` | unchanged |
> | unhandled `Exception` | **`<none>`, body `b''`** | `application/problem+json` |
> | unmatched route (404) | `application/json` | `application/problem+json` |
> | wrong method (405) | `application/json` | `application/problem+json` |
> | validation error (422) | `application/json` | `application/problem+json` |
> | 401 | problem+json, **`WWW-Authenticate` dropped** | preserved |
> | 304 | problem+json **+ a body** | bodiless (RFC 9110) |
>
> **404/405/422 are one mechanism.** `_lookup_exception_handler` walks `type(exc).__mro__`, so a
> handler keyed on a **subclass never catches the parent**. Starlette raises *its* `HTTPException`
> for unmatched routes; the plan registered FastAPI's subclass. This is worse than falling through:
> FastAPI **pre-registers its own plain-JSON handler** against the parent and against
> `RequestValidationError`, so those paths answer *confidently, in the wrong media type*. Fixed by
> keying on `StarletteHTTPException`.
>
> **The bodyless 500 is the serious one — and its test was green.** With `structlog` unconfigured,
> its default `ConsoleRenderer` draws the traceback through **rich**, whose box glyphs are
> unencodable on cp1252 stdout. The `UnicodeEncodeError` **escapes the error handler**. Driven as
> uvicorn drives it:
>
> ```
> app raised: UnicodeEncodeError: 'charmap' codec can't encode characters in position 151-152
> messages the client would receive: 0
> ```
>
> The caller gets nothing at all — from the one module whose entire job is to work when everything
> else has broken. **Fourth sighting of this cp1252 failure class** (after the Imports gate, and two
> probe scripts). pytest hides it: its capture replaces `sys.stdout` with a UTF-8 `CaptureIO`, so
> **the encoding that breaks production is never the one under test**.
>
> **`raise_app_exceptions=False` is doing more than it looks.** `ServerErrorMiddleware` re-raises
> *unconditionally* after sending (*"We always continue to raise the exception"*), so the flag is
> required, not stylistic. But it also makes httpx **fabricate `status_code = 500` with empty
> headers when the app sends nothing** — so `assert r.status_code == 500` over that transport is not
> evidence this module produced anything. That is exactly how the bodyless 500 stayed invisible; the
> plan's test survives only because its *content-type* assertion happens to be load-bearing. The
> shipped tests drive raw ASGI for this reason.
>
> **One fix would have introduced a bug:** 405 was previously served by FastAPI's default, which
> *preserves* headers. Routing it through the plan's handler drops the RFC-mandated `Allow`. Headers
> now pass through (minus content-type/content-length).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_api_errors.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```powershell
git add backend
git commit -m "feat(api): RFC 9457 problem+json error handlers"
```

---

## Task 16: `api.app` and `api.routes` — the HTTP surface

> **Carry these three in from Task 15, all measured:**
> 1. **`configure_logging()` must run before anything logs.** Unconfigured structlog renders through
>    rich, which is unencodable on cp1252 stdout — that is how Task 15's 500 handler ended up
>    returning a bodyless response. `api.errors` is now immune (its log call is suppressed); every
>    other call site, including `routes.py`, is **not**, and nothing detects it. Call
>    `configure_logging()` in lifespan *first*.
> 2. **`instance` (RFC 9457 §3.1) belongs here.** The useful value is `urn:uuid:<correlation_id>`,
>    which needs per-request binding — so it could not be done in Task 15. Today's opaque 500 body
>    gives the caller no handle to quote back at support. Bind the ID in middleware, then add it.
> 3. **Check for double-logged tracebacks.** uvicorn logs its own traceback for a re-raised 500
>    alongside ours (inferred from its source, unverified) — which would partly undo Task 7's
>    no-double-printing work. Verify against a real server.

**Files:**
- Create: `backend/src/fdi/api/app.py`, `backend/src/fdi/api/routes.py`, `backend/src/fdi/api/schemas.py`, `backend/src/fdi/api/deps.py`
- Test: `backend/tests/integration/test_api.py`

> ### ⛔ Four defects found here. Three are in the tests, which is the pattern by now.
>
> 1. **`X-Request-ID` let a client choose the response code.** It flows unvalidated into a
>    `String(36)` column, and Task 11 measured that PG raises `StringDataRightTruncation` past the
>    width — so **a 37-character header is a 500**, and a 10 KB one likewise. Log injection, by
>    contrast, is **not** a hazard: structlog's `JSONRenderer` escapes correctly (measured with
>    `'x","level":"critical","evil":"'` — one line, `level` still `info`). Fixed by validating to
>    `[A-Za-z0-9._~-]{1,36}` → **400**. Deliberately *not* UUID-only: that closes every hazard by
>    construction but 400s every request behind an ALB/nginx stamping its own format — a worse
>    failure than the bug. Truncate and ignore-and-mint both silently break the one property the
>    header exists to provide: that the caller's ID and ours are the same string.
> 2. **`/ready`'s `broker` field was the literal `True`.** Decorative, and it passed because the
>    plan's test never asserted it — the other two passed because the containers were up, which is
>    equally true of `return True`. Now a real kombu probe; each backend is proven by killing *that
>    backend only* (→ 503, that field `False`, the others `True`).
> 3. **The plan's fixture never ran lifespan.** `ASGITransport` sends only an `http` scope, so
>    `app.state.session_factory` was never set — Step 7's "5 passed" was unreachable; 4 of 5 died on
>    `KeyError`.
> 4. **`len(job_id) == 36` is incidental** — `"a"*36` passes it. 36 *is* load-bearing, but at the DB.
>
> **Carry-forward 3 answered: uvicorn DOES double-log**, verified against a real uvicorn on a real
> socket. Under the planned `CMD ["uvicorn", ...]` its default dictConfig gives the `uvicorn` logger
> a stderr handler with `propagate=False`, so its traceback prints raw *beside* our JSON —
> reintroducing exactly what `ProcessorFormatter` was chosen to prevent in Task 7, and falsifying
> `configure_logging`'s own docstring. Fixed at the source.
>
> **A test-harness trap worth knowing generally:** pytest uses a *different* `CaptureIO` for the
> setup phase than the call phase. `configure_logging()` in a fixture binds a handler to a stream the
> test body then closes — every record dropped with `ValueError: I/O operation on closed file`. Any
> log assertion wired that way silently reads `""` **and passes**.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    monkeypatch.setenv("FDI_DATABASE_URL", postgres_url)
    monkeypatch.setenv("FDI_BROKER_URL", amqp_url)
    monkeypatch.setenv("FDI_RESULT_BACKEND", redis_url)
    monkeypatch.setenv("FDI_S3_ENDPOINT", minio_endpoint)
    monkeypatch.setenv("FDI_S3_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("FDI_S3_SECRET_KEY", "minioadmin")
    from fdi.platform.config import get_settings

    get_settings.cache_clear()

    from fdi.api.app import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_health_is_liveness_only(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_checks_backends(client: AsyncClient, _alembic_applied) -> None:
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["postgres"] is True
    assert body["s3"] is True


async def test_post_ping_job_returns_202_with_job_id(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/ping-job")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert len(body["job_id"]) == 36
    assert len(body["correlation_id"]) == 36


async def test_post_ping_job_honours_inbound_request_id(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/ping-job", headers={"X-Request-ID": "caller-supplied-id"})
    assert r.status_code == 202
    assert r.json()["correlation_id"] == "caller-supplied-id"


async def test_get_unknown_job_is_404_problem_json(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.api.app'`

- [ ] **Step 3: Write `backend/src/fdi/api/schemas.py`**

```python
from pydantic import BaseModel


class JobAccepted(BaseModel):
    job_id: str
    correlation_id: str
    status: str


class JobView(BaseModel):
    job_id: str
    status: str
    artifact_key: str | None = None
    artifact_url: str | None = None
    error: str | None = None


class ReadyView(BaseModel):
    postgres: bool
    s3: bool
    broker: bool
```

- [ ] **Step 4: Write `backend/src/fdi/api/deps.py`**

```python
"""Request-scoped dependencies. The engine is owned by lifespan, never by a module."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.platform.storage.artifact_store import ArtifactStore


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


def get_store(request: Request) -> ArtifactStore:
    store: ArtifactStore = request.app.state.artifact_store
    return store


def get_minio(request: Request) -> Minio:
    client: Minio = request.app.state.minio
    return client


SessionDep = Annotated[AsyncSession, Depends(get_session)]
StoreDep = Annotated[ArtifactStore, Depends(get_store)]
```

- [ ] **Step 5: Write `backend/src/fdi/api/routes.py`**

```python
"""HTTP surface."""

import uuid

import sqlalchemy
import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status

from fdi.api.deps import SessionDep, StoreDep
from fdi.api.schemas import JobAccepted, JobView, ReadyView
from fdi.platform.config import get_settings
from fdi.platform.db.repository import JobRepository
from fdi.platform.obs.logging import bind_correlation_id, new_correlation_id

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Must never touch a backend, or a DB blip restarts the API."""
    return {"status": "ok"}


@router.get("/ready", response_model=ReadyView)
async def ready(session: SessionDep, store: StoreDep, response: Response) -> ReadyView:
    """Readiness: can this instance actually serve traffic?"""
    try:
        await session.execute(sqlalchemy.text("SELECT 1"))
        postgres_ok = True
    except Exception:
        log.exception("ready.postgres_failed")
        postgres_ok = False

    try:
        await store.ensure_bucket()
        s3_ok = True
    except Exception:
        log.exception("ready.s3_failed")
        s3_ok = False

    view = ReadyView(postgres=postgres_ok, s3=s3_ok, broker=True)
    if not (postgres_ok and s3_ok):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return view


@router.post("/ping-job", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
async def create_ping_job(request: Request, session: SessionDep) -> JobAccepted:
    """Mint a correlation id, persist the job, publish it. The worker picks it up."""
    # NOT request.headers.get("X-Request-ID") directly -- that is the unvalidated
    # client string that turns a 37-char header into a varchar(36) 500, because
    # correlation_id is String(36) and PG raises StringDataRightTruncation past it.
    # Task 16's CorrelationIdDep validates ([A-Za-z0-9._~-]{1,36} or 400) and binds.
    job_id = str(uuid.uuid4())
    await JobRepository(session).create(job_id, correlation_id=correlation_id)
    await session.commit()

    from fdi.workers.tasks import ping_job

    ping_job.apply_async(args=[job_id], kwargs={"correlation_id": correlation_id})
    log.info("ping_job.queued", job_id=job_id)

    return JobAccepted(job_id=job_id, correlation_id=correlation_id, status="queued")


@router.get("/jobs/{job_id}", response_model=JobView)
async def get_job(job_id: str, session: SessionDep, store: StoreDep) -> JobView:
    job = await JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    artifact_url = None
    if job.artifact_key:
        artifact_url = await store.presigned_get_url(
            job.artifact_key, ttl_seconds=get_settings().artifact_url_ttl_seconds
        )

    return JobView(
        job_id=job.id,
        status=job.status,
        artifact_key=job.artifact_key,
        artifact_url=artifact_url,
        error=job.error,
    )
```

> **Why `/health` never touches Postgres:** liveness answers "should this process be
> restarted?" A DB outage is not a reason to kill the API — the orchestrator would
> restart-loop every replica while the DB recovers. Readiness answers "send it traffic?",
> which is the question a DB outage actually changes.

- [ ] **Step 6: Write `backend/src/fdi/api/app.py`**

```python
"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from minio import Minio

from fdi.api.errors import install_error_handlers
from fdi.api.routes import router
from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.obs.logging import configure_logging
from fdi.platform.storage.artifact_store import ArtifactStore


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Owns the engine. Created here — inside the running loop — not at import."""
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = make_engine(settings.database_url)
    client = Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )

    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.minio = client
    app.state.artifact_store = ArtifactStore(client, bucket=settings.s3_bucket)
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Financial Document Intelligence", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
```

- [ ] **Step 7: Run to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -v`
Expected: 5 passed

- [ ] **Step 8: Verify all gates**

Run: `uv run --directory backend ruff check .`
Run: `uv run --directory backend mypy --strict src`
Run: `uv run --directory backend lint-imports --config .importlinter`
Expected: all clean. **`api` importing `workers.tasks` is allowed** — they are independent siblings on the same layer, so `|` forbids it. **If `lint-imports` fails here, that is the contract working.** Fix by moving the publish call behind a `pipeline`-layer function rather than weakening the contract.

- [ ] **Step 9: Commit**

```powershell
git add backend
git commit -m "feat(api): health, ready, ping-job and job views"
```

---

## Task 17: Resolve the api→workers layering violation

Task 16 Step 8 will fail the contract: `api/routes.py` imports `fdi.workers.tasks`, but `api | workers` are independent siblings. **This is the contract doing its job** — the fix is a seam, not an exemption.

**Files:**
- Create: `backend/src/fdi/pipeline/dispatch.py`
- Modify: `backend/src/fdi/api/routes.py`
- Test: `backend/tests/unit/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import MagicMock

from fdi.pipeline.dispatch import JobDispatcher


def test_dispatch_publishes_with_correlation_id() -> None:
    sender = MagicMock()
    JobDispatcher(sender).dispatch_ping(job_id="j-1", correlation_id="c-1")
    sender.apply_async.assert_called_once_with(
        args=["j-1"], kwargs={"correlation_id": "c-1"}
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_dispatch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.pipeline.dispatch'`

- [ ] **Step 3: Implement**

`backend/src/fdi/pipeline/dispatch.py`:

```python
"""Publishing seam.

`api` and `workers` are independent siblings — neither may import the other, so
the API cannot import the task object directly. `pipeline` sits below both and
owns dispatch. The API depends on this; the concrete task is injected at wiring
time, which also makes dispatch trivially testable with no broker.
"""

from typing import Protocol


class TaskSender(Protocol):
    def apply_async(self, args: list[str], kwargs: dict[str, str]) -> object: ...


class JobDispatcher:
    def __init__(self, ping_task: TaskSender) -> None:
        self._ping_task = ping_task

    def dispatch_ping(self, job_id: str, correlation_id: str) -> None:
        self._ping_task.apply_async(args=[job_id], kwargs={"correlation_id": correlation_id})
```

- [ ] **Step 4: Wire it in `backend/src/fdi/api/app.py` lifespan**

Add to `lifespan`, after the `artifact_store` line:

```python
    from fdi.pipeline.dispatch import JobDispatcher
    from fdi.workers.tasks import ping_job

    app.state.dispatcher = JobDispatcher(ping_job)
```

> This import lives in `api/app.py`, which is the composition root. import-linter still flags
> `api -> workers`. Add the wiring module to the contract's ignore list — an explicit, reviewed
> exception at the composition root only, not a general weakening:

Append to `backend/.importlinter`:

```ini
[importlinter:contract:fdi-layers]
...
exhaustive = true
ignore_imports =
    fdi.api.app -> fdi.workers.tasks
```

> ### ⚠️ That exemption must name ONE EDGE. A wildcard here is a fake gate, one character away.
>
> Measured, with Task 16's real violation (`fdi.api.routes -> fdi.workers.tasks`) planted in the tree:
>
> | `ignore_imports` spelling | Result |
> |---|---|
> | `fdi.api.app -> fdi.workers.tasks` (correct) | **`0 kept, 1 broken`** — names line 129 |
> | `fdi.api.* -> fdi.workers.*` (wildcard) | **`1 kept, 0 broken`** — green, violation in tree |
>
> The wildcard reads like the same idea and is an `api -> workers` amnesty. This project has already
> found five gates that enforced nothing while looking green; an exemption added to make a gate pass
> is exactly how the sixth gets made.
>
> Proven narrow, with the exemption live: re-introducing `routes -> workers.tasks` → **broken**;
> the reverse `workers -> api` → **broken**; a new undeclared `fdi.utils` → **broken** (`exhaustive`
> survives). It is also self-cleaning — delete the wiring it excuses and lint-imports fails with
> *"No matches for ignored import"*, so a stale exemption cannot rot silently.

- [ ] **Step 5: Add the dependency to `backend/src/fdi/api/deps.py`**

```python
from fdi.pipeline.dispatch import JobDispatcher


def get_dispatcher(request: Request) -> JobDispatcher:
    dispatcher: JobDispatcher = request.app.state.dispatcher
    return dispatcher


DispatcherDep = Annotated[JobDispatcher, Depends(get_dispatcher)]
```

- [ ] **Step 6: Update `create_ping_job` in `backend/src/fdi/api/routes.py`**

Replace the body's import + publish with the injected dispatcher:

```python
@router.post("/ping-job", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
async def create_ping_job(
    correlation_id: CorrelationIdDep, session: SessionDep, dispatcher: DispatcherDep
) -> JobAccepted:
    # NOT request.headers.get("X-Request-ID") directly -- that is the unvalidated
    # client string that turns a 37-char header into a varchar(36) 500, because
    # correlation_id is String(36) and PG raises StringDataRightTruncation past it.
    # Task 16's CorrelationIdDep validates ([A-Za-z0-9._~-]{1,36} or 400) and binds.
    job_id = str(uuid.uuid4())
    await JobRepository(session).create(job_id, correlation_id=correlation_id)
    await session.commit()

    dispatcher.dispatch_ping(job_id=job_id, correlation_id=correlation_id)
    log.info("ping_job.queued", job_id=job_id)

    return JobAccepted(job_id=job_id, correlation_id=correlation_id, status="queued")
```

Add `DispatcherDep` to the imports from `fdi.api.deps` and remove the now-unused inline import.

- [ ] **Step 7: Run everything**

Run: `uv run --directory backend pytest -q`
Run: `uv run --directory backend lint-imports --config .importlinter`
Expected: all pass; `Contracts: 1 kept, 0 broken.`

- [ ] **Step 8: Commit**

```powershell
git add backend
git commit -m "refactor(pipeline): JobDispatcher seam so api and workers stay independent"
```

---

## Task 18: The backend image

**Files:**
- Create: `backend/Dockerfile`, `backend/.dockerignore`

- [ ] **Step 1: Write `backend/.dockerignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
tests/
```

- [ ] **Step 2: Write `backend/Dockerfile`**

Built from the **repo root** so the context can see `backend/`.

```dockerfile
# syntax=docker/dockerfile:1
########################  builder  ########################
FROM python:3.13-slim-trixie AS builder

# Distroless uv image — the ONLY correct kind for COPY --from.
# Pinned, not :latest — the uv guide calls pinning best practice for reproducibility.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Phase 1: dependencies only. Cached until uv.lock / pyproject.toml change.
# --locked fails the build on a stale lockfile (--frozen is for workspaces).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=backend/uv.lock,target=uv.lock \
    --mount=type=bind,source=backend/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --no-editable

# Phase 2: the project itself.
COPY backend/ /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

########################  runtime  ########################
FROM python:3.13-slim-trixie

# NO curl, NO apt layer. The compose healthcheck (Task 20) hits /health with the
# venv's own Python, saving curl's ~13.5 MB apt layer and dropping any reliance on
# curl -- which a stale CURL_CA_BUNDLE breaks host-side on this machine.
RUN groupadd --system fdi && useradd --system --gid fdi --home-dir /app fdi

COPY --from=builder --chown=fdi:fdi /app/.venv /app/.venv
COPY --from=builder --chown=fdi:fdi /app/alembic /app/alembic
COPY --from=builder --chown=fdi:fdi /app/alembic.ini /app/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER fdi
EXPOSE 8000

CMD ["uvicorn", "fdi.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Build it**

Run: `docker build -f backend/Dockerfile -t fdi-backend:dev .`
Expected: build succeeds.

- [ ] **Step 4: Verify both entrypoints resolve**

Run (the six `FDI_` vars are REQUIRED — see below):
```
docker run --rm \
  -e FDI_DATABASE_URL=postgresql+asyncpg://u:p@h:5432/db \
  -e FDI_BROKER_URL=amqp://guest:guest@h:5672/%2F \
  -e FDI_RESULT_BACKEND=redis://h:6379/0 \
  -e FDI_S3_ENDPOINT=h:9000 -e FDI_S3_ACCESS_KEY=ak -e FDI_S3_SECRET_KEY=sk \
  fdi-backend:dev python -c "import fdi.api.app, fdi.workers.app; print('ok')"
```
Expected: `ok`

> **A BARE `docker run` (no env) cannot print `ok`, and that is correct behaviour, not a build fault.**
> `import fdi.workers.app` runs `app = create_celery_app()` in the module body, which reads all six
> settings — so a bare run raises `pydantic ValidationError: 6 validation errors for Settings`,
> naming each missing field. Loud and immediate, exactly as designed (Task 13). The plan previously
> printed `Expected: ok` against the bare command, which is unreachable. `celery -A fdi.workers.app
> worker --help` (needs no broker but *does* trigger autodiscover) is the stronger entrypoint check
> — it would have caught the Task 13 `force=True` circular import, which a plain `import` also does.

Run: `docker run --rm fdi-backend:dev celery --version`
Expected: `5.6.3`

- [ ] **Step 5: Commit**

```powershell
git add backend/Dockerfile backend/.dockerignore
git commit -m "build(backend): one image, two entrypoints, uv --locked with cache mounts"
```

---

## Task 19: RabbitMQ configuration

**Files:**
- Create: `docker/rabbitmq.conf`

- [ ] **Step 1: Write `docker/rabbitmq.conf`**

```ini
# consumer_timeout: milliseconds. Default 1800000 (30 min).
#
# Currently INERT, kept deliberately. RabbitMQ 4.3 stopped evaluating consumer
# timeouts for classic queues -- it is now quorum-queue-only -- and Celery declares
# classic queues by default. So the 30-minute-requeue trap does not exist on this
# stack today.
#
# It costs nothing and becomes load-bearing the moment quorum queues are adopted
# (the HA path, since 4.x dropped classic mirrored queues). A 500-page annual
# report can exceed 30 minutes of CPU; with task_acks_late that would mean the
# channel is killed, the message requeued, and the CPU burned again -- forever.
#
# Enforcement is evaluated at one-minute intervals; sub-five-minute values are
# discouraged. 3 hours:
consumer_timeout = 10800000

loopback_users.guest = false
```

- [ ] **Step 2: Commit**

```powershell
git add docker/rabbitmq.conf
git commit -m "build(rabbitmq): consumer_timeout headroom for multi-minute stages"
```

---

## Task 20: Compose with profiles

**Files:**
- Create: `docker/compose.yml`, `.env.example`

> ### ⛔ Five defects, three of which each independently prevent the round-trip. All fixed; all found only by running the stack.
>
> This task is the payoff of SP-1, and none of these five show up in any unit test — they are what
> "bring it up and prove the round-trip" exists to catch. Verified end-to-end against a live `core`
> stack (round-trip `done` in ~2s, correlation ID `586a0d3d…` in **both** api and worker logs,
> artifact URL fetched from the host → **HTTP 200**, total RAM ~460 MiB).
>
> 1. **The presigned `artifact_url` was unreachable from the host** — Task 12 predicted this exactly.
>    Under compose the API's `FDI_S3_ENDPOINT=minio:9000`, so `presigned_get_object` embedded
>    `http://minio:9000/…` and a host GET died with `getaddrinfo failed`. Fixed by a new Settings
>    field **`FDI_S3_PUBLIC_ENDPOINT`** and a second Minio client used *only* for signing (falls back
>    to `s3_endpoint` when unset, so prod/tests are unchanged). **One subtlety beyond Task 12's
>    sketch:** `presigned_get_object` is not purely local — absent a constructor `region` it issues a
>    live `GetBucketLocation` to its endpoint, which is the deliberately-unreachable host view, so it
>    500'd until `region="us-east-1"` (MinIO's default) was set to sign offline.
> 2. **The RabbitMQ 4.x worker crash-loop — the whole round-trip was impossible as written.** The
>    worker died with `RestartFreqExceeded` on `Feature transient_nonexcl_queues is deprecated. By
>    default, this feature is not permitted anymore.` RabbitMQ 4.x denies transient non-exclusive
>    queues by default; Celery declares exactly those for its pidbox (worker mingle, and
>    `celery inspect ping` — which *is* the worker healthcheck). No test caught it: every worker test
>    runs eager/in-process, none against a real 4.x broker. Fixed with
>    `deprecated_features.permit.transient_nonexcl_queues = true` in `docker/rabbitmq.conf`.
> 3. **`FDI_ARTIFACT_URL_TTL_SECONDS` was in the allowlist but missing from `.env.example`.** Exactly
>    the inert-config shape the `x-backend-env` comment warns about. `docker compose config` *warns*
>    and injects `""`; pydantic then **raises `int_parsing` on the empty string** (it does NOT fall
>    back to the default), and since `alembic/env.py` builds Settings at import, this takes
>    migrate → api → worker down at boot. Added to `.env.example` (=3600).
> 4. **The F: bind mounts failed as written.** `device: "F:/fdi-data/…"` errors at `up` — the `local`
>    driver resolves `device` *inside the WSL2 VM*, which has no `F:`. Fixed:
>    `COMPOSE_DATA_ROOT=/run/desktop/mnt/host/f/fdi-data` (the VM-visible path; the dirs are still
>    created Windows-side at `F:\`). Confirmed `/run/desktop/mnt/host/f`, `/mnt/host/f`, and
>    `/host_mnt/f` all map to `F:\`.
> 5. **Step 4's `docker compose config` (no profile) validates nothing** — every service carries a
>    profile, so it renders `services: {}` and checks only the top-level anchors. Real validation
>    needs `--profile core`.
>
> **Not a defect, but know it:** the worker never calls `configure_logging()`, so its logs are
> structlog's console `key=value`, not the API's JSON. The correlation ID still crosses correctly
> (proven above); the shape just differs. Pre-existing, acknowledged in `logging.py`, a candidate for
> SP-2.

- [ ] **Step 1: Write `.env.example`**

```bash
# Compose reads this file and injects it as environment variables.
# The application itself reads ENV ONLY -- it never parses .env (12-factor).
# Copy to .env and adjust. .env is gitignored.

# Volumes live on F: -- D: has only ~29 GB free.
#
# NOT FDI_-prefixed, deliberately. `FDI_` is reserved for Settings fields and
# nothing else -- the app rejects any unknown FDI_ var it can see, so a
# Compose-only var wearing that prefix is a landmine: the moment someone adds
# `env_file:` to a service, the app refuses to boot citing a variable that is
# legitimately required from the operator's point of view. That reads as a false
# positive and invites weakening the validator. Every other Compose-only var here
# (POSTGRES_*, MINIO_*, RABBITMQ_*) already sits outside FDI_; this restores that
# rule rather than inventing one.
# WSL2 path, NOT F:/fdi-data. The `local` volume driver resolves `device` inside
# the Docker Desktop VM, which has no drive letters; /run/desktop/mnt/host/f maps
# to F:\. Create the dirs Windows-side at F:di-data (Step 3); mount them here.
COMPOSE_DATA_ROOT=/run/desktop/mnt/host/f/fdi-data

POSTGRES_USER=fdi
POSTGRES_PASSWORD=fdi
POSTGRES_DB=fdi

MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin

RABBITMQ_DEFAULT_USER=guest
RABBITMQ_DEFAULT_PASS=guest

FDI_DATABASE_URL=postgresql+asyncpg://fdi:fdi@postgres:5432/fdi
FDI_BROKER_URL=amqp://guest:guest@rabbitmq:5672/%2F
FDI_RESULT_BACKEND=redis://redis:6379/0
FDI_S3_ENDPOINT=minio:9000
FDI_S3_ACCESS_KEY=minioadmin
FDI_S3_SECRET_KEY=minioadmin
FDI_S3_BUCKET=artifacts
FDI_S3_SECURE=false
FDI_WORKER_CONCURRENCY=2
FDI_LOG_LEVEL=INFO
# REQUIRED even though it has a default. x-backend-env interpolates it
# unconditionally, so absence here injects FDI_ARTIFACT_URL_TTL_SECONDS="" into the
# container -- which pydantic rejects with int_parsing (it does NOT use the default
# for an empty string), taking migrate/api/worker down at boot.
FDI_ARTIFACT_URL_TTL_SECONDS=3600
# Host-reachable endpoint used ONLY to presign download URLs. Distinct from
# FDI_S3_ENDPOINT (minio:9000), which is in-network and unresolvable from a browser.
FDI_S3_PUBLIC_ENDPOINT=localhost:9000
```

- [ ] **Step 2: Write `docker/compose.yml`**

```yaml
name: fdi

# This allowlist MUST contain every Settings field, or the field is inert: the var
# is set in .env, never reaches the container, and the app silently uses its
# default. That is the same silent-misbehaviour class the config validator exists
# to prevent, arriving through Compose instead of through os.environ -- and the
# validator cannot catch it, because it only sees what is already inside the
# container. Add a Settings field, add it here.
x-backend-env: &backend-env
  FDI_DATABASE_URL: ${FDI_DATABASE_URL}
  FDI_BROKER_URL: ${FDI_BROKER_URL}
  FDI_RESULT_BACKEND: ${FDI_RESULT_BACKEND}
  FDI_S3_ENDPOINT: ${FDI_S3_ENDPOINT}
  # Host-reachable endpoint used ONLY for presigning download URLs. FDI_S3_ENDPOINT
  # (minio:9000) is in-network and a browser cannot resolve it, so the artifact_url
  # a UI needs would 404 without this. See the presigning callout.
  FDI_S3_PUBLIC_ENDPOINT: ${FDI_S3_PUBLIC_ENDPOINT}
  FDI_S3_ACCESS_KEY: ${FDI_S3_ACCESS_KEY}
  FDI_S3_SECRET_KEY: ${FDI_S3_SECRET_KEY}
  FDI_S3_BUCKET: ${FDI_S3_BUCKET}
  FDI_S3_SECURE: ${FDI_S3_SECURE}
  FDI_LOG_LEVEL: ${FDI_LOG_LEVEL}
  FDI_WORKER_CONCURRENCY: ${FDI_WORKER_CONCURRENCY}
  FDI_ARTIFACT_URL_TTL_SECONDS: ${FDI_ARTIFACT_URL_TTL_SECONDS}

x-backend-build: &backend-build
  build:
    context: ..
    dockerfile: backend/Dockerfile

services:
  postgres:
    image: postgres:18.4
    profiles: [core]
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - pgdata:/var/lib/postgresql
    ports: ["5432:5432"]
    healthcheck:
      # $$ escapes compose interpolation so the shell expands it in-container.
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

  minio:
    # NOT RELEASE.2025-10-15T17-29-55Z: that release exists but was never published
    # as an image. This is the newest pullable tag, and is knowingly unpatched for
    # CVE-2025-62506. Local dev only -- never production. See spec 3.4.
    image: minio/minio:RELEASE.2025-09-07T16-13-09Z
    profiles: [core]
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    volumes:
      - miniodata:/data
    ports: ["9000:9000", "9001:9001"]
    healthcheck:
      # `mc`, not curl -- MinIO's UBI-micro base has no curl.
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:8.8.0
    profiles: [core]
    volumes:
      - redisdata:/data
    ports: ["6379:6379"]
    healthcheck:
      # grep PONG: redis-cli can exit 0 while the server is still loading.
      test: ["CMD-SHELL", "redis-cli ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

  rabbitmq:
    image: rabbitmq:4.3.2-management
    profiles: [core]
    environment:
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_DEFAULT_USER}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_DEFAULT_PASS}
    volumes:
      - rabbitdata:/var/lib/rabbitmq
      - ./rabbitmq.conf:/etc/rabbitmq/conf.d/20-fdi.conf:ro
    ports: ["5672:5672", "15672:15672"]
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 60s

  migrate:
    <<: *backend-build
    profiles: [core]
    environment: *backend-env
    command: ["alembic", "upgrade", "head"]
    restart: "no"
    depends_on:
      postgres: {condition: service_healthy}

  api:
    <<: *backend-build
    profiles: [core]
    environment: *backend-env
    ports: ["8000:8000"]
    depends_on:
      migrate: {condition: service_completed_successfully}
      minio: {condition: service_healthy}
      rabbitmq: {condition: service_healthy}
    healthcheck:
      # Python, not curl: the image ships no curl (Task 18 dropped the apt layer).
      # urlopen raises on any non-2xx or connection failure, so a bare exit code is
      # a sufficient liveness signal.
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s

  worker:
    <<: *backend-build
    profiles: [core]
    environment: *backend-env
    # No --concurrency flag, deliberately. Concurrency comes from
    # FDI_WORKER_CONCURRENCY -> Settings -> app.conf.worker_concurrency (Task 13).
    # A CLI flag here would be a SECOND source of truth that silently wins over the
    # env var, making FDI_WORKER_CONCURRENCY look configurable while doing nothing.
    command:
      - celery
      - -A
      - fdi.workers.app
      - worker
      - --pool=prefork
      - --loglevel=INFO
    depends_on:
      migrate: {condition: service_completed_successfully}
      minio: {condition: service_healthy}
      rabbitmq: {condition: service_healthy}
    healthcheck:
      test: ["CMD-SHELL", "celery -A fdi.workers.app inspect ping -d celery@$$HOSTNAME"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  neo4j:
    # Pinned exactly: neo4j:latest is the CalVer line, a DIFFERENT lineage from
    # 5.26 LTS. Defined now; wired at SP-9.
    image: neo4j:5.26.28
    profiles: [graph]
    environment:
      NEO4J_AUTH: neo4j/fdipassword
    volumes:
      - neo4jdata:/data
    ports: ["7474:7474", "7687:7687"]

  etcd:
    image: quay.io/coreos/etcd:v3.5.25
    profiles: [vector]
    command:
      - etcd
      - --advertise-client-urls=http://127.0.0.1:2379
      - --listen-client-urls=http://0.0.0.0:2379
      - --data-dir=/etcd
    volumes:
      - etcddata:/etcd

  milvus:
    # NEVER :latest -- it is months stale. And do NOT copy the master-branch
    # compose: it points at v3.0-beta. Defined now; wired at SP-13.
    image: milvusdb/milvus:v2.6.20
    profiles: [vector]
    command: ["milvus", "run", "standalone"]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
    volumes:
      - milvusdata:/var/lib/milvus
    ports: ["19530:19530"]
    depends_on: [etcd, minio]

  frontend:
    profiles: [ui]
    build:
      context: ../frontend
      dockerfile: ../docker/frontend.Dockerfile
    environment:
      NEXT_PUBLIC_API_URL: http://localhost:8000
    ports: ["3000:3000"]

volumes:
  pgdata:
    driver_opts: {type: none, o: bind, device: "${COMPOSE_DATA_ROOT}/postgres"}
  miniodata:
    driver_opts: {type: none, o: bind, device: "${COMPOSE_DATA_ROOT}/minio"}
  redisdata:
    driver_opts: {type: none, o: bind, device: "${COMPOSE_DATA_ROOT}/redis"}
  rabbitdata:
    driver_opts: {type: none, o: bind, device: "${COMPOSE_DATA_ROOT}/rabbitmq"}
  neo4jdata:
    driver_opts: {type: none, o: bind, device: "${COMPOSE_DATA_ROOT}/neo4j"}
  etcddata:
    driver_opts: {type: none, o: bind, device: "${COMPOSE_DATA_ROOT}/etcd"}
  milvusdata:
    driver_opts: {type: none, o: bind, device: "${COMPOSE_DATA_ROOT}/milvus"}
```

- [ ] **Step 3: Create the volume directories on F:**

```powershell
Copy-Item .env.example .env
"postgres","minio","redis","rabbitmq","neo4j","etcd","milvus" | ForEach-Object {
    New-Item -ItemType Directory -Force "F:/fdi-data/$_" | Out-Null
}
```

- [ ] **Step 4: Validate the compose file**

Run: `docker compose -f docker/compose.yml --env-file .env --profile core config`
Expected: renders with no errors; every `${...}` resolved. **`--profile core` is required** --
without it every service (all carry a profile) is filtered out and `config` renders `services: {}`,
validating only the top-level anchors.

- [ ] **Step 5: Bring up the core profile**

Run: `docker compose -f docker/compose.yml --env-file .env --profile core up -d`
Then: `docker compose -f docker/compose.yml --env-file .env --profile core ps`
Expected: `postgres`, `minio`, `redis`, `rabbitmq`, `api`, `worker` all healthy; `migrate` exited 0.

- [ ] **Step 6: Prove the round-trip by hand**

```powershell
$r = Invoke-RestMethod -Method Post http://localhost:8000/ping-job
$r.job_id
Start-Sleep -Seconds 3
Invoke-RestMethod "http://localhost:8000/jobs/$($r.job_id)"
```
Expected: `status: done`, a non-null `artifact_key`, and an `artifact_url` that is **actually
fetchable from the host** — `curl "$($r.artifact_url)"` returns `{"kind":"ping","schema_version":1}`.
"Usable" here means host-reachable, which is only true because of `FDI_S3_PUBLIC_ENDPOINT` (defect 1
above); the raw `minio:9000` URL a naive build produces returns `getaddrinfo failed` from the host.

- [ ] **Step 7: Prove the correlation ID crosses the process boundary**

```powershell
docker compose -f docker/compose.yml --env-file .env logs api worker | Select-String $r.correlation_id
```
Expected: lines from **both** `api` and `worker` carrying the same `correlation_id`. This is done-criterion 5.

- [ ] **Step 8: Record RAM**

```powershell
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}"
```
Expected: total well under the ~5 GB budget. Record the number in `README.md` at Task 24.

- [ ] **Step 9: Commit**

```powershell
git add docker/compose.yml .env.example
git commit -m "build(compose): profiled stack with pinned tags and F: volumes"
```

---

## Task 21: E2E round-trip test

**Files:**
- Test: `backend/tests/e2e/test_walking_skeleton.py`

- [x] **Step 1: Write the test**

```python
"""The SP-1 deliverable, asserted end to end against the running compose stack.

Requires: docker compose --profile core up -d
Skipped automatically when the stack is not up.
"""
import time

import httpx
import pytest

API = "http://localhost:8000"


def _stack_is_up() -> bool:
    try:
        return httpx.get(f"{API}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _stack_is_up(), reason="compose --profile core is not up"),
]


def test_ready_reports_all_backends() -> None:
    body = httpx.get(f"{API}/ready", timeout=10.0).json()
    assert body["postgres"] is True
    assert body["s3"] is True


def test_full_round_trip() -> None:
    accepted = httpx.post(f"{API}/ping-job", timeout=10.0)
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        view = httpx.get(f"{API}/jobs/{job_id}", timeout=10.0).json()
        if view["status"] in {"done", "failed"}:
            break
        time.sleep(1)
    else:
        pytest.fail(f"job {job_id} did not settle within 60s")

    assert view["status"] == "done", f"job failed: {view.get('error')}"
    assert view["artifact_key"] == f"jobs/{job_id}/ping.json"

    artifact = httpx.get(view["artifact_url"], timeout=10.0)
    assert artifact.status_code == 200
    assert artifact.json() == {"kind": "ping", "schema_version": 1}


def test_correlation_id_is_echoed() -> None:
    r = httpx.post(f"{API}/ping-job", headers={"X-Request-ID": "e2e-fixed-id"}, timeout=10.0)
    assert r.json()["correlation_id"] == "e2e-fixed-id"
```

- [x] **Step 2: Add the `e2e` marker to `backend/pyproject.toml`**

```toml
markers = [
    "integration: needs Docker containers",
    "e2e: needs the compose stack running",
]
```

- [x] **Step 3: Run it**

Run: `uv run --directory backend pytest tests/e2e -v`
Expected: 3 passed (with the core profile up).

- [x] **Step 4: Commit**

```powershell
git add backend
git commit -m "test(e2e): assert the full walking-skeleton round-trip"
```

> **Task 21 — done. Verified against a live core stack: 3 passed in 5.64s
> (`test_ready_reports_all_backends`, `test_full_round_trip`,
> `test_correlation_id_is_echoed`). With the stack DOWN, all three report
> `skipped` with a visible reason — an honest gate, not a green one.**
>
> Two plan defects fixed before the test landed (both in the plan's proposed
> version above; the shipped file avoids them):
>
> 1. **Collection-time network probe.** The proposed `pytestmark = [..., skipif(not
>    _stack_is_up())]` calls `_stack_is_up()` — a real socket to `localhost:8000`
>    — at COLLECTION, so every invocation probes, including the fast unit loop
>    (`pytest -m "not integration and not e2e"`), whose defining property is that
>    it touches no network. It is also unsound: if any unrelated process answers
>    on :8000 the probe passes and the e2e tests run against the wrong server.
>    Shipped fix: the probe lives in a **session-scoped `stack` fixture** each test
>    requests; `-m` deselection runs before fixture setup, so `-m "not e2e"` opens
>    no socket. Verified: fast suite = 57 passed, 65 deselected, zero network.
>
> 2. **Fast-suite pollution / silent-skip false confidence.** The e2e module was
>    not marked, so `-m "not integration"` INCLUDED it and showed a green
>    `skipped` while testing the SP-1 deliverable not at all. Shipped fix:
>    registered the `e2e` marker in `pyproject.toml`; the canonical fast command
>    is now `-m "not integration and not e2e"`, documented in `backend/README.md`
>    with a note that `pytest -q` runs e2e and they skip unless the stack is up.
>    **Handoff to Task 23 (CI):** CI must run e2e against a real stack OR
>    explicitly DESELECT it — a silently-skipped e2e must never be counted green.
>
> Operational note (not a code defect, recorded for operators): compose's
> `${FDI_...}` interpolation takes the invoking SHELL over `--env-file`, so a
> single `FDI_` var exported EMPTY in the operator's shell silently overrides
> `.env` and, for the bool/int fields, crashes the stack at `migrate` with
> `int_parsing`/`bool_parsing` on `""` — the same silent-override class the
> `x-backend-env` comment warns about, arriving from the shell side. The str
> fields swallow `""` (a valid string), so only `s3_secure`/`worker_concurrency`/
> `artifact_url_ttl_seconds` surface it — masking that ALL vars were empty. The
> up command must be run from a shell with no stray `FDI_` vars; the plan's
> `--env-file .env` form (Step 2/3 above) is correct and required.

---

## Task 22: Next.js frontend

**Files:**
- Create: `frontend/` (scaffold), `frontend/app/page.tsx`, `docker/frontend.Dockerfile`

- [x] **Step 1: Scaffold**

Run from the repo root:

```powershell
pnpm create next-app@latest frontend --typescript --app --tailwind --eslint --no-src-dir --import-alias "@/*" --use-pnpm
```
Expected: `frontend/` created. Confirm `node -v` reports v24 first.

- [x] **Step 2: Replace `frontend/app/page.tsx`**

```tsx
"use client";

import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type JobView = {
  job_id: string;
  status: string;
  artifact_key: string | null;
  artifact_url: string | null;
  error: string | null;
};

export default function Home() {
  const [job, setJob] = useState<JobView | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      const accepted = await fetch(`${API}/ping-job`, { method: "POST" });
      const { job_id } = await accepted.json();

      for (let i = 0; i < 60; i++) {
        const view: JobView = await (await fetch(`${API}/jobs/${job_id}`)).json();
        setJob(view);
        if (view.status === "done" || view.status === "failed") return;
        await new Promise((r) => setTimeout(r, 1000));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-2xl p-8 font-mono">
      <h1 className="text-2xl font-bold">Financial Document Intelligence</h1>
      <p className="mt-1 text-sm opacity-60">SP-1 walking skeleton</p>

      <button
        onClick={submit}
        disabled={busy}
        className="mt-6 rounded bg-black px-4 py-2 text-white disabled:opacity-40 dark:bg-white dark:text-black"
      >
        {busy ? "Running…" : "Submit ping job"}
      </button>

      {job && (
        <pre className="mt-6 overflow-x-auto rounded border p-4 text-sm">
          {JSON.stringify(job, null, 2)}
        </pre>
      )}
    </main>
  );
}
```

- [x] **Step 3: Write `docker/frontend.Dockerfile`**

```dockerfile
FROM node:24-slim
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
EXPOSE 3000
CMD ["pnpm", "dev", "--hostname", "0.0.0.0"]
```

- [x] **Step 4: Verify against the running API**

Run: `docker compose -f docker/compose.yml --env-file .env --profile core --profile ui up -d`
Open `http://localhost:3000`, click **Submit ping job**.
Expected: the JSON view settles to `"status": "done"` with a non-null `artifact_url`. This is done-criterion 6.

- [x] **Step 5: Commit**

```powershell
git add frontend docker/frontend.Dockerfile
git commit -m "feat(frontend): Next.js shell rendering live job status"
```

> **Task 22 — done. done-criterion 6 proven at the gold standard: a REAL headless
> Chromium loaded the page, clicked Submit, and rendered `"status":"done"` with a
> non-null host-fetchable `artifact_url` and ZERO browser errors. Committed as
> `feat(frontend)`; the CORS enabler shipped first as `feat(api)`.**
>
> One defect the plan missed, and it is invisible to every other test:
>
> - **The API had no CORS, so the browser round-trip could not work.** The page
>   fetches the API from `http://localhost:3000` — a different origin. A browser
>   sends the request but withholds the RESPONSE from the page's JS unless it
>   carries `Access-Control-Allow-Origin`; even the bodyless POST needs it to be
>   readable. curl and httpx enforce no origin policy, so Task 21's e2e round-trip
>   and every unit test are blind to it — done-criterion 6 would have failed in a
>   real browser with a green backend suite. Fixed by adding `CORSMiddleware`
>   (allowlist `["http://localhost:3000"]`, not `"*"`) as the outermost user
>   middleware in `create_app`, with `test_cors.py` proving preflight + simple-GET
>   ACAO and that an unlisted origin gets none (gate-proved by neutralising it).
>
> Recorded, not defects:
>
> - **`create-next-app@latest` now installs Next 16.2.10 / React 19.2.4**, not the
>   15 the plan text anticipated. App Router is unchanged; `next build` + lint are
>   clean; `page.tsx` is verbatim. No pin was added (the plan used `@latest`).
> - **Toolchain:** Node here is v21 (satisfies Next's `>=20`), and pnpm is
>   reachable only as `corepack pnpm` — a PATH-shim `EPERM` under
>   `C:\Program Files\nodejs`. `create-next-app`'s built-in install crashed on
>   `spawn pnpm ENOENT`; completed with `corepack pnpm install`.
> - **The live verification patched the corrected `app.py` into the running api
>   container (`docker cp` + restart), because the deployed image is the Task-20
>   build (pre-CORS) and a rebuild is blocked: `ghcr.io/astral-sh/uv:0.11.28` is
>   not cached and ghcr.io is currently unreachable (HTTP 000). The SOURCE is
>   correct and unit-tested; the image rebuild is a mechanical `docker compose
>   build` pending network — NOT a code defect. Any operator on a working network
>   gets the CORS layer for free at build. Flagged forward to Task 23/24: CI
>   builds the image on a network that can reach ghcr, so CI is where the built
>   image's CORS is exercised end to end.**

---

## Task 23: CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [x] **Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          version: "0.11.28"
          enable-cache: true

      - name: Sync
        run: uv sync --project backend --locked

      - name: Lint
        run: uv run --directory backend ruff check .

      - name: Types
        run: uv run --directory backend mypy --strict src

      - name: Import contract
        run: uv run --directory backend lint-imports --config .importlinter

      - name: Unit tests
        run: uv run --directory backend pytest tests/unit -q

      # Integration tests need Docker. ubuntu-latest has it; testcontainers
      # manages lifecycles itself, so no service containers are declared.
      - name: Integration tests
        run: uv run --directory backend pytest tests/integration -q

  frontend:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "24"
      - run: corepack enable
      - run: pnpm install --frozen-lockfile
      - run: pnpm run lint
      - run: pnpm run build
```

- [x] **Step 2: Verify the gates pass locally first**

```powershell
uv sync --project backend --locked
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
$env:PYTHONUTF8=1; uv run --directory backend lint-imports --config .importlinter
uv run --directory backend pytest tests/unit tests/integration -q
```
Expected: all clean. (The last path was `backend/tests/integration` in the draft
— wrong: `--directory backend` already makes `backend/` the cwd, so that resolves
to `backend/backend/tests/integration` and collects nothing. `tests/integration`
is correct. `PYTHONUTF8=1` is required on Windows or lint-imports dies rendering
its report through cp1252 — a crash the plan's bare command would misread as a
broken contract.)

- [x] **Step 3: Commit**

```powershell
git add .github
git commit -m "ci: ruff, mypy --strict, import contract, unit and integration tests"
```

> **Task 23 — done. Every CI command verified locally, command-by-command:
> backend `uv sync --locked` / ruff / mypy --strict / `lint-imports` (Contracts: 1
> kept, 0 broken) / unit (55 passed); frontend `pnpm install --frozen-lockfile` /
> lint / `next build` all green; the workflow YAML parses (2 jobs). What is NOT
> verified: the GitHub runner itself (setup-uv@v5, setup-node@v4, ubuntu Docker
> for the testcontainers integration job) — standard actions, but not executed
> here.**
>
> One fix the frontend job REQUIRED to be deterministic:
>
> - **`package.json` had no `packageManager` field.** `corepack enable` +
>   `pnpm install --frozen-lockfile` with no pin is non-deterministic: corepack
>   picks whatever pnpm the runner's Node bundles, which may not match the
>   `lockfileVersion: '9.0'` this repo's `pnpm-lock.yaml` was written with, and a
>   frozen install then fails on a version it cannot reproduce. Pinned
>   `"packageManager": "pnpm@9.15.9"` (the version that produced the lockfile).
>   This also makes `docker/frontend.Dockerfile`'s `corepack enable` +
>   `pnpm install --frozen-lockfile` reproducible for free. Confirmed the frozen
>   install is a no-op ("Lockfile is up to date") under the pin.
>
> Recorded coverage gaps (stated, not silently accepted):
>
> - **`tests/e2e` is not in CI.** It drives a live compose stack and skips itself
>   when absent; CI runs `tests/unit` and `tests/integration` by PATH, so a silent
>   e2e skip can never be miscounted as a pass (the Task 21 handoff, honoured by
>   exclusion). The walking-skeleton round-trip and the browser check are proven
>   at build time, not by this workflow.
> - **CI never BUILDS the Docker images.** Backend gates run under uv, frontend
>   under pnpm; neither exercises `backend/Dockerfile`, `docker/frontend.Dockerfile`
>   or `docker/compose.yml`. So a green CI does not prove the deployable image —
>   including that the CORS layer is in it (Task 22's image note). Corrects the
>   Task 22 forward-reference: this CI does NOT build the image; wiring an
>   image-build + compose round-trip into CI is a follow-up for the first task
>   that owns a deployment target.

---

## Task 24: README and the done-criteria audit

**Files:**
- Create: `README.md`
- Modify: `docs/superpowers/specs/2026-07-15-project-setup-design.md` (status line)

- [ ] **Step 1: Write `README.md`**

```markdown
# Financial Document Intelligence

Ingestion platform for annual reports: PDF → OCR → structural intelligence →
AST → graph → ontology → embeddings → retrieval.

- **Roadmap:** `docs/architecture/ingestion-roadmap.md` — read this first
- **Current state:** SP-1 (walking skeleton) complete. No document processing yet.

## Quick start

```powershell
uv python install 3.13
Copy-Item .env.example .env
"postgres","minio","redis","rabbitmq","neo4j","etcd","milvus" | ForEach-Object {
    New-Item -ItemType Directory -Force "F:/fdi-data/$_" | Out-Null
}
docker compose -f docker/compose.yml --env-file .env --profile core up -d
```

Then:

```powershell
$r = Invoke-RestMethod -Method Post http://localhost:8000/ping-job
Invoke-RestMethod "http://localhost:8000/jobs/$($r.job_id)"
```

## Profiles

Run only what the current sub-project needs — the full stack is ~18.5 GB on a
19.8 GB machine.

| Profile | Services | ~Container RAM | From |
|---|---|---|---|
| `core` | postgres, minio, redis, rabbitmq, api, worker | 5 GB | now |
| `graph` | + neo4j | +2 GB | SP-9 |
| `vector` | + etcd, milvus | +5 GB | SP-13 |
| `ui` | + frontend | +1 GB | now |

## Tests

```powershell
uv run --directory backend pytest tests/unit -q          # no containers
uv run --directory backend pytest tests/integration -q   # testcontainers
uv run --directory backend pytest tests/e2e -q           # needs compose up
```

## Gates

```powershell
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
uv run --directory backend lint-imports --config .importlinter
```

## Architecture rules

1. **`fdi.core` is pure** — no I/O, config, clock or RNG. Enforced by `lint-imports`.
2. **Never `async def` under `@app.task`** — Celery reports SUCCESS and does nothing.
   Enforced by an AST test.
3. **Never build a loop, engine or pool at module import** — `fork()` copies the loop
   without its thread and hangs silently.
4. **Bind the correlation ID before `asyncio.run()`** — contextvars is copy-in,
   no-copy-out.
5. **Prefork only.** Memory-bound, so `--concurrency=2` on this machine.

## Known caveats

- **MinIO is local-dev only and knowingly unpatched** for CVE-2025-62506. Its patched
  release shipped source-only after the project archived; the newest pullable image
  predates the fix. Application code targets the generic S3 API, so replacing it is a
  compose change. Never run it in production.
- `consumer_timeout` in `docker/rabbitmq.conf` is currently **inert** — RabbitMQ 4.3
  evaluates it for quorum queues only, and Celery declares classic. Kept for when
  quorum queues arrive.
```

- [ ] **Step 2: Record measured RAM**

Run: `docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}"`
Replace the `5 GB` figure in the profile table with the measured total.

- [ ] **Step 3: Audit every done criterion from spec §12**

Walk them one by one and record the evidence:

| # | Criterion | Verify |
|---|---|---|
| 1 | six services healthy | `docker compose ... --profile core ps` |
| 2 | migrate completes before api/worker | `docker compose ... ps` shows `migrate` exited 0 |
| 3 | ping round-trip | Task 20 Step 6 |
| 4 | `GET /jobs/{id}` → working `artifact_url` | Task 21 `test_full_round_trip` |
| 5 | one `correlation_id` across both logs | Task 20 Step 7 |
| 6 | Next.js renders live status | Task 22 Step 4 |
| 7 | CI green on four gates | Task 23 |
| 8 | RAM measured and recorded | Step 2 above |
| 9 | `lint-imports` passes | Task 23 |
| 10 | determinism test | Task 14 `test_ping_artifact_body_is_deterministic` |

Any criterion without evidence means SP-1 is not done. Fix it before proceeding.

- [ ] **Step 4: Update the spec status line**

Change:
```markdown
- **Status:** Approved 2026-07-15. Not yet implemented.
```
to:
```markdown
- **Status:** Implemented 2026-07-15 on `feature/sp-1-project-setup`. All §12 done criteria verified.
```

- [ ] **Step 5: Update the roadmap**

In `docs/architecture/ingestion-roadmap.md`, change SP-1's status from `📝 spec written` to `✅ done`,
and add a "What SP-1 shipped that SP-2 must know" note recording: the `core`/`platform` boundary
and its `exhaustive` contract, the canonical task shape, `canonical_json`, the `JobDispatcher` seam,
and the ignore-list entry for `fdi.api.app -> fdi.workers.tasks`.

- [ ] **Step 6: Commit**

```powershell
git add README.md docs
git commit -m "docs: README, SP-1 done-criteria audit, roadmap status"
```

---

## Self-Review

**Spec coverage** — every §-numbered section maps to a task:

| Spec § | Task(s) |
|---|---|
| §2 pre-work | Environment pre-work block |
| §3.1 one package, two entrypoints | 1, 18 |
| §3.2 purity boundary | 2, 17 |
| §3.3 async boundary | 9, 14 |
| §3.4 S3 / MinIO | 12, 20 |
| §3.5 profiles | 20 |
| §3.6 Python 3.13 / Node 24 | 1, 22 |
| §4 layout | 1, 2 |
| §5 topology | 20 |
| §6 profiles | 20 |
| §7 execution model | 13, 14, 19 |
| §8 walking skeleton | 14, 16, 21 |
| §9 config | 6, 20 |
| §10 error handling | 15, 16, 20 |
| §11 testing | 4, 23 |
| §12 done criteria | 24 |

**Known risks carried into execution:**

1. **Task 4 is a hard gate.** testcontainers-python#1015 is an open Windows port-mapping regression against 4.14.2. If Task 4 fails, stop and escalate — the fallback (compose-based integration tests) is a spec change.
2. **Task 16 Step 8 is expected to fail `lint-imports`.** Task 17 resolves it. This is written deliberately as two tasks so the contract is *seen* to bite before it is accommodated.
3. **MinIO is knowingly vulnerable.** Local dev only. Documented in the README and spec §3.4.
4. **Compose bind-mounted volumes on Windows** can be slow under WSL2. If Postgres throughput is painful, switch `pgdata` to a plain named volume (Docker-managed, inside the WSL2 VM) and accept that it lives on C:.
