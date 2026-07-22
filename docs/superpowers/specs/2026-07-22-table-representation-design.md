# TR-1 — Table Representation for General Annual-Report Extraction

- **Status:** Designed — 2026-07-22. Makes table extraction general across all
  NSE/BSE companies with user-extensible fields, and correct for the
  standalone/consolidated duality. Base: `main` == `60fbed1`.
- **Motivation:** the layer attribution over the last e2e run showed the
  remaining accuracy ceiling is the **LLM layer** (wrong row, wrong column,
  sign, wrong table), not retrieval. Every one of those traces to the same
  root: the pipeline **flattens** the canonicalized table grid into text
  (`t{i}r{r}` row chunks, `Details=X, Details=Y` when Azure DI loses the
  header row) and asks the LLM to reason over the flattened copy — while a
  clean grid sits one artifact upstream.
- **Depends on / builds on:**
  - `canonicalize_table` (`core/table_grid.py`) — already produces the grid
    (spans expanded, nested headers joined to `" / "` paths, per-cell page).
    This is the source of truth this spec stops discarding.
  - `find_consolidated_start_page` / `filter_candidates` (EX-1, merged) — the
    consolidated page-boundary filter, which this spec **generalizes** into a
    basis dimension.
  - RT-9 BM25 + table-card-friendly Milvus schema (`fdi_chunks_v2`).
  - `find_concept_rows` / ontology (`core/tables.py`, `core/ontology.py`) —
    the deterministic path, **out of scope here** (validator is a later spec).

## 1. Governing principle

**Store structure once; flatten only to LOCATE; render structured to REASON.**

```
INGEST                          LOCATE                     REASON
structure artifact              Milvus                     extract_unit
  grid (source of truth)    →   one card per table      →  fetch grid by id
  + basis map                    + basis metadata           → render markdown
                                                             → LLM reasons
```

Three concerns, cleanly separated, where today extraction reasons over the
flattened LOCATE representation.

## 2. Scope

**In scope (P1–P4):**
- P1 — a `basis` stage: standalone/consolidated region segmentation → a basis
  map; generalizes the EX-1 page-boundary filter.
- P2 — table cards: `chunks` emits one card per table (replacing `t{i}r{r}`
  row chunks); `index` stores basis/statement metadata.
- P3 — grid → markdown rendering with resolved headers, units, identity;
  header-repair for collapsed Azure-DI headers; wired into extraction.
- P4 — schema v3.2 per-unit `basis_preference`; basis-aware retrieval.

**Explicitly OUT of scope:**
- **P5 deterministic validator** (`find_concept_rows` cross-check) — deferred
  to a later spec, per the product decision. Not built here.
- **Figure/chart vision parsing** — figures are tagged with basis (from
  caption) and treated as narrative corroboration; a field whose only source
  is a chart is a known gap, not addressed here.
- Reranker changes; the dense/BM25 fusion; the LLM/model choice.

## 3. Basis as a first-class dimension (P1)

Every listed company **with subsidiaries** files two near-duplicate statement
sets — standalone (parent) and consolidated (group) — with different numbers.
The EX-1 filter "drop everything at/after page 133" encodes three assumptions
that fail across companies: standalone-first ordering, schema-wants-standalone
only, and a reliable single page cutoff. The general model treats **basis like
period**: a labeled attribute carried through the pipeline, not a filter that
deletes half the report.

### 3.1 The `basis` stage

A pure, deterministic stage:

```
basis  (deps: ast, provenance)  ->  basis artifact
  { schema_version: 1,
    regions: [{start_page, end_page, basis}],   # sorted, non-overlapping
    default_basis }                              # basis for pages in no region
```

`basis` ∈ `{standalone, consolidated, unknown}`. Detection anchors on the
**regulator-mandated, company-invariant** headings (SA-700 / Schedule III):

- `"independent auditor's report on the consolidated financial statements"`
  (strongest anchor — every consolidated section opens with it),
- `"independent auditor's report on the standalone financial statements"`,
- `"consolidated balance sheet"` / `"consolidated statement of profit and
  loss"` / `"consolidated ... cash flow"`, and their standalone counterparts.

Algorithm: scan AST section headings (with their provenance page) in reading
order for these anchors; each anchor opens a region of its basis that runs
until the next anchor of a different basis or end of document. A document with
no consolidated anchor is **standalone-only** → one region, `standalone`. The
`default_basis` covers front/back matter (narrative) → `unknown`.

`page_basis(basis_artifact, page) -> basis` is a pure lookup any later stage
uses. This subsumes `find_consolidated_start_page`: "the first consolidated
region's start page" is exactly the old boundary, now one value derived from a
richer model.

### 3.2 Tagging

`page_basis` stamps a `basis` onto: every table (in the `chunks`/card path),
every note (via its owning section), every figure (from its caption / page).
The tag rides on the table card's Milvus metadata and is available at
extraction.

