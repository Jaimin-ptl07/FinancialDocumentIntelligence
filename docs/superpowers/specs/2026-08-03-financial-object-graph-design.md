# Design: The Financial Object Graph — Domain 1, PDF → FOG

**Date:** 2026-08-03
**Status:** Design, approved. Supersedes the similarity-first extraction path.
**Scope:** Domain 1 only — Document Intelligence, ending at the Financial Object
Graph. Domains 2 (Reasoning) and 3 (Analytics) get their own spec → plan cycles.
**Baseline read out of code:** `docs/architecture/pipeline-end-to-end.md`
**Supersedes / completes:** `2026-08-01-note-driven-extraction-design.md`

---

## 1. The problem, restated

The output of this system is not extracted text. It is an **accounting model**:
402 canonical fields with validation, confidence, evidence, reconciliation,
derived values and an audit trail. A pipeline shaped as

```
Document → Retrieve → LLM → Fields
```

asks *"find this number"*. The taxonomy asks *"reconstruct the financial model of
a company"*. Those are different systems, and the second one needs a persistent,
accounting-typed object model that the first never builds.

### 1.1 What already exists (measured, do not re-earn)

An audit of the live code found that roughly 60% of the target architecture is
already present in `fdi.core` as pure, unit-tested modules — **but was never
wired to a route or a stage.** The live `/extract/document` is still the old
similarity-first path.

| Layer | State |
|---|---|
| L0 Document Intelligence | **Live** — 17 stages, `ocr → structure → normalize → ast → graph → provenance → basis` |
| L1 Statement Understanding | Built (`core/statements.py`), reached only as a helper inside `_face_pages` |
| L2 Financial Object Builder | Face rows only, in-memory, per-request. **Real gap** |
| L3 Note Intelligence | Built (`core/note_plan.py`), **imported only by tests** |
| L4 Policy Engine | **Missing** — deferred to domain 2 |
| L5 Financial Knowledge Graph | **Missing** — `core/graph.py` is a *document* graph, not accounting-typed |
| L6 Ontology Mapper | Built (`core/mapping.py`, `concepts.py`, `memory.py`), **wired to nothing** |
| L7 Composition | Built (`core/compose.py`), wired to nothing; an older `derivation.py` is live instead |
| L8 Validation | Live but thin (109 lines) — no roll-forwards |
| L9 Confidence | **Live and already evidence-based** — `triangulate()` + `calibrate()` |
| L10 Canonical Model | **Live** — `/factsheet/document`, coverage, XBRL |

These modules carry measured scars that a greenfield rewrite would silently
re-earn: the comma bug (`8,685 → 8`), the RITES note-grammar bug (0/30 refs
parsed), the table label-column fix (**156 → 173/188**), and the basis fallback
that took RIL from 0 extraction to 186/190.

**Therefore: build the genuinely-missing layers new; call the measured cores
rather than reimplementing them; cut over on a number.**

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Ground truth first**, 3 issuers (TCS, RITES, RIL), all statements, both bases | "Maximum accuracy" is unfalsifiable without it. Every prior win in this repo came from measurement; the single-issuer pilot provably could not validate a multi-issuer contract |
| 2 | **Consolidated primary, standalone secondary** | Consolidated is what banks and rating agencies underwrite. Both are extracted when present; the canonical model resolves to consolidated. Requires re-measuring the standalone-tuned defaults |
| 3 | **Async DAG stages; FOG is a cached artifact** | 40–80 LLM calls per document cannot live in a synchronous HTTP request. Stages give content-addressed caching, versioned backfills, `stage_run` audit rows and per-stage cost accounting for free |
| 4 | **FOG contract + measured cutover** (not greenfield, not incremental wire-up) | Same end architecture as a greenfield, but every layer lands with a number and there is never a window with no working system |
| 5 | **Policy engine → domain 2** | Its output is interpretive text, not numbers; it cannot be scored the way facts can. Build the scoreable layers first |
| 6 | **`fog_reason` is one stage, not three** | Compose/validate/confidence/coverage are all pure and cheap; each emits a named section of one artifact, inspectable without three more stage boundaries |

---

## 3. The FOG contract

### 3.1 The defining property: the FOG is pre-ontology

The FOG is the **issuer's own financial model, in the issuer's own words**, with
values parsed deterministically. It is not the 402 tags. `MAPS_TO` edges are the
single seam where the issuer's model meets the taxonomy.

This is what decouples document parsing from financial reasoning: documents
change from company to company, accounting concepts do not. Everything
downstream — ontology mapping, derivation, validation, ratios, comparison,
analytics — works on this graph rather than on OCR text.

