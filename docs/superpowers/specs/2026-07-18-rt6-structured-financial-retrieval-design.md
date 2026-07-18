# RT-6 — Structured Financial Retrieval (table grids + concept→number linking)

- **Status:** Implemented — 2026-07-18. Sixth sub-project of the retrieval layer (`docs/architecture/
  retrieval-roadmap.md`), Wave B (parallel with RT-2, RT-3, RT-5 — independent of them, depends only
  on RT-1 for roadmap sequencing; in practice RT-6 depends directly on ingestion's `structure` +
  `fdi.core.ontology`, not on anything RT-1 shipped). All done criteria (§7) verified; see
  `docs/superpowers/plans/2026-07-18-rt6-structured-financial-retrieval.md` for the executed plan and
  the retrieval roadmap's RT-6 row for the RT-7 handoff.
- **Depends on (ingestion, all complete):**
  - **SP-7b/SP-7c** (`structure` artifact, `StructureStage`, `deps=("ocr",)`, `version=2`:
    `{schema_version:2, blocks:[...], tables:[{row_count, column_count, cells, spans,
    bounding_regions, stitched, grid:{n_rows, n_cols, header_row_count, column_headers:[str],
    grid:[[str]]}}]}` — `fdi.core.table_grid.canonicalize_table` is what produces `grid`: spanning
    cells expanded into a full rectangular matrix, leading `columnHeader`-kind rows joined into one
    `column_headers[c]` path per column. This is the retrievable table store RT-6 queries.).
  - **SP-11** (`fdi.core.ontology`: `CanonicalConcept` enum — `REVENUE`, `COST_OF_GOODS_SOLD`,
    `GROSS_PROFIT`, `OPERATING_EXPENSE`, `NET_PROFIT`, `TOTAL_ASSETS`, `TOTAL_LIABILITIES`, `EQUITY`,
    `CASH`, `UNKNOWN`; a normalized-label synonym dictionary `_SYNONYMS: dict[str, CanonicalConcept]`;
    `map_label(label) -> CanonicalConcept` (normalize + exact dict lookup); `map_document(ast)` maps
    AST headings/paragraphs — its own docstring flags the gap RT-6 exists to close: *"a real OCR line
    item like 'Revenue  12,345' will not match the dictionary... Numeric-value extraction + real
    line-item parsing is a follow-on."* RT-6 is that follow-on, but over table grids, not AST text —
    a table's row-label cell (column 0) is already a bare label with no trailing number baked in, so
    the matching problem is "which concept does this label mean", not "strip the number first".).
  - **RT-1** (roadmap sequencing only — `chunks`/`resolve_chunk` are NOT used by RT-6; tables are
    resolved straight from the per-document `structure` artifact, the same read shape as `resolve_
    chunk` uses for `chunks` — `RegistryRepository.latest_run` → `ArtifactStore.get_json`).
  - **SP-2** (stage/DAG framework — used only for the executor pre-check that `structure` already
    chains automatically; RT-6 adds no new pipeline **stage**, see decision 1).

## 1. Purpose

The ingestion layer already canonicalizes every table into a rectangular grid with resolved column
headers (`structure[…].tables[i].grid`) and already maps bare labels to `CanonicalConcept`s
(`fdi.core.ontology`) — but the two have never been joined, and no route can read either. Two gaps:

