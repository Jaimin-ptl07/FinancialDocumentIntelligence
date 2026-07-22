# TR-1 Wave 3 — P3-wire (render located tables) + P4b (basis-aware retrieval)

## Status
Complete.

## What was built

### Deliverable 1 — P4b: basis-aware retrieval replaces the consolidated cutoff

`backend/src/fdi/pipeline/extractor.py`:

- `_basis_keep(cand_basis: str, preference: str) -> bool` — pure helper: `"both"`
  keeps everything; otherwise keep when `cand_basis == preference` or
  `cand_basis == "unknown"` (front-matter/unrecognized tables serve either
  basis); drop the opposite basis. Never raises.
- `_filter_by_basis(candidates, *, basis_artifact, preference)` — maps
  `page_basis(basis_artifact, c.page)` over candidates and keeps those
  `_basis_keep` approves.
- `extract_unit` gained `basis_artifact`/`structure` kwargs (replacing
  `consolidated_start_page`). It still calls `filter_candidates(...,
  consolidated_start_page=None, needs_tables=...)` — the core function and
  its ~12 existing tests in `test_extractor.py` are UNCHANGED (permitted by
  the task: `find_consolidated_start_page`/the `consolidated_start_page`
  parameter are left in `fdi.core.extractor` as superseded, always passed
  `None` from the pipeline now) — then applies `_filter_by_basis` as a
  second, independent pass using `unit.basis_preference`.
- `extract_document` reads the `basis` artifact ONCE via a new
  `_basis_artifact_for(ctx)` (mirrors `_consolidated_start_page`'s old
  read-and-log seam: logs `extractor.basis_artifact_missing` or
  `extractor.basis_artifact_found` with region count) and threads it, plus
  the already-read `structure` artifact, into every `extract_unit` call.
  The old `_consolidated_start_page` function and its `read_chunks` import
  were removed (no longer called anywhere).

### Deliverable 2 — P3-wire: render located tables as markdown

`backend/src/fdi/pipeline/extractor.py`:

