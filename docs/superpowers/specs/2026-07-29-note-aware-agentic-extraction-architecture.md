# Architecture: Note-Aware, Domain-Scoped Extraction with Agentic Escalation

**Date:** 2026-07-29
**Status:** Target architecture + phased plan (supersedes the single-shot
extraction path for v4.x; the Fact/derivation/validation model from the
2026-07-28 vision is unchanged and is consumed by this pipeline)
**Author:** brainstormed with the team

---

## 1. Why this document exists

M1–M4 delivered the Fact model, derivation, validation, confidence and the audit
surface. The v4.1 reasoned schema (513 tags, 402 extracted leaves, true 4-level
hierarchy) was then run **end-to-end against a real annual report** (TCS
FY2025-26 standalone) and manually scored against the statements. That run is the
evidence this architecture answers to.

**What worked** (measured, not projected):

| Result | Number |
|---|---|
| Headline line items exact to the rupee | 19 / 26 |
| Values correct among those the system committed to | ~110 / 118 (~93%) |
| Values grounded in their own cited evidence row | 98 / 118 (83%) |
| Current ratio (engine vs hand calc) | 2.006 — exact |
| Balance-sheet reconciliation | flagged Δ 362 and localised it to the equity mislabel |

**What failed, and why it matters more than the successes:**

Of the 284 null fields, hand-verification against the OCR found roughly **230
correctly absent** (TCS is a debt-free services company — it genuinely has no
borrowings, raw materials, or investment property) and roughly **50 false
nulls**: line items that *are* in the report and were missed. Every one of them
lives in a **note**, not on the statement face:

```
Balance sheet face:   Property, plant and equipment | 8(a) | 8,259     ← extracted
Note 8(a), page 285:  Freehold land 323 · Buildings 8,685 · Plant 1,024 ← missed
```

Two distinct root causes, and they need two distinct fixes:

1. **Retrieval never reached the note.** A fixed similarity query for "Land"
   cannot find page 285; the only reliable pointer is the `8(a)` reference
   printed on the face row. The pipeline had no way to follow it.
2. **Vocabulary mismatch.** Even with the note in context, TCS writes
   *"Computer equipment"* where the taxonomy says *"Plant and equipment"*
   (`GRSPNE`). RIL will write *"Plant and machinery"*. A bank will write
   *"Premises and equipment"*. No alias list closes this by enumeration.

Cause 1 is a **planning** problem. Cause 2 is an **ontology alignment** problem.
The current pipeline has no stage for either.

## 2. The two design laws (unchanged)

Inherited verbatim from the vision document; every stage below serves at least
one:

1. **Self-explaining** — no number without recursive provenance.
2. **Self-validating** — no number trusted until cross-checked; confidence is a
   calibrated first-class output.

To which this architecture adds a third, learned from the TCS run:

3. **Absent ≠ missed.** The system must distinguish "this company does not report
   this" from "we failed to find it". A null with no explanation is a defect.

## 3. The pipeline

```
  SCHEMA
    ↓
  PLANNING ←──────────────────────┐
    ↓                             │  escalate on:
  RETRIEVAL ←──────────┐          │  null · sum-break · no-home · low-confidence
    ↓                  │ hop: note reference discovered
  REASONING ───────────┘          │
    ↓                             │
  VALIDATION ─────────────────────┘
    ↓ (passes)
  NORMALIZATION
    ↓
  STRUCTURED JSON
```

The linear spine is the deterministic path. **The two back-edges are the
architecture** — a purely linear pipeline is what produced the 50 false nulls.

- **Reasoning → Retrieval (multi-hop).** The model reads a face row, discovers
  its note reference, and fetches that note. This is the direct fix for cause 1.
- **Validation → Planning (escalation).** A broken sum, an unmapped label, or a
  null after the note-follow re-enters planning with a different strategy. This
  is where reasoning is worth its cost.

### 3.1 Why not "agent plans everything"

The face→note hop is **deterministic and knowable in advance** — the Note column
is mandatory under Ind-AS/IFRS presentation. Paying an LLM to rediscover that
rule per field, per report, forever, buys a variable, non-reproducible answer to
a fixed question. Measured baseline: 402 fields ran in 635 s / ~66 LLM calls; a
per-field agent at ~4 tool-turns is order-of-magnitude ~25× that (estimate).

