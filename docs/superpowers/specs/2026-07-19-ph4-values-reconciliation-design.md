# Phase 4 — Financial semantics: values + reconciliation

- **Status:** Design — 2026-07-19. Phase 4 of the ingestion improvement plan
  (`docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md`). **Parallel** track — depends
  only on already-merged work (SP-11 `fdi.core.ontology`, SP-7c/RT-6 `fdi.core.table_grid`/`fdi.core.tables`,
  SP-17b `fdi.core.quality`), **not** on Phase 3b (table/figure graph nodes) or Phase 5. Per the plan's own
  DAG (`docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md` §6), Phase 4 and Phase 5 are
  the two independently-mergeable "early wins" that fan out once Phase 1's `structure`/`ontology` artifacts
  are stable — which they already are (Phase 2 is the last merged work that touched `structure`'s shape,
  additively, via `cell_regions`/`cell_pages`, `StructureStage v3`; Phase 3a operated on `chunks`/
  `embedding`, not `structure`, so `structure["tables"][].grid` has been stable since Phase 2).
- **Closes:** audit gaps **P2-6** ("no concept→value linking") and **P3-8** ("no reconciliation" — the BS
  actually balances, 16,427.37 == 16,427.37, but nothing checks it).
- **Verified on the real doc (ground truth, `D:/FinancialDocIntelligence/data/ground_truth/STTL_2023-24.json`,
  used only to shape tolerance/synonym decisions below, never read at runtime by any function this spec
  defines):** `total_assets` (16427.37) == `total_equity_and_liabilities` (16427.37) **exactly**;
  `total_income` (20925.03) − `total_expenses` (18795.89) = 2129.14, exactly ground truth's own
  `profit_before_tax` (2129.14) — BS and P&L both foot **exactly**, diff 0.00; `opening_cash_balance`
  (871.50) + `net_change_in_cash` (−643.61) = 227.89 vs. `closing_cash_balance` 227.90 — a **0.01**
  discrepancy, the GT file's own compounding-rounding slack from summing ~15 two-decimal line items, not
  an extraction bug. That one measured number (0.01 on figures in the hundreds) is the empirical basis
  for §2 decision 6's tolerance.

## 1. Purpose

The meaning-layer audit found two gaps that are really one gap in two places:

1. **`fdi.core.ontology.map_document` maps LABELS only.** Its own docstring already says why: an AST
   paragraph mixes a label and a number in one string ("Revenue 12,345"), so `map_document` can only
   match bare headings/paragraphs — 32 mappings, 7/9 concepts hit, **zero numbers**, vs. 147 numeric
   ground-truth leaf fields. RT-6 already solved the numeric half of this problem for **table grids**
   specifically (`fdi.core.tables.find_concept_rows` + `parse_numeric`) — because a table's row-label
   cell (column 0) is already a bare label with the number safely isolated in the other columns, RT-6's
   own docstring flags this table-grid matching as *"the deferred numeric extraction... map_document's
   docstring points at"* — but nothing yet calls it as part of the standing `ontology` artifact; it is
   only reachable on-demand via RT-6's `GET /documents/{doc_id}/concepts/{concept}` route.
2. **`fdi.core.quality` has no reconciliation validator anywhere**, even though the audit's own headline
   finding is that the real document's statements DO reconcile (BS balances, P&L foots, CF ties) — the
   pipeline extracts every number correctly enough to check this, and simply never checks it.

Phase 4 closes both by (a) folding RT-6's existing pure numeric-extraction primitives into the standing
`ontology` artifact as an additive `concept_values` key, and (b) adding three reconciliation validators to
`quality` that read those values and warn (never hard-error) when a statement's own arithmetic doesn't
hold together.

**Explicitly not attempted:** re-deriving `find_concept_rows`/`parse_numeric` (RT-6 already built and
shipped them — this phase is a *consumer*, not a re-implementation) and touching `graph.py`/`ast.py`/
`structure.py`/`retrieval.py` (owned by Phase 3b/5, both parallel tracks with their own in-flight
artifact-shape decisions this phase must not collide with).

## 2. Scope decisions (autonomous; documented) — read this section first

1. **Companion function, not an extended `map_document` — and it cannot physically live in
   `fdi.core.ontology` or `fdi.core.tables`.** `map_document`'s signature/behavior stay untouched
   (smallest blast radius; nothing downstream that already reads `mappings` needs to change). The new
   value-extraction function is `map_concept_values(structure) -> dict`, reusing `fdi.core.tables.
   find_concept_rows` exactly as instructed. But `fdi.core.tables` already does `from fdi.core.ontology
   import CanonicalConcept, normalize_label, synonyms_for` (RT-6 decision 2b) — so a function that needs
   BOTH `CanonicalConcept` (from `ontology`) AND `find_concept_rows` (from `tables`) **cannot be added to
   either module** without creating a two-file import cycle (`ontology → tables → ontology`). This isn't
   a style preference, it's a hard Python `ImportError` if attempted naively. Phase 4 therefore adds a
   **third, new pure core module**, `fdi.core.concept_values`, that imports both leaf modules — a normal
   core→core→core DAG (`ontology` ← `tables` ← `concept_values`), not a cycle. **This is the spec's one
   real architectural fork; flagged again in §6.** The alternative (fold the matching logic directly into
   `quality.py`, which has no existing dependents to create a cycle with) was considered and rejected:
   it would make `quality.py` — today a validators-only module — also own concept/value extraction, a
   second responsibility `ontology`'s own name and existing role ("the concept-mapping layer") already
   claims; keeping extraction in a `core.ontology`-adjacent module and consumption in `quality.py` mirrors
   the existing `ast`-produces / `quality`-validates separation.
