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

`--env-file .env` is not optional: with `-f docker/compose.yml` the project
directory is `docker/`, so Compose otherwise looks for `docker/.env` and injects
empty strings for every `FDI_*` var, which crashes `migrate` at boot on the
int/bool fields. Relatedly, run this from a shell with **no stray `FDI_` variables
exported** — Compose substitutes `${FDI_...}` from the shell over `.env`, so an
`FDI_S3_SECURE=` left in your environment silently overrides the file.

Then drive the walking skeleton:

```powershell
$r = Invoke-RestMethod -Method Post http://localhost:8000/ping-job
Invoke-RestMethod "http://localhost:8000/jobs/$($r.job_id)"   # poll until status = done
```

Add the UI with `--profile core --profile ui` and open http://localhost:3000.

## Profiles

Run only what the current sub-project needs. The Docker VM on this machine caps at
~9.6 GB, and the `vector` profile (Milvus) is the memory-heavy one that can
exhaust it — so profiles keep everything else off until a sub-project needs it.

| Profile | Services | Container RAM | From |
|---|---|---|---|
| `core` | postgres, minio, redis, rabbitmq, api, worker | **~0.42 GB measured** (424 MiB idle, ≈460 MiB under a job) | now |
| `graph` | + neo4j | +~2 GB (est.) | SP-9 |
| `vector` | + etcd, milvus | +~5 GB (est., OOM risk) | SP-13 |
| `ui` | + frontend | +~0.5 GB (next dev) | now |

Per-container at idle (measured `docker stats`, core): rabbitmq 122 · worker 128 ·
api 72 · minio 69 · postgres 26 · redis 7 MiB.

## Tests

```powershell
uv run --directory backend pytest -m "not integration and not e2e" -q  # fast, no Docker
uv run --directory backend pytest tests/unit -q          # no containers
uv run --directory backend pytest tests/integration -q   # testcontainers (needs Docker)
uv run --directory backend pytest tests/e2e -q           # needs compose --profile core up
```

`tests/e2e` **skips itself** when the stack is down — that is an honest skip, not
a pass. Bring the core stack up first, or those three tests verify nothing.

## Gates

```powershell
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
$env:PYTHONUTF8=1; uv run --directory backend lint-imports --config .importlinter
```

`PYTHONUTF8=1` matters on Windows: `lint-imports` renders its report through a
library that dies on cp1252 stdout, and that crash exits 1 exactly like a broken
contract — so without it a green architecture reads as a violation.

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
- **CORS on the API is a hardcoded dev allowlist** (`http://localhost:3000` only), so
  the walking-skeleton UI can read responses cross-origin. A real deployment serves
  the UI same-origin or makes the origin list configurable.
- `consumer_timeout` in `docker/rabbitmq.conf` is currently **inert** — RabbitMQ 4.3
  evaluates it for quorum queues only, and Celery declares classic. Kept for when
  quorum queues arrive.