1. **No retrieval tool reads `structure` at all.** A table's canonicalized grid — the one artifact
   in the whole ingestion output that already looks like a spreadsheet — is invisible to every
   retrieval consumer (`/ask`'s eventual agent included) until something exposes it per document,
   optionally filtered.
2. **Numbers in table grids are unlinked to `CanonicalConcept`s** (the roadmap's gap (e), closed
   here). `fdi.core.ontology.map_document` deliberately does not attempt this — see its docstring
   quoted above — because AST paragraph text mixes a label and a number in one string. A table's
   grid does NOT have this problem: the label lives in column 0, the numbers live in the other
   columns, already separated by Azure DI's cell model. RT-6 is the "deferred numeric extraction"
   the roadmap and `map_document`'s docstring both point at: (a) parse a table's numeric cell
   strings into real numbers — thousands separators, parenthetical negatives, currency, percent,
   blanks — and (b) match a concept's synonyms against row-label cells to find which rows are
   "the" revenue/assets/equity/etc. row, returning every numeric column for that row (never
   guessing which period is "the answer" — that judgment call is the caller's, not RT-6's).

Thin slice, pure-core-first, same shape as every ingestion/retrieval SP: two pure functions in a new
`fdi.core.tables` module (`parse_numeric`, `find_concept_rows`) with exhaustive unit tests, two small
additions to `fdi.core.ontology` they lean on, a pipeline orchestrator module (`fdi.pipeline.tables`)
that does the I/O (read `structure`, no new artifact), and two GET routes.

## 2. Scope decisions (autonomous; documented)

1. **No new pipeline stage, no new artifact.** `get_tables`/`concept_lookup` read the EXISTING
   `structure` artifact on demand — the same "read a cached artifact at request time" shape
   `resolve_chunk` (RT-1) and `resolve_entities` (SP-16) already use, not a DAG stage. Table
   canonicalization and numeric parsing are cheap, pure, and per-request; persisting a derived
   `tables`/`concepts` artifact would be a **premature cache** with a real staleness cost (any
   `parse_numeric` bug fix or synonym-dictionary addition would need every document's cached
   artifact reprocessed to take effect, vs. an on-demand read picking it up immediately). **Flagged
   as a genuine fork a human should confirm** (§8) — if profiling later shows the join is hot, a
   `tables`/`concepts` stage can be added without changing `fdi.core.tables`' pure functions at all,
   only their caller.
2. **`concept_lookup` does NOT read the `ontology` artifact.** The per-document `ontology` artifact
   (`OntologyStage`, `deps=("ast",)`) maps AST headings/paragraphs to concepts — a different corpus
   (section text) than table cells. RT-6 reuses `fdi.core.ontology`'s **synonym data** (`_SYNONYMS`,
   normalization) as a pure library, not the `ontology` **artifact**. This means `concept_lookup`
   depends on exactly one upstream artifact per document: `structure`. (Documented so a future reader
   does not go looking for an `ontology` dependency that was deliberately not added.)
2b. **`fdi.core.tables` imports `fdi.core.ontology` (core→core).** The roadmap explicitly sanctions
   this: *"reusing the ontology synonym dict from core is acceptable since both are core and pure"*
   (`retrieval-roadmap.md` cross-cutting decision 4 lineage). The alternative — the caller passes a
   `list[str]` of synonyms into `find_concept_rows`, decoupling `fdi.core.tables` from `fdi.core.
   ontology` the way RT-1 decoupled `fdi.core.retrieval` from `fdi.core.ast`/`semantic`/`provenance`
   — was considered and rejected: RT-1's inputs (`ast`, `semantic`, `provenance`) are three DIFFERENT
   artifacts with no shared vocabulary, so decoupling to plain `Mapping`s was the only sane contract.
   Here `find_concept_rows` and `map_label` need to agree on ONE normalization rule (lowercase, strip
   punctuation, collapse whitespace) — passing that logic twice (once inside `ontology.py`, once
   reimplemented by whatever calls `find_concept_rows`) is a duplication bug waiting to happen, not a
   decoupling win. `fdi.core.ontology` gains two small **public** additions for this (decision 3);
   nothing already public changes shape.
3. **`fdi.core.ontology` gains `normalize_label` (promoted from `_normalize_label`) and
   `synonyms_for(concept) -> list[str]`.** `_normalize_label` is renamed to a public `normalize_label`
   (its one caller, `map_label`, is updated in place; grepped — no test or other module imports the
   private name, so this is a safe rename, not a compatibility break). `synonyms_for` is new: `[k for
   k, v in _SYNONYMS.items() if v is concept]`, i.e. every normalized dictionary key that maps to a
   given `CanonicalConcept`, in declaration order. Both are pure, both stay inside `fdi.core.ontology`
   (no new module) since `_SYNONYMS` is that module's data — `fdi.core.tables` calls them, it does not
   duplicate them.
