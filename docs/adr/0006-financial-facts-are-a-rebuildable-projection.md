---
status: accepted
---

# Financial facts are a rebuildable Postgres projection of per-Report graph artifacts

Each Report's financial object graph artifacts (`fog_objects`, `fog_mapping`, `fog_reason`) stay the record of *how* its values were derived: line items, edges, which line won a Concept, and the reconciliation checks. Postgres holds the *cross-Report* view as tables:
- `financial_observation`: every value a source states.
- `financial_fact`: the value the fact rule picks.
- `financial_validation`: the graph's checks.
- `financial_projection`: what each Report version was projected from.

Those tables are a deterministic projection of the artifacts plus Invitee-reviewed Extracted values, and nothing else writes them. Drop them and `fdi-facts backfill` (or `rebuild`) brings back the same rows, with the same ids. This decides the design's ADR-006, as amended by S5 (#34).

## Decisions

- **Observation identity.** An Observation is identified by Report version × basis × Concept × Fiscal year × column (`current` / `comparative`) × source (`graph_face`, `graph_note`, `graph_composed`, `extraction_accepted`, `extraction_corrected`). Its row id is a UUIDv5 of that identity, so projecting twice cannot duplicate a row.
- **The projection is lossless by construction.** Within one Report it takes the winner the reasoning stage arbitrated (`fog_reason.fact_claims`), the same winner `score_pipeline.py` scores. `score_pipeline.py --facts` (via `fdi-facts export`) and `--reasoning` print the same table.
  - **Measured:** both TCS Reports, both bases, 605 facts, all identical. TCS FY2025-26 scores 33 correct / 0 wrong / 5 missed against the ground truth either way.
- **Printed text comes from the OCR, not a rebuilt graph.** The graph stores floats. Re-building it to carry strings would invalidate the model-mapped `fog_mapping` stage for every Report, re-paying Azure for no change in mapping.
  - Instead, the projection re-reads the very cells the graph parsed (`statements.printed_amounts`, which shares the face parser's column logic) and keeps the text only when it parses back to the graph's value.
  - Values are `NUMERIC` from the printed text (`parse_decimal`, the same grammar as `parse_amount`). Nothing is stored as a float.
  - **Measured on both TCS Reports:** every face value kept its printed text.
- **The unit is read from the page.** `(₹ crore)`, `(₹ in lakhs)` and similar are read from the text just above each table, and a little way into it, because Azure DI split `(₹ c|rore)` across the table boundary on TCS FY2024-25.
  - Per-share lines are rupees; counts and ratios carry no currency.
  - An unread unit is `unknown`, never assumed. Its Observation has no normalised value, and the Calculation engine refuses it.
- **The fact rule is one pure function** (`fdi.core.financial_facts.select_fact`):
  1. An Invitee correction wins.
  2. Otherwise the latest Report by Fiscal year, then version. Within a Report: face, then note, then an accepted Extracted value, then composed.
  3. Superseded Document versions never take part.
  4. A printed nil alone is `not_reported`; composed alone is `composed`.
- **A Restatement is the same face line differing, and nothing else.**
  - The design said "differ beyond rounding means restated". **Measured on TCS FY2024-25 against FY2025-26's comparative column:** all 132 face lines agree, and the 58 facts that differ are all note rows whose label repeats across notes ("Loans", "Others", "Dividend paid" 12 against 18,177). That is mapping noise, not the issuer's revision.
  - So a difference on the same face line (same label, beyond half a printed step of each) is a Restatement. Any other cross-Report difference is recorded as **sources differ**: both values kept, latest wins, never called a restatement.
- **A Company's facts are re-derived from all its Observations** in the same transaction that changes them. The transaction holds a row lock on the Company (and on the previous Company, when a Report was re-filed), taken in id order, so concurrent projections of one Company serialise and never deadlock.
- **Projection runs when its inputs can change:**
  - after `fog_reason` completes, or after the issuer stage files the Report (whichever comes second);
  - after an Invitee re-files a Report or corrects its Fiscal year (a `fdi.project_facts` task);
  - after a document is deleted (its Company's facts are re-derived from what remains);
  - on demand (`fdi-facts`).
  
  A projection key covers the input artifacts' hashes, the Report's Company, Fiscal year and period, the Concepts, the formulas and the reviewed values. Unchanged inputs are a no-op.
- **Concepts are rows seeded from the standard schema document**, never created by a model. An Observation's Concept is a foreign key.

## Considered Options

- **Projection with per-Report artifacts kept as the record** (chosen).
- **Postgres as the store of record**: one store, but the derivation (edges, arbitration, contested claims) would need its own relational model, or would be lost.
- **Artifacts only, read at query time**: no new tables, but every multi-year question opens N artifacts, and nothing can index across Reports.
- **Extend the graph build to carry printed strings**: the spec's first suggestion. It costs an Azure re-mapping of every Report for no change in mapping, and the same text is recoverable from the OCR the graph was built from.

## Consequences

- `financial_fact` is derived data. Reading it never needs the rule; changing the rule means re-deriving (`fdi-facts backfill --force`).
- Gaps are explained, not just absent:
  - `no_report`: no Report covers the year.
  - `not_processed`: a Report covers it, but isn't projected.
  - `missed`: FDI asked and got no answer, which is our defect.
  - `not_found`: FDI found no line. The graph cannot yet prove non-disclosure; see the follow-up ticket.
- A note table with no unit line keeps `unknown` units. Some are foreign-currency tables, where any guess would be wrong.