Conversely, *"these children do not sum to the parent and one label has no
home"* is genuinely unpredictable. **Deterministic where the rule is known,
agentic where it is not** — and the escalation trigger is itself deterministic,
so the audit trail survives.

## 4. Stage contracts

| Stage | Input → Output | Layer | Purity |
|---|---|---|---|
| **Schema** | — → concept set (tag, definition, aliases, siblings, formula) | `core` | pure |
| **Planning** | parent node + memory → retrieval plan (tools, note refs, candidates) | `pipeline` | may call LLM on escalation only |
| **Retrieval** | plan → evidence set (chunks, tables, cells) | `pipeline`/`platform` | I/O |
| **Reasoning** | evidence + candidates → mapped rows (`label → tag`, parsed value) | `pipeline` (LLM) + `core` (lexical) | mixed |
| **Validation** | rows + parent total → accept/flag + residual | `core` | pure |
| **Normalization** | validated facts → canonical unit/sign/period | `core` | pure |
| **JSON** | facts → Fact bundle + coverage | `core` | pure |

This respects the existing import-linter contract (`api|workers > pipeline >
platform > core`) and keeps `fdi.core` total and deterministic.

### 4.1 Normalization is two things in two places

The single most-forgotten stage, and it splits:

| | What | Where |
|---|---|---|
| **Parse-normalize** | `(522)` → −522; `1,10,270` → 110270; apply the statement's scale header (₹ crore / lakhs / millions) | inside **Reasoning** — must precede Validation or a column cannot be footed |
| **Canonicalize** | target reporting unit, currency, taxonomy sign convention, period labels | the **Normalization** stage |

Validation runs on parsed-but-native numbers so extraction errors are caught
*before* a scale factor multiplies them.

### 4.2 Terminal states

Output carries a state, never a bare null:

`extracted` · `derived` · `unmapped` (label found, no concept fits) ·
`needs_review` (validation broke) · `absent` (no evidence the company reports it)

`absent` vs a missing field is the whole lesson of §1 and must be explicit in the
coverage report.

## 5. Cross-cutting concerns (not stages)

- **Provenance** threads through every stage. Evidence (page, table, cell,
  snippet) must survive to the JSON or the audit claim collapses.
- **Confidence** is adjusted by each stage: retrieval quality, mapping method
  (`memory` > `alias` > `semantic`), validation corroboration. Reuses
  `fdi.core.confidence` banding.
- **Memory** sits beside the pipeline: consulted at *Planning* (known note
  locations) and *Reasoning* (known `label → tag`), written back at the end.
  This is what makes run #2 deterministic and cheap.
- **Original label preservation** is mandatory. Every fact carries
  `source_label` ("Computer equipment") next to `tag` (`GRSPNE`). The tag is for
  computation; the label is what an auditor trusts.

## 6. The mapping model (cause 2)

The inversion that makes this generalize:

| | Question asked | Failure mode |
|---|---|---|
| **Today (pull)** | "What is the value of `GRSPNE`?" | Unknown vocabulary → silent null |
| **Target (push)** | "Which of these 7 sibling concepts does each *report line* belong to?" | Every line gets a decision; unknowns surface as `unmapped` |

Three properties follow: the candidate set collapses from ~500 tags to the
parent's children (note scoping did this); we classify the document's own words
instead of searching for ours; and nothing is silently invisible.

**The arbiter turns a fuzzy mapping into a proven one.** The parent total is
already extracted at high confidence, so:

| Outcome | Action |
|---|---|
| Σ(children) == parent | accept all, boost confidence (corroboration) |
| Σ off by exactly one unmapped line | that line is the missing child; assign it |
| Σ off otherwise | flag the **note** as `needs_review`; commit nothing silently |

Deliberate asymmetry: **tolerant at leaf level, strict at total level.** A
sibling swap is harmless when the parent ties and both roll into the same ratio.

Edge cases needing explicit paths: one line → two tags (split); many lines → one
tag (aggregate, retain all source labels); "Others" → the taxonomy's own other-
bucket; no home → `unmapped`, surfaced in coverage.

## 7. Agentic escalation

Deterministic triggers only, so every agent invocation is explainable:

