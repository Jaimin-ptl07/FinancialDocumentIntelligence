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
uv run --project backend ruff check backend
uv run --project backend mypy backend/src
uv run --project backend pytest -q
```
