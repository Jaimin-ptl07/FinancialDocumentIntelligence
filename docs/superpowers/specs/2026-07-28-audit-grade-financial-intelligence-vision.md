# Vision: From Extraction Engine to Audit-Grade Financial Intelligence Platform

**Date:** 2026-07-28
**Status:** North-star vision (per-milestone specs follow this document)
**Author:** brainstormed with the team

---

## 1. Why this document exists

The current system extracts financial figures from annual reports and returns
`{value, source, confidence}` per field against a retrieval-oriented schema
(`docs/architecture/field-extraction-schema.json`, v3.2.0: ~200 fields across 15
retrieval modules). Its provenance layer is genuinely strong — every extracted
field already resolves to **cell-precise bounding boxes**, split by current/prior
period, scoped to the actually-cited table (`fdi.core.provenance`).

But the product we are selling is to **banks**, and a bank does not buy
extraction — it buys **trust**. The current system "is just extraction": it has
no notion of a *derived* value, no *financial validation*, and no explanation for
anything it computes. This document defines the upgrade from an extraction engine
into an **auditable financial-intelligence platform**, and the new customer-
supplied taxonomy (`Q3VENI LATEST TAXANOMY.xlsx`) is the schema that anchors it.

## 2. The two invariants (design laws)

Every component in this platform exists to enforce one or both of these. If a
proposed feature serves neither, it is out of scope.

1. **Self-explaining** — no number exists without a traceable origin. An
   extracted number traces to a bounding box on a page; a computed number traces
   to a formula and the input tags it consumed. Provenance is **recursive**:
   follow any value down and it bottoms out at pixels on a page.
2. **Self-validating** — no number is trusted until it has been cross-checked
   against the financial relationships it must obey. **Confidence is a
   first-class, calibrated output, not an afterthought.** Accuracy is the
   top-priority product attribute.

## 3. This is not bespoke — it mirrors standards banks already trust

The model below is deliberately aligned with **XBRL**, the global standard for
machine-readable financial statements that India's MCA already mandates for
filings. This is a selling point: we implement a model auditors recognize.