4. **Row-label matching is normalized WORD-TOKEN SUBSEQUENCE containment, not whole-label exact
   match.** `map_label`'s exact match (`_SYNONYMS.get(normalized, UNKNOWN)`) is right for a bare AST
   heading ("Revenue") but wrong for a real financial statement row label, which routinely carries a
   qualifier the dictionary's terse synonym doesn't: "**Total** revenue from operations", "Revenue
   **(net)**", "**Less:** Cost of materials consumed". `find_concept_rows` therefore does NOT call
   `map_label` on the row label; it normalizes the row label the same way (`normalize_label`, decision
   3) and checks, for each of the concept's synonyms, whether the synonym's normalized WORD sequence
   appears as a **contiguous subsequence** of the row label's normalized word sequence — e.g. synonym
   tokens `["revenue", "from", "operations"]` are found inside label tokens `["total", "revenue",
   "from", "operations"]` at offset 1, so `"Total revenue from operations"` matches `REVENUE`. Word-
   token containment (not raw substring containment) is deliberate: raw substring matching would also
   match `"cost of materials consumed"` against a hypothetical synonym `"material"` inside an unrelated
   longer word — token boundaries avoid that specific failure mode, though NOT the more fundamental one
   below.
5. **Known, documented limitation of decision 4 (not solved in RT-6): a synonym word can appear
   inside a label that means a DIFFERENT concept.** `"Cost of revenue"` contains the token sequence
   `["revenue"]`, one of REVENUE's synonyms — a naive lookup would tag it REVENUE even though the row
   is actually a cost line. RT-6 does not attempt disambiguation (negative-keyword lists, longest-
   synonym-wins ranking, or a classifier) — `find_concept_rows(grid, COST_OF_GOODS_SOLD)` also will
   NOT match `"Cost of revenue"` (no COGS synonym is `"cost of revenue"` or a subsequence of it), so
   the failure mode is specifically **querying `REVENUE` and getting a false-positive row that is
   actually a cost line**, not a missed row. This is called out explicitly as future-refinement
   territory in §6, not silently accepted. Every ambiguous case still requires the caller (a human or
   the RT-8 agent) to look at the returned `row_label` before trusting `values` — which is exactly
   why `row_label` is always echoed back in the result (§3.1), never discarded.
6. **`parse_numeric` returns the printed magnitude for percents (no ÷100), and strips currency
   markers without FX conversion.** `"12.5%"` → `12.5`, not `0.125` — RT-6 has no unit-of-measure
   model and guessing would silently mislabel a genuine percentage-point figure (e.g. a growth-rate
   row) as a fraction. `"₹1,234"` / `"Rs. 1,234"` / `"$1,234"` → `1234.0` — the currency SYMBOL is
   noise for numeric parsing purposes, but no currency CONVERSION happens (a `$` cell and a `₹` cell
   both parse to their printed digits; cross-currency comparison is out of scope, matching the
   roadmap's explicit "numeric reconciliation... is a quality-layer follow-on" deferral). Indian
   digit grouping (`1,00,000`, lakh-style) parses correctly because the implementation strips ALL
   commas regardless of grouping pattern rather than validating a specific thousands-grouping shape.
7. **Blank/dash/non-numeric cells parse to `None`, never `0.0` and never an exception.** A financial
   statement's "–" (em dash, no data for this period) and "" (empty cell) are semantically "no value
   reported", not zero — collapsing them to `0.0` would corrupt any downstream sum. `parse_numeric`
   never raises: unparseable text (`"abc"`, `"1.2.3"`, `"1.5x"`) also returns `None`, matching the
   `fdi.core` no-raise convention used throughout ingestion (`build_chunks`, `canonicalize_table`,
   etc.) — a malformed cell degrades the ONE value it covers, never aborts the whole table.
8. **Return ALL numeric columns for a matched row; never pick "the" period.** Per the roadmap row
   verbatim: *"extracting the numeric cell (the deferred numeric extraction)"* means parsing cell
   text into numbers, not selecting which of a table's several period columns is authoritative. A
   matched row's `values` list has one entry per data column (`column_index >= 1`, i.e. excluding the
   row-label column), each carrying `{column_header, raw, value}` — `column_header` lets the caller
   (or the RT-8 agent) pick "FY2024" vs. "FY2023" vs. a restated prior-year column itself; RT-6 does
   not know which one a question is asking about.