2. **`CanonicalConcept` gains 6 new members — the reconciliation triples don't exist as concepts today.**
   Checked against the real document's own statement lines (ground truth field names in §1's verified
   block): `total_income` and `profit_before_tax` have **no** existing concept (`OPERATING_EXPENSE`'s
   synonym set already covers "total expenses", so no new concept is needed there); the balance sheet's
   actual grand-total row is **"Total Equity and Liabilities"** (a single combined line — Indian Schedule
   III format does not report a bare "Total Liabilities" line the existing `TOTAL_LIABILITIES` concept
   would match), not `EQUITY + TOTAL_LIABILITIES` summed from two separate rows; the cash-flow tie needs
   three concepts (opening/closing/net-change) none of which exist. New members, added to `CanonicalConcept`
   and `_SYNONYMS` in `fdi/core/ontology.py` (starter synonym sets — deliberately small, tuned like every
   other synonym set in this dict via real-doc measurement per §6, not treated as final here):
   - `TOTAL_EQUITY_AND_LIABILITIES` — `"total equity and liabilities"`, `"total equities and liabilities"`.
   - `TOTAL_INCOME` — `"total income"`.
   - `PROFIT_BEFORE_TAX` — `"profit before tax"`, `"pbt"`, `"profit before tax and exceptional items"`.
   - `OPENING_CASH_BALANCE` — `"opening cash and cash equivalents"`, `"cash and cash equivalents at the
     beginning of the year"`.
   - `CLOSING_CASH_BALANCE` — `"closing cash and cash equivalents"`, `"cash and cash equivalents at the
     end of the year"`.
   - `NET_CHANGE_IN_CASH` — `"net change in cash and cash equivalents"`, `"net increase in cash and cash
     equivalents"`. **Documented, known-weak synonym coverage**: real cash-flow statements routinely phrase
     this as "Net (Decrease)/Increase in Cash and cash equivalents" — `normalize_label`'s punctuation
     stripper (`_PUNCT_RE = r"[^\w\s]"`) deletes `(`/`)`/`/` without inserting a space, collapsing
     `"(Decrease)/Increase"` into the single glued token `"decreaseincrease"`, which will not
     word-subsequence-match any listed synonym. This is an **existing, shared** `normalize_label`
     limitation (affects any label with parenthetical/slash constructs, not something Phase 4 introduces)
     — fixing the regex is out of scope here (its blast radius is every existing synonym match in the
     system, not just this one new concept); §4 documents the resulting degrade (the CF-ties validator
     simply has fewer real matches to work with until this is tuned, never a false reconciliation claim).
   - **Side effect, accepted as a feature, not a bug:** `CanonicalConcept` backs RT-6's
     `GET /documents/{doc_id}/concepts/{concept}` route (`concept: CanonicalConcept` path param, FastAPI
     enum validation) — these 6 new members become independently queryable through that existing route
     with **zero code change to `fdi/api` or `fdi/pipeline/retrieval.py`** (neither is touched by this
     phase). Flagged because it is a real, if welcome, expansion of an already-shipped public API surface
     as a side effect of an `ontology.py`-only change — a human should be aware even though nothing in
     `retrieval.py`/`api/` needed editing to produce it.
3. **`map_concept_values(structure)` returns a FLAT list, one entry per (matched row × data column) —
   not `find_concept_rows`'s nested `{row_label, values:[...]}` shape.** Per the task brief's own target
   shape, extended with two additive fields beyond the minimum requested set (both free — already present
   in `find_concept_rows`'s per-value dict or trivially derivable, and both load-bearing for §2 decision 7's
   pairing algorithm):
   ```
   {concept: str, row_label: str, value: float | None, raw: str,
    column_header: str, column_index: int, page: int | None, table_index: int}
   ```
   `concept` is `CanonicalConcept.value` (a plain string, e.g. `"TOTAL_ASSETS"`) — matching `mappings[].
   concept`'s existing convention in `map_document`'s output, so a consumer that already knows how to read
   `ontology["mappings"]` needs no new mental model. `raw` is the cell's unparsed text (RT-6 already
   returns it; dropping it would be a pure information loss for zero benefit). `column_index` is the
   0-based position of this value among the matched row's DATA columns (`c - 1` for `c in range(1, n_cols)`
   in `find_concept_rows`'s own iteration) — added specifically to enable §2 decision 7's positional
   period-alignment fallback; it is not in the task's literal 6-key list but costs nothing to carry (it's
   just `enumerate()`'s own index over `match["values"]`) and is the only clean way to align "this
   period's column" across two DIFFERENT tables (a balance sheet grid and a P&L grid) whose `column_header`
   text may not match verbatim even when they mean the same reporting date. `page` is the table's own
   first `bounding_regions[0].page_number` — the same "first region" convention `core.provenance._page_of`
   and Phase 2's `cell_pages` already use, chosen for consistency rather than inventing a second
   page-resolution rule. `table_index` is the position in `structure["tables"]` (post-stitch), the SAME
   index RT-6's `get_tables`/`concept_lookup` already use — a `concept_values` entry citation joins
   zero-translation against `GET /documents/{doc_id}/tables?...`.
