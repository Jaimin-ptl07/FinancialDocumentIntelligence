# Design: Note-Driven Extraction — schema, retrieval, derivation, ingestion

**Date:** 2026-08-01
**Status:** Design, validated by a P&L pilot on three issuers. Supersedes the
similarity-first extraction path for v4.x.
**Pilot artifacts:** `docs/architecture/field-extraction-schema.v4_3-pl.json`

---

## 1. What this replaces, and why

The v4.2 pipeline asked a **pull** question — *"what is the value of `GRSPNE`?"* —
and found context by similarity. Measured on TCS FY2025-26:

| | v4.2 |
|---|---|
| Recall of the 47 P&L fields TCS actually reports | 29/47 (62%) |
| Precision on committed values | 91% |
| LLM calls | 66 (all four modules) |
| Derived facts on live data | 5 of 88 |
| Leaves whose label matched a note | 4 of 402 |

The failures were not where I first assumed. Three hypotheses were tested and
**disproved by measurement**: batching was not dropping fields (0 of 402 keys
unanswered); per-field extraction did not help (16/24 vs 15/24 at 6x cost); and
the matrix-transpose fix never fired on real grids. The actual causes were
**retrieval reach** (~50 fields sat in notes similarity could not find),
**number parsing** delegated to the model (`8,685` read as `8.685` → `8`), and
**vocabulary** (`RFTG` vs "Sale of equipment and software licences").

---

## 2. The field schema (v4.3) — done for P&L

188 extractable leaves, 48 composition formulas, 3 waterfall steps, 6 reported
totals, **0 invariant violations**. Hand-authored from the taxonomy sheet.

### 2.1 Seven rules

1. **The parent carries the tag.** A subtotal row's tag is hoisted onto the
   container it totals. v4.2 left `RFOGRS` as a *sibling* of the five lines it
   summed, and the container untagged — so it could never be a Fact.
2. **The tree is the calculation graph.** Invariant, asserted at build time:
   `derivation.inputs == the tags of a node's child concepts`. Presentation
   groups (headers with no total row) are transparent.
3. **Roll-ups determine placement.** Sheet column 8 (`Q3Redirection`) says which
   parent an unnumbered row feeds — `EXCG → NONOI`, `BNKC → BC`, `NLFA →
   WOANL`. v4.2 never read that column. Those rows are now *nested* under their
   target, which is what makes rule 2 satisfiable.
4. **`classified_as` is not a roll-up.** Column 7 maps a tag into another
   dataset (`EXINTOI → INTOI` = "interest income shown as exceptional"). It is
   the SAME money reported once; summing it double-counts.
5. **Signed weights.** `RFONET = 1 minus 2`, and exceptional income vs expense,
   are subtractions. Inputs may be `{tag, weight}`.
6. **`NOT CONSIDERED` is metadata, not an exclusion.** It is a value of the
   redirection column meaning "no counterpart in that dataset". All of section 8
   is `NOT CONSIDERED` yet is unambiguously part of `PBTR`. v4.2 dropped these
   from sums, understating `COMC`, `CHINVT` and the auditor block.
7. **Reported wins.** A parent the report prints keeps the reported value; the
   sum of children is a CHECK. This is what lets TCS's `DEPEXP = 4,288` stand
   while `DEP`/`AMT` are null.

### 2.2 Three kinds of formula

| Kind | Inputs are | Invariant applies | Example |
|---|---|---|---|
| `derivation` | this node's **children** | yes | `TOTOTING = INTOI + DIVOI + COMOI + RENTOI + NONOI` |
| `waterfall` | **peer** nodes earlier in the statement | no | `RFONET = RFOGRS − TOTTAX`; `PBMI = PATR + OTCI + TOTPNLDOP` |
| `reconciliation` | either, against a **reported** value | no | `TOTEXPR` vs its eleven components |

The waterfall/composition split was discovered by the validator failing on
`RFONET`, `PBMI` and `NSN` — conflating them would have produced wrong sums.

`PBMI = 11 ± 12 & 13` was confirmed on **both** TCS bases: consolidated
`49,454 + 2,750 = 52,204` (face: TOTAL COMPREHENSIVE INCOME 52,204); standalone
`49,096 − 600 = 48,496`. It also corrects the semantics — `PBMI` is total
*comprehensive* income before minority interest, and `TOTMI` is the NCI share of
comprehensive income (283), not of profit (244).

---

## 3. Retrieval

### 3.1 Find the statement by CONTENT, not by heading

Heading detection is unreliable: on TCS it returned pages 166, 167, 228, 252,
253 — three false positives, and no indication of *which* statement each was.

Score every table against the line names Schedule III mandates
(`total assets`, `revenue from operations`, `profit before tax`, …). Require ≥3
distinct anchors plus numeric content, then scope by basis region. Measured:

| | TCS | RITES | RIL |
|---|---|---|---|
| Balance sheet | p252 + p166 | p60 | p145 |
| P&L | p253 + p167 | p61 | p146 |
| Cash flow | p256 + p172 | **not found** | **not found** |