### 3.2 Node and edge types

```
Node types                              Edge types
──────────                              ──────────
Statement   type · basis · page         CONTAINS         Statement→LineItem, Note→LineItem
            table_index · footing       REFERENCES       LineItem→Note      (printed; a FACT)
            period_labels               MAPS_TO          LineItem→Concept   (+ proof)
Note        ref · title · offset_window COMPOSES         LineItem→LineItem  (note total)
            page_span · basis           RECONCILES_WITH  face LineItem ↔ note total
LineItem    label · caption · ref       SAME_AS          cross-basis / cross-period
            current · prior · unit
            row_index · source_row
            origin
Period      label · end_date · current

RESERVED, not built in domain 1 (see decision 5):
Policy      section_id · page · span    GOVERNED_BY      LineItem→Policy
```

`Policy` / `GOVERNED_BY` are named here so the schema version that adds them is
additive rather than breaking. **Domain 1 emits neither.**

Every node carries its **address**: `page · table_index · source_row ·
span_offset`. That address resolves to exactly one table cell, so geometry
(`bounding_regions`) is derived **read-side** through the existing
`core/provenance.py` helpers rather than copied onto every node — which is how
`resolve_field_regions` already works today, and keeps the artifact from
duplicating polygon data that the `structure` grid already holds.

**`source_row`, not `row_index`, is the addressing component**, and the two are
deliberately both kept. `row_index` is the ordinal within a table's *valued*
rows: it is what `line_item_id` hashes on, so it must stay put when a header or
caption row is reclassified. `source_row` is the raw `cell.row_index` in the OCR
grid. `core/provenance.py::_table_row_regions` indexes the raw grid, so handing
it a valued-row ordinal returns the geometry of a different line — off by
however many header and caption rows precede it — or `None` near the end of the
table. A read-side highlight box drawn from `row_index` points at the wrong
line, silently.

The audit trail is therefore complete and cell-precise, without the FOG storing
a second copy of the geometry that would then have to be kept in sync.

### 3.3 Two consequences worth stating explicitly

**`REFERENCES` is never inferred.** The note column on a statement face is
printed navigation the issuer asserted:

```
Property, plant and equipment | 8(a) | 8,259
```

Similarity search provably cannot reproduce this — ~50 TCS taxonomy leaves were
null purely because retrieval never reached the note (the label "Land" is
generic and the schedule sits ~30 pages from the face). `REFERENCES` edges are
therefore built by deterministic parse only. When the column is absent — RIL
prints none on its P&L face — the graph records *absent*, and mapping falls back
to tier 2. **Note-following is an accelerator, never a prerequisite.**

**`LineItem` exists for note rows, not just face rows.** Today only face rows
become objects, in memory, per request. Persisting every note row as a
`LineItem` is what turns "Facility expenses 2,966" from a silent vanish into a
visible `unmapped` node, and what makes a note's own total available as the
**arbiter** for its components.

---

## 4. Stages and data flow

```
ocr ─┬─ structure ─┬────────────────────────────────┐
     └─ normalize ─ ast ─ graph ─ provenance ─ basis │
                                                     │
        ┌────────────────────────────────────────────┴──────┐
        ▼                                                   ▼
  statement_index  (ocr, structure, basis)          note_index  (ocr, structure, basis)
  L1 · pure · 0 LLM                                 L3a · pure · 0 LLM
        └────────────────────┬──────────────────────────────┘
                             ▼
                    fog_objects   (statement_index, note_index, structure)
                    L2 · PURE · 0 LLM
                             ▼
                    fog_mapping   (fog_objects, schema)
                    L6 · LLM · the ONLY billed stage
                             ▼
                    fog_reason    (fog_mapping)
                    L7/L8/L9 · PURE · 0 LLM
```

### 4.1 `statement_index` — deps `(ocr, structure, basis)`, pure

Find every face table by **content anchors, not headings**. Heading detection on
TCS returned pages 166, 167, 228, 252, 253 — three false positives — and could
not say *which* statement it had found. Score each table against the line names
Schedule III mandates (`total assets`, `revenue from operations`, `profit before
tax`, …), require ≥3 distinct anchors plus numeric content, then scope by basis
region.

**Verify by footing.** `income − expenses = PBT` ties on TCS, RITES and RIL —
that is what proves the right table was chosen, whatever found it.

Promotes `core/statements.py` from a per-request helper to a cached, inspectable
artifact.

### 4.2 `note_index` — deps `(ocr, structure, basis)`, pure

`ref → {offset window, page span, title, basis, tables in window}`.

Offset windows, not pages: TCS p301 holds *two* notes (13(a) at offset 897,095,
13(b) at 897,486), so page granularity hands both to whichever concept asks
first.

