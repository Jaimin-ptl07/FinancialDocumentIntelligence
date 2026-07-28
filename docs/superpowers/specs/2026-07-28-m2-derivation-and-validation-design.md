# Milestone 2 — Derivation + Validation Engines

**Date:** 2026-07-28
**Parent vision:** `2026-07-28-audit-grade-financial-intelligence-vision.md`
**Depends on:** M1 (Fact model + v4 taxonomy schema)
**Status:** spec (implementation follows)

## Goal

Make numbers self-validating and derived numbers self-explaining. Turn the
taxonomy's `do not tag` subtotals into computed Facts with real formulas, add a
declarative ratio/CARO library, and run reconciliation + cross-statement +
domain checks — every derived value and every check fully traceable.

## What the taxonomy actually supports (measured)

- 79 derived rows; **30 carry an explicit "Total of X to Y" / "minus"
  reference**; the rest have no machine-readable formula.
- Only 55/434 extracted leaves carry `sr_no`, so range-resolution is partial.
- "As reported" totals exist (`TOTCAR` Total Current Assets as reported,
  `TOTCLR` Total Current Liabilities as reported) → reconciliation anchors.
- Redirection targets mostly point into a *separate* Dataset-2 tag namespace
  (LTOT, STOB, …), so redirection is a cross-dataset rollup, NOT in-sheet
  parent/child. It stays metadata this milestone; the Dataset-1/2 rollup is a
  later pass.

**Design consequence — accuracy first:** derive a subtotal ONLY when every
operand resolves unambiguously. An unresolved subtotal is emitted with
`status: "unresolved"` and no value, never a guessed one. Wrong numbers are
worse than absent ones for a bank.

## Deliverables (pure `fdi.core`, no I/O)

### 1. `fdi.core.formula` — parse a subtotal's formula from its Particulars
`parse_formula(particulars) -> Formula | None`. Handles:
- `Total of 1.1 to 1.5` → sum of an sr_no range
- `Total of 1.2 & 1.4 to 1.11` → union of ranges/singletons
- `(1.1 to 1.13)` → sum range (parenthesised, no "Total of")
- `a minus b` / `(1 minus 2)` → difference of two sr_no operands
Returns a structured `{op: "sum"|"diff", operands: [sr_no ranges/singletons]}`
or `None` when nothing parses.

### 2. `fdi.core.derivation` — compute derived Facts
`derive_facts(schema_or_taxonomy, facts_by_tag) -> {tag: DerivedFact}`.
- For each derived tag, `parse_formula` its Particulars, resolve operand sr_no
  → sibling tags within the same block, look up their values in
  `facts_by_tag`, compute the signed result.
- Each `DerivedFact`: `{tag, value, period, origin:"derived", derivation:
  {method, formula_text, inputs:[tags], explanation}, confidence, status}`.
  `confidence = min(input confidences)` (propagation). `status = "ok" |
  "unresolved" | "missing_inputs"`.
- Recurses so a subtotal built on subtotals resolves in dependency order.

### 3. `fdi.core.ratios` — declarative ratio / CARO library
A static `RATIOS` list; each: `{key, label, caro_clause, numerator, denominator,
explanation}` where numerator/denominator are tag expressions.
`compute_ratios(facts_by_tag) -> [RatioFact]`, computing a ratio only when all
input tags are present (else skipped with a reason). Seed set (CARO xix /
Schedule III), tag bindings explicit and documented as verify-against-report:
current ratio, debt-equity, return-on-equity, net-profit-margin. More can be
appended without code change.

### 4. `fdi.core.validation` — assertions over Facts
`validate(facts_by_tag, derived, ratios) -> [ValidationResult]`. Three families:
- **Reconciliation** — an "as reported" total vs its derived sum
  (`TOTCAR` vs derived current assets, etc.). Auto-paired by tag binding.
- **Cross-statement identity** — Total Assets == Total Liabilities + Equity.
- **Domain/sign** — currency ≥ 0 where expected; both periods present.
Each `ValidationResult`: `{rule_id, kind, tags, expected, actual, delta,
tolerance, severity: "error"|"warn", passed}`. Tolerance is absolute in INR
Lakhs (default 1.0, rounding), per-rule overridable.

## Testing / acceptance

- `formula`: table of real Particulars strings → expected parsed structure,
  incl. ranges, `&` unions, `minus`, and un-parseable → None.
- `derivation`: synthetic taxonomy where children have sr_no → subtotal computes;
  missing input → `missing_inputs`; nested subtotal resolves; confidence = min.
- `ratios`: present inputs → value; absent input → skipped with reason;
  divide-by-zero → skipped, never raises.
- `validation`: reconcile pass within tolerance; fail outside tolerance flags
  with correct delta; identity check; sign check. Determinism, never raises.
- Runs against the real committed taxonomy: report how many subtotals resolve
  (expected ~20–30) — logged, not asserted brittle.
- ruff + mypy clean; import contract KEPT.

## Out of scope (later)

Confidence calibration bands (M3), Dataset-1/2 redirection rollups, the full
11-ratio Schedule III set, UI, XBRL. M2 delivers the engines and their traces.