**Edge cases (all handled, not guessed):**
- standalone-only filing → no consolidated region → a `consolidated` field
  returns `null` with a stated reason, never a silent miss;
- reverse / interleaved order → anchored on headings, not position;
- a table in `unknown` (front-matter) → tagged `unknown`, surfaced to both
  bases rather than dropped.

## 4. Storage: grid stays, chunks become cards (P2)

### 4.1 `structure` artifact — unchanged source of truth

`canonicalize_table`'s grid (resolved `column_headers`, expanded `grid`,
`cell_pages`) is already stored per table. This spec **stops copying a
flattened version into the reasoning path**; the grid stays authoritative,
addressed by a stable `table_id = f"t{i}"` (the existing convention).

### 4.2 `chunks` stage v3 — one card per table

**Replace** the per-row `t{i}r{r}` chunks (~1,985 on the STTL report) with
**one card per table** (~257). Section (`s{i}`) chunks are unchanged. A card's
text is the LOCATION summary — findable, compact, no values:

```
[Balance Sheet | Standalone | Note 3 | ₹ in Lakhs | p.91]
Columns: As at 31-03-2024 | As at 31-03-2023
Rows: Property plant and equipment; Capital work-in-progress;
      Investments; Trade receivables; Unbilled revenue; ...;
      Total assets; Total equity; Total equity and liabilities
```

- **Rationale for replace, not augment:** per-row fragments are what flooded
  the reranker (the reason table injection was removed pre-EX-1). Cards locate
  at table granularity; the full grid is fetched on hit, so a value is still
  reached via its row label. Fewer Milvus rows, no flooding.
- The card carries `table_id`, `basis`, `statement_type` (from caption/section
  when derivable, else `unknown`), `note_no` (parsed from caption when
  present), `page` as metadata.
- `statement_type` heuristic: match the caption/owning-section heading against
  the primary-statement anchors (`balance sheet`, `statement of profit and
  loss`, `cash flow`); else `unknown`. Never blocks — `unknown` is valid.

### 4.3 `index` stage v4 — card metadata

The Milvus row for a card gains `table_id`, `basis`, `statement_type`,
`note_no` alongside the existing `text`/`doc_id`/`chunk_id`/`heading`/`page`.
`section_type` stays `None` for cards. Version bump re-runs index (DAG is
content-addressed); embeddings for section chunks are reused, cards are
newly embedded (their text is new).

## 5. Rendering + header-repair (P3)

### 5.1 grid → markdown

A pure `render_table_markdown(grid, *, identity) -> str`:

```
Table: Balance Sheet — Standalone — Note 3 — ₹ in Lakhs — page 91

| Particulars | As at 31-03-2024 | As at 31-03-2023 |
| --- | --- | --- |
| Property, plant and equipment | 2,126.88 | 2,016.80 |
| Unbilled revenue | 2,658.21 | 1,982.40 |
| Total assets | 16,427.37 | 14,213.08 |
```

Markdown is the default: the best-supported table format in LLM training, and
faithful here because `canonicalize_table` already expanded spans and joined
nested headers. `identity` = statement_type + basis + note_no + units + page,
carried on the card. The identity line is what kills wrong-*table* confusion
(share-count "Total assets" vs balance-sheet "Total assets"); the real column
headers kill wrong-*column* (the `Details=X` collapse). Units (`₹ in Lakhs`)
are detected from the table caption / near-table text; `unknown` when absent.

### 5.2 header-repair

A pure `repair_headers(grid) -> grid` run before render. When
`header_row_count == 0` or `column_headers` collapse to blanks/duplicates
(the Azure-DI failure that yields `Details, Details`):
1. detect period patterns in the first data row(s) — dates (`31-03-2024`),
   years (`2024`, `FY24`, `2023-24`) — and promote them to column headers;
2. else synthesize positional headers: `Column 1 (most recent)`,
   `Column 2 (prior)` — Indian statements print current-then-prior
   left-to-right, so positional is reliable.
Pure, total, never raises; a well-formed grid passes through unchanged.

### 5.3 extraction wiring

`extract_unit`'s context assembly changes:
1. pool = table cards + section chunks (hybrid, basis-filtered — §6);
2. for each **card** hit → fetch its grid by `table_id` from the `structure`
   artifact (one batched read per document, not per hit) → `repair_headers`
   → `render_table_markdown`;
3. **section** hits contribute their text unchanged;
4. context = rendered tables + narrative → the LLM.

The whole located table is rendered (financial statements are bounded ~40
rows); a pathologically large note schedule is capped at a char budget with a
logged truncation, never silently.

## 6. Schema v3.2 + basis-aware retrieval (P4)

### 6.1 schema