Basis scoping is mandatory — a report with both bases has two "note 8", and an
unscoped index resolves a reference to whichever came first. `find_statement_
pages` trims the front matter and the tail so the Board's Report and the AGM
notice (which restart their own `1)`, `4)`, `7)` numbering) cannot collide.

**The trim is derived PER REGION**, from the paragraphs inside that region —
never once per document. One document-wide span intersected with every region
lets the standalone face decide where the *consolidated* region ends, and on a
filing whose AGM notice precedes the statements that empties the primary basis
entirely, with every diagnostic reading as it would for an issuer who files no
consolidated accounts.

Each region records the span it was narrowed to, the raw span the basis anchors
delimited (`raw_page_lo` / `raw_page_hi`), what closed it (`bound`) and the refs
the bound excluded (`refs_beyond_bound`). Without the last one a truncated span
and a failed reference grammar both read `face_refs > 0, refs_resolved == 0`.

The reference grammar lives in **one place** next to `parse_note_ref`. It was
previously a caller argument, so the face parser accepted RITES's dotted refs
(`2.17`) while the heading matcher did not — all 10 references resolved to
nothing, silently. Fixing that took RITES from 0/10 to 8/10.

### 4.3 `fog_objects` — deps `(statement_index, note_index, structure)`, **pure, 0 LLM**

**The largest single accuracy lever in this design, and it makes no model call.**

Parse every face row and every note row into a `LineItem`; build `CONTAINS` and
`REFERENCES` edges. Values come from `core/numbers.parse_amount` — number
parsing is fully decidable (`'2,20,938' → 220938`, `'(468)' → -468`,
`'14,372.80' → 14372.80`, `'-' → None`) and delegating it to the model was
measurably wrong.

A row with no amount **cells** is a **caption**, not a concept, and it is
carried onto every row beneath it. RIL prints "a) Owners of the Company" under
three different captions; the label alone is undecidable without it.

The test is STRUCTURAL, not "did anything parse". A nil row
(`Communication | - | -`) has amount cells that the issuer filled in with an
explicit nil marker, so it is a `LineItem` with `current=None` — not a caption.
Treating it as one re-captions every following row of the block (`Deletions`
filed under "Communication" instead of "Gross block"), and movement schedules
print `-` constantly. A wrong caption is worse than an absent one: it is an
affirmatively false claim on the field §4.4's mapper arbitrates with.

On TCS P&L, 22 of 31 facts already came from the face with **no LLM call at
all**. Extending the same deterministic parse to note rows shrinks the model's
job from *"find and read this number"* to *"which concept is this row?"* — the
one thing it is reliably good at.

### 4.4 `fog_mapping` — deps `(fog_objects, schema)`, **LLM**

The push model: not *"what is `LNPC`'s value?"* but **"here is Note 13(b) — list
every line item, then map each to one of these concepts, or mark it unmapped."**
One call per statement face, one per note.

Measured on the same face tables across three issuers:

| | TCS | RITES | RIL |
|---|---|---|---|
| alias only | 69% | 58% | **30%** |
| + normalization | 72% | 68% | 33% |
| **+ semantic mapper (1 call)** | **90%** | **79%** | **80%** |

Alias matching cannot be the only mechanism. Uses `core/mapping.py` (push-model
mapper + proof), `core/concepts.py` (bounded candidate sets) and
`core/memory.py` (mapping memory, so a second run of a document is deterministic
and cheaper).

**Face rows and note rows need different candidate sets.** Face rows are
summaries and match *parents*; note rows are detail and match *descendants*.
Conflating them left 15 of TCS's 28 face rows unmapped, including "Revenue from
operations" and every waterfall total.

`read_note_reply` drops any tag the unit never offered — a fact attributed to a
note that does not mention it is unauditable.

**Why the LLM is isolated in exactly one stage:** a validation-rule change, a new
ratio, a confidence re-tune or a taxonomy edit re-runs `fog_reason` at zero
marginal cost and zero re-billing. Under today's synchronous route, every one of
those re-pays for the whole document.

### 4.5 `fog_reason` — deps `(fog_mapping)`, pure

Four engines, each emitting a named section of one artifact:

1. **Compose** (`core/compose.py`) — fixpoint over the tree: composition, then
   waterfall, then reconciliation. Because `derivation.inputs == child concepts`,
   it is a tree walk, not a lookup.
2. **Validate** — see §6.
3. **Confidence** (`core/confidence.py`) — `triangulate()` corroborates a tag
   read from both the face and its note (agreement boosts; disagreement lowers
   and flags, never silently picks a side); `calibrate()` folds validation
   outcomes into bands.
