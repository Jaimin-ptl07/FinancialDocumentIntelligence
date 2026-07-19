# Phase 5 — Cross-page table stitching fix

- **Status:** Design — 2026-07-19. Sub-slice of Phase 5 of the ingestion improvement plan
  (`docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md`), covering only the
  cross-page table stitching item (closes **P2-4**). Phase 5's other items — blank-vs-populated cell
  signal + extractor prompt guardrail (P1-3), `SemanticStage` batching + expanded `SectionType`
  (P3-12), low-confidence numeric flagging — are separate, independently schedulable sub-slices and
  are **not** designed here.
- **Depends on:** **Phase 0** (`fd11ee3` — table `spans`/`bounding_regions`/`caption`/`footnotes`,
  per-cell `bounding_regions`) and **Phase 2** (`dedaed0` — `StructureStage` v2→3,
  `canonicalize_table` additively returns `cell_regions`/`cell_pages`). Does **not** depend on Phase 1,
  3, or 4 — `fdi.core.structure`/`fdi.pipeline.stages.structure` sit on their own branch off `ocr`
  (`deps=("ocr",)`), parallel to `normalize`/`ast`. Sibling of Phase 3b (graph table/figure nodes) and
  Phase 4 (values + reconciliation) in the plan's DAG — both parallel, independently mergeable.
- **SCOPE — touches only `fdi/core/structure.py`, `fdi/pipeline/stages/structure.py`, and their tests**
  (`test_merge_tables.py`, `test_structure_stage.py`). Does **not** touch `table_grid.py`'s
  canonicalization logic (this spec only *calls* `canonicalize_table` as a read-only helper — its
  header-resolution algorithm is unmodified, verified in §3.2). Does **not** touch
  `graph`/`ast`/`ontology`/`quality`/`retrieval` — those are Phase 3b/4's own tracks; see §6 for the
  cross-phase coordination note this phase's behavior change creates for Phase 3b.

## 1. Purpose

The audit measured `_can_merge`'s actual failure mode directly against the real STTL FY2023-24 filing:
cross-page stitching fires **0/261 (0%)**. The cause is precise — `_can_merge` requires the character
gap between table A's span end and table B's span start to be `<= 2`:

```python
gap = _span_start(b) - _span_end(a)                   # reading-order gap <= 2
if not 0 <= gap <= 2:
    return False
```

That gap **is** the page furniture sitting between the two tables in the linear content stream — a
real running header + page number, e.g. `"\n60\nAnnual Report 2023-2024\n"`, measures **~28-29
characters**. A 2-character budget cannot fit any real furniture on any real document; the threshold
is unreachable by construction, not by bad luck. Manual sampling of candidate table pairs on the real
doc found **~83% are genuine cross-page continuations** — director-attendance, remuneration, and
energy-disclosure schedules whose rows are currently split across two separate `structure["tables"]`
entries instead of being stitched into one.

