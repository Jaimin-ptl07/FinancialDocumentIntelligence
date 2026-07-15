# Financial Document Intelligence — Ingestion Layer Roadmap

- **Status:** Roadmap agreed 2026-07-15. SP-1 spec written; nothing implemented yet.
- **Last updated:** 2026-07-15
- **Purpose:** the single durable entry point for this project. It records the vision, the
  sub-project decomposition, the build order, and the invariants every sub-project must honor.

> **This is a map, not a task list.** Each sub-project gets its own design spec + TDD plan
> (`superpowers:brainstorming` → `superpowers:writing-plans`) at the time it is implemented,
> then executed with `superpowers:subagent-driven-development`. Do not implement directly from
> this doc — use it to scope the next sub-project's own spec.

---

## 1. The Vision

> **Process the document once → create reusable knowledge artifacts → every downstream task
> operates on knowledge instead of PDF.**

Target corpus: **5000+ annual reports, 300–600 pages each.** One report yields tens of MB of OCR
output; the corpus implies roughly **700 GB of artifacts** and ~2.5M pages of OCR.

The pipeline is a linear sequence of stages. Each stage consumes the previous stage's artifact and
emits its own. Artifacts are content-addressed and version-stamped, so **any single stage can be
improved and its downstream regenerated without re-running the stages above it** — most importantly,
without re-running (and re-paying for) OCR.

---

## 2. The 12 Ingestion Phases

```
PDF
 │
 ├─ 1  Document Intake            upload · checksum · dedup · versioning · registry
 ├─ 2  OCR & Layout Extraction    OCR JSON · images · bboxes · polygons · confidence
 ├─ 3  Normalization              vendor output → internal unified schema
 ├─ 4  Structural Intelligence    ◄── where almost every OCR defect is corrected
 ├─ 5  Document AST               hierarchical tree · sections · page spans
 ├─ 6  Canonical Structure Graph  typed edges: hierarchy · reading · spatial · table · xref
 ├─ 7  Semantic Intelligence      section classification · entities · statement detection
 ├─ 8  Ontology Mapping           Revenue│Sales│Turnover → CANONICAL_REVENUE
 ├─ 9  Provenance Graph           Fact → Cell → Table → Page → Polygon → Span → PDF
 ├─ 10 Embeddings                 document · section · paragraph · table summary · entity
 ├─ 11 Retrieval Indexes          metadata · entity · section · clause · table · vector · graph
 └─ 12 Knowledge Store
```

### Phase 4 in detail (the highest-value, highest-risk phase)

Reading-order recovery · header/footer removal · page-number detection · column detection ·
paragraph stitching · section stitching · multi-page table stitching · table header recovery ·
merged-cell recovery · nested-header recovery · caption linking · footnote linking ·
cross-reference detection · table-continuation detection.

---

## 3. Sub-Project Decomposition

Each row is one spec → plan → implement cycle.

| # | Sub-project | Phase | Research | Status |
|---|---|---|---|---|
| **SP-1** | Project setup & walking skeleton | setup | light | 📝 spec written |
| **SP-2** | The base — unified schema, artifact store, registry, **stage/DAG framework** | base | medium | ⬜ |
| **SP-3** | Eval harness & ground truth | — | medium | ⬜ |
| **SP-4** | Document Intake | 1 | light | ⬜ |
| **SP-5** | OCR & Layout Extraction | 2 | medium | ⬜ |
| **SP-6** | Normalization | 3 | light | ⬜ |
| **SP-7a** | Structural: page layout — reading order, columns, header/footer | 4 | **heavy** | ⬜ |
| **SP-7b** | Structural: cross-page stitching — paragraphs, sections, tables | 4 | **heavy** | ⬜ |
| **SP-7c** | Structural: table internals — headers, merged/nested cells, grid | 4 | **heavy** | ⬜ |
| **SP-7d** | Structural: linking — captions, footnotes, cross-references | 4 | **heavy** | ⬜ |
| **SP-8** | Document AST | 5 | medium | ⬜ |
| **SP-9** | Canonical Graph → Neo4j | 6 | medium | ⬜ |
| **SP-10** | Semantic Intelligence | 7 | **heavy** | ⬜ |
| **SP-11** | Ontology Mapping | 8 | **heavy** | ⬜ |
| **SP-12** | Provenance Graph | 9 | medium | ⬜ |
| **SP-13** | Embeddings → Milvus | 10 | **heavy** | ⬜ |
| **SP-14** | Retrieval Indexes | 11 | **heavy** | ⬜ |
| **SP-15** | Knowledge Store consolidation | 12 | light | ⬜ |
| **SP-16** | Cross-document layer — entity resolution, version linking, cross-doc refs | enterprise | **heavy** | ⬜ |
| **SP-17** | Quality validation framework | enterprise | medium | ⬜ |
| **SP-18** | Next.js frontend | — | light | ⬜ |

