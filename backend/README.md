# backend

Python backend for the Financial Document Intelligence ingestion platform (`fdi`).

This is the SP-1 "walking skeleton": it wires the integration seams (API, queue,
workers, database, object storage, cache) without doing real document processing.
Source lives under `src/fdi`; tests live under `tests/` (`unit`, `integration`, `e2e`).

## Setup

```powershell
uv sync --project backend
```

## Quality gates

Run from the repo root:

```powershell
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
uv run --directory backend pytest -q
```

### Why `--directory` and not `--project`

`--project` picks the venv but leaves the working directory alone, and pytest and mypy
discover their config from the filesystem rather than from uv. Run from the repo root,
`--project` silently discards everything in `pyproject.toml`: pytest's rootdir misdetects
and `asyncio_mode` falls back to `STRICT` (so every unmarked `async def` test fails with
"async def functions are not natively supported"), and mypy never loads `[tool.mypy]` at
all — its config search is cwd-only — so `ignore_missing_imports` and `strict` are both
dead. `--directory` sets the cwd, so every tool finds the config. Don't "simplify" it back.