**Naively loosening the threshold is unsafe.** The audit also found the specific case that breaks:
Note 2 "Fixed Assets", pages 155→156, where the current-year and prior-year depreciation schedules sit
in two separate table objects with a heading — `"PREVIOUS YEAR DETAILS"` — between them that Azure
mis-tagged as `role=pageHeader` (furniture) rather than a real content heading. `_only_furniture_between`
therefore already approves the gap as furniture-only, and the two tables have **matching
`column_count`** (the schedule's column shape repeats for both years) — so once the gap ceiling stops
being the accidental blocker, this pair would wrongly merge, splicing a prior-year schedule onto a
current-year one as if they were one continued table. **The fix must both (a) raise the unreachable
threshold so real furniture no longer blocks genuine continuations, and (b) add an independent
structural guard that blocks this specific false-merge shape, which the gap/furniture checks alone
cannot distinguish from a genuine continuation.**

**Closes P2-4** (cross-page stitching never fires) from the audit.

## 2. Scope decisions (autonomous; documented)

1. **The char-gap check becomes a generous sanity ceiling, not the primary discriminator; the primary
   gate is (and already was) `_only_furniture_between`.** Today's `gap <= 2` conflates two different
   questions: "is there content in the gap, and is all of it furniture?" (answered correctly today by
   `_only_furniture_between`, which scans every paragraph whose span falls in `[lo, hi)` and rejects
   the merge if any has a non-furniture role) and "is the gap short?" (the redundant, over-strict
   check that is actually unreachable). Once `_only_furniture_between` already proves every paragraph
   in the gap is furniture, an additional tight length cap adds no correctness value — it only
   *narrows* which real furniture spans qualify. The fix removes the tight cap and replaces it with
   `_MAX_FURNITURE_GAP_CHARS`, a new named constant defined in `fdi/core/structure.py`:
   ```python
   _MAX_FURNITURE_GAP_CHARS = 300
   ```
   **Rationale for 300:** the one directly measured real furniture instance is ~28-29 characters
   (footer/running-header pair). 300 is roughly 10x that single-instance measurement — generous enough
   to cover multi-line furniture (a page's footer *and* the next page's header, plus stray blank
   lines, which the single "\n60\nAnnual Report...\n" sample does not itself exercise) without being
   large enough to plausibly span real, unrelated body content between two tables that merely happen
   to be on adjacent pages with equal `column_count`. This is a **defensive backstop, not the fix
   itself** — the fix is trusting `_only_furniture_between`'s per-paragraph role scan over the whole
   gap, whatever its length. The exact constant is a documented estimate (the repo's own audit
   script/report that produced the "~28 chars" measurement is not itself checked in — see §7's
   real-doc regression) and should be revisited if the real-doc validation run (§7 item 3) finds gaps
   that are furniture-only but exceed 300 chars, or finds the current 300 is already looser than
   needed. **Negative gaps are still rejected** (`gap < 0` — defensive against malformed/overlapping
   span data, unchanged in spirit from today's `0 <=` floor).
2. **New structural guard: resolved `column_headers` must be compatible, not just `column_count`
   equal.** `column_count` equality is Microsoft's own original heuristic condition and stays
   (`a.get("column_count") != b.get("column_count")` — unchanged) — but the audit's own Fixed-Assets
   case proves `column_count` equality alone is not sufficient: the current-year and prior-year
   schedules share the same column shape by count. The new guard resolves each table's own column
   headers via `canonicalize_table` (the same header-joining algorithm `table_grid` already uses,
   unmodified — see §3.2) and requires them to match **when table B has a header of its own to
   compare**. This distinguishes the two candidate shapes correctly:
   - **Genuine continuation (the common case, ~83% of real candidates):** Azure's table-splitting
     does not restate the column header on a continuation page's table object — table B's cells
     simply have no `kind="columnHeader"` entries, so `canonicalize_table(b)["header_row_count"] == 0`
     and `column_headers` resolves to all-blank strings. This degenerate "no header of its own" shape
     is treated as **compatible by default** — B is trusting A's already-established header, which is
     exactly what a continuation slice is. The guard does not block this case.
   - **Fixed-Assets false merge:** whichever of the two tables is "B" in reading order (the prior-year
     schedule) has its own real header content — even if the paragraph-level "PREVIOUS YEAR DETAILS"
     heading was mis-tagged as furniture, the audit's finding is that the two schedules **differ in
     structure**, which surfaces in the tables' own resolved `column_headers` (different column
     labels, or a different `header_row_count`) once B is not the headerless case above. When B *does*
     carry its own header, this guard requires `canonicalize_table(a)["column_headers"] ==
     canonicalize_table(b)["column_headers"]` exactly — a mismatch blocks the merge, independent of
     whatever `_only_furniture_between` concluded about the mis-tagged heading between them.
   This is a genuine **fork on strictness**, flagged for sign-off in §6: exact list equality (chosen
   here) vs. a looser match (e.g., same `header_row_count` only, or a fuzzy/prefix comparison). Exact
   equality is the conservative choice — consistent with this module's existing "a missed merge is
   recoverable downstream; a wrong merge corrupts data" posture (SP-7b's own stated design principle,
   unchanged) — at the cost of also blocking a genuine continuation whose second slice happens to
   *restate* a header that differs even trivially (e.g. reformatted whitespace after
   `canonicalize_table`'s own `" / "`-joining, which already normalizes most of this). No real example
   of that failure mode is in the audit's findings; if the real-doc regression (§7 item 3) surfaces
   one, loosening to a subset/fuzzy match is the documented escape hatch.
3. **"Compatible first-row" (mentioned as a candidate guard in the plan) is not separately implemented
   — the header-compatibility guard already subsumes it for the measured case, and a real first-row
   heuristic has no signal to work from.** Azure's per-cell `kind` (`columnHeader` vs data) is already
   exactly what `header_row_count`/`column_headers` are built from; there is no additional font-style
   or emphasis signal to distinguish "genuinely different first row" from "differently-worded but
   still a header" — OCR schema Phase 0 explicitly dropped Azure's `styles` (bold/emphasis) as an
   omission (audit §2 OCR row: `styles` dropped), so a first-row-visual-weight heuristic is not
   possible with data the pipeline currently retains. Left as a documented gap, not implemented.
4. **`_merge_two` additively carries forward `a`'s `caption`/`footnotes`.** Independently-discovered
   gap while reviewing the merge path: `_merge_two` currently builds a brand-new dict listing exactly
   six keys (`row_count`, `column_count`, `spans`, `bounding_regions`, `cells`, `stitched`) — it does
   **not** carry forward `caption`/`footnotes`, two keys every table dict has carried since Phase 0
   (`fdi/core/ocr_schema.py`'s `normalize_ocr` always sets both, `caption: dict | None` and
   `footnotes: list`). This has been silently invisible until now because merges have never actually
   fired on a real document (0/261) — an *unmerged* table (`{**ordered[i]}`, a shallow copy of the
   original) already keeps its `caption`/`footnotes` untouched; only the *merged* output shape was
   ever missing them, and nothing has exercised that path in production. Since Phase 5 is the change
   that makes real merges happen for the first time, this becomes an observable regression the moment
   stitching fires unless fixed here. The owner of a merged multi-page table's caption is table A (the
   first physical page) — Azure attaches caption/footnote text to the table object it belongs to, and
   a continuation slice B does not carry its own duplicate caption — so the fix is `a`'s values,
   unconditionally, with the same `.get(..., default)` defensive posture the rest of `core` uses
   (`caption`: `None` default, `footnotes`: `[]` default, so an older/malformed input missing the key
   degrades rather than raises):
   ```python
   "caption": a.get("caption"),
   "footnotes": a.get("footnotes", []),
   ```
   This is in-scope (touches only `_merge_two` in `structure.py`) and directly relevant (table caption
   is audit row `#7` — "statement identification + restatement caveats" — Phase 4's future consumer;
   losing it silently on every stitched table would undercut that phase before it starts).
