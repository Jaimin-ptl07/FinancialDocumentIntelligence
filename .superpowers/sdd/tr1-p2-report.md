# TR-1 P2 — table cards (chunks v3: one card per table)

## Status
Complete.

## What was built

### Deliverable A — `backend/src/fdi/core/retrieval.py`
- `build_chunks(ast, semantic, provenance, structure, basis)` — gained the
  `basis` parameter (5th positional arg). Section (`s{i}`) chunk building is
  unchanged; `basis` is threaded down to `_table_chunks`.
- `_table_chunks(structure, basis)` — **replaced** the old per-row `t{i}r{r}`
  chunks + capped whole-table blob with **ONE card per table** (`chunk_id =
  f"t{i}"`, the pre-existing whole-table id). New helpers:
  - `_table_caption_text` — raw `caption.content` (`""` if absent), the text
    the detectors below run against (distinct from `_table_context`'s
    `f"Table {i}"` DISPLAY fallback).
  - `_statement_type(caption_text) -> "BS"|"PL"|"CF"|"unknown"` — casefolded
    anchor match (`"balance sheet"`, `"statement of profit and loss"` /
    `"profit and loss"`, `"cash flow"`).
  - `_note_no(caption_text) -> str` — leading `"Note <n>"` (case-insensitive)
    or `"<n>."`, else `""`.
  - `_units(caption_text) -> str` — `"lakh"→"Rs Lakhs"`, `"crore"→"Rs Crore"`,
    `"million"→"Rs Million"`, else `""`.
  - `_table_grid_rows` / `_row_label` — grid-reading helpers factored out of
    the old row-chunk loop.
  - Card `text` = the LOCATION summary (identity line + resolved column
    headers + data-row labels, **no cell values**):
    ```
    [{context} | {statement_type} | basis={basis_str}{note_part}{units_part} | page {page}]
    Columns: {col_headers_joined}
    Rows: {row_labels_joined}
    ```
  - `basis_str = page_basis(basis, page)` — the one new cross-module import
    (`fdi.core.basis.page_basis`, pure/total), noted in the module docstring
    as the sole exception to "no import of other `fdi.core.*` artifact
    modules."
  - All new helpers are total (malformed table/grid/caption/basis degrade to
    `"unknown"`/`""`/`None`, never raise).
- Dedup-by-text behavior (`_dedup_by_text`) unchanged; now dedups across the
  smaller card set.

### Deliverable B — `backend/src/fdi/pipeline/stages/chunks.py`
- `ChunksStage.version`: 2 → 3.
- `ChunksStage.deps`: `("ast","semantic","provenance","structure")` →
  `("ast","semantic","provenance","structure","basis")`.
- `run`: reads the `basis` artifact and passes it to `build_chunks`.

## Before/after card shape

**Before** (Phase 3a, per table with 1 header row + 2 data rows): 3 chunks —
`t0r1`, `t0r2` (one per data row, `"{context} | {label}: {header}={value}, ..."`)
plus `t0` (capped whole-table blob, header line + all row lines joined).

**After** (TR-1 P2): 1 chunk — `t0`:
```
[Balance Sheet | BS | basis=unknown | page 5]
Columns: As at 31-03-2024 | As at 31-03-2023
Rows: Property, plant and equipment; Total assets
```
No cell values anywhere in the card — it is a LOCATION summary only. Values
reach the LLM again in P3 (grid render), out of scope here.

## Tests updated/extended

- `backend/tests/unit/test_retrieval_chunks.py` — full rewrite of the
  table-chunk section (18 tests total, up from 15): one-card-per-table shape,
  zero `t0r*` survivors, identity line assertions (statement_type/basis/note/
  units/page), basis reflecting `page_basis` (region hit, no-region-covers ->
  falls back to `default_basis`), statement_type/note/units best-effort
  detection (BS+lakh+leading digit-note, PL+"Note N"+crore, CF, undetectable
  -> all `"unknown"`/`""`), caption-then-index context fallback, page-None
  degrade, dedup across duplicate table cards, dedup collapsing a section
  chunk against a byte-identical table card.