4. **Coverage** — found / derived / genuinely absent, against the fixed tag list,
   filtered by `applicability` (§5).

### 4.6 Retrieval becomes a four-tier fallback, recorded per fact

```
1. REFERENCES edge → offset window → note tables   deterministic, preferred
2. note TITLE matched against concept aliases      finds tax/EPS/auditor notes
                                                   the face never cites
3. hybrid_search over the basis-scoped pool        demoted to last resort
4. not_answered                                    never a guess
```

Tier 3 is exactly today's primary path, kept as a safety net — RIL needs it.
Every fact records which tier produced it, so a wrong value traces to its
retrieval strategy.

### 4.7 DAG changes

**Remove** (verified by grep that nothing reads their artifacts): `linking`,
`graph_load` (fails every run; Neo4j down), and the closed
`ontology → quality → knowledge` loop. Disable rather than delete — `ontology`
and `quality` are possibly harvestable later.

**Demote to tier 3:** `semantic → chunks → embedding → index`. Still built, no
longer the primary retrieval path.

**Fix:** `FDI_MILVUS_URI` is absent from the container environment, so `index`
has returned `{skipped: "vector_store not configured"}` on **every document ever
uploaded through the API**. Tier 3 does not currently work at all.

### 4.8 API surface

`/extract/document` and `/factsheet/document` become **cheap reads over the FOG
artifact** — no retrieval, no LLM, no minutes-long block. Ingestion is `POST` +
poll, as it already is.

---

## 5. The executable ontology

v4.3 already carries `tag`, `name`, `aliases`, `note_aliases`, `definition`,
`origin`, `derivation{inputs, weights}`, and the nested parent/child tree —
where **the tree is the calculation graph**, machine-checked at build time by
`core/schema.py::validate_composition` (`derivation.inputs == child concept
tags`, one formula per concept, no duplicate tags).

### 5.1 Added

| Field | Why |
|---|---|
| `expected_statements` / `expected_notes` | Scopes the candidate set handed to the mapper. Both measured mapping bugs were "a candidate set that looked complete from one caller's vantage and was silently partial from another's" — this makes the set explicit and checkable |
| `applicability` (industry variants) | Required for honest metrics. ~180 of 402 P&L fields cannot fill for a services company; 25 extraordinary-item tags cannot fill for **any** Ind-AS filer. Without it, coverage permanently reads as broken and *absent* is indistinguishable from *missed* |
| `validation_rules` | Links a concept to the roll-forwards it participates in, making validation data rather than hand-written checks |

### 5.2 Rejected, with reasons

- **`discovery_strategy` / `search_priority`** — this is a property of the four
  fallback tiers (§4.6), which are uniform and measured. Encoding it per-concept
  duplicates that logic in 402 places and guarantees drift. This codebase has
  been bitten twice by exactly that shape.
- **Per-concept `confidence_rules` / `evidence_rules`** — confidence is derived
  from *evidence structure* (independent corroboration, note-total arbitration,
  validation outcome), which is uniform across concepts and already implemented.
  Confidence should come from evidence, not from 402 hand-written judgement
  calls.

**Deferred:** `accounting_policies` links → domain 2.

---

## 6. The validation engine

Grows from three thin families to:

```
identity         Assets = Equity + Liabilities
waterfall        PBT − Tax = PAT  ·  Revenue − OpEx = Operating Profit
cash tie         Opening Cash + Net Cash Flow = Closing Cash
roll-forward     Opening PPE + Additions − Disposals − Depreciation = Closing PPE
cross-statement  Depreciation: P&L == CF add-back == PPE note movement
reconciliation   face total == note total            (the RECONCILES_WITH edge)
tax              Current + Deferred = Total tax expense · ETR sanity band
```

Every result stays fully traceable: rule, tags, expected vs actual, delta,
tolerance, severity, passed.

### 6.1 The precedence rule (corrected by measurement)

```
reported value
  → else the sum of the children that ARE present, WITH completeness recorded
  → else null
```

Requiring *every* input was implemented and falsified: it collapsed derivation
from 10 facts to 1 and suppressed 37 nodes, because no real filer discloses
every child the taxonomy defines (TCS reports 2 of 5 revenue lines, 1 of 9
non-operating income items).

Each derived fact carries `complete`, `coverage` ("3/4"), `confidence` and the
`missing` tags.

### 6.2 Two hard constraints

1. **A partial value may never satisfy a reported total's reconciliation.** It
   cannot be used to claim the books tie. `TOTRNOTINR` reporting `unverified`
   because `RFONET` is partial is the *correct* answer.