Both bases surface naturally; basis scoping then picks. **Verify by footing** —
`income − expenses = PBT` ties on all three issuers, which is what proves the
right table was chosen regardless of how it was found.

### 3.2 Parse the face deterministically — no LLM

Azure DI gives cells with `row_index`/`column_index`. Regroup into rows, find the
**Note column once from the header** (never by pattern-matching cells — that read
the *value* `45` as note 45), then per row: label, note ref, current, prior.

Number parsing is a string operation with one correct answer:
`'2,20,938' → 220938`, `'(468)' → -468`, `'14,372.80' → 14372.80`, `'-' → None`.
**The model never interprets a separator.** This is the whole fix for the
comma-as-decimal bug, and it worked unchanged on RITES's decimal-and-comma
format.

Each face row yields three things at once: the **value**, the **concept**, and
the **note pointer**.

### 3.3 Resolve the note by offset window

Paragraphs and tables both carry `spans {offset, length}`. Page-level selection
is too coarse — TCS p301 holds *two* notes (13(a) at offset 897,095 and 13(b) at
897,486). So:

```
ref "13(b)" → heading offset 897,400 → next heading 898,050
            → window [897,400, 898,050) → tables whose span.offset falls inside
```

Reference grammar must be permissive: TCS uses `13(a)`, RITES uses `2.01`, RIL
uses `1`. The v4.2 regex parsed **0 of 30** RITES references. Range and
conjunction forms (`1 to 44`, `1 & 2`) are recognised as *non*-references and
dropped rather than mis-parsed.

### 3.4 Fallback chain — never silently empty

```
1. note reference → offset window → tables          (deterministic, preferred)
2. note TITLE matched against concept aliases       (finds tax/EPS/auditor notes
                                                     that no face row points at)
3. hybrid_search over the basis-scoped pool         (similarity, last resort)
4. nothing → emit not_answered, never a guess
```
Every fact records **which mode** produced it, so a wrong value traces to its
retrieval strategy.

---

## 4. Extraction

### 4.1 The unit is a NOTE

Not a field, not a statement, not a fixed chunk. Measured alternatives:

| Design | Result |
|---|---|
| per field (402 calls) | 16/24 vs 15/24 — a wash at 6x cost, and it *introduced* errors |
| per statement (4 calls) | 100+ fields per call, worst context ratio |
| fixed chunks of 15 (27 calls) | 102/402 filled |
| section-aware (66 calls) | 118/402 filled |
| **per note (8 calls, P&L)** | **same recall, 91% precision, 2x derived facts** |

### 4.2 The push model

Not *"what is `LNPC`'s value?"* but: **here is Note 13(b) — list every line item,
then map each to one of these concepts, or mark it unmapped.** Three
consequences: unseen vocabulary stops being fatal; "Facility expenses 2,966"
surfaces as `unmapped` instead of vanishing; and the note's own total becomes the
**arbiter**.

### 4.3 Prompt composition

The per-field `extraction_prompt` is deleted. It carried no information the
schema does not already hold, and one of them actively caused the ×10 unit bug by
saying "report in INR Lakhs" while the shared rules said "exactly as printed".

```
SYSTEM    number format, digit separators, bracket-negatives, dual-period,
          "never guess"                                    [one place, versioned]
UNIT      "Note 13(b), Other expenses, as printed, FY26/FY25"   [from note index]
CONCEPTS  tag · name · aliases · definition                 [from the schema]
```

### 4.4 Label → concept mapping generalizes only WITH semantics

Measured across three issuers on the same face tables:

| | TCS | RITES | RIL |
|---|---|---|---|
| alias only | 69% | 58% | **30%** |
| + normalization (`&`↔`and`, plurals, dashes, parentheticals) | 72% | 68% | 33% |
| **+ semantic mapper (1 call)** | **90%** | **79%** | **80%** |

The alias list was TCS-shaped; that was the defect, not the architecture.
Normalization contributed little (+1/+2/+1); the mapper contributed +5/+2/+19.
**Alias matching cannot be the only mechanism.**

---

## 5. Derivation

Pure, no LLM. Fixpoint over the tree, composition then waterfall. Because
`derivation.inputs == child concepts`, it is a tree walk, not a lookup.

Measured on the pilot: **10 derived facts** including three-level chains
(`INTTLWC + INTOT → INTTL → INTEXP → TOTFC`) and the roll-up groups
(`EXCG/CFG/NPRCA… → NONOI`, `PYAUD*×4 → TOTPYAUD`).

### 5.1 The precedence rule — CORRECTED BY MEASUREMENT

This spec originally said *"reported → else COMPLETE sum of children → else
null"*. That was implemented and **falsified**: requiring every input collapsed
derivation from 10 facts to 1, suppressing 37 nodes. No real filer discloses
every child the taxonomy defines -- TCS reports 2 of 5 revenue lines and 1 of 9
non-operating income items -- so "all inputs present" nulls almost every parent.
It also failed to fix the case it was written for: `ADMINEXP` has a single child,
so "all present" was trivially satisfied and it still synthesised 2,581 from
`TCEXP` alone.