9. **Route shape: two per-document GETs, not POSTs.** `GET /documents/{doc_id}/tables?keyword=` and
   `GET /documents/{doc_id}/concepts/{concept}`. Both `get_tables` and `concept_lookup` take a `doc_
   id` that is ALREADY a path segment (unlike `/search`'s free-text query or `/entities/resolve`'s
   optional-threshold body, which have no natural path resource) plus a small filter (`keyword`) or a
   closed-enum path param (`concept`) — this is exactly the shape of the existing `GET /documents/
   {doc_id}/artifacts/{stage}` and `GET /documents/{doc_id}/stages` routes, not the shape of `/search`
   or `/entities/resolve`. `concept` as a `CanonicalConcept` path param gets FastAPI's automatic enum
   validation (an unrecognized concept string → 422) for free, the same way `{stage}` is a free string
   path segment for the generic artifact proxy. Both 404 when the document has no `structure`
   artifact yet, mirroring `document_artifact`'s existing `run is None or run.artifact_key is None`
   check.
10. **Per-doc primary; corpus-wide concept scan is NOT built in RT-6.** The roadmap's own dependency
   line settles this, not just convenience: **RT-7** ("Cross-document / entity retrieval") is listed
   as depending on **RT-6 (concepts)** specifically to build `compare_concept_across_versions(entity,
   concept)` — a corpus/version scan calling `concept_lookup` once per document is RT-7's job by
   construction, the same way `resolve_entities` (SP-16, not RT-1) is what iterates `list_documents()`
   for cross-document work rather than RT-1's `chunks` foundation doing it. Building a corpus scan
   here would duplicate work RT-7 already owns. `fdi.pipeline.tables.concept_lookup` is written so
   RT-7 can call it once per `doc_id` from a `list_documents()` loop exactly as `resolve_entities`
   loops calling `_read_artifact` per document — no interface change needed later.
11. **Table citation = `(doc_id, table_index, row_label, column_header)`.** `table_index` is the
   0-based position of the table within `structure["tables"]` for that document's LATEST run — stable
   for a given `structure` artifact (content-addressed: same `ocr` output_hash → same `structure`
   output → same list order, since `merge_cross_page_tables` sorts deterministically by span offset)
   but not a corpus-wide durable id; a citation is only meaningful alongside the `doc_id` it came from,
   same posture as RT-1's `chunk_id` being meaningless without its `doc_id` prefix.

## 3. Architecture

### 3.1 `fdi.core.tables` (pure, new module)

```python
def parse_numeric(cell: object) -> float | None:
    """Parse a table cell's raw content into a float.

    Handles: thousands separators of any grouping ("12,345", Indian "1,00,000"),
    parenthetical negatives ("(1,234)" -> -1234.0), a leading -/+ sign, a
    trailing "%" (returned as the printed number, NOT divided by 100), a
    leading/embedded currency marker (Rs., INR, ₹, $, €, £ -- stripped, no FX
    conversion), and surrounding whitespace. Returns None for blanks, dashes
    ("-", "--", em/en dash), "N/A"/"NA"/"Nil"/"NM" (any case), non-str/non-
    numeric input, and any text that still isn't a plain decimal after the
    above stripping. Never raises."""


def find_concept_rows(
    grid: Mapping[str, object], concept: CanonicalConcept
) -> list[dict[str, object]]:
    """Match `concept`'s ontology synonyms against `grid`'s row-label column
    (column 0) using normalized word-token-subsequence containment (see spec
    decision 4), skipping the grid's header rows. For each matched row,
    returns {row_label, values: [{column_header, raw, value}, ...]} covering
    every non-label column (column_index >= 1); `value` is `parse_numeric(raw)`.
    Degrades to [] on a malformed/empty grid (missing keys, n_cols == 0,
    ragged rows) -- never raises. `grid` is the exact `{n_rows, n_cols,
    header_row_count, column_headers, grid}` shape `canonicalize_table`
    produces (fdi.core.table_grid)."""


def filter_tables(
    tables: list[Mapping[str, object]], keyword: str | None
) -> list[Mapping[str, object]]:
    """Keep only tables where some `grid.column_headers[c]` contains `keyword`
    (case-insensitive substring). `keyword` None or "" returns `tables`
    unchanged. A table missing/malformed `grid`/`column_headers` is dropped
    when a keyword IS given (nothing to match), kept unchanged when it isn't."""
```