2. **A node that is itself extractable-and-absent is never synthesised.** This
   is what stopped `ADMINEXP` being invented from a single child.

---

## 7. Ground truth and the cutover gate

### 7.1 Format

`docs/groundtruth/{issuer}-{fy}-{basis}.json` — a flat `{tag: value}` map per
statement, plus an explicit `not_reported: [tags]` list.

The `not_reported` list is the important half. It is the only way to separate
**absent** from **missed**, and without it recall is meaningless on a 402-tag
taxonomy where ~180 tags cannot apply to a services company.

### 7.2 The harness reports

Per issuer × basis × statement: **recall** (of tags actually reported),
**precision** (of committed values), the **absent-vs-missed split**, and
**cost** — plus a per-fact trace of which fallback tier produced it, so a
regression is diagnosable rather than merely visible.

### 7.3 The gate

Phase 0 scores the **current** pipeline against this GT first. That baseline is
what the FOG path must beat on all three issuers before `pipeline/extractor.py`
and the similarity path are deleted. Not "looks better" — a number, on the same
harness.

---

## 8. Error handling

- **`fdi.core` is total.** Never raises on any input shape; malformed input
  degrades field-by-field (`page → None`, `basis → "unknown"`).
- **Decision 10 — never raise.** One note's failure degrades *that note's*
  LineItems to `not_answered`, never the document.
- **New: degradation is explicit in the graph.** A missing `REFERENCES` edge
  means "this issuer printed no note column" (RIL), recorded as such, and
  mapping drops to tier 2. It never becomes a silent null. `absent ≠ missed` is
  a property of the data structure, not a convention.

One non-obvious failure mode carried forward: usage bookkeeping needs its own
guard **and its own rollback**. A failed telemetry insert poisons the shared
session, which silently killed retrieval for every later unit — TCS fell 125 → 71
filled and 19 → 9 correct, deterministically. Costing is bookkeeping; it must
never take extraction down.

---

## 9. Testing

- **Pure cores** — unit tests in the existing style. The ~800-test suite is the
  reason these modules are trustworthy.
- **Stages** — integration tests against fixture OCR artifacts. No live Azure in
  the default suite, matching the `Fake*Client` convention used throughout.
- **GT harness** — an acceptance gate run **manually against real documents**,
  not CI. It costs money and needs the real stack.

---

## 10. Phasing

| Phase | Work | Gate |
|---|---|---|
| **0** | GT harness + hand-keyed GT (3 issuers × 4 statements × basis) + baseline score of today's pipeline | A baseline number exists |
| **1** | `statement_index` + `note_index` stages | Face found, typed and footed on all 3 issuers, both bases; notes resolved ≥ TCS 9/11, RITES 8/10 |
| **2** | `fog_objects` | Deterministic-only recall measured, 0 LLM calls |
| **3** | `fog_mapping` | Mapping accuracy ≥ 90/79/80, on note rows as well as face rows |
| **4** | `fog_reason` | 0 composition violations; all §6 roll-forwards execute and trace |
| **5** | v4.3 assets / liabilities / equity / **cash flow** + `applicability` | 0 composition violations per module |
| **6** | Routes read the FOG · stage removals · `FDI_MILVUS_URI` · delete the old path | FOG path beats the Phase 0 baseline on all 3 issuers |

**Phase 5 carries a known hole:** the taxonomy has **no cash-flow module at
all**, and the CF content anchors currently miss both RITES and RIL. Two
independent gaps pointing at the same place. Cash flow is real authoring work,
not a port.

---

## 11. Open questions

1. **Duplicate tag `WCL`** — used twice in the liabilities taxonomy (working-
   capital loans at sr 1.2, Buyers Credit at sr 1.3). Whichever loads second is
   permanently unreachable. Needs a taxonomy decision, not a schema edit.
2. **`EXCHEXP` roll-up** — `CFL` / `EXCL` / `NONLMTM` / `NONLRSA` / `NONLRSL`
   have no pointer in the source sheet. The forex family is currently placed
   under `OTNONOEC` / `OTNONOENC` where the sheet supports it; `EXCHEXP` is left
   `extracted` with the open question retained.
3. **OCI tag collision** — two legitimately distinct OCI lines collide on one
   tag; `map_lines` claims a concept once, so the second is dropped. A schema
   gap, not a mapper bug.
4. **Consolidated re-tuning scope** — decision 2 makes consolidated primary, but
   every measured default in the repo (basis regions, note maps, the 173/188
   run) was tuned on standalone. Phase 0's baseline must be scored on both so
   the size of this shift is known before Phase 1, not discovered in Phase 6.
