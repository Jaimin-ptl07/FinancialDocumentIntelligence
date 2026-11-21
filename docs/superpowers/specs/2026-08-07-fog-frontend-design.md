# Design: Wiring the Financial Object Graph into the UI

**Date:** 2026-08-07
**Status:** Design, approved.
**Scope:** Frontend only. Upload a filing, watch the FOG stages run, and read the
resulting graph — facts, the proof behind each, where it is uncertain, and how
healthy it is.
**Depends on:** [`2026-08-03-financial-object-graph-design.md`](2026-08-03-financial-object-graph-design.md)

---

## 1. What this replaces

The UI drives `POST /extract/document` — the similarity-first path, one LLM call
per retrieval unit, returning a flat list of field records. The backend has since
grown a different and better answer: five DAG stages producing a Financial Object
Graph, where facts arrive as a by-product of `fog_reason` and every one carries
the evidence that produced it.

**Decision: the UI shows the FOG only.** `/extract/document` stays live in the API
(its deletion is gated on a measured cutover that has no baseline yet) but
disappears from the screen. Showing both would put two different answers for one
filing side by side with nothing to say which is real.

## 2. Architecture

**No new backend API.** `getArtifact(docId, stage)` already exists, so the feature
reads `fog_objects`, `fog_mapping` and `fog_reason` through it. The FOG's contract
is that artifacts *are* the product; a bespoke UI endpoint would fork that.

| File | Change |
|---|---|
| `frontend/lib/stages.ts` | Add the five FOG stages; `TERMINAL_STAGE` `"index"` → `"fog_reason"` |
| `frontend/lib/fog.ts` | **New.** Pure readers + types over the three artifacts |
| `frontend/components/fog/FogViewer.tsx` | **New.** The screen — PDF left, evidence right |
| `frontend/components/fog/FactsPanel.tsx` | **New.** Tag · value · method |
| `frontend/components/fog/AttentionPanel.tsx` | **New.** Contested + unmapped |
| `frontend/components/fog/HealthPanel.tsx` | **New.** Footing, checks, coverage, mapping cost |
| `frontend/app/documents/[docId]/page.tsx` | Swap `ReportViewer` → `FogViewer` |

`lib/fog.ts` holds no fetching and no React, matching `lib/stages.ts`'s posture, so
the artifact-shape logic is testable without mounting anything. That boundary
matters: the artifact bodies have changed shape ten times across phases 2–6, and
every change should break one focused test file rather than a component tree.

`ReportViewer` and the old `FieldList` stay on disk, unreferenced. They are the
fallback if the FOG screen proves wrong, and deleting them belongs with the
backend cutover.

### 2.1 Stage list

Adding the five stages to `STAGES` is enough for the ingest screen to display
them: `layerStages()` derives columns from `deps`, so nothing is hand-positioned
and a wrong dep cannot silently draw a correct-looking graph.

```ts
{ name: "basis",           deps: ["ast", "provenance"] },
{ name: "statement_index", deps: ["ocr", "structure", "basis"] },
{ name: "note_index",      deps: ["ocr", "structure", "basis"] },
{ name: "fog_objects",     deps: ["statement_index", "note_index", "structure", "ocr"] },
{ name: "fog_mapping",     deps: ["fog_objects"] },
{ name: "fog_reason",      deps: ["fog_mapping", "fog_objects"] },
```

`basis` is currently **missing** from `STAGES` (verified: zero occurrences) even
though the backend has run it since TR-1. It is included above because
`statement_index` depends on it, and `layerStages()` treats an unknown dep as
depth 0 — so omitting it would silently draw `statement_index` in the wrong
column rather than fail.

Every `deps` line above is transcribed from `backend/src/fdi/pipeline/stages/*.py`
as of 2026-08-07 and verified against the source, not recalled.

Moving `TERMINAL_STAGE` to `fog_reason` changes what the ingest screen waits for.
That is the intent: "ready" now means the graph is readable, not merely that the
vector index was written.

## 3. The screen