| Trigger | Agent task |
|---|---|
| Field null after note-follow | locate the value with alternative strategies |
| Σ breaks / residual unexplained | investigate the discrepancy |
| Label has no candidate home | propose a concept or confirm genuinely unmapped |
| Confidence below band threshold | second opinion |

**Guardrails (non-negotiable for a bank product):**

1. **Evidence required** — an agent may not emit a number without the chunk/cell
   it came from. No synthesis from parametric memory.
2. **Bounded budget** — max turns and calls per field; on exhaustion return
   `needs_review`, never a guess.
3. **Rules stay fixed** — the agent *investigates* validation failures; it never
   decides which rules apply. The same checks must run on every document.
4. **Decisions are cached** — first run reasons, subsequent runs replay
   deterministically. Recovers reproducibility and amortises cost.

### 7.1 Tool surface

An agent is only as good as its tools; precision beats breadth.

| Tool | Status |
|---|---|
| `vector_search`, `bm25_search`, `hybrid_search` | exists |
| `get_note(ref, basis)` | **new — Phase 0; the highest-leverage tool** |
| `get_table(page \| id)` | exists via structure artifact |
| `check_sum(parent, children)` | Phase 3 arbiter, exposed as a tool |
| `lookup_concept(tag)` | Phase 2 schema |
| `memory_lookup(label, section)` | Phase 5 |
| `graph_traverse` | **not available** — see §10 |

`search("land")` is hopeless; `get_note("8(a)", basis="standalone")` is exact.
That is why Phase 0 precedes the agent work rather than competing with it.

## 8. Phased plan

Each phase has a hard, measurable gate. No phase starts before its predecessor's
gate passes.

| Phase | Build | Gate |
|---|---|---|
| **0 — Note index** | `build_note_index(structure)`: note headings + boundaries, scoped by basis → `{("8(a)","standalone") → page, table_ids}` | resolves the note reference for ≥80% of the ~50 known false-null parents to the correct page |
| **1 — Reference-follow** | inject the parent's note chunks into the pool in `_pool_for` | the ~50 false nulls have their evidence in context; fill rate rises measurably |
| **2 — Concept schema** | add `definition`, `aliases`, precomputed sibling-candidate set per parent → v4.2 | every parent has a bounded child-candidate list |
| **3 — Mapper + arbiter** | `core/mapping.py` (lexical + arbitrate, pure) and `pipeline/mapper.py` (LLM for unresolved) | `Computer equipment → GRSPNE` **and** Σ(PPE classes) ties to 8,259 |
| **4 — Generalization gate** | run the *frozen, unmodified* pipeline on RIL | see §9 |
| **5 — Scale** | mapping memory (Postgres) + agentic escalation tier + optional graph persistence | escalation recovers N of the M fields the deterministic path missed |

Phase 0 is the correct first move: pure function over an artifact we already
have, no Azure, no LLM, no graph store, and it produces a **number** that
validates or invalidates the entire plan before any large build.

## 8.1 Phase 0 result — GATE PASSED (2026-07-29)

Measured on TCS FY2025-26, no LLM and no graph store involved.

| Metric | Naive index | After title binding |
|---|---|---|
| Face references resolvable | 31/41 (75%) | **39/41 (95%)** |
| False-null notes landing on the right page | 8/10 (80%) | **10/10 (100%)** |

**The failure the spike found, and the design decision it forced.** Azure DI
emits note headings as `sectionHeading` (`N)` top level, `(x)` sub-note). But
TCS's `8)` heading was **never emitted**, so its sub-notes `(a)..(f)` inherited
the previously-open note 7 (Leases): PPE — the single largest false-null cluster
— was filed as `7(a)`, and a lookup of `8(a)` would have **confidently returned
the wrong page**. Silent misdirection is worse than a null.

Decision: **the face table is authoritative for the note number.** When a
reference does not resolve against the heading-derived index, match the face
row's LABEL to a sub-note heading's TITLE and rebind under the face's ref. This
depends only on the Note column, which is mandatory under Ind-AS presentation,
rather than on the OCR happening to tag every heading — so it generalizes better
than heading detection alone.

Two further constraints confirmed empirically:

