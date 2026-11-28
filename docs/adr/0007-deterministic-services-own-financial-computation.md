---
status: accepted
---

# Deterministic services own financial computation

Any margin, growth rate, ratio or derived total that FDI states is computed by the Calculation engine (`fdi.core.calculation`). It works in exact decimals, from Financial facts, with a closed catalogue of Metrics defined in code as data. An agent requests a Metric and explains the result; it never does the arithmetic. This decides the design's ADR-003 for S5 (#34), and it is what S8's Analytics and Comparison agents call.

## Decisions

- **The catalogue is closed, and adding a Metric is a reviewed code change.**
  - Each Metric has a name, a formula over Concepts (with ordered alternatives where the taxonomy has two homes, e.g. revenue is `RFONET`, else `RFOGRS`), a unit (`percent`, `ratio`, `currency`) and a period rule (a Fiscal-year offset, e.g. growth reads `t-1`).
  - v1 has the design's seven Metrics, plus the S1 benchmark's calculation items with the benchmark's own definitions.
  - Effective tax rate is `(PBT − PAT) / PBT`. That is independent of whether an issuer prints tax as a positive expense (TCS) or a bracketed deduction (REMC), and it gives both golds exactly.
- **Refusal is a result, not an exception.** A Metric that cannot be computed returns `not_computable` with the Concept and Fiscal year it needed, or `not_comparable` with why its inputs cannot be combined: unknown unit, different currencies, different period ends, or a sum across printed scales. A gap is never filled.
- **Arithmetic is in normalised absolute units, in exact decimals.**
  - Ratios therefore cancel across printed scales: crore over lakh is fine.
  - Percent and ratio results are rounded half-even to four places. Sums are exact and shown in their inputs' printed unit.
- **Comparability is one rule set, shared.** Calculation and the Comparison agent both use `comparability`:
  - same basis;
  - same unit after normalisation;
  - period ends on the same month and day (and, for two Companies in one Fiscal year, the same date);
  - no restated input.
  
  The verdict carries its reasons, so `not_comparable` is a rule, not a judgement.
- **Restated inputs are computed and flagged.** A Metric over a restated input uses the latest value and says so in `warnings`. A comparison across it is `not_comparable` (`restated_input`), so a trend never silently mixes as-restated and as-originally-reported figures.

## Considered Options

- **A deterministic engine with a closed catalogue** (chosen).
- **Let the model compute from retrieved facts**: flexible, but unverifiable and measurably error-prone. The S1 baseline answered 0 of 10 calculation items.
- **Free-form formulas from the caller**: flexible, but it moves formula authorship and its review out of code, and makes "operating margin" mean whatever the last caller said.

## Consequences

- A question the catalogue cannot answer is answered "not a Metric FDI computes". Adding one is a pull request with tests against ground-truth values.
- S1's calculation items are deterministic given the facts. On the live TCS facts, five of the six TCS items reproduce their gold answers exactly. The sixth, effective tax rate, refuses because TCS's "Profit before tax" line is not mapped by the graph today. That is an upstream recall defect, ticketed separately.