Existing split preserved — PDF left, evidence right — with three stacked panels in
one scrolling column. No tabs: the entire point is seeing a number next to its
doubts, and a tab hides exactly that.

### 3.1 Facts

`tag · value · method`, ordered by tag. The method is not decoration — it is what
turns "wrong number" into "wrong because", and it located every defect found
during phases 5 and 6. Rendered small-caps monospace, weight tracking the evidence
tier (`memory` / `lexical:section` / exact `lexical` heaviest, `llm` mid, bare
token overlap lightest), mirroring `core/fog_reason.py::_tier` exactly.

Clicking a fact navigates the PDF to its page (see §4).

### 3.2 Attention

Two lists, in this order:

**Contested** — tags more than one row claimed, ordered by `spread` **descending**.
A near-zero spread means the sources corroborate; a large one means one of them is
wrong. Each row shows the winner (label + value) and its losers, because the
winner alone cannot be judged.

**Unmapped** — labels nothing resolved, grouped by `reason`. The grouping is the
point: `model_call_failed` (a swallowed outage) must never read the same as
`model_said_unmapped` (an honest refusal), and before Phase 5 they were literally
indistinguishable.

### 3.3 Health

- **Footing** per statement, three-valued: `✓` passed, `⚑` failed, and *nothing
  rendered* when `checked: false`. "Could not check" must not read as failure.
- **Validation checks** — rule, expected vs actual, delta, severity.
- **Coverage** — found / derived / unreached.
- **Mapping** — lexical vs llm vs unmapped counts, `llm_calls` vs
  `llm_calls_billed`, and `prompt_candidates_dropped`. Cost is visible because
  `fog_mapping` is the only stage that spends money.
- **Schema usability** — `schema.usable`. A schema yielding zero concepts makes
  every basis map nothing, and its per-basis diagnostics are identical to an
  honest "this filing discloses nothing". This is the one banner worth shouting.

## 4. Provenance, and what it costs

`PdfCanvas` takes `{current, prior}` polygons. A FOG `LineItem` carries its
**address** — `page · table_index · source_row` — not its geometry, because the
FOG spec (§3.2) keeps geometry read-side in `core/provenance.py` rather than
duplicating polygons into every artifact.

**Decision: page-level navigation now, cell-precise highlighting next, via the
backend.** Clicking a fact jumps the PDF to its page. The obvious shortcut —
resolving `structure.tables[table_index].grid.cell_regions[source_row]` in
TypeScript — is rejected: it is a second implementation of a rule that already
exists in measured Python, and two representations of one rule drifting apart is
precisely the shape of the RITES note-grammar bug (the face parser and the heading
matcher accepted different grammars, and all 10 references resolved to nothing,
silently).

The follow-up is a backend read endpoint returning regions for a line-item id,
reusing `core/provenance.py`. Out of scope here.

## 5. Error handling

Every panel degrades independently and says why:

- **Artifact missing** (stage not run) → "not built yet", with the stage name. Not
  an error; a filing mid-ingest is the normal case.
- **Artifact malformed** → that panel renders empty with a note; the others still
  render. `lib/fog.ts` readers are total, mirroring `fdi.core`'s law, so a shape
  change breaks one panel rather than the page.
- **Basis selection** — a filing may carry `consolidated`, `standalone` or
  `unknown`. Default to **consolidated** (spec decision 2: it is what banks
  underwrite), fall back to whichever exists, and name the basis on screen. A
  number shown without its basis is not a number a bank can use.

## 6. Testing

`lib/fog.ts` gets unit tests over fixture artifacts — real shapes copied from
`out/fog-run-llm-p6b/`, not invented ones, since inventing fixtures is how the
`aliases` / `note_aliases` mismatch survived three tasks.

Each panel gets a render test: populated, empty, and malformed. The existing
suite (`page.test.tsx`, `Value.test.tsx`, …) sets the pattern.

## 7. Out of scope

Cell-precise highlighting (§4) · deleting the old extraction path · any backend
change · charts beyond the existing `ConfidenceHistogram` · editing the graph from
the UI. The FOG is evidence, and evidence is read-only.
