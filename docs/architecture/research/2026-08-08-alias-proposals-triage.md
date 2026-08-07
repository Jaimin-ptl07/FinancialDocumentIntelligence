# Triage of the 141 "alias proposals"

**Date:** 2026-08-08
**Inputs:** `out/alias-proposals-{TCS,REMC,RIL}.json` (64 / 21 / 56)
**Verdict:** do not apply as aliases. 11% are alias-shaped; 3% are safe; none is
measurable today.

## Why the framing was wrong

The files are a dump of labels the mapper did not resolve. "Alias proposal" reads
as *(label, tag) pairs ready to apply*, and that is not what they are — 89 of the
141 carry `"candidates": []`, i.e. no proposed tag at all.

Classifying all 141 by **what would actually fix each one**:

| count | class | the real fix |
|---:|---|---|
| 52 | already matched, rejected downstream | none — an alias is a no-op |
| 25 | ambiguous: tie refused | section context, not vocabulary |
| 25 | taxonomy gap: nothing comes close | a new concept, if the concept is wanted |
| 24 | structural sub-label | parent+child composition |
| 15 | genuine alias candidate | an alias |

### The 52 that are no-ops

These already match at 0.9–1.0 and were rejected *after* matching — a claimed
tag, a kind mismatch, or the deliberate total-vs-component veto. `Lease
liabilities → LL` at 1.000 is the clearest: `_match_with_section` refuses it on
purpose so the unqualified TOTAL cannot swallow a component row. Adding an alias
changes nothing, because matching was never the step that failed.

The 52 also include outright wrong pairings that applying blind would install:
`Depreciation & Amortisation Expenses → TOTEXPR "Expenses"`, `Earning Per Share
→ TOTEQNR "Total Equity"`, `Equity attributable to shareholders → TOTCIR "Profit
(loss) for period"`.

### The 25 ambiguous ones

`Loans`, `Inventories`, `Investments` have no exact concept and tie at the top —
`Loans` ties at 1.000 across `WCL`/`WCLB`/`WCLI`/`WCLO`. `lexical_match` refuses
ties by design, which is correct: the label alone genuinely does not say which
concept it is, and only the section does. An alias would have to pick one
arbitrarily and would be wrong for every issuer that meant another.

### The 25 taxonomy gaps

Some are issuer-specific and want no concept (`Fee For Service Obtained`,
`Salary Reimbursement`, `Statutory impact of new Labour Codes`).

**One is a real omission: the taxonomy has no Non-Controlling Interest concept
at all** — zero of the 565 concept names contain "controlling". NCI is a
mandatory line on every consolidated balance sheet and P&L under Ind AS, and
both RIL and TCS print it. This is a taxonomy decision, not an alias, and is
left for the owner.

### The 15 alias-shaped ones, reviewed individually

Eleven are wrong on inspection:

- `Trade and Other Payables → TRP "Trade payables"` — a combined line onto one
  component
- `Loans and other financial assets → TOTCAO "Total current assets OTHER THAN
  loans, advances and financial assets"` — the literal opposite
- `Inter-corporate deposits placed → BSD "Deposits"`, `Proceeds from bank
  deposits → BSD` — a cash MOVEMENT onto a BALANCE
- `Net change in fair values of investments → AUTOPL005 "Net Change in Other
  Inventory"` — an auto-generated placeholder tag
- `Depreciation / Amortisation` (a P&L row) `→ CFDEP`, a cash-flow adjustment
- `Borrowings - Current (Net)` (a cash-flow row) `→ BOR`, a balance

Four are sound: `Net cash flows generated from operating activities → CFO`,
`Proceeds from disposal / redemption of investments → CFIINVS`, `Purchase of
investments → CFIINVP`, `Bad debts and advances written off → PRVDD`.

## Why even those four are not applied

All four are **cash-flow** lines, and `docs/groundtruth/` covers the P&L, balance
sheet, equity and assets — not the cash-flow statement. Applying them would
change no measurable number, while an alias is global: it affects every issuer
the taxonomy is ever used on. Shipping an unmeasurable global change to chase 3%
of a mis-framed backlog is the wrong trade.

## What would make this worth revisiting

Cash-flow ground truth for at least one issuer. That turns these four from
unverifiable to measurable, and would also give the 24 structural sub-labels
(`a) Owners of the Company`, `Billed`, `Basic (in ₹)`) a scoreboard — they are
the larger prize, since they need parent+child composition rather than
vocabulary, and that is a mapper change with a real ceiling behind it.