Algorithm sketch (`find_concept_rows`):
```
n_rows, n_cols = grid.get("n_rows"), grid.get("n_cols")           # int-or-0 defensively
header_row_count = grid.get("header_row_count") or 0
rows = grid.get("grid")                                            # list[list[str]] or []
column_headers = grid.get("column_headers")                        # list[str] or []
synonyms = [tok := normalize_label(s).split() for s in synonyms_for(concept)]  # token lists

matches = []
for r in range(header_row_count, n_rows):
    row = rows[r] if r < len(rows) else []
    label_raw = row[0] if row else ""
    label_tokens = normalize_label(label_raw).split()
    if not label_tokens:
        continue
    if not any(_contains_subsequence(label_tokens, syn_tokens) for syn_tokens in synonyms):
        continue
    values = []
    for c in range(1, n_cols):
        raw = row[c] if c < len(row) else ""
        header = column_headers[c] if c < len(column_headers) else ""
        values.append({"column_header": header, "raw": raw, "value": parse_numeric(raw)})
    matches.append({"row_label": label_raw, "values": values})
return matches
```
`_contains_subsequence(haystack, needle)` — `True` if `needle` (a non-empty token list) appears
contiguously in `haystack` starting at some offset; a private helper, unit-tested indirectly through
`find_concept_rows`'s cases.

### 3.2 `fdi.core.ontology` additions

```python
def normalize_label(label: str) -> str:              # renamed from _normalize_label (was private)
    ...                                                # body unchanged; map_label's call site updated

def synonyms_for(concept: CanonicalConcept) -> list[str]:
    """Every normalized synonym key in `_SYNONYMS` that maps to `concept`, in
    dict-declaration order. `CanonicalConcept.UNKNOWN` -> [] (no dictionary
    entry ever maps to UNKNOWN by construction)."""
    return [k for k, v in _SYNONYMS.items() if v is concept]
```

### 3.3 `fdi.pipeline.tables` (orchestrator, new module — I/O, same shape as `fdi.pipeline.retrieval`)

```python
async def get_tables(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, keyword: str | None = None
) -> list[dict[str, object]] | None:
    """Read doc_id's latest `structure` artifact; return its tables (each
    tagged with `table_index`), filtered by `keyword` via
    fdi.core.tables.filter_tables. None if the document has no `structure`
    artifact (caller maps this to 404)."""


async def concept_lookup(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, concept: CanonicalConcept
) -> list[dict[str, object]] | None:
    """Read doc_id's latest `structure` artifact; run
    fdi.core.tables.find_concept_rows(table['grid'], concept) over every
    table, tag each match with its table_index, and flatten. None if the
    document has no `structure` artifact (caller maps this to 404); an empty
    list (not None) means the document HAS tables but none matched -- a real,
    distinct outcome from "no structure artifact at all"."""
```
Both read `RegistryRepository.latest_run(doc_id, "structure")` → `ArtifactStore.get_json`, the exact
read path `resolve_chunk` (RT-1) and `_read_artifact` (SP-16 crossdoc) already use — no new I/O
pattern introduced.

### 3.4 Routes (`fdi.api`)

```python
@router.get("/documents/{doc_id}/tables", response_model=TablesView)
async def get_tables_route(
    doc_id: str, session: SessionDep, store: StoreDep, keyword: str | None = Query(default=None)
) -> TablesView: ...

@router.get("/documents/{doc_id}/concepts/{concept}", response_model=ConceptLookupView)
async def concept_lookup_route(
    doc_id: str, concept: CanonicalConcept, session: SessionDep, store: StoreDep
) -> ConceptLookupView: ...
```
Both 404 (`f"no structure artifact for document {doc_id}"`) when the orchestrator returns `None`. No
new `deps.py` entries — `SessionDep`/`StoreDep` (already used by `document_artifact`) are sufficient;
neither route touches Milvus/embedder/dispatcher, so no `503`-missing-service branch like `/search`'s
is needed. `schemas.py` gains `TableGridView`, `TablesView`, `ConceptValueView`, `ConceptRowView`,
`ConceptLookupView` (plain `BaseModel`s mirroring the dict shapes in §3.1/§3.3 field-for-field).