- `_card_identity(text) -> str` — a table card's identity line is its
  TEXT's first line (`"[...]"`); this returns it with the brackets
  stripped, reusing the card's own identity verbatim rather than
  reconstructing one (per the task's "simplest correct approach").
- `_render_table_candidate(candidate, *, structure) -> Candidate` — if
  `candidate`'s evidence_id chunk_id matches `^t(\d+)$`, fetches
  `structure["tables"][table_index]["grid"]`, renders it via
  `render_table_markdown(repair_headers(grid), identity=_card_identity(...))`,
  and returns a NEW `Candidate` (via `dataclasses.replace`, since `Candidate`
  is frozen) with `text` replaced. Returns `candidate` UNCHANGED (never
  raises) when: it's a section chunk (`s{i}`), the evidence_id has no colon,
  `table_index` is out of range, the table entry / its `grid` is
  missing/malformed, or the render comes back empty.
- `extract_unit`: after `rerank_candidates` produces `ranked`, a separate
  `prompt_candidates = [_render_table_candidate(c, structure=...) for c in
  ranked]` list is built and passed to `build_unit_messages` — `ranked`
  itself (with its original card-summary text) is still what
  `assemble_unit_results` uses for its citation fallback (page/snippet from
  the top candidate), so a fallback citation never surfaces a raw markdown
  blob as a "snippet."

### Deliverable 3 — harden the period regex

`backend/src/fdi/core/table_render.py`:

- All period regexes (`_DATE_DMY`, `_DATE_YMD`, `_YEAR`, `_FY_TOKEN`,
  `_FISCAL_RANGE_2/4`) had their leading `^` dropped (trailing `$` kept —
  the period token is always the LAST thing printed in a header cell); `_is_period`
  now uses `re.search` instead of `re.match`, so "As at 31-03-2024", "For the
  year ended FY24", etc. are detected.
- Added two spelled-month patterns (`_SPELLED_MONTH_DAY_FIRST` /
  `_MONTH_FIRST`, case-insensitive, optional ordinal suffix + comma, no
  anchors): day-month-year ("31st March, 2024") and month-day-year ("March
  31, 2024", "For the year ended March 31, 2024").
- `_YEAR`'s bare-year branch now extracts `int(year_match.group())` (the
  matched substring) rather than `int(text)` (the whole cell) — required
  once the match is no longer guaranteed to span the entire string.

## Tests

- `backend/tests/unit/test_table_render.py` — 3 new tests: prefix-date
  ("As at 31-03-2024"), spelled-month-with-ordinal-and-comma ("31st March,
  2024"), and month-first-with-prefix ("For the year ended March 31,
  2024"). All existing tests (including the positional-fallback ones)
  unchanged and still pass.
- `backend/tests/unit/test_pipeline_extractor_render_basis.py` (new file) —
  14 tests: `_basis_keep` (standalone/consolidated/both truth table),
  `_card_identity` (bracket-stripping, no-brackets, empty), and
  `_render_table_candidate` (renders markdown + preserves
  evidence_id/page/score, uses card identity as title, out-of-range index
  unchanged, missing grid unchanged, malformed structure shapes never
  raise, section chunk (`s{i}`) untouched, colon-less evidence_id
  untouched).
- `backend/tests/integration/test_extract_document.py`:
  - Adapted `test_extract_document_pools_keyword_candidates_only`'s P2
    placeholder assertion (`"Rows: Total assets" in _user`) to assert the
    real rendered shape: finds the one call whose user text contains
    `"| --- | --- |"` and asserts it also contains
    `"| Particulars | FY24 |"` and `"| Total assets | 12,345 |"` — i.e. the
    real header AND the real cell value now reach the prompt, not just a
    location summary.
  - Added `test_extract_document_basis_filter_drops_consolidated_table_for_standalone_unit`:
    builds a document with two SA-700 auditor's-report anchors (standalone
    at page 1, consolidated at page 50) and one "Total assets" table in
    each region with DIFFERENT values (12,345 vs 99,999). Asserts the
    `basis` artifact actually split into both regions (so the test isn't
    vacuous), then runs `extract_document` with a single field whose unit
    omits `basis_preference` (defaults to `"standalone"`) and asserts the
    LLM's one prompt contains `"12,345"` and never contains `"99,999"` —
    proving the basis filter, not just the render wiring.

## Verification (all commands run from the worktree root; Docker was up)

- `uv run --directory backend pytest tests/unit/test_table_render.py -q`
  → **19 passed**.
- `uv run --directory backend pytest -m "not integration and not e2e" -q`
  → **645 passed, 11 skipped, 214 deselected** in 116.90s.
- `uv run --directory backend pytest tests/integration/test_extract_document.py -q`
  → **4 passed** in 96.33s.
- `uv run --directory backend pytest tests/integration -q` (full integration
  suite) → **213 passed, 2 skipped** in 452.17s — no failures (the
  `test_schema_endpoints.py` version mismatch noted as pre-existing/
  unrelated in the TR-1 P2 report is not present in this worktree's state).
- `uv run --directory backend ruff check .` → **All checks passed** (fixed
  two `E501` line-too-long violations introduced by this change's own new
  test code before this run).
- `uv run --directory backend mypy --strict src` → **Success: no issues
  found in 104 source files**.

## Binding constraints honored

- `fdi.core` (table_render.py, basis.py) stayed pure — no logging added
  there; all new logging (`extractor.basis_artifact_missing`/`_found`) lives
  in `fdi.pipeline.extractor`.
- `extract_unit`/`extract_document`/`_filter_by_basis`/`_render_table_candidate`/
  `_card_identity` never raise on any input shape (guarded with
  `isinstance` checks and `contextlib.suppress` around I/O, matching the
  existing decision-10 posture in this module).
- Milvus schema, index stage, chunks stage, and the schema JSON were not
  touched.
- `fuse`/`rerank_candidates` were not modified.
- `filter_candidates`'s `needs_tables` behavior and its existing tests in
  `test_extractor.py` are untouched; `find_consolidated_start_page` is left
  in `fdi.core.extractor` as superseded/unused (permitted by the task).

## Commit

1 commit on `tr1-p3wire-p4b` (base `2020e12`, TR-1 P2 table-cards wave). SHA
in the final assistant message.

## Concerns / follow-ups (not blocking)

- `_render_table_candidate`'s render is skipped (candidate left unchanged)
  whenever `render_table_markdown` returns an empty string (e.g. a grid
  with `n_cols<=0` or no data rows after `header_row_count`) — this favors
  keeping the card's LOCATION-summary text over silently truncating to a
  title-only markdown block. Not spec-mandated either way; flagging the
  choice for review.
- Per the design spec sec 5.3's "pathologically large note schedule is
  capped at a char budget with a logged truncation" — that char-budget cap
  was NOT implemented in this wave (out of the four explicit deliverables
  given); every located table's full grid is rendered as-is. Worth a
  follow-up if a real annual report's largest note schedule turns out to
  blow the LLM's context budget.