- `backend/tests/integration/test_chunks_stage.py` — added `BasisStage` to
  the registry/run chain; `chunks` artifact key version 2→3 with `basis`
  folded into `derived_input_hash`; expected body rewritten to the one-card
  shape; successor-of-deps test extended to assert `"chunks" in
  resolver.successors("basis")`.
- `backend/tests/integration/test_index_enrichment.py` — added `BasisStage`
  + `run_stage("basis", ...)` before `chunks` (table-free fixture, so no
  card-shape assertions needed).
- `backend/tests/integration/test_embedding_stage.py` — added `BasisStage`
  wiring; `fake_embedder.seen` and `body["chunks"]` ids rewritten from
  `["s0","t0r1","t0"]` to `["s0","t0"]` with the new card text.
- `backend/tests/integration/test_extract_document.py` — added `BasisStage`
  wiring; evidence_id `t0r1` → `t0` (2 occurrences); the "12,345" prompt
  assertion was replaced with `"Rows: Total assets"` — TR-1 P2 cards carry no
  cell values by design, so the old value-in-prompt assertion no longer holds
  until P3 wires grid rendering back in; comments updated accordingly.
- `backend/tests/integration/test_agent_tools.py`, `test_ask.py`,
  `test_hybrid_search.py`, `test_vector_search.py`, `test_index_stage.py`,
  `test_resolve_chunk.py`, `test_resolve_entities.py`, `test_api.py` (4
  separate inline-import occurrences) — every `StageRegistry` that registers
  `ChunksStage` now also registers `BasisStage` and runs `stage_name="basis"`
  after `provenance`/before `chunks` (mechanical wiring fix: `ChunksStage.deps`
  now includes `"basis"`, and `DagResolver.__init__` raises `ValueError` if a
  registered stage has an unregistered dep). All these fixtures use
  table-free OCR fixtures (`"tables": []`), so no card-shape assertions
  needed updating there.
- Left untouched (confirmed out of scope): `test_chunk_regions.py` (tests
  `resolve_chunk_regions`, a generic chunk-id parser fed synthetic ids
  directly, not build_chunks output) and `test_extractor.py`'s
  `filter_candidates` tests (synthetic `Candidate` fixtures exercising
  `_TABLE_CHUNK_RE`'s own regex, unrelated to build_chunks). Docstring
  comments in `core/extractor.py`, `core/provenance.py`,
  `pipeline/extractor.py`, `pipeline/stages/embedding.py` still mention
  `t{i}r{j}` — stale prose only (no logic depends on it), left alone per the
  binding constraint against touching extraction/retrieval-filtering code.

## Verification (Docker up)

- `uv run --directory backend pytest tests/unit/test_retrieval_chunks.py -q`
  → **18 passed**.
- `uv run --directory backend pytest -m "not integration and not e2e" -q`
  → **628 passed, 11 skipped, 213 deselected**.
- `uv run --directory backend pytest tests/integration/test_index_enrichment.py -q`
  → **3 passed**.
- `uv run --directory backend pytest -m "integration" -q` (full integration
  suite) → **208 passed, 2 failed** (`test_schema_endpoints.py` — pre-existing,
  unrelated: expects schema `"3.1.0"`, on-disk seed is already `"3.2.0"` from
  the already-merged TR-1 P4a wave; confirmed via `git log`/`git status` that
  this file has zero local changes and the seed file's version predates this
  task).
- `uv run --directory backend ruff check .` → **All checks passed**.
- `uv run --directory backend mypy --strict src` → **Success: no issues
  found in 104 source files**.

## Commit
1 commit on `tr1-p2-cards` (base `d5fa892`, TR-1 wave-1 merge including P1
basis + P3-render + P4a schema). See final assistant message for SHA.

## Concerns / follow-ups (not blocking)
- The pre-existing `test_schema_endpoints.py` version-string failures (2)
  should be fixed by whoever owns the P4a/wave-1 merge cleanup — not this
  task's scope.
- Table cards now carry no cell values (by design, per spec §4.2). Any test
  or manual smoke-check that expects a numeric value inside the *retrieval
  pool's raw chunk text* (rather than the LLM's extracted answer) will need
  re-auditing once P3 wires `render_table_markdown` back into
  `extract_unit`'s context assembly.