5. **Phase 2's `cell_regions`/`cell_pages` are verified — by inspection — to already survive
   `_merge_two` correctly; no code change is needed there, only a regression test.** `_merge_two`'s
   existing row-shift for B's cells is a dict-spread shallow copy:
   ```python
   b_cells = [{**c, "row_index": int(cast(int, c["row_index"])) + a_rows}
              for c in cast(list[dict[str, object]], b["cells"])]
   ```
   `{**c, "row_index": ...}` copies every key of the original cell dict — including `bounding_regions`
   (Phase 0/2's per-cell geometry) — and overrides only `row_index`. `a_cells` is not touched at all.
   `canonicalize_table` (called *after* `merge_cross_page_tables`, in `StructureStage.run`, unchanged)
   reads exactly this merged `cells` list to build `grid`/`cell_regions`/`cell_pages`, and its
   row-indexing already handles arbitrary `row_index` values correctly (that is the function's whole
   job). So a merged table's rows `0..a_rows-1` correctly carry A's original per-cell pages, and rows
   `a_rows..a_rows+b_rows-1` correctly carry B's — **already true today, unexercised only because no
   merge has ever fired on a real document.** Phase 5 adds the first test that actually proves this
   end-to-end (merge two tables with cells on different pages, then `canonicalize_table` the merged
   result, and assert `cell_pages` reflects the correct page per row) — closing the "was this ever
   actually tested" gap the audit implicitly raises by finding 0/261 merges, not a code defect.
6. **`fdi.core.structure` gains a new import of `fdi.core.table_grid.canonicalize_table`.** Verified
   against `backend/.importlinter`: the layers contract (`api | workers` → `pipeline` → `platform` →
   `core`) only constrains cross-**layer** imports; `core` is one flat layer with no internal
   sub-layering, and `table_grid.py` itself has zero imports beyond `collections.abc`/`typing` (no
   cycle risk). `structure.py` calling `canonicalize_table` — a pure function, no I/O — to resolve a
   candidate table's header shape during the merge decision keeps `merge_cross_page_tables` itself
   pure and self-contained (same `core`-purity guarantee the contract enforces), and reuses the
   **exact** header-joining algorithm `table_grid` already ships rather than re-implementing a second,
   possibly-divergent one. `stages/structure.py` already imports both modules today for its own
   purposes (calls `canonicalize_table` once per final stitched table after merging); this adds a
   second, earlier call inside the merge decision itself — recomputing `canonicalize_table` on
   candidate/`cur` tables during the pairwise scan, and once more on the final merged table in the
   stage, same as today. Cost is bounded: this is per-candidate-pair work over at most 261 real tables
   with modest cell counts, not a hot loop over a larger corpus dimension.
7. **Version bump: `StructureStage.version 3 → 4`; `structure` artifact's `schema_version` stays 3.**
   Per SP-2's content-addressed cache and the exact precedent Phase 2 already established for
   `ProvenanceStage` (§2.10 of `2026-07-19-ph2-provenance-design.md`: "only the *resolution algorithm*
   changed, not the output shape" → `schema_version` unchanged, `.version` bumped to force recompute):
   this phase changes `merge_cross_page_tables`'s and `_merge_two`'s *behavior* (which tables merge,
   and — per decision 4 — a previously-missing `caption`/`footnotes` on merged outputs, which is a
   correctness fix within the *existing* documented shape of a table dict, not a new key), not the
   `structure` artifact's declared top-level shape (`{schema_version, blocks, tables}}`, each table
   already documented as optionally carrying `caption`/`footnotes`/`stitched`/`grid`). `schema_version`
   stays **3**; `StructureStage.version: 3 → 4` is bumped regardless, because the cache key must change
   to force every previously-processed document's `structure` artifact to recompute — old cached
   structure output reflects the never-firing threshold and is simply stale, not wrong-shaped.
   **Cascade:** `chunks` (`deps=("ast","semantic","provenance","structure")`) and `quality`
   (`deps=("ast","structure")`) both depend directly on `structure` and recompute automatically via
   `input_hash`; `embedding`/`index` recompute transitively behind `chunks`. No code or version change
   needed in any of those stages for this phase — the established SP-2 propagation mechanism (already
   the pattern documented in both the Phase 2 and Phase 3 specs) is sufficient.
8. **No new external call, no new config.** `merge_cross_page_tables`, `_can_merge`,
   `_only_furniture_between`, `_merge_two`, and the new `_headers_compatible` all stay pure,
   synchronous, no-I/O functions in `fdi.core`. `import-linter`'s layering is unaffected (§2.6).

## 3. Architecture

### 3.1 `fdi.core.structure` — gap ceiling

```python
_FURNITURE = {"pageHeader", "pageFooter", "pageNumber"}
_MAX_FURNITURE_GAP_CHARS = 300  # sanity backstop; _only_furniture_between is the real gate (§2.1)
```

```python
def _can_merge(a: Mapping[str, object], b: Mapping[str, object],
               paragraphs: list[Mapping[str, object]]) -> bool:
    la, fb = _last_page(a), _first_page(b)
    if la is None or fb is None or fb != la + 1:          # adjacent pages only (unchanged)
        return False
    if a.get("column_count") != b.get("column_count"):    # equal columns (unchanged)
        return False
    gap = _span_start(b) - _span_end(a)
-   if not 0 <= gap <= 2:
+   if not 0 <= gap <= _MAX_FURNITURE_GAP_CHARS:
        return False
-   return _only_furniture_between(a, b, paragraphs)
+   if not _only_furniture_between(a, b, paragraphs):
+       return False
+   return _headers_compatible(a, b)
```

`_only_furniture_between` itself is **unchanged** — its `[lo, hi)` window logic already scales
correctly to a wider gap; it was never the unreachable part.

### 3.2 `fdi.core.structure` — `_headers_compatible` (new, pure, calls `table_grid`)

```python
from fdi.core.table_grid import canonicalize_table

def _headers_compatible(a: Mapping[str, object], b: Mapping[str, object]) -> bool:
    """True iff merging a<-b would not splice together two tables whose OWN
    resolved column headers disagree. A genuine continuation's second slice
    normally has no header cells of its own (Azure does not restate the
    header across a table split) -- that degenerate all-blank case is
    compatible by default. A table that DOES carry its own header (e.g. a
    same-shaped-by-column-count but semantically different schedule, such as
    a prior-year comparative table) must match `a`'s resolved header exactly."""
    b_grid = canonicalize_table(b)
    b_headers = cast(list[str], b_grid["column_headers"])
    if b_grid["header_row_count"] == 0 or not any(h.strip() for h in b_headers):
        return True
    a_grid = canonicalize_table(a)
    return cast(list[str], a_grid["column_headers"]) == b_headers
```

`a`/`b` are the same raw-shaped table mappings `_can_merge` already receives (`row_count`,
`column_count`, `cells`, ...) — exactly what `canonicalize_table` expects as input (it is already
called on this same shape, once per final table, in `StructureStage.run`). No signature change to
`canonicalize_table` itself (§ scope: `table_grid.py`'s canonicalization logic is unmodified).

### 3.3 `fdi.core.structure` — `_merge_two` carries `caption`/`footnotes`

```python
def _merge_two(a: Mapping[str, object], b: Mapping[str, object]) -> dict[str, object]:
    a_rows = int(cast(int, a["row_count"]))
    b_rows = int(cast(int, b["row_count"]))
    a_cells = list(cast(list[dict[str, object]], a["cells"]))
    b_cells = [{**c, "row_index": int(cast(int, c["row_index"])) + a_rows}
               for c in cast(list[dict[str, object]], b["cells"])]
    return {
        "row_count": a_rows + b_rows,
        "column_count": a["column_count"],
        "spans": list(cast(list[object], a.get("spans", [])))
                 + list(cast(list[object], b.get("spans", []))),
        "bounding_regions": list(cast(list[object], a.get("bounding_regions", [])))
                            + list(cast(list[object], b.get("bounding_regions", []))),
        "cells": a_cells + b_cells,
        "stitched": True,
+       "caption": a.get("caption"),
+       "footnotes": a.get("footnotes", []),
    }
```

`cell_regions`/`cell_pages` need **no code change** here (§2.5) — they do not exist on the raw table
dict at merge time; they are computed by `canonicalize_table` after `merge_cross_page_tables` returns,
from exactly the `cells` list this function already assembles correctly.

### 3.4 `fdi.pipeline.stages.structure.StructureStage` — version bump only

```python
class StructureStage:
    name = "structure"
-   version = 3
+   version = 4  # Phase 5 -- unreachable stitching threshold fixed; column-header
+                # structural guard added; merged tables carry caption/footnotes
    deps: tuple[str, ...] = ("ocr",)

    async def run(self, ctx: StageContext) -> object:
        ...  # unchanged -- merge_cross_page_tables / canonicalize_table calls as today
        return {
            "schema_version": 3,  # unchanged -- shape unchanged, see §2.7
            "blocks": blocks,
            "tables": stitched,
        }
```

## 4. Error handling

- `_can_merge`: `gap < 0` (malformed/overlapping span data) → unchanged rejection. `gap >
  _MAX_FURNITURE_GAP_CHARS` → rejected, same defensive posture as today's now-removed `gap <= 2`
  ceiling, just a larger, justified number (§2.1). Missing `column_count` on either table → `None !=
  None` is `False` in the sense both are `None` and considered "equal" only if literally the same
  Python value — unchanged behavior from today (`a.get("column_count") != b.get("column_count")`).
- `_headers_compatible`: a table `b` with malformed/empty `cells` → `canonicalize_table(b)` already
  degrades gracefully (existing behavior, unmodified — `n_rows`/`n_cols` from `row_count`/
  `column_count`, defaulting to `0`, produces an empty grid, `header_row_count == 0`) → treated as the
  headerless/compatible case (§2.2), never raises. A table `a` similarly malformed → its
  `canonicalize_table(a)["column_headers"]` resolves to whatever `table_grid`'s existing degrade path
  produces (e.g. all-blank), compared for equality like any other value — no special-casing needed,
  `canonicalize_table` itself already never raises on malformed input (existing contract, unchanged).
- `_merge_two`: `a` missing `caption`/`footnotes` (an older cached table shape predating Phase 0, or a
  hand-built test fixture) → `.get("caption")` → `None`, `.get("footnotes", [])` → `[]`, matching
  `ocr_schema.normalize_ocr`'s own defaults for a table with no real caption/footnotes. Never raises.
- `merge_cross_page_tables`: no change to its own error posture — a table missing `spans`/
  `bounding_regions` still passes through as non-mergeable (existing `_span_start`/`_span_end`/
  `_pages` default-to-empty behavior, unmodified).
- `StructureStage.run`: unchanged — `canonicalize_table(t)` is still called once per final stitched
  table after merging, same as today; the new in-merge calls to `canonicalize_table` inside
  `_headers_compatible` are additional but use the identical, already-hardened function.

## 5. Testing

- **Unit `test_merge_tables.py`** (extend):
  - `test_realistic_furniture_gap_now_merges`: two tables on adjacent pages, equal `column_count`, a
    single furniture paragraph between them whose span is **~28 chars** (mirroring the audit's
    measured `"\n60\nAnnual Report 2023-2024\n"`) — under the old `gap<=2` this could never merge;
    under the new ceiling it does. This is the direct regression proof for the audit's headline
    finding (0/261 → nonzero).
  - `test_gap_exceeding_sanity_ceiling_does_not_merge`: a furniture-only gap of, e.g., 1,000 chars
    (every paragraph in the window has a furniture role) — still rejected, proving
    `_MAX_FURNITURE_GAP_CHARS` remains an active backstop, not a removed check.
  - `test_existing_small_gap_cases_unchanged`: rerun (not modify) the existing `test_clean_continuation
    _merges` (gap of 2), `test_column_mismatch_does_not_merge`, `test_body_paragraph_between_does_not_
    merge`, `test_non_adjacent_pages_do_not_merge`, `test_three_page_run_merges_to_one`,
    `test_unrelated_tables_pass_through` byte-for-byte — all pass unchanged (their gaps are within both
    the old and new ceiling, so this phase does not alter their outcome; the header-compatibility guard
    does not block them either since their tables' cells never populate `kind="columnHeader"` beyond
    what column-count already governs — verify this explicitly per case, not assumed).
  - `test_mismatched_resolved_headers_blocks_merge_even_with_furniture_only_gap_and_equal_columns`:
    **the Fixed-Assets regression** — two tables, adjacent pages, equal `column_count`, only a
    furniture-role paragraph between them (reproducing the mis-tagged-heading shape), but table B
    carries its own `kind="columnHeader"` cells whose resolved `column_headers` differ from A's →
    `merge_cross_page_tables` must **not** merge them. This is the core proof the false-merge stays
    blocked once the gap ceiling stops being the accidental protector.
  - `test_headerless_continuation_with_furniture_gap_still_merges`: table B has **no** header cells at
    all (the common, ~83%-of-candidates real shape) — furniture-only gap, equal columns → merges,
    proving the header guard does not regress the common case it must not block.
  - `test_restated_matching_header_still_merges`: table B *does* carry header cells, but they resolve
    to the **same** `column_headers` as A (a continuation that happens to restate its header
    identically) → still merges — proves the guard is a mismatch check, not a "B must never have a
    header" check.
  - `test_merge_carries_forward_captions_and_footnotes_from_first_table`: `a` has a non-`None` caption
    and non-empty footnotes, `b` has different (or absent) ones → merged output's `caption`/
    `footnotes` equal `a`'s, unmodified by `b`'s.
  - `test_merged_cell_regions_and_cell_pages_reflect_correct_source_page_per_row` (new, may live in
    `test_merge_tables.py` or a small new file importing both `merge_cross_page_tables` and
    `canonicalize_table`): table A's cells carry `bounding_regions` with `page_number: 1`, table B's
    with `page_number: 2`; merge, then `canonicalize_table` the merged result; assert
    `cell_pages[r]` is `1` for `r < a_rows` and `2` for `r >= a_rows` for every column — the concrete,
    previously-never-exercised proof of §2.5.
- **Integration `test_structure_stage.py`** (extend):
  - Update the existing `structure_key = artifact_key(doc.id, "structure", 3, ...)` call to `4`
    (`StructureStage.version` literal — the one stale literal this phase's version bump touches in
    this file, confirmed by grep, §5 gates below).
  - A new end-to-end case with realistic furniture (a `"pageFooter"`/`"pageNumber"`-role paragraph of
    ~28 chars between two equal-column adjacent-page tables with no header cells on the second) →
    asserts the stage's `tables` list has **one** stitched entry with the combined row count — the
    real-shaped-input proof that the fix works through the full `ocr → structure` chain, not only at
    the unit level.
  - A companion case reproducing the Fixed-Assets shape (equal `column_count`, furniture-tagged
    paragraph between, second table has its own differing header cells) → asserts the stage's `tables`
    list still has **two** separate entries — the false-merge stays blocked end-to-end through the
    stage, not just in the pure function.
- **Gates:** grep for every stale `"structure"` artifact-key / `StructureStage.version ==` /
  `schema_version` literal this phase's version bump touches (confirmed: `test_structure_stage.py`'s
  `artifact_key(doc.id, "structure", 3, ...)` is the one hardcoded structure-version literal in the
  test suite; every other file only imports/registers `StructureStage()` without asserting its version
  number, per repo-wide search). Full suite green; `ruff`, `mypy --strict`, `lint-imports` (still
  1-kept/0-broken — `core` still importing nothing from `pipeline`/`platform`; the new
  `structure.py → table_grid.py` import is intra-`core`, unconstrained by the layers contract, §2.6).

## 6. Explicitly deferred (and the fork a human should decide)

- **FORK — exact `column_headers` equality (chosen, §2.2) vs. a looser/fuzzy match.** Exact equality
  is the conservative default consistent with this module's "a missed merge is recoverable; a wrong
  merge corrupts data" design principle (SP-7b), and is what the audit's own framing ("require ... to
  match") most directly supports. The risk it accepts: a genuine continuation whose second table slice
  restates a header with any difference at all (extra whitespace beyond what `" / "`-joining already
  normalizes, a trivial rewording) would be wrongly blocked from merging — recoverable (the two halves
  still exist as separate, correct tables; only the stitching benefit is lost for that one pair), not
  data-corrupting. No such case appears in the audit's measured findings; if the real-doc regression
  (§7 item 3) finds one, the documented escape hatch is loosening to a `header_row_count`-only match or
  a normalized/fuzzy header comparison — flagged here rather than pre-built, since building the
  looser rule without a real failing example risks solving the wrong problem.
- **NOT in scope — `source_indices` (raw ↔ structure table-index bridge).** The already-written Phase 3
  spec (`2026-07-19-ph3-table-chunks-design.md` §2 decision 6 / §3.2) proposes `merge_cross_page_tables`
  additively emit `source_indices: list[int]` per output table, explicitly so that `get_tables`/RT-6's
  `table_index`, the `chunks` artifact's `t{i}` ids, and future graph table nodes stay aligned once
  real merges start happening — but that spec's decision 6 also notes the bridge is currently *inert*
  because "stitching threshold is unreachable per the audit... `structure["tables"]` is currently a
  1:1, same-order copy of `ocr["tables"]`." **This phase is exactly what makes that assumption false
  for the first time** — once Phase 5 ships, `structure["tables"]` will genuinely shrink (fewer entries
  than `ocr["tables"]`) whenever a real merge fires, and any downstream consumer indexing tables by
  raw/structure position without the bridge will silently shift. Implementing `source_indices` is
  **not** this phase's job (it is Phase 3b's, and `table_grid.py`/`ast`/`graph`/`retrieval` are
  explicitly out of scope here) — but it is a **sequencing risk worth flagging**: Phase 3b's
  `source_indices` work should land at or before the point Phase 5's stitching fix reaches a real
  corpus, or `get_tables`'/RT-6's `table_index` and any Phase-3a `t{i}r{j}` chunk ids computed against
  a pre-Phase-5 `structure` artifact will not match a post-Phase-5 re-run's numbering for any document
  containing a genuine merge. This is a coordination note for whoever schedules Phase 3b relative to
  this phase's merge, not a code change owned here.