- **Basis scoping is mandatory.** TCS carries consolidated (p163–243) and
  standalone (p244–360) statements; both have a "note 8". The index is built
  per basis region (`basis_bounds`), or references resolve to the wrong
  statement set.
- **Tail bounding is mandatory.** The AGM notice restarts its own `1)`, `4)`,
  `7)` numbering; `find_statement_pages` trims the notes section before the
  notice begins.

Delivered in `fdi.core.notes` (pure, total, 9 unit tests including a regression
for the missing-heading case) and wired into `extract_unit`: note candidates are
injected **after** the similarity/basis filters — evidence resolved from a
printed reference must not be discarded by a heuristic — scored above the search
pool and capped so a long schedule cannot crowd it out.

## 9. The generalization gate (Phase 4)

Not a build step — a go/no-go checkpoint. RIL is chosen because it is
structurally opposite to TCS: manufacturing (real inventory, real borrowings,
plant & machinery, excise/GST) and consolidated/integrated basis. It exercises
precisely the ~230 tags that were *correctly* null on TCS.

Metrics, fixed in advance:

1. **Recovery on the other half** — do inventory/borrowing tags fill for RIL? A
   tag null on *both* companies is dead weight or the mapper is blind to it.
2. **Cross-company concept agreement** — TCS "Computer equipment" and RIL "Plant
   and machinery" both → `GRSPNE`. This *is* the generalization test.
3. **Reconciliation rate** — % of notes where Σ(children) ties. Company-
   independent and needs **no ground truth**; the best correctness proxy at
   scale.
4. **Zero TCS regression** — the 19 exact headline items stay exact.
5. **Unmapped-label rate** — a rise on RIL identifies taxonomy gaps for
   manufacturing. That is information, not failure.

**Anti-overfit discipline: freeze the pipeline before RIL runs.** Any fix RIL
forces must be a *general mechanism*, after which TCS is re-run to confirm no
regression. Tuning on RIL overfits to two companies instead of one.

Honest boundary: passing this gate means "ready for the long tail with
monitoring", not "solved". A third sector — bank or NBFC, where the balance sheet
diverges most — is the real stress test.

## 10. Known constraints and risks

- **Neo4j is down and misconfigured** in this environment (`Invalid value for
  NEO4J_AUTH`), and the retrieval layer's own notes flag the corpus graph (RT-5)
  as broken plus a chunk-text gap (RT-1). `graph_traverse` is therefore not an
  available tool today. Phases 0–1 deliberately need no graph store; graph
  persistence is optional in Phase 5 and must be justified by reuse (multi-hop
  agentic queries, cross-document/year traversal), not built reflexively.
- **Operational fragility observed during the TCS run**: Milvus collections
  require an explicit `load_collection` after a restart, and the host's BGE model
  cache was corrupt and needed re-download. Both are repaired; both should become
  startup checks.
- **Note-boundary detection is the hard 60% of Phase 0** — sub-notes (8a/8b/8c)
  under one heading, notes spanning pages, and standalone vs consolidated reusing
  the same numbers. The index must be basis-scoped or it will fetch the wrong
  statement.
- **Industry structural divergence** may exceed vocabulary divergence. A bank's
  balance sheet (deposits, advances, NPAs) barely overlaps a manufacturer's; this
  may require industry-variant profiles over a common core. Deferred until the
  Phase 4 gate reports.
- **Cost of the agentic tier** is bounded only by the escalation rate. If
  escalation exceeds ~20% of fields, the deterministic spine is under-performing
  and should be fixed rather than papered over with reasoning.

## 11. What already exists

| Stage | Status |
|---|---|
| Schema | v4.1 built and validated; needs definitions/aliases (Phase 2) |
| **Planning** | **does not exist — the core gap** |
| Retrieval | hybrid search built; needs `get_note()` (Phase 0) |
| Reasoning | extraction built; **mapping is new** (Phase 3) |
| Validation | derivation + validation + confidence engines built (M2/M3) |
| Normalization | dual-period parsing works; unit/scale handling is thin |
| Structured JSON | Fact model, factsheet, coverage, XBRL export built (M1/M4) |

Two real gaps — **Planning** and **Mapping** — plus a thin spot in
**Normalization**. Everything else is built and tested (720 backend unit tests
green as of `edd0176`).