4. **Every match is kept — concept_values NEVER deduplicates or silently picks "the" row.** Direct
   instruction from the task, and a direct inheritance of RT-6 decision 5's own documented false positive:
   a row literally titled "Cost of revenue" contains the REVENUE synonym token `"revenue"` and WILL appear
   in `concept_values` tagged `concept: "REVENUE"` alongside the correct "Revenue from operations" row —
   `map_concept_values` does not attempt to fix this (RT-6 didn't either, by its own decision 5), it
   inherits `find_concept_rows`'s exact behavior unchanged and, critically, `row_label` is always present
   on every entry so a human or a future agent can see and discount a wrong tag. §2 decision 7 explains
   why this is survivable for the three reconciliation validators specifically (closest-pairing selection
   is an implicit disambiguator), not solved in general.
5. **Ontology artifact: additive `concept_values` key, not a new artifact.** `mappings` (from `ast`, label
   text) and `concept_values` (from `structure`, table numbers) are two views of the same "concept
   mapping" responsibility, over two different source artifacts — grouping them under the existing
   `ontology` name keeps the DAG shape simple (one new stage dependency, not two) and matches this
   codebase's repeated "augment additively" posture (Phase 2's `ast`/`graph`/`structure` shape changes;
   Phase 3's `chunks` shape change) rather than the "new artifact per new field" alternative, which would
   require `QualityStage` to gain TWO new deps instead of one and would fragment "what a document's
   ontology says" across two artifacts a caller has to join manually. **Flagged as a fork in §6** — the
   task explicitly asked for this decision to be surfaced, even though this spec has a clear recommendation.
6. **Reconciliation tolerance: `max(0.05, 0.001 × max(|a|, |b|))` — absolute floor OR 0.1% relative,
   whichever is larger.** Grounded in §1's verified numbers, not a guess: the real document's BS/P&L
   reconcile **exactly** (diff 0.00) and its CF ties within **0.01** on figures in the hundreds — a
   compounding-rounding artifact from summing ~15 two-decimal line items, not a real defect. `0.05`
   absolute gives that observed 0.01 a 5× margin without being so loose it would mask a real, smaller-
   magnitude break; `0.1%` relative scales the same margin up for a much larger document's larger
   aggregate totals. Both constants are named module-level constants (`_ABS_TOL`, `_REL_TOL`) in
   `fdi/core/quality.py`, not buried in a formula — a human tuning this later (e.g. against a second
   ground-truth document) changes two numbers, not an expression.
7. **Column/period alignment: exact `column_header` match first; positional (`column_index`) fallback
   second; ambiguity among multiple matched rows resolved by "closest pairing wins."** Two concepts being
   compared may come from different tables whose header text for "the same period" doesn't match verbatim
   (a balance sheet's `"As at 31-03-2024"` vs. a cash-flow statement's `"Year ended 31-03-2024"`). Alignment
   tries `column_header` equality (case/whitespace-insensitive) across every candidate pair first; if that
   yields **zero** pairs for the whole candidate set, it falls back to `column_index` equality — a
   documented heuristic assumption (current-period-first-then-prior-period column ordering, the same
   convention Indian filings and this codebase's own table grids consistently use) rather than a proven
   fact, exactly the caveat class Phase 3's `attach_table_refs` hierarchy-replay assumption already
   established a precedent for flagging rather than silently trusting. **Disambiguation is a side effect
   of "closest pairing wins," not a separate mechanism**: when a concept has multiple candidate rows
   (decision 4's false-positive case, or a genuine duplicate elsewhere in the document, e.g. inside a
   Note's own sub-schedule), the reconciliation validator evaluates EVERY period-aligned `(entry_a,
   entry_b)` pair and keeps the one minimizing `abs(value_a − value_b)` — a row that is actually the
   concept it claims to be will naturally reconcile (near-zero diff) against its counterpart and win the
   selection; a false-positive row's unrelated value will not. Only when even the CLOSEST candidate pair
   fails §2 decision 6's tolerance does the validator emit a warning, citing that best-effort pair's
   `row_label`/`table_index`/`column_header`/values explicitly (never just "doesn't balance" with no
   numbers) — so a human reading the warning can tell at a glance whether it's a real break or a
   still-unresolved ambiguous match. If NO period-aligned pair exists at all for a concept pair (one or
   both concepts have zero matches anywhere in the document) the validator emits **nothing** — a missing
   concept is an ontology-coverage gap (already visible via low `concept_count`/`concept_values` size),
   not a reconciliation defect, and inventing a 4th "indeterminate" issue code was rejected to keep the
   severity taxonomy exactly `{"error","warning"}` as `_KNOWN_SEVERITIES` already enforces.
8. **`balance_sheet_balances` has a two-tier concept preference, not a single fixed pair.** Primary:
   `TOTAL_ASSETS` vs. the new `TOTAL_EQUITY_AND_LIABILITIES` (matches the real document's actual single
   grand-total row). Fallback, only when `TOTAL_EQUITY_AND_LIABILITIES` has zero matches anywhere:
   `TOTAL_ASSETS` vs. `EQUITY + TOTAL_LIABILITIES` (summed per aligned period first, via the same §2
   decision 7 pairing, THEN compared to assets) — for a differently-formatted statement (non-Indian GAAP)
   that reports liabilities and equity as two separate grand-total lines instead of one combined line.
   Both existing concepts (`EQUITY`, `TOTAL_LIABILITIES`) are left completely unchanged; this is a
   consumption-order decision in `quality.py`, not an `ontology.py` change.
9. **Three new Issue codes, all `"warning"` severity, message-only (no `Issue` schema change).**
   `quality.balance_sheet_imbalance`, `quality.profit_and_loss_does_not_foot`, `quality.cash_flow_does_not_tie`
   — every number (both values, the diff, the period/column_header, the row_label(s) and table_index(es)
   used) is embedded in the message string, matching `tables_are_rectangular`'s existing style (its message
   already embeds `index`/`column_count` inline) rather than adding a new structured `details` field to
   `Issue`. This keeps `Issue = dict[str, object]` and `_issue(severity, code, message)` byte-identical —
   zero change to `validate_all`'s generic contract, and every existing test asserting the exact `Issue`
   shape (`{"severity", "code", "message"}`) keeps passing unmodified. **Never `"error"`** — per the task's
   explicit instruction and this codebase's own established posture (Phase 0-3 threaded confidence/
   ambiguity signals as warnings, never hard failures, when the underlying extraction is a best-effort
   read of a real-world document): a reconciliation miss is very often an extraction/matching artifact
   (decision 4's false positives, decision 7's column-alignment heuristic), not proof the source document
   itself is wrong — `error_count`/`ok` must never flip on what could be this pipeline's own
   mis-extraction.
10. **`OntologyStage` gains a `structure` dep; `QualityStage` gains an `ontology` dep — both stages'
    `.version` bumped, matching the "bump on any output change" convention every prior phase spec
    established** (Phase 1 §2.6: "`AstStage.version` bumped (its output changes...)"; Phase 3 §2.10:
    "`EmbeddingStage.version: 1 → 2` (code changed)"):
    - `fdi.core.ontology` artifact `schema_version 1 → 2` (new `concept_values` key — additive but a real
      shape change, over-signaled the same way Phase 2 bumped `graph`'s `schema_version` for an additive
      `source_index` key) + `OntologyStage.version 1 → 2`, `deps: ("ast",) → ("ast", "structure")`.
    - `fdi.core.quality`'s **outer** artifact `schema_version` stays **1** — `{schema_version, issues,
      error_count, warning_count, ok}` is byte-identical; only new possible `issues[].code` values exist,
      which is a behavior change, not a shape change (mirrors Phase 2's `provenance` precedent: algorithm/
      input changed, `schema_version` didn't). `QualityStage.version 2 → 3`, `deps: ("ast", "structure")
      → ("ast", "structure", "ontology")` — the pipeline's first **three**-dependency stage after
      `KnowledgeStage` (which is itself downstream of `quality`, so this is a new precedent one layer
      earlier in the DAG, not a repeat of `KnowledgeStage`'s existing one).
    - `fdi.core.knowledge` artifact `schema_version` stays **1** (§2 decision 11's two new keys are
      additive, same "behavior/content changed, outer shape didn't" reasoning) + `KnowledgeStage.version
      1 → 2` (its output changes even though `KnowledgeStage.run`'s own 3-line body is textually
      unchanged — the same "bump because the artifact changes" rule Phase 1 applied to `AstStage`).
      `KnowledgeStage.deps` stays `("semantic", "ontology", "quality")` — unchanged, it already depends
      on both artifacts this phase modifies; it re-runs automatically via their `output_hash` cascade.
    - Cascade: bumping `OntologyStage` forces `ontology`'s `output_hash` to change → `knowledge` (already
      depends on `ontology`) recomputes automatically; `quality`'s own version bump is independent
      (its `deps` tuple change alone would already force this, the version bump is the established
      over-signal, not a second, different requirement). No stage downstream of `quality`/`ontology`
      other than `knowledge` exists today, so the cascade is short.
11. **`structure.py`, `tables.py`, `ast.py`, `graph.py`, `retrieval.py` are read-only inputs, never
    edited.** `map_concept_values` reads `structure["tables"][i]["grid"]` and `structure["tables"][i]
    ["bounding_regions"]` — both already present and stable since Phase 0/2; no change needed or made to
    how `StructureStage` produces them. `find_concept_rows`/`parse_numeric`/`normalize_label`/
    `synonyms_for` are called, never modified. This keeps Phase 4 fully parallel to Phase 3b/5 (both of
    which DO touch `structure.py`/`ast.py`/`graph.py`) — no merge-order dependency, no shared-file
    conflict risk with either.
12. **Statement identification via table `caption`/`footnotes` (audit's Azure-structure row #7):
    explicitly OUT of scope for Phase 4, documented, not silently dropped.** Two independent findings make
    this the right call now rather than a missed opportunity: (a) the audit itself measured **1 caption
    across all 261 tables** in the real document (`docs/architecture/2026-07-19-ingestion-audit-and-
    improvement-plan.md` §2, OCR layer) — captions are not a usable per-table signal on this corpus
    regardless of how Phase 4 might consume them; (b) `fdi.core.structure.merge_cross_page_tables`'s
    `_merge_two` (read, not modified — out of scope per §2 decision 11) constructs a stitched table's
    output dict from an explicit allow-list (`row_count, column_count, spans, bounding_regions, cells,
    stitched`) that **does not include `caption`/`footnotes`** — a stitched table loses whatever caption it
    had, silently, today. Reconciliation therefore runs **unscoped by statement** (over the whole
    document's `concept_values`, not "only the BS table's values") — acceptable because the new concepts'
    synonyms (§2 decision 2) are specific enough to a single statement type in practice (e.g. "Total Equity
    and Liabilities" only ever appears on a balance sheet) that per-statement scoping is a precision
    nice-to-have, not a correctness requirement, for the three validators this phase ships. The natural,
    reliable future signal is Phase 3b's `table_refs`/section-heading membership (a table's OWNING
    section, e.g. literally headed "Balance Sheet") — Phase 4 does not depend on Phase 3b landing first
    (§1), so this is deferred to whichever of Phase 3b or a dedicated follow-up picks it up, not solved
    with the much weaker caption signal now.

## 3. Architecture

### 3.1 `fdi.core.ontology` — 6 new `CanonicalConcept` members (only change to this file)

```python
class CanonicalConcept(StrEnum):
    REVENUE = "REVENUE"
    COST_OF_GOODS_SOLD = "COST_OF_GOODS_SOLD"
    GROSS_PROFIT = "GROSS_PROFIT"
    OPERATING_EXPENSE = "OPERATING_EXPENSE"
    NET_PROFIT = "NET_PROFIT"
    TOTAL_ASSETS = "TOTAL_ASSETS"
    TOTAL_LIABILITIES = "TOTAL_LIABILITIES"
    EQUITY = "EQUITY"
    CASH = "CASH"
+   TOTAL_EQUITY_AND_LIABILITIES = "TOTAL_EQUITY_AND_LIABILITIES"
+   TOTAL_INCOME = "TOTAL_INCOME"
+   PROFIT_BEFORE_TAX = "PROFIT_BEFORE_TAX"
+   OPENING_CASH_BALANCE = "OPENING_CASH_BALANCE"
+   CLOSING_CASH_BALANCE = "CLOSING_CASH_BALANCE"
+   NET_CHANGE_IN_CASH = "NET_CHANGE_IN_CASH"
    UNKNOWN = "UNKNOWN"
```
`_SYNONYMS` gains the entries listed in §2 decision 2. `map_label`, `map_document`, `normalize_label`,
`synonyms_for` — **zero code change**; they are generic over `CanonicalConcept`/`_SYNONYMS` already and
pick up the new members for free (this is exactly `synonyms_for`'s own existing contract: "every
normalized synonym key... that maps to `concept`"). `map_document`'s existing 32-mapping behavior on the
real doc is unaffected (none of the 6 new concepts' synonyms match a bare AST heading/paragraph any
differently than before — they're table-grid-shaped labels).

### 3.2 `fdi.core.concept_values` (new, pure module)

```python
"""Concept -> numeric value extraction over a document's `structure` artifact.
Companion to fdi.core.ontology.map_document (which maps AST labels, no
numbers) -- reuses fdi.core.tables.find_concept_rows (RT-6) to do the actual
matching/parsing; this module only flattens + tags results with page/
table_index citations. Pure -- no I/O. Lives in its own module, not inside
ontology.py or tables.py, specifically to avoid the ontology<->tables import
cycle a shared home would create (tables.py already imports ontology.py for
CanonicalConcept/normalize_label/synonyms_for) -- see design spec §2 decision 1."""

from collections.abc import Mapping
from typing import cast

from fdi.core.ontology import CanonicalConcept
from fdi.core.tables import find_concept_rows


def _first_page(table: Mapping[str, object]) -> int | None:
    regions = table.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return None
    first = regions[0]
    if not isinstance(first, Mapping):
        return None
    page = first.get("page_number")
    return page if isinstance(page, int) and not isinstance(page, bool) else None


def map_concept_values(structure: Mapping[str, object]) -> dict[str, object]:
    """For every real CanonicalConcept (UNKNOWN excluded -- synonyms_for(UNKNOWN)
    is always [], find_concept_rows would trivially return [] for it) and every
    table in structure["tables"], run find_concept_rows(table["grid"], concept)
    and flatten every {row_label, values:[{column_header, raw, value}]} match
    into one FLAT record per (row, column): {concept, row_label, value, raw,
    column_header, column_index, page, table_index}. `concept` is
    CanonicalConcept.value (a plain str, matching map_document's mappings[].
    concept convention). NEVER deduplicates or picks "the" match for an
    ambiguous concept (RT-6 decision 5's "Cost of revenue" false positive is
    preserved verbatim, tagged and returned, not filtered) -- every consumer
    sees row_label and can judge for itself. Degrades to
    {"schema_version": 1, "concept_values": []} on missing/malformed
    structure/tables/grid, never raises."""
    tables_raw = structure.get("tables")
    tables = tables_raw if isinstance(tables_raw, list) else []
    out: list[dict[str, object]] = []
    for table_index, table in enumerate(tables):
        if not isinstance(table, Mapping):
            continue
        grid = table.get("grid")
        if not isinstance(grid, Mapping):
            continue
        page = _first_page(table)
        for concept in CanonicalConcept:
            if concept is CanonicalConcept.UNKNOWN:
                continue
            for match in find_concept_rows(grid, concept):
                row_label = cast(str, match.get("row_label", ""))
                values = cast(list[dict[str, object]], match.get("values", []))
                for column_index, v in enumerate(values):
                    out.append({
                        "concept": concept.value,
                        "row_label": row_label,
                        "value": v.get("value"),
                        "raw": v.get("raw"),
                        "column_header": v.get("column_header"),
                        "column_index": column_index,
                        "page": page,
                        "table_index": table_index,
                    })
    return {"schema_version": 1, "concept_values": out}
```
Cost note: `for concept in CanonicalConcept` (15 real concepts) × 261 tables × ~10 rows/table on the real
doc ≈ 39,000 cheap token-subsequence checks — the same per-request cost RT-6's `concept_lookup` route
already pays per single concept per document today, just run for all 15 concepts once instead of one
concept per request; negligible (`find_concept_rows` is O(rows × synonyms), no I/O, no external call).

### 3.3 `fdi.pipeline.stages.ontology` — `OntologyStage` gains `structure`

```python
"""OntologyStage: a DERIVED stage that maps the AST's section headings/
paragraphs onto CanonicalConcepts (map_document) AND the structure artifact's
table grids onto CanonicalConcept numeric values (map_concept_values),
merging both into ONE artifact. Auto-chains off AstStage AND StructureStage
via deps=("ast", "structure") -- Phase 4."""

from collections.abc import Mapping
from typing import cast

from fdi.core.concept_values import map_concept_values
from fdi.core.ontology import map_document
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class OntologyStage:
    name = "ontology"
    version = 2
    deps: tuple[str, ...] = ("ast", "structure")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        doc_mappings = map_document(ast)
        values = map_concept_values(structure)
        return {
            "schema_version": 2,
            "mappings": doc_mappings["mappings"],
            "concept_values": values["concept_values"],
        }


REGISTRY.register(OntologyStage())
```

### 3.4 `fdi.core.quality` — reconciliation validators (additive)

```python
_ABS_TOL = 0.05
_REL_TOL = 0.001  # 0.1% -- see design spec §2 decision 6

# Concept-string triples/pairs each validator below reads (documentation +
# the literal values passed to _numeric_entries -- not destructured tuples,
# named here so the three checks' concept sets are visible at a glance).
_BS_PRIMARY = ("TOTAL_ASSETS", "TOTAL_EQUITY_AND_LIABILITIES")
_BS_FALLBACK = ("TOTAL_ASSETS", "EQUITY", "TOTAL_LIABILITIES")
_PL = ("TOTAL_INCOME", "OPERATING_EXPENSE", "PROFIT_BEFORE_TAX")
_CF = ("OPENING_CASH_BALANCE", "NET_CHANGE_IN_CASH", "CLOSING_CASH_BALANCE")


def _reconciles(a: float, b: float) -> bool:
    return abs(a - b) <= max(_ABS_TOL, _REL_TOL * max(abs(a), abs(b)))


def _norm_header(h: object) -> str:
    return h.strip().lower() if isinstance(h, str) else ""


def _numeric_entries(
    concept_values: list[object], concept: str
) -> list[Mapping[str, object]]:
    out = []
    for e in concept_values:
        if not isinstance(e, Mapping) or e.get("concept") != concept:
            continue
        v = e.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(e)
    return out


def _period_pairs(
    a_entries: list[Mapping[str, object]], b_entries: list[Mapping[str, object]]
) -> list[tuple[Mapping[str, object], Mapping[str, object]]]:
    """§2 decision 7: exact column_header match first (non-blank only);
    positional column_index fallback if that yields nothing at all."""
    exact = [
        (a, b) for a in a_entries for b in b_entries
        if _norm_header(a.get("column_header")) == _norm_header(b.get("column_header"))
        and _norm_header(a.get("column_header"))
    ]
    if exact:
        return exact
    return [
        (a, b) for a in a_entries for b in b_entries
        if a.get("column_index") == b.get("column_index")
    ]


def _closest(
    pairs: list[tuple[Mapping[str, object], Mapping[str, object]]],
) -> tuple[Mapping[str, object], Mapping[str, object]] | None:
    """§2 decision 7: the pair minimizing |a.value - b.value| -- doubles as
    an implicit disambiguator among multiple candidate rows for a concept."""
    if not pairs:
        return None
    return min(pairs, key=lambda ab: abs(cast(float, ab[0]["value"]) - cast(float, ab[1]["value"])))


def balance_sheet_balances(ontology: Mapping[str, object]) -> list[Issue]:
    cv = ontology.get("concept_values")
    concept_values = cv if isinstance(cv, list) else []
    assets = _numeric_entries(concept_values, "TOTAL_ASSETS")
    tel = _numeric_entries(concept_values, "TOTAL_EQUITY_AND_LIABILITIES")
    pair = _closest(_period_pairs(assets, tel))
    label = "total equity and liabilities"
    if pair is None:
        # §2 decision 8 fallback: EQUITY + TOTAL_LIABILITIES summed per period
        equity = _numeric_entries(concept_values, "EQUITY")
        liab = _numeric_entries(concept_values, "TOTAL_LIABILITIES")
        summed = [
            ({**e, "value": cast(float, e["value"]) + cast(float, l["value"])}, e, l)
            for e, l in _period_pairs(equity, liab)
        ]
        best = _closest([(a, s) for a in assets for s, _, _ in summed for _ in [1]]) \
            if summed else None
        # (full fallback pairing/citation logic elided here for brevity -- same
        # shape as the primary path, citing both the EQUITY and TOTAL_LIABILITIES
        # rows that were summed)
        pair = best
        label = "equity + total liabilities"
    if pair is None:
        return []
    a, b = pair
    if _reconciles(cast(float, a["value"]), cast(float, b["value"])):
        return []
    return [_issue(
        "warning", "quality.balance_sheet_imbalance",
        f"Total assets ({a['value']}, table {a['table_index']} row {a['row_label']!r}, "
        f"period {a['column_header']!r}) does not equal {label} "
        f"({b['value']}, table {b['table_index']} row {b['row_label']!r}, "
        f"period {b['column_header']!r}); diff={abs(cast(float, a['value']) - cast(float, b['value'])):.2f}.",
    )]


def profit_and_loss_foots(ontology: Mapping[str, object]) -> list[Issue]:
    """total_income - total_expenses (OPERATING_EXPENSE) vs profit_before_tax,
    triple-aligned the same way as balance_sheet_balances but over three
    concepts; emits quality.profit_and_loss_does_not_foot. Full pairing logic
    mirrors the pattern above, extended to a 3-way closest-triple selection
    minimizing |(income - expense) - pbt|."""


def cash_flow_ties(ontology: Mapping[str, object]) -> list[Issue]:
    """opening_cash_balance + net_change_in_cash vs closing_cash_balance;
    emits quality.cash_flow_does_not_tie. Same 3-way pattern as
    profit_and_loss_foots. Documented: on the real doc this validator will
    have fewer NET_CHANGE_IN_CASH matches than the other two concepts (§2
    decision 2's normalize_label parenthetical-stripping gap) until that
    synonym set is tuned -- a missing match means this validator emits
    nothing for that document, not a false pass or false warning."""


RECONCILIATION_VALIDATORS: tuple[Validator, ...] = (
    balance_sheet_balances, profit_and_loss_foots, cash_flow_ties,
)
```
`Issue`/`Validator`/`_issue`/`_KNOWN_SEVERITIES`/`AST_VALIDATORS`/`STRUCTURE_VALIDATORS`/`validate_all`/
`validate` — **zero changes**. `RECONCILIATION_VALIDATORS` is a third exported tuple, symmetric with the
two that already exist, each validator taking the SAME `Mapping[str, object]` (here: the `ontology`
artifact) → `list[Issue]` shape every other validator already has.

### 3.5 `fdi.pipeline.stages.quality` — `QualityStage` gains `ontology`

```python
class QualityStage:
    name = "quality"
    version = 3
    deps: tuple[str, ...] = ("ast", "structure", "ontology")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        ontology = cast(Mapping[str, object], await ctx.read("ontology"))
        return validate_all([
            (ast, AST_VALIDATORS),
            (structure, STRUCTURE_VALIDATORS),
            (ontology, RECONCILIATION_VALIDATORS),
        ])
```

### 3.6 `fdi.core.knowledge` — two additive keys (recommended, not mandatory)

```python
def consolidate(
    semantic: Mapping[str, object], ontology: Mapping[str, object], quality: Mapping[str, object],
) -> dict[str, object]:
    ...  # section_count/section_types/concepts/concept_count logic UNCHANGED
+   cv = ontology.get("concept_values")
+   concept_values_count = len(cv) if isinstance(cv, list) else 0
+   issues_raw = quality.get("issues")
+   issues = issues_raw if isinstance(issues_raw, list) else []
+   _RECON_CODES = {
+       "quality.balance_sheet_imbalance", "quality.profit_and_loss_does_not_foot",
+       "quality.cash_flow_does_not_tie",
+   }
+   reconciles = not any(
+       isinstance(i, Mapping) and i.get("code") in _RECON_CODES for i in issues
+   )
    return {
        "schema_version": 1,
        "section_count": section_count,
        "section_types": section_types,
        "concepts": concepts,
        "concept_count": concept_count,
+       "concept_values_count": concept_values_count,
+       "reconciles": reconciles,
        "quality": {...},  # unchanged
    }
```
`reconciles` defaults `True` when there is nothing to reconcile against (no matching concepts anywhere,
§2 decision 7's "emit nothing" case) — this mirrors `validate_all`'s own `ok = error_count == 0` posture
(absence of a detected problem reads as "fine," not "unknown"); a caller wanting to distinguish "verified
reconciling" from "nothing to check" should read `concept_values_count` alongside it. `quality["issues"]`
was already being passed into `consolidate` (the full `quality` artifact, not a pre-trimmed summary) —
reading one more of its keys is not a new input, just a previously-unread one.

## 4. Error handling

- `map_concept_values`: a malformed/missing `structure`/`structure["tables"]`/a table entry that isn't a
  `Mapping`/a missing or non-`Mapping` `grid` → that table contributes zero entries, never raises (mirrors
  `find_concept_rows`'s own "degrade to `[]`, never raise" contract, one layer up). `map_concept_values({})
  == {"schema_version": 1, "concept_values": []}`.
- `_first_page`: missing/malformed `bounding_regions`, a non-int/`bool` `page_number` → `None`, never
  raises (identical defensive shape to `core.provenance._page_of`/Phase 2's `cell_pages` derivation).
- `OntologyStage.run`: unaffected by a `structure` artifact with zero tables (empty `concept_values`,
  `mappings` computed as before) — no special-casing needed, `map_concept_values` already degrades.
- `balance_sheet_balances`/`profit_and_loss_foots`/`cash_flow_ties`: a malformed/missing `ontology`
  or `ontology["concept_values"]` → `_numeric_entries` returns `[]` for every concept → `_period_pairs`
  returns `[]` → `_closest` returns `None` → the validator returns `[]` (no issue) — the SAME "nothing to
  check is not a problem" posture as §2 decision 7. `None`/non-numeric/`bool` `value` entries are excluded
  by `_numeric_entries` before any arithmetic — never a `TypeError` from comparing `None` to a `float`. A
  concept with entries but every value `None` (all-blank column) behaves identically to "no matches at
  all" for that concept.
- `_reconciles`: pure numeric comparison, cannot raise on any `float` input (its only callers pass values
  already filtered non-`None`/non-`bool` by `_numeric_entries`).
- `consolidate`: `ontology.get("concept_values")`/`quality.get("issues")` missing or non-`list` → both
  degrade to `0`/`[]`/`True` respectively (never raises) — the exact defensive `isinstance` pattern every
  other field in this function already uses; `consolidate({}, {}, {})` remains a fully-defined, zeroed
  record (`concept_values_count: 0, reconciles: True`).

## 5. Testing

- **Unit `test_ontology.py`** (extend): the 6 new `CanonicalConcept` members exist with the expected
  string values; `test_synonyms_for_every_concept_nonempty_except_unknown` (already parametrized over
  `for concept in CanonicalConcept`) automatically covers them — must still pass, proving every new
  concept's synonym list is non-empty; `map_label`/`map_document` behavior on the existing 32-mapping
  real-doc-shaped fixtures is **byte-identical** (none of the 6 new synonyms match any existing fixture's
  bare headings).
- **Unit** (new), `test_concept_values.py`: a synthetic `structure` with one table (2-column-header +
  2-data-row grid: `["Particulars","FY24","FY23"], ["Total revenue from operations","12,345","10,000"],
  ["Total Assets","16,427.37","14,213.08"]`) → `map_concept_values` returns 4 entries (2 rows × 2 data
  columns), each with the right `concept`/`row_label`/`value`/`raw`/`column_header`/`column_index`/
  `table_index`; `page` sourced from the table's `bounding_regions[0]["page_number"]`; a second table
  with `bounding_regions: []` → `page: None` for its entries. The decision-4 regression case: a row
  `"Cost of revenue"` → an entry tagged `concept: "REVENUE"` IS present (pinned, not filtered — the exact
  false positive, proving `map_concept_values` does not attempt to fix RT-6's known limitation).
  `map_concept_values({})`/`map_concept_values({"tables": "not-a-list"})` → `{"schema_version": 1,
  "concept_values": []}`. A table with a malformed `grid` (missing keys) → contributes nothing, sibling
  tables unaffected.
- **Unit `test_quality.py`** (extend): fixture `ontology` artifacts built directly (no pipeline) —
  - `balance_sheet_balances`: `TOTAL_ASSETS`/`TOTAL_EQUITY_AND_LIABILITIES` entries with equal values,
    same `column_header` → `[]`; a genuine 100-unit mismatch → one `quality.balance_sheet_imbalance`
    warning citing both values, the diff, table_index, row_label, column_header; a 0.02 mismatch (within
    `_ABS_TOL=0.05`) → `[]`; a 50-unit mismatch on figures around 100,000 that IS within the 0.1% relative
    band but NOT the 0.05 absolute floor → confirms the `max(...)` formula picks the larger bound, not the
    absolute one unconditionally.
  - Fallback path: no `TOTAL_EQUITY_AND_LIABILITIES` entries, but `EQUITY` + `TOTAL_LIABILITIES` entries
    summing to (within tolerance of) `TOTAL_ASSETS` → `[]`; summing to something that does NOT match →
    one warning citing "equity + total liabilities" in its message.
  - Disambiguation regression: `TOTAL_ASSETS` has one entry (16427.37); `TOTAL_EQUITY_AND_LIABILITIES`
    has TWO entries for the same period — one at 16427.37 (correct), one at 999.00 (a spurious/duplicate
    match from elsewhere) → `[]` (closest pairing selects the reconciling 16427.37 entry, proving §2
    decision 7's "closest pairing is an implicit disambiguator" claim, not just asserted in prose).
  - No-data case: empty `concept_values` → all three validators return `[]` (never `None`, never raise).
  - `profit_and_loss_foots`/`cash_flow_ties`: analogous exact-match / tolerance-boundary / no-data cases
    using the §1 ground-truth-shaped numbers (20925.03/18795.89/2129.14 and 871.50/-643.61/227.90 — the
    latter specifically exercises the "reconciles within tolerance despite 0.01 real-world rounding" case
    this phase's tolerance constants were sized for).
  - `RECONCILIATION_VALIDATORS` is a 3-tuple of exactly these three functions; `validate_all([(ontology,
    RECONCILIATION_VALIDATORS)])` composes correctly (reuses the existing `validate_all` machinery
    unmodified — no new test needed for `validate_all` itself, only for the new validators feeding it).
- **Unit `test_knowledge.py`** (extend, if adopted per §3.6/§6's smaller fork): a `quality` artifact whose
  `issues` includes one `quality.balance_sheet_imbalance` entry → `consolidate(...)["reconciles"] is
  False`; an `issues` list with only `ast.*`/`structure.*` codes (no reconciliation code) →
  `reconciles is True`; an `ontology["concept_values"]` with 4 entries → `concept_values_count == 4`;
  `consolidate({}, {}, {})` → `concept_values_count == 0`, `reconciles is True`.
- **Integration `test_ontology_stage.py`** (extend): `resolver.successors("structure")` now includes
  `"ontology"` (the new second dep chains automatically, same assertion style as
  `test_quality_chains_off_ast_and_structure`'s existing `resolver.successors("structure")` check); a
  `FakeOcrClient` fixture with ONE table carrying a numeric data row (the codebase currently has **no**
  such fixture reused across tests per RT-6's own integration test note — build a small one inline, same
  approach RT-6's `test_tables_lookup.py` took) → the stored `ontology` artifact has both a non-empty
  `mappings` (from the existing AST-label path, unchanged) and a non-empty `concept_values` (from the new
  `structure` path); `artifact_key(doc.id, "ontology", 2, derived_input_hash([ast.output_hash,
  structure.output_hash]))` — deps-order-sensitive, confirms the `("ast","structure")` tuple order.
- **Integration `test_quality_stage.py`** (extend): `resolver.successors("ontology")` includes `"quality"`;
  a chain producing an `ontology.concept_values` with a genuine BS mismatch → the stored `quality`
  artifact's `issues` includes `quality.balance_sheet_imbalance`, `warning_count` incremented,
  `error_count`/`ok` **unaffected** (still `True`/no errors — proving decision 9's "never error" holds
  end-to-end, not just in the unit test); `artifact_key(doc.id, "quality", 3,
  derived_input_hash([ast.output_hash, structure.output_hash, ontology.output_hash]))` — three-hash tuple,
  order-sensitive; existing `test_quality_chains_off_ast_and_structure`/`test_quality_merges_ast_and_
  structure_defects_into_one_report` updated for the new `version`/`deps`/hash-tuple, their actual
  assertions about `ast.*`/`structure.*` issue codes unchanged (no reconciliation-triggering data in
  those two fixtures' empty/near-empty tables, so their `issues` lists gain nothing new).
- **Real-doc regression (scratchpad, documented — not committed, same posture as every prior phase's
  real-doc check):** re-run ingestion on the real STTL doc through Phase 4; confirm (a)
  `ontology.concept_values` contains entries for `TOTAL_ASSETS`/`TOTAL_EQUITY_AND_LIABILITIES` whose
  values match ground truth to the OCR/grid-extraction accuracy already measured (audit: BS grid 79%,
  P&L grid 96%) — this phase does not change extraction accuracy, only whether it gets checked; (b) on
  this real, correctly-balancing document, `balance_sheet_balances`/`profit_and_loss_foots` produce ZERO
  warnings (the audit's own headline finding — the pipeline should now be ABLE to prove this, not just
  assert it); (c) measure `cash_flow_ties`'s actual match rate for `NET_CHANGE_IN_CASH` against the real
  document's phrasing — the quantified proof (or disproof) of §2 decision 2's documented weak-synonym
  concern, feeding a synonym-list follow-up if the match rate is as low as predicted; (d) spot-check that
  `REVENUE`'s `concept_values` entries include the "Cost of revenue" false positive on the real doc (RT-6's
  decision 5 finding, now visible through this new path too) to confirm decision 4's "never filter" claim
  holds against real data, not just a synthetic fixture.
- **Gates:** grep every stale `OntologyStage.version==1`/`ontology.schema_version==1`/
  `QualityStage.version==2`/`quality`-artifact-shape/`KnowledgeStage.version==1` literal and update. Full
  suite green; `ruff`, `mypy --strict`, `lint-imports` 1-kept/0-broken — confirm specifically that
  `fdi.core.concept_values` appears in the import graph as `ontology ← tables ← concept_values` (no cycle)
  and that `core` still imports nothing from `pipeline`/`platform` (every touched/new function in this
  phase takes plain `Mapping`s/lists, zero I/O, zero external call, zero new config).

## 6. Explicitly deferred (and the forks a human should decide)

- **FORK — new `fdi.core.concept_values` module (recommended, §2 decision 1) vs. folding extraction logic
  directly into `quality.py`.** This spec's clearest architectural decision: a shared home is
  IMPOSSIBLE inside `ontology.py` or `tables.py` without a real import cycle (not a style call — Python
  would refuse to import it), so the only two live options are a third core module (recommended: keeps
  `quality.py` validators-only, mirrors the existing "ontology extracts, quality validates" split) or
  merging value-extraction into `quality.py` itself (rejected above, but not unreasonable — it would mean
  one fewer file, at the cost of `quality.py` also owning concept-matching, a responsibility its name and
  every existing docstring in this codebase currently assigns to `ontology`).
- **FORK — augment `ontology` additively (recommended, §2 decision 5) vs. a new `concept_values` artifact.**
  Recommended for DAG simplicity (one new `QualityStage` dep, not two) and to keep "what a document's
  ontology says" in one artifact a caller reads once. The alternative gives value-extraction its own
  independent cache/version lifecycle (e.g., a future fix to `parse_numeric` could re-run just
  `concept_values` without invalidating `mappings`) — a real, if currently unneeded, decoupling benefit a
  human with cache-granularity concerns might prefer.
- **FORK (smaller) — `knowledge.consolidate` gains `concept_values_count`/`reconciles` (recommended,
  §3.6) vs. leaving `knowledge` untouched.** The task explicitly left this open ("+ knowledge
  if it should surface values"). Recommended because it is the one place today that answers "does this
  document's financials make sense" as a single field, closing the audit's related (not identical) finding
  that "`Knowledge`... no numeric roll-ups" — but it is genuinely optional: `quality["warning_count"]`
  already reflects reconciliation warnings today with zero `knowledge.py` change, so a human could defer
  this specific, small addition without losing the underlying signal, just its convenient single-field form.
- **NOT in scope, by direct instruction:** `graph.py`, `ast.py`, `structure.py`, `retrieval.py` — untouched,
  read-only. **NOT in scope:** re-implementing/modifying `core/tables.py`'s `find_concept_rows`/
  `parse_numeric`/`filter_tables` — called exactly as RT-6 shipped them. **NOT in scope:** fixing
  `normalize_label`'s parenthetical/slash punctuation-stripping gap (§2 decision 2's `NET_CHANGE_IN_CASH`
  caveat) — shared logic, oversized blast radius for this phase, left as a documented follow-up. **NOT in
  scope:** statement identification via table `caption`/`footnotes` (§2 decision 12) — deferred to Phase
  3b's `table_refs`/section-heading membership, a materially stronger signal than the near-empty (1/261)
  caption field. **NOT in scope:** disambiguating RT-6 decision 5's "Cost of revenue"-style false positives
  in general (only the three named reconciliation validators get an implicit disambiguation as a side
  effect of closest-pairing selection, §2 decision 7 — `concept_values` itself, and any OTHER future
  consumer of it, still sees every match, unfiltered). **NOT in scope:** exposing `concept_values` through
  a new or existing public API route — RT-6's `GET /documents/{doc_id}/concepts/{concept}` route already
  reads live from `structure` per-request (RT-6 decision 1) and is unaffected by this phase's ontology-
  artifact caching of the same underlying computation; wiring a route that reads the cached
  `ontology.concept_values` instead is a separate, later decision, not needed for reconciliation to work.

## 7. Done criteria

1. `CanonicalConcept` (16 members incl. `UNKNOWN`) and `_SYNONYMS` in `fdi/core/ontology.py` gain the 6
   new concepts (§2 decision 2, §3.1); `map_label`/`map_document`/`normalize_label`/`synonyms_for` need
   and receive zero code changes; existing `map_document` behavior on real-doc-shaped fixtures is
   byte-identical.
2. `fdi.core.concept_values.map_concept_values(structure)` (new, pure module, §2 decision 1, §3.2) flattens
   `find_concept_rows` matches across every table/concept into `{concept, row_label, value, raw,
   column_header, column_index, page, table_index}` records; never deduplicates/filters ambiguous matches
   (§2 decision 4); degrades to `{"schema_version": 1, "concept_values": []}` on malformed input, never
   raises; imports only `fdi.core.ontology` + `fdi.core.tables` (no cycle, verified by `lint-imports`).
3. `OntologyStage` (`v2`, `deps=("ast","structure")`) emits `{schema_version: 2, mappings: [...],
   concept_values: [...]}`, additive over today's `{schema_version: 1, mappings: [...]}`.
4. `quality.py` gains three reconciliation validators (`balance_sheet_balances`, `profit_and_loss_foots`,
   `cash_flow_ties`, exported as `RECONCILIATION_VALIDATORS`) that read `ontology["concept_values"]`,
   align matched rows by period (§2 decision 7: exact `column_header`, else `column_index` fallback),
   disambiguate ambiguous concept matches via closest-pairing, and emit `"warning"`-only issues (never
   `"error"`) with the actual numbers/citations embedded in the message when a reconciliation check fails
   beyond `max(0.05, 0.001 × max(|a|,|b|))` tolerance (§2 decision 6, grounded in the real ground truth's
   own 0.01 rounding slack); emit nothing when the required concept(s) have no matches anywhere (§2
   decision 7's "missing is not a defect" rule). `Issue`/`Validator`/`validate_all` unchanged.
5. `QualityStage` (`v3`, `deps=("ast","structure","ontology")`) merges `RECONCILIATION_VALIDATORS` into its
   existing `validate_all` call alongside `AST_VALIDATORS`/`STRUCTURE_VALIDATORS`; outer `quality` artifact
   `schema_version` stays `1`.
6. (Recommended, not mandatory per §6's smaller fork) `fdi.core.knowledge.consolidate` additively gains
   `concept_values_count`/`reconciles`; `knowledge` artifact `schema_version` stays `1`; `KnowledgeStage.
   version 1 → 2`; `deps` unchanged.
7. `structure.py`, `tables.py`, `ast.py`, `graph.py`, `retrieval.py` show zero diff (confirmed by the
   phase's own PR diff, not just by design intent) — Phase 4 stays fully parallel to Phase 3b/5.
8. Unit + integration tests cover every new function/shape change, every degrade-not-raise path, the
   decision-4 false-positive-is-preserved regression, the decision-7 closest-pairing-disambiguates
   regression, and the decision-6 tolerance-boundary cases (§5); all stale `schema_version`/`.version`/
   `deps` literals across `ontology`/`quality`/`knowledge` tests updated. All four gates green (`ruff`,
   `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); `core` stays pure — no new I/O, no new
   external call, no new config in any touched or new function.
9. Real-doc regression (scratchpad, §5) shows: `concept_values` populated with real page/table_index
   citations; **zero** `balance_sheet_balances`/`profit_and_loss_foots` warnings on the real, correctly-
   balancing STTL document (the audit's headline finding, now provably checked, not just true by
   inspection); a measured (not assumed) `NET_CHANGE_IN_CASH` match rate, feeding a documented synonym
   follow-up if low; the "Cost of revenue" false positive visibly present, unfiltered, in real
   `concept_values` output tagged `REVENUE`.