### Why SP-2 is the highest-leverage sub-project

*Incremental reprocessing* and *artifact versioning* are **properties of the stage framework**, not
features that can be bolted on. If SP-2 does not deliver content-addressed, version-stamped
artifacts with a real dependency DAG, no later sub-project can retrofit them. Everything plugs into
whatever SP-2 decides.

### Why SP-3 exists

The goal is *"the best algorithm at each step for most accurate functioning."* Research produces
**candidates** (XY-cut vs. LayoutReader vs. topological sort for reading order). It cannot say which
wins **on Indian annual reports** — only measurement does. Without an eval harness built *before*
the heavy layers, "best algorithm" is unfalsifiable and selection degenerates to reputation.
One annotated document with ground truth outweighs a week of literature review.

Research is therefore **per-layer and continuous**, feeding candidates into SP-3's harness, which
picks the winner.

---

## 4. Build Order — Hybrid (spine first, then deepen)

**Stage 1 — get a real document through a real stack.**

```
SP-1 setup → SP-2 base → SP-3 eval harness
  → SP-4 intake → SP-5 OCR → SP-6 normalize
  → SP-8 AST (naive) → SP-18 UI (thin)
```

Outcome: real OCR output and real measurements to design against, plus a demoable system.

**Stage 2 — deepen, in dependency order, measured by SP-3.**

```
SP-7a/b/c/d structural intelligence   ◄── the hard part, now with a feedback loop
SP-9 graph → SP-12 provenance
SP-10 semantic → SP-11 ontology
SP-13 embeddings → SP-14 indexes
SP-15 store · SP-16 cross-doc · SP-17 quality
```

**Why this is safe rather than sloppy:** naive-then-deepen is precisely what artifact versioning and
incremental reprocessing exist to enable. Phase 4 gets rebuilt later *without re-running OCR*. Strict
layer-by-layer was rejected because it defers the first end-to-end run to ~SP-14, lands all
Neo4j/Milvus integration risk last, and forces Phase 4's algorithms to be designed with no
downstream signal.

---

## 5. Hard Invariants

Every sub-project must honor these. They are cheap now and expensive to retrofit.

1. **`core` is pure.** No I/O, no config singleton, no wall-clock, no RNG. Enforced by an
   `import-linter` contract in CI: `api, workers → pipeline → platform → core`.
2. **Determinism.** Re-running a stage on the same input yields a byte-identical artifact. No
   wall-clock, no RNG, stable sorts, pinned float formatting. Assert it in an idempotency test.
3. **Provenance survives every transform.** Every derived object traces to page, polygon and source
   span. A node that moves or merges keeps its source reference.
4. **Prefork only.** Stages are CPU-bound; gevent/eventlet cannot parallelize them, starve the
   broker heartbeat, and cannot enforce time limits.
5. **`asyncio.run()` once per task, never at module import.** A loop created before fork is copied
   without its thread and hangs silently.
6. **Artifacts are content-addressed and version-stamped** with the code version that produced them.
7. **12-factor.** Config from env only; no host paths in code. This is what lets the same images run
   in cloud unchanged.

---

## 6. Deployment Posture

**Now:** local Docker Compose on a 19.8 GB / 4-core laptop, using Compose profiles so only the
services a sub-project needs are running (`core` ≈ 5 GB; `graph` at SP-9; `vector` at SP-13).
Volumes live on **F:** (202 GB free), never D: (29 GB free). Corpus: ~5–20 real reports.

**Later:** the same images lift to cloud — managed Postgres, S3/Blob, hosted Neo4j, managed vector
store, autoscaled workers. Expected around SP-13/SP-14, when the 5000-document run becomes real.
The 12-factor discipline above is what makes this a config change rather than a redesign.

---

## 7. Source-of-Truth Documents

- **This roadmap:** `docs/architecture/ingestion-roadmap.md`
- **SP-1 spec:** `docs/superpowers/specs/2026-07-15-project-setup-design.md`
- Sub-project specs: `docs/superpowers/specs/<date>-<slug>-design.md`
- Sub-project plans: `docs/superpowers/plans/<date>-<slug>.md`