## 4. Error handling

- `parse_numeric`: never raises (decision 7). `None`/non-`str`/non-numeric input, blanks, dashes,
  "N/A" family → `None`. A already-numeric `int`/`float` (not `bool`) cell → `float(cell)` directly
  (defensive: `structure`'s `grid` cells are always `str` per `canonicalize_table`, but the function
  does not assume its caller never hands it something else).
- `find_concept_rows`: a malformed/empty `grid` (missing `n_rows`/`n_cols`/`grid`/`column_headers`,
  `n_cols == 0`, a ragged `rows[r]` shorter than `n_cols`) degrades to skipping that row/table, never
  raises; `find_concept_rows({}, CanonicalConcept.REVENUE) == []`.
- `filter_tables`: a table missing/malformed `grid` is dropped only when a keyword is given (nothing
  to test the keyword against); `filter_tables(tables, None) == tables` unchanged, including malformed
  entries (a keyword-less call is a pass-through, not a validation pass).
- `get_tables`/`concept_lookup`: `run is None or run.artifact_key is None` (no `structure` artifact
  yet, or unknown `doc_id`) → `None`, mapped to a 404 by the route — the SAME "missing artifact is not
  an exception" posture as `resolve_chunk` returning `None` and `document_artifact`'s existing 404.
  `concept_lookup` on a document that HAS `structure` but whose tables contain no matching row →
  `[]`, a 200 with an empty `rows` list, never a 404 (the document and its tables exist; the concept
  just wasn't found there — a real, query-shaped answer, not an error).
- `concept_lookup_route`'s `concept` path param: an unrecognized string → FastAPI's own 422 (Pydantic/
  enum validation), before the route body ever runs — no bespoke validation code needed.

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_tables.py`:
  - `parse_numeric`: `"12,345"` → `12345.0`; `"(1,234)"` → `-1234.0`; `"-1,234"` → `-1234.0`; `"1.2"`
    → `1.2`; `"$12,345.67"` → `12345.67`; `"Rs. 1,234"` → `1234.0`; `"₹1,00,000"` (Indian grouping) →
    `100000.0`; `"12.5%"` → `12.5`; `"(12.5%)"` → `-12.5`; `""`, `"-"`, `"—"`, `"N/A"`, `"n/a"`,
    `"Nil"`, `"NM"` → `None`; `"abc"`, `"1.2.3"`, `"1.5x"` → `None`; `"  42  "` → `42.0`; `None` (the
    Python value) → `None`; `42` (already-numeric input) → `42.0`; `True` (bool, not a number here) →
    `None`.
  - `find_concept_rows`: a 2-row-header, 3-column grid (`column_headers=["Particulars","FY24","FY23"]`)
    with a data row `["Total revenue from operations", "12,345", "10,000"]` → one match, `row_label ==
    "Total revenue from operations"`, `values == [{"column_header":"FY24","raw":"12,345","value":
    12345.0}, {"column_header":"FY23","raw":"10,000","value":10000.0}]`, proving decision 4's
    subsequence match (label has an extra "Total" the synonym doesn't).
  - A row `"Cash and cash equivalents"` matched against `CanonicalConcept.CASH` (multi-token synonym
    spanning the whole label).
  - A row `"Cost of revenue"` queried against `CanonicalConcept.REVENUE` — asserts it INCORRECTLY
    matches (decision 5's documented limitation, pinned as a test so a future fix is a deliberate,
    visible behavior change, not a silent one) AND against `CanonicalConcept.COST_OF_GOODS_SOLD` —
    asserts it does NOT match (no COGS synonym is a subsequence of "cost of revenue").
  - Header rows excluded: a grid whose `header_row_count == 1` with a header-row cell that happens to
    contain a synonym word never appears in `find_concept_rows`'s output.
  - `find_concept_rows({}, CanonicalConcept.REVENUE) == []`; a grid with `n_cols: 0` → `[]`; a ragged
    `grid` list (a row shorter than `n_cols`) degrades that row's missing cells to `raw: ""`, `value:
    None` rather than raising (`IndexError`).
  - `filter_tables`: keyword `"fy24"` matches a table whose `column_headers` includes `"FY24"` (case-
    insensitive); `keyword=None` and `keyword=""` both return the input list unchanged, including a
    table with a malformed/missing `grid`.
  - `fdi.core.ontology.synonyms_for(CanonicalConcept.REVENUE)` returns the exact set of REVENUE keys
    from `_SYNONYMS`; `synonyms_for(CanonicalConcept.UNKNOWN) == []`.
- **Integration** (testcontainers + `FakeOcrClient`), `backend/tests/integration/test_tables_lookup.py`:
  - **Executor pre-check (do first, document finding):** confirm `FakeOcrClient`'s existing fixtures
    across the suite (`test_structure_stage.py` et al.) have NO table with numeric data-row cells —
    every existing table fixture is header-only or has `cells: []`. This integration test therefore
    needs a NEW `FakeOcrClient` table fixture with at least one non-header row carrying numeric
    content (e.g. a 2-column-header + 2-data-row table: `["Particulars","FY24"], ["Total revenue from
    operations","12,345"], ["Net profit","(500)"]`) — build it inline in the test (or a small local
    helper), not by editing the shared fixtures other stage tests pin exact-equality assertions
    against.
  - Run `ocr → structure` via `run_stage` over a LOCAL `StageRegistry` (`OcrStage` + `StructureStage`,
    the exact two-stage harness `test_structure_stage.py` already uses) with the new numeric-table
    fixture.
  - `get_tables(session=..., store=..., doc_id=doc.id)` returns the one table with `table_index == 0`
    and the expected `grid`; `get_tables(..., keyword="FY24")` keeps it, `keyword="FY99"` returns `[]`.
  - `concept_lookup(..., doc_id=doc.id, concept=CanonicalConcept.REVENUE)` returns one row (`"Total
    revenue from operations"`) with a `12345.0` value tagged `table_index == 0`; `concept=
    CanonicalConcept.NET_PROFIT` returns the `"Net profit"` row with `value == -500.0` (parenthetical
    negative, proving the fixture's raw `"(500)"` round-trips through `parse_numeric` end-to-end, not
    just in the unit test).
  - `get_tables`/`concept_lookup` on a `doc_id` with NO `structure` artifact yet (only `intake_
    document`, no `run_stage("ocr", ...)`) → `None`.
- **API** (`httpx.AsyncClient` against the app, `backend/tests/integration/test_api.py` additions,
  reusing the app/client fixtures already in that file):
  - `GET /documents/{doc_id}/tables` → 200, body shape `{doc_id, tables:[...]}` matching `get_tables`'s
    result for a processed doc (reuse/extend `resolved_corpus_via_api`-style fixture, or a smaller
    dedicated one, run through `structure` with the numeric-table fixture above).
  - `GET /documents/{doc_id}/tables?keyword=...` filters as expected.
  - `GET /documents/{doc_id}/concepts/REVENUE` → 200 with the matched row; `GET /documents/{doc_id}/
    concepts/not-a-real-concept` → 422 (FastAPI enum validation).
  - `GET /documents/{unknown-id}/tables` and `.../concepts/REVENUE` → 404.
- **Config/registry:** no new stage, so no `successors(...)`/registration assertions are needed — the
  executor confirms (and notes in the module docstring) that `fdi.pipeline.tables` and the two new
  routes introduce NO new entry into `fdi.pipeline.stages.__init__` or `REGISTRY`.

## 6. Explicitly deferred (YAGNI)

- **Corpus-wide `concept_lookup`/cross-document comparison** — RT-7's job by the roadmap's own
  dependency line (decision 10); RT-6 ships the per-document primitive RT-7 will loop over.
- **Persisted `tables`/`concepts` artifact** — on-demand read chosen (decision 1); revisit only if
  profiling shows the per-request `structure` read + parse is measurably hot, per §8's flagged fork.
- **Concept-match disambiguation** (negative-keyword lists, longest-synonym-wins ranking, an ML
  classifier for "cost of X" vs. "X") — decision 5's documented false-positive is left as-is; the
  `row_label` is always returned precisely so a human/agent can catch it.
- **Row-header (`kind == "rowHeader"`) canonicalization** — SP-7c already deferred symmetric row-
  header joining (multi-row-spanning row labels); RT-6 reads grid column 0 as the row label
  unconditionally, inheriting that same deferral rather than re-opening it.
- **Currency conversion / unit normalization** (₹ vs. $, thousands vs. actual vs. crore/lakh
  denomination) — `parse_numeric` strips symbols but performs no FX or scale conversion (decision 6);
  matches the roadmap's "numeric reconciliation... is a quality-layer follow-on."
- **A `/concepts/lookup` POST alternative for multi-concept batched queries** — one concept per call
  (decision 9) is the thin slice; a batch endpoint is a caller-side optimization (N calls today) not
  built until an actual multi-concept caller (likely RT-8's agent) shows it is needed.

## 7. Done criteria

1. `fdi.core.tables.parse_numeric` handles every format in §5's unit-test list (thousands separators
   incl. Indian grouping, parenthetical negatives, leading sign, percent-as-printed, currency symbols,
   blanks/dashes/N-A family → `None`, never raises); `fdi.core.tables.find_concept_rows` matches via
   normalized word-token-subsequence containment (decision 4), skips header rows, returns ALL numeric
   columns per matched row (never guesses a period), degrades to `[]` on malformed input, never raises;
   `fdi.core.tables.filter_tables` case-insensitive substring-matches keyword against `column_headers`.
   `fdi.core.tables` stays pure (no I/O; its only cross-core import is `fdi.core.ontology`, per
   decision 2b — no import of `fdi.core.structure`/`table_grid` beyond reading the `grid` shape as a
   plain `Mapping`).
2. `fdi.core.ontology.normalize_label` (renamed, public) and `synonyms_for(concept)` (new, public)
   ship without changing `map_label`'s or `map_document`'s existing behavior/signatures.
3. `fdi.pipeline.tables.get_tables`/`concept_lookup` read the `structure` artifact via the established
   `latest_run` → `get_json` path, return `None` when it is absent (never raise on a missing
   document/artifact), and tag every result with its `table_index` citation.
4. `GET /documents/{doc_id}/tables` (optional `keyword` query) and `GET /documents/{doc_id}/concepts/
   {concept}` ship, 404 on a missing `structure` artifact, 422 on an invalid `concept` path value, and
   both are exercised end-to-end (not just unit-tested) against a `FakeOcrClient` table fixture that
   — unlike every existing fixture in the suite — carries numeric data rows.
5. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); no new
   pipeline stage or artifact was introduced (confirmed by grep/registry inspection, not just absence
   of a failing test); the retrieval roadmap's RT-6 row flips to done with a handoff note for RT-7
   (the `concept_lookup(session, store, doc_id, concept)` signature it will loop `list_documents()`
   over, and decision 5's known false-positive limitation it inherits).

## 8. Forks flagged for a human decision (not resolved by this spec)

1. **On-demand vs. persisted artifact (decision 1).** This spec picks on-demand for freshness and
   simplicity; a persisted `tables`/`concepts` stage would trade that for query-time speed at corpus
   scale. No profiling data exists yet to decide which matters more in practice.
2. **Synonym-matching aggressiveness (decisions 4–5).** Word-token-subsequence containment is a
   deliberate middle point between `map_label`'s too-strict exact match (misses "Total revenue from
   operations") and unconstrained substring matching (more false positives than token containment,
   e.g. matching inside compound words). Whether the "Cost of revenue" false-positive class (decision
   5) is acceptable for the product's actual use (financial-statement Q&A, where a wrong row returned
   alongside its `row_label` is recoverable by inspection) or needs disambiguation NOW rather than as
   a documented follow-on is a product-risk call, not a technical one this spec can settle alone.