- **NOT in scope — the other Phase 5 items** (blank-vs-populated cell signal + extractor prompt
  guardrail closing P1-3; `SemanticStage` batching + expanded `SectionType` closing P3-12;
  low-confidence numeric flagging using Phase 0's `low_confidence_words`) — separate sub-specs.
- **NOT in scope — horizontal (left/right split) table merge, cross-page section/reading-order
  stitching, repeated-text-based furniture detection** — these were already explicitly deferred by
  SP-7b (7b-continuation / 7c) and remain out of scope here; this phase only fixes the vertical-merge
  heuristic's unreachable threshold and adds the structural guard, it does not expand the heuristic's
  coverage.

## 7. Done criteria

1. `_can_merge`'s gap check is replaced with `0 <= gap <= _MAX_FURNITURE_GAP_CHARS`
   (`_MAX_FURNITURE_GAP_CHARS = 300`, documented rationale §2.1); `_only_furniture_between` is
   unchanged and remains the primary correctness gate.
2. `_can_merge` additionally calls a new `_headers_compatible(a, b)` (pure, calls
   `table_grid.canonicalize_table`, unmodified) that blocks a merge when table B carries its own
   header cells whose resolved `column_headers` differ from A's, while treating a headerless B (the
   common genuine-continuation shape) as compatible by default (§2.2).