| Standard concept | Our equivalent |
|---|---|
| XBRL **concept/element** (a tagged fact) | `Fact.tag` (the taxonomy's `Q3 TAG` / `ICICI TAG` code) |
| XBRL **calculation linkbase** (children sum to parent, signed by `weight`) | Derived subtotal Facts (the `do not tag` rows) |
| XBRL **formula linkbase** (assertions that must hold) | The validation engine |
| Audit **assertions** (existence, accuracy, completeness, valuation) | Provenance + confidence + validation results |
| **CARO 2020 clause (xix)** + Schedule III ratios | Derived ratio/analysis Facts |

Known boundary we design past on purpose: XBRL's *calculation* linkbase only does
simple signed addition within one period. Ratios and roll-forwards
(`Closing = Opening + Additions − Disposals`) need richer logic — our
**derivation engine** is that formula layer.

References: [XBRL Calculation Linkbase](https://www.openriskmanual.org/wiki/XBRL_Calculation_Linkbase),
[XBRL US calculation checks](https://xbrl.us/data-rule/dqc_0118/),
[ACCA audit assertions](https://www.accaglobal.com/us/en/student/exam-support-resources/fundamentals-exams-study-resources/f8/technical-articles/assertions.html),
[CARO 2020 clauses](https://coraa.ai/blog/caro-2020-complete-clause-by-clause-guide).

## 4. The taxonomy we are adopting

`Q3VENI LATEST TAXANOMY.xlsx` — 4 sheets, one per statement block, sharing a
common column layout:

| Column | Meaning |
|---|---|
| **BlockID** (`PL01`, `BS04`…) | Statement section grouping |
| **Tag** (`ICICI TAG` / `Q3 TAG`) | Canonical short code, e.g. `RFSP`, `GST`, `TL`, `INTOI` |
| **Sr. No.** | Hierarchical numbering (1 → 1.1 → 1.1.1) — the parent/child tree |
| **Particulars** | Human-readable line-item name |
| **Comments** | Flags: `do not tag` = **subtotal (derived)**, `NEW`, `NOT CONSIDERED` |
| **redirection (Dataset No-2)** / **q3 redirection (Dataset No-1)** | Rollup: maps a granular tag → a coarser tag in another dataset |

Scale: **~512 unique tags / ~580 line items** — P&L 224, Assets 133,
Liabilities 106, Equity 49. (Contrast the current ~200-field schema.)

Two structural facts drive the design:
- **`do not tag` rows are derived**, not extracted — they are subtotals whose
  Particulars often name their own formula ("Total of 1.1 to 1.5").
- **Redirection columns are a rollup map** — they let ~500 granular tags
  aggregate into a smaller canonical set, and they feed ratio calculations.

## 5. Core concept — every value is a traceable Fact

Replace the flat per-field output with a **directed graph of Facts**,
tag-addressed. A Fact is the same audit envelope whether read or computed; only
its provenance shape differs.

```
Fact {
  tag:        "RFSP"                 // canonical taxonomy tag
  value:      1234.56
  period:     current | prior
  unit:       "INR Lakhs"
  confidence: 0.0–1.0               // calibrated, first-class
  origin:     EXTRACTED | DERIVED   // the discriminator
  provenance: <bbox> | <derivation>
}
```

- **EXTRACTED** → provenance = page, table, row, **bounding box**, snippet,
  evidence_ids. (Reuses `fdi.core.provenance` unchanged.)
- **DERIVED** → provenance = `{ method, formula, inputs:[tags], explanation }`;
  each input is itself a Fact. Recurse and every derived number bottoms out at
  extracted numbers with boxes.

Three layers of Facts live in one graph: **line items** (extracted) → **subtotals**
(`do not tag`, derived) → **ratios / CARO** (derived). Validation is an assertion
over Facts; a validation result is itself a traceable record.

## 6. The six pillars

Each pillar notes what it **reuses** from today vs what is **new**.

### Pillar 1 — Fact graph (foundation)
The structural change that makes auditability and validation possible; both
become graph traversals. *Reuses:* existing bbox provenance as leaf provenance.
*New:* the `Fact` record + a per-document graph store.

### Pillar 2 — Taxonomy → typed, hierarchical, tagged schema
Convert the 4 sheets into the new field schema: `object` parents / typed leaves
driven by `Sr. No.`, every leaf carrying its `tag`, `do not tag` rows marked
**derived**, redirection captured as rollup metadata. The schema becomes the
**contract** declaring, per field: type, tag, extracted-vs-derived, and what
validates it. *New:* schema format + a deterministic converter from the xlsx.

### Pillar 3 — Derivation engine
Computes derived Facts in dependency order. **Subtotals** auto-generated from the
"Total of X to Y" structure (signed, XBRL-calc style). **Ratios & CARO metrics**
from a declarative formula library (Current Ratio, Debt-Equity, DSCR, Interest
Coverage, ROE, Net Profit Margin, receivable/payable days — the CARO xix +
Schedule III set). Every derived Fact records formula, inputs, and a plain-English
explanation. *New:* the entire layer (system has none today).

### Pillar 4 — Validation engine
Assertions over the graph, results attached as first-class records:
- **Footing/reconciliation** — subtotal == Σ children (auto-derived).
- **Cross-statement identities** — Assets = Liab + Equity; PP&E roll-forward
  (small curated library at schema root).
- **Domain/sign constraints** — non-negativity, period completeness, ranges.
Each result: rule id, tags, expected vs actual, delta, tolerance, **severity**
(error/warn). *Partially reuses:* the current schema's advisory
`validation.rules` strings become executable.

### Pillar 5 — Confidence & accuracy system (priority #1)
Financials let numbers police each other — turn that into hard accuracy signals:
- **Reconciliation-as-corroboration** — a subtotal that independently equals its
  children corroborates them → boost confidence; a failure → flag + drop.
- **Cross-source triangulation** — the same figure on the face of the statement
  and in a note should agree; disagreement → review flag naming both boxes.
- **Confidence propagation** — a derived Fact is capped by its weakest input.
- **Calibrated bands** — surface `high / needs-review / failed`, not a raw float.

### Pillar 6 — Audit & review surface (the "enterprise" packaging)
- **Explainability drill-down** — click any value → lineage to pixels (extend the
  existing bbox viewer to walk derived → inputs).
- **Coverage/completeness report** — of ~500 tags: found / derived / absent.
- **Human-in-the-loop review queue** — prioritized by low confidence + failed
  validation; corrections are versioned and re-flow downstream.
- **Immutability & versioning** — append-only Facts/rules/corrections with
  who/when.
- *(Optional)* **XBRL export** — the model maps to the linkbases.

## 7. How it answers the bank's questions

| Bank asks | Answered by |
|---|---|
| "Where did this number come from?" | Pillars 1 + 6 (lineage to bbox) |
| "How did you compute this ratio?" | Pillar 3 (derivation record) |
| "Do your numbers foot / tie out?" | Pillar 4 (validation results) |
| "How sure are you?" | Pillar 5 (calibrated confidence, corroboration) |
| "What did you miss?" | Pillar 6 (coverage report) |
| "Prove it to our auditor." | Pillar 6 (immutable trail) + XBRL export |

## 8. Milestone roadmap

Each milestone is independently shippable and gets its **own detailed spec**.

1. **Foundation** — Fact model + taxonomy→schema conversion (Pillars 1–2). The
   contract every later pillar reads from. Testable: schema parses to N tagged
   fields; extractor emits tag-keyed Facts with existing bbox provenance.
2. **Self-validation** — derivation + validation engines (Pillars 3–4).
3. **Confidence** — accuracy/corroboration system (Pillar 5).
4. **Audit surface** — review queue, coverage, immutability, export (Pillar 6).

Recommended first spec: **Milestone 1**.

## 9. Non-goals / out of scope (for now)

- Rewriting the OCR / structure / retrieval stages — extraction *mechanics* stay;
  we change the *schema* it targets and the *shape* it emits.
- Building the full ratio catalogue before the Fact graph exists.
- XBRL export in Milestone 1 (kept as an optional later capability).
- Any UI work beyond noting the drill-down extension (its own milestone).

## 10. Open questions to resolve in per-milestone specs

- **Schema representation:** extend the current `modules → retrieval_units →
  fields` JSON with nesting + `tag` + `origin`, or introduce a new schema
  document alongside it? (Milestone 1.)
- **Retrieval at ~500 tags:** how the retrieve-once/extract-many unit mapping
  scales from ~200 to ~580 fields without a retrieval-count blowup. (Milestone 1.)
- **Redirection semantics:** are Dataset-1 and Dataset-2 two distinct downstream
  consumers, and does each need its own rollup Facts? (Milestone 2.)
- **Tolerance policy:** rounding tolerance for reconciliation in INR Lakhs, and
  per-rule overrides. (Milestone 2.)
- **Confidence calibration:** how corroboration boosts/penalties compose into the
  calibrated bands. (Milestone 3.)
