# `tests/azure` -- opt-in real Document Intelligence smoke test

This directory holds a single test, `test_real_ocr.py`, that calls the real
Azure Document Intelligence service. It is **never** run automatically: it is
skipped at collection time unless both of the following are true.

1. Credentials are set in the environment:
   - `FDI_AZURE_DI_ENDPOINT`
   - `FDI_AZURE_DI_KEY`
2. A sample PDF exists at `backend/tests/azure/sample.pdf`.

## Why no sample PDF is committed

A hand-crafted or minimal PDF risks being invalid (Azure DI may reject it
outright), and committing real financial documents to source control is
inappropriate. Instead, an operator who wants to run this smoke test drops a
real single-page PDF at `backend/tests/azure/sample.pdf` (git-ignored) before
running the test locally.

## Running it

```
FDI_AZURE_DI_ENDPOINT=https://<resource>.cognitiveservices.azure.com \
FDI_AZURE_DI_KEY=<key> \
uv run --directory backend pytest tests/azure -rs
```

With no credentials and/or no `sample.pdf` present, the test reports
`1 skipped` with a human-readable reason -- it is never collected as an error
and never silently passes.

## Marker

The test is also tagged `@pytest.mark.azure`, registered in
`backend/pyproject.toml`. The fully-offline fast loop excludes it explicitly:

```
uv run --directory backend pytest -m "not integration and not e2e and not azure" -q
```