3. `_merge_two` additively carries forward `a`'s `caption`/`footnotes` into the merged table dict
   (§2.4) — a previously-invisible gap now made observable by real merges firing for the first time.
4. `cell_regions`/`cell_pages` (Phase 2) are verified — by code inspection and a new regression test
   (§5) — to already survive `_merge_two` correctly via its existing per-cell dict-spread row-shift; no
   code change was needed in `_merge_two`/`canonicalize_table` for this, only the missing test.
5. `StructureStage.version: 3 → 4`; `structure` artifact's `schema_version` stays `3` (behavior change,
   not a shape change — mirrors Phase 2's `ProvenanceStage` precedent, §2.7). `chunks`/`quality`
   (and transitively `embedding`/`index`) recompute automatically via the existing content-addressed
   cascade; no code or version change needed in any of those stages.
6. Unit tests cover: the realistic-furniture-gap merge (the headline regression proof), the sanity
   ceiling still rejecting an excessive gap, all pre-existing merge/no-merge cases unchanged, the
   Fixed-Assets false-merge staying blocked, the headerless-continuation and restated-matching-header
   cases still merging, caption/footnotes threading, and the cell_regions/cell_pages-through-merge
   proof. Integration tests cover the same false-merge-blocked / genuine-merge-fires distinction
   end-to-end through `StructureStage`. All stale `"structure"`-artifact-key / version literals
   updated (confirmed: one in `test_structure_stage.py`). All four gates green (`ruff`,
   `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); `core` stays pure — the one new import
   (`structure.py → table_grid.py`) is intra-`core`, no new I/O, no new external call, no new config.
7. **Real-doc regression (scratchpad, not committed, same posture as Phase 0/1/2/3's real-doc
   checks):** re-run `ocr → structure` on the real STTL FY2023-24 document; confirm (a) cross-page
   stitching now fires for **more than 0/261** tables, with the merged count in the plausible range
   suggested by the audit's ~83%-genuine-candidate sampling (a specific target count is not asserted —
   the audit's 83% figure was over a *sampled candidate set*, not all 261 tables, so the expected
   absolute merge count depends on how many real candidate pairs exist in the full document; the check
   is "meaningfully nonzero and consistent with hand-inspection of a handful of merged results," not a
   fixed number); (b) the Note 2 "Fixed Assets" current-year/prior-year schedule pair (pages 155→156)
   is confirmed **still two separate `structure["tables"]` entries**, not merged — the direct proof the
   structural guard holds on the real document, not only on synthetic fixtures; (c) a hand-picked
   sample of newly-merged tables (director-attendance, remuneration, or energy-disclosure schedules)
   is inspected to confirm the merged row content is coherent (row order preserved, no duplicated
   header row, `cell_pages` correctly spans both source pages) rather than merely "a merge fired."