The rule that survived measurement:

```
reported value
  → else the sum of the children that ARE present, WITH completeness recorded
  → else null
```

Each derived fact carries `complete`, `coverage` ("3/4"), `confidence` and the
`missing` tags. Two hard constraints:

1. **A partial value may never satisfy a reported total's reconciliation.** It
   cannot be used to claim the books tie. `TOTRNOTINR` is reported `unverified`
   because `RFONET` is partial -- which is the correct answer, not a failure.
2. **A node that is itself extractable-and-absent is never synthesised.** This
   is what stopped `ADMINEXP` and `FWS` being invented from one child.

Measured outcome: 9 derived facts, all correctly flagged partial; false
positives 4 → 3; recall and precision unchanged at 62% / 91%. The output splits
into **43 facts a consumer may trust** and **9 shown with their coverage** --
which is the honest shape for a bank: show the number, show that it is
incomplete, and never let an incomplete number vouch for a total.

---

## 6. Ingestion pipeline changes

### 6.1 Keep

`ocr` (paragraphs with roles + spans; tables with cells + spans) · `structure`
(cross-page stitching, header/footer removal) · `normalize` → `ast` · `graph` →
`provenance` (**bounding boxes = the audit trail**) · `basis` (RIL proved totals
mix across bases without it).

### 6.2 Demote to fallback

`semantic` → `chunks` → `embedding` → `index`. Still built; no longer the
primary retrieval path.

### 6.3 Remove from the default DAG

Verified by grep that **nothing reads these artifacts**:

```
linking                        → no consumer
graph_load                     → no consumer, and fails every run (Neo4j down)
ontology → quality → knowledge → a closed loop with no external consumer
```

Disable rather than delete: `ontology` maps headings to CanonicalConcepts (close
to the concept-alias work and possibly harvestable) and `quality` produces table
diagnostics that could feed an audit surface.

### 6.4 Add

```python
class NoteIndexStage:      # deps: (ocr, structure, basis)
    """ref → {offset window, page span, title, basis}. Currently rebuilt
       in-memory per extraction run; as an artifact it is cached, inspectable and
       auditable -- and a ref-grammar failure like RITES's 0/30 becomes visible
       in one file instead of silently degrading retrieval."""

class FaceIndexStage:      # deps: (ocr, structure, basis, note_index)
    """per statement: content-anchored detection, footing verification, and the
       face rows as (label, note_ref, current, prior). The deterministic
       bootstrap -- values and note pointers in one parse, zero LLM."""
```

### 6.5 Fix

`FDI_MILVUS_URI` is absent from the container environment, so **`index` has
failed on every document ever uploaded through the API**. Independent of this
design, and it silently disables fallback retrieval.

---

## 7. Plan

| Phase | Work | Gate |
|---|---|---|
| **A** | Verify mapper **accuracy** (not coverage) on RITES + RIL by hand | newly mapped concepts are correct, not merely present |
| **B** | ~~Partial-input suppression~~ → completeness-marking (see 5.1) | DONE: 9 partial facts flagged with coverage; footing gate rejects partial inputs |
| **C** | Port to `fdi.core` + `fdi.pipeline`: `note_index`, `face_index`, note-unit extractor, mapper wiring | TCS P&L reproduces the pilot: ~8 calls, ≥62% recall, ≥91% precision |
| **D** | Author Assets / Liabilities / Equity in v4.3 shape | 0 invariant violations per module |
| **E** | Add a **cash-flow module** | the taxonomy has none, and CF anchors miss RITES and RIL — two independent gaps pointing at the same hole |
| **F** | Stage removals + `FDI_MILVUS_URI` | ingestion runs clean, no permanent failures |
| **G** | Mapping memory | second run of a document is deterministic and cheaper |

---

## 8. Open questions

1. **Consolidated or standalone as the product default?** Both exist with
   *different note numbering* (other income is note 13 consolidated, note 11
   standalone). Everything measured this session is standalone. Banks usually
   want consolidated. This is a product decision, not a bug.
2. **`EXCHEXP` roll-up.** `CFL`/`EXCL`/`NONLMTM`/`NONLRSA`/`NONLRSL` have no
   pointer in the sheet. The forex family is currently placed under
   `OTNONOEC`/`OTNONOENC` where the sheet supports it; `EXCHEXP` is left empty
   with the inference recorded but **not applied**.
3. **Taxonomy gaps found by running it:** ~180 of 402 P&L fields cannot fill for
   a services company; 25 extraordinary-item tags cannot fill for **any** Ind-AS
   filer; and real disclosed lines have no home (`Facility expenses`,
   `Communication expenses`, RITES's `Fee For Service Obtained`). Coverage
   metrics should report *applicability* per industry, or they will always look
   broken.