Add a per-**unit** `basis_preference` ∈ `{standalone, consolidated, both}`,
default `standalone` when omitted (statutory/parent view; back-compatible with
every current unit, which implies standalone). Basis is a property of a whole
statement, not a line, so it lives on the unit, not the field.

`parse_field_specs` reads it onto `UnitSpec.basis_preference`; `FieldSpec`
carries it too (flattened, like the other unit members) so a caller holding a
field knows its basis. Schema `version` → `"3.2.0"`.

### 6.2 basis-aware retrieval

`extract_unit` filters/prefers candidates by the unit's `basis_preference`:
- `standalone` / `consolidated` → keep cards whose `basis` matches **or** is
  `unknown` (front-matter tables serve either); drop the opposite basis;
- `both` → keep all, and the render's identity line lets the LLM (and the
  output) distinguish; the field's value becomes basis-tagged.

The Milvus `basis` metadata makes this a filter expression, same mechanism as
`doc_id`. This **replaces** `filter_candidates`'s consolidated page cutoff —
which becomes `basis == standalone` scoping, derived from the basis stage
rather than a hardcoded page.

## 7. Data flow (end to end)

```
ocr → structure (grid) → ast
                       ↘ basis (regions)         [P1]
chunks v3: section chunks + table cards(basis,stmt,note)   [P2]
index v4: cards → Milvus (+ basis/stmt/note metadata)      [P2]

extract_unit(field, basis_preference):                     [P3,P4]
  pool = hybrid_search(cards + sections, filter basis)     [P4]
  for card hit: grid = fetch(table_id)
                render_table_markdown(repair_headers(grid)) [P3]
  context = rendered tables + section text
  → LLM → value (basis-tagged when 'both')
```

## 8. Error handling

Every new pure function (`page_basis`, card builder, `repair_headers`,
`render_table_markdown`, `basis_preference` parse) is **total** — no raise on
any input shape, degrade to a safe default (`unknown` basis, positional
headers, empty card). The `basis` stage degrades to a single `unknown` region
on malformed input, so extraction proceeds unscoped rather than failing — the
same never-raise posture as the rest of `fdi.core` / the stage layer. A
missing basis artifact at extraction → treat all tables as `unknown` (serve
both), never crash.

## 9. Phasing (one design, four plans)

Each phase is its own implementation plan. Dependencies drive the waves:

| Phase | What | Depends on | Wave |
|---|---|---|---|
| **P1** | `basis` stage + `page_basis` | — | 1 (parallel) |
| **P3-core** | `repair_headers` + `render_table_markdown` (new `core/table_render.py`, pure) | — | 1 (parallel) |
| **P4a** | schema v3.2 `basis_preference` + parse | — | 1 (parallel) |
| **P2** | `chunks` v3 cards + `index` v4 metadata | P1 | 2 |
| **P3-wire + P4b** | extraction render + basis-aware retrieval | P1, P2, P3-core, P4a | 3 |

Wave 1's three pieces are **file-disjoint** (new `core/basis.py`+`stages/
basis.py`; new `core/table_render.py`; `core/extractor.py`+schema JSON) — only
`stages/__init__.py` is shared (one register line), so parallel worktrees are
safe. Waves 2–3 are sequential (shared `core/retrieval.py`, `stages/
chunks.py`, `stages/index.py`, `pipeline/extractor.py`).

## 10. Testing

- **Pure/unit:** `page_basis` (region hit, standalone-only → all standalone,
  malformed → unknown); basis detection (anchors, reverse order, no
  consolidated); card builder (labels/metadata, unknown statement/basis);
  `repair_headers` (date/year promotion, positional fallback, well-formed
  pass-through); `render_table_markdown` (identity line, real headers, spans);
  `parse_field_specs` basis_preference default + explicit.
- **Integration:** `basis` stage over the real OCR (STTL → standalone region
  ends, consolidated region starts at the known page); `chunks` emits cards
  not row chunks; `index` stores basis; extraction renders markdown and
  basis-filters.
- **End-to-end (the proof):** re-run the e2e + layer attribution. Success =
  the wrong-column / wrong-table / sign misses (L3) drop, measured by reading
  output against the schema examples (no scoring script). Retrieval recall must
  not regress from RT-9's 91.3%.

## 11. Success criteria

1. `basis` stage segments the real report correctly (standalone vs
   consolidated regions), and `page_basis` reproduces the EX-1 boundary as a
   derived value.
2. `chunks` emits one card per table, zero `t{i}r{r}` row chunks; `index`
   carries basis/statement/note metadata.
3. Extraction feeds the LLM markdown tables with real headers + identity +
   units, not flattened `Details=X` text.
4. A unit's `basis_preference` selects the right region; `standalone` default
   is back-compatible; `both` returns tagged values.
5. On re-run, L3 wrong-column/wrong-table/sign misses measurably drop; recall
   holds ≥ RT-9's 91.3%.
