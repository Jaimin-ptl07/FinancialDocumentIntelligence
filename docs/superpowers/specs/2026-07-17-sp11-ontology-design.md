# SP-11 — Ontology Mapping (dictionary-based)

- **Status:** Implemented 2026-07-17.
- **Depends on:** SP-8 (Document AST — `{schema_version, title, sections:[{heading, paragraphs}]}`), SP-10
  (Semantic Intelligence — `{schema_version, sections:[{heading, type, paragraph_count}]}`; consumed
  conceptually, not read by the thin slice — see §2.4).
- **Roadmap:** phase 8 "Ontology Mapping: Revenue│Sales│Turnover → CANONICAL_REVENUE". Build order §4
  puts SP-11 right after SP-10 semantic in Stage 2 ("SP-10 semantic → SP-11 ontology").

## 1. Purpose

Map financial line-item labels — the vocabulary a document actually uses ("Revenue", "Sales", "Turnover",
"PAT", "Total Assets", …) — onto a small, closed set of **canonical concepts** (`CANONICAL_REVENUE`-style,
modeled here as `CanonicalConcept.REVENUE` etc.) so that later layers (SP-9 graph, SP-16 cross-document)
can compare "Revenue" in one report against "Turnover" in another without a bespoke synonym table at every
call site. This is the ontology seam: everything downstream commits to the canonical concept, never to a
document's own wording.

Thin vertical slice: a pure `map_label` (single-string classifier) + a pure `map_document` (walks an AST) +
an `OntologyStage` (`deps=("ast",)`) that auto-chains off AST. **Dictionary-based and deterministic** — no
LLM in this slice. An LLM-based mapper (better recall on labels the starter dictionary misses) is a
**measured candidate**, deferred to SP-3's harness (§6) exactly as SP-10's semantic classifier was: ship the
simple thing now, let a later dataset decide if a smarter (costlier, non-deterministic) mapper earns its
keep.

## 2. Scope decisions (autonomous; documented)

1. **A closed, canonical concept set, not free-text.** `fdi.core.ontology.CanonicalConcept` is a `StrEnum`
   of 10 values: `REVENUE`, `COST_OF_GOODS_SOLD`, `GROSS_PROFIT`, `OPERATING_EXPENSE`, `NET_PROFIT`,
   `TOTAL_ASSETS`, `TOTAL_LIABILITIES`, `EQUITY`, `CASH`, `UNKNOWN`. Same rationale as SP-10's 8-label
   `SectionType`: a closed set is what makes mapping checkable (SP-3 `exact_match` against a ground-truth
   label→concept dataset) and keeps this layer's contract small. `UNKNOWN` is a first-class member (not an
   absence) — a label the dictionary doesn't recognize is a valid, expected outcome, not an error.
2. **A module-level synonym dictionary, not a database table.** `fdi.core.ontology._SYNONYMS: dict[str,
   CanonicalConcept]` maps a *normalized* label to a concept. It is code, versioned with the module,
   reviewed like any other source file — appropriate for a starter set of a few dozen entries. A richer,
   data-driven ontology (thousands of entries, multi-language, maintained outside a Python file) is
   deferred (§6) until real corpus coverage numbers show the starter set is the bottleneck.
3. **Normalization is deliberately simple:** lowercase, strip surrounding whitespace, drop punctuation,
   collapse internal whitespace to single spaces. This absorbs the cheap, high-frequency variation
   ("Revenue:", "REVENUE", "  Revenue  ", "Revenue.") without attempting stemming, pluralization handling,
   or fuzzy/edit-distance matching — those are exactly the kind of recall improvements an LLM-based or
   embedding-based mapper (§6) would bring over a dictionary, and are the reason this slice is explicitly
   NOT trying to be complete.
4. **`map_document` reads only the AST for the thin slice — not the SP-10 `semantic` artifact.** The
   roadmap's SP-10 handoff note suggested SP-11 read `semantic` alongside `ast` (same `heading` order) to
   scope mappings by statement type (e.g. only look for `REVENUE` inside an `INCOME_STATEMENT`-typed
   section). That is a real accuracy improvement but not required for a first thin slice that just needs
   the label→concept plumbing working end-to-end; scoping by section type without it first would couple
   two derived stages before either has a wired consumer. `OntologyStage` therefore takes `deps=("ast",)`
   only. **Follow-on (§6):** widen to `deps=("ast", "semantic")` and scope the synonym lookup per
   `SectionType` once there is a concrete accuracy reason to.
5. **`OntologyStage` is a DERIVED stage** (`name="ontology"`, `version=1`, `deps=("ast",)`) — the same shape
   as SP-8's `AstStage` (`deps=("normalize",)`) and SP-6's `NormalizeStage` (`deps=("ocr",)`): a pure
   function of its single upstream artifact, no injected service, no external call. It reads the AST via
   `ctx.read("ast")` and returns the mappings artifact. Because it depends on `ast`, the SP-2 resolver makes
   it a successor — `run_stage("ast", ...)` (itself already auto-chained from `ocr`) auto-enqueues
   `ontology` too.
6. **Every section heading AND every paragraph is a mapping candidate, kept only if non-`UNKNOWN`.** The
   thin slice does not try to distinguish "this paragraph is a labeled line item" from "this paragraph is
   narrative prose" — it runs `map_label` over the whole paragraph string either way and keeps whatever
   isn't `UNKNOWN`. This is a deliberate, documented simplification (§6): a real line item is usually
   `"Revenue  12,345"` (label + number in one OCR paragraph), which a whole-string dictionary lookup will
   miss because the trailing number breaks the exact match. Recognizing embedded labels-with-numbers needs
   real line-item parsing (regex/tokenization to split label from value) and is exactly the kind of case
   numeric-value extraction (deferred to a later ontology follow-on, §6) exists to solve. The thin slice
   still has value today: headings ("Revenue", "Total Assets") and any paragraph that IS just a label on
   its own line map correctly, and the wiring (stage, artifact, chain) is proven end-to-end.

## 3. Architecture

### 3.1 Layering

- **`fdi.core.ontology`** — `CanonicalConcept` (`StrEnum`), the `_SYNONYMS` dictionary, `map_label(label:
  str) -> CanonicalConcept` (pure), `map_document(ast: Mapping[str, object]) -> dict[str, object]` (pure;
  no I/O).
- **`fdi.pipeline.stages.ontology`** — `OntologyStage`, self-registered into `REGISTRY`.

### 3.2 `CanonicalConcept` + synonym dictionary

```python
# fdi.core.ontology
import re
from enum import StrEnum


class CanonicalConcept(StrEnum):
    REVENUE = "REVENUE"
    COST_OF_GOODS_SOLD = "COST_OF_GOODS_SOLD"
    GROSS_PROFIT = "GROSS_PROFIT"
    OPERATING_EXPENSE = "OPERATING_EXPENSE"
    NET_PROFIT = "NET_PROFIT"
    TOTAL_ASSETS = "TOTAL_ASSETS"
    TOTAL_LIABILITIES = "TOTAL_LIABILITIES"
    EQUITY = "EQUITY"
    CASH = "CASH"
    UNKNOWN = "UNKNOWN"


# Indian-annual-report starter set. Keys are ALREADY in normalized form
# (lowercase, no punctuation, single-spaced) -- see _normalize_label.
_SYNONYMS: dict[str, CanonicalConcept] = {
    "revenue": CanonicalConcept.REVENUE,
    "sales": CanonicalConcept.REVENUE,
    "turnover": CanonicalConcept.REVENUE,
    "revenue from operations": CanonicalConcept.REVENUE,
    "income from operations": CanonicalConcept.REVENUE,
    "total revenue": CanonicalConcept.REVENUE,
    "cost of goods sold": CanonicalConcept.COST_OF_GOODS_SOLD,
    "cogs": CanonicalConcept.COST_OF_GOODS_SOLD,
    "cost of materials consumed": CanonicalConcept.COST_OF_GOODS_SOLD,
    "gross profit": CanonicalConcept.GROSS_PROFIT,
    "operating expenses": CanonicalConcept.OPERATING_EXPENSE,
    "operating expense": CanonicalConcept.OPERATING_EXPENSE,
    "total expenses": CanonicalConcept.OPERATING_EXPENSE,
    "net profit": CanonicalConcept.NET_PROFIT,
    "profit after tax": CanonicalConcept.NET_PROFIT,
    "pat": CanonicalConcept.NET_PROFIT,
    "profit for the year": CanonicalConcept.NET_PROFIT,
    "net income": CanonicalConcept.NET_PROFIT,
    "total assets": CanonicalConcept.TOTAL_ASSETS,
    "total liabilities": CanonicalConcept.TOTAL_LIABILITIES,
    "equity": CanonicalConcept.EQUITY,
    "total equity": CanonicalConcept.EQUITY,
    "shareholders equity": CanonicalConcept.EQUITY,
    "shareholders funds": CanonicalConcept.EQUITY,
    "cash": CanonicalConcept.CASH,
    "cash and cash equivalents": CanonicalConcept.CASH,
    "cash and bank balances": CanonicalConcept.CASH,
}


_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _normalize_label(label: str) -> str:
    lowered = label.lower()
    stripped_punct = _PUNCT_RE.sub("", lowered)
    return _WS_RE.sub(" ", stripped_punct).strip()


def map_label(label: str) -> CanonicalConcept:
    """Normalize `label` and look it up in the synonym dictionary.

    Returns CanonicalConcept.UNKNOWN for anything not recognized -- an
    expected, non-error outcome, not a failure mode.
    """
    normalized = _normalize_label(label)
    return _SYNONYMS.get(normalized, CanonicalConcept.UNKNOWN)
```

`map_label` is total (never raises): any `str` input, including `""`, produces a `CanonicalConcept`
(`""` normalizes to `""`, which is not a dictionary key, so it maps to `UNKNOWN`).

### 3.3 `map_document`

```python
def map_document(ast: Mapping[str, object]) -> dict[str, object]:
    """Map every section heading and every paragraph in `ast` through
    map_label, keeping only the non-UNKNOWN results.

    SIMPLIFICATION (documented, §2.6 / §6): a paragraph is mapped as a WHOLE
    string. A real OCR line item like "Revenue  12,345" will not match the
    dictionary (the trailing number breaks the exact lookup) -- only headings
    and paragraphs that are themselves bare labels map today. Numeric-value
    extraction + real line-item parsing is a follow-on.
    """
    mappings: list[dict[str, object]] = []
    sections_raw = ast.get("sections")
    if isinstance(sections_raw, list):
        for section in sections_raw:
            if not isinstance(section, dict):
                continue
            heading = section.get("heading")
            if isinstance(heading, str):
                concept = map_label(heading)
                if concept is not CanonicalConcept.UNKNOWN:
                    mappings.append({"label": heading, "concept": concept.value})
            paragraphs_raw = section.get("paragraphs")
            if isinstance(paragraphs_raw, list):
                for paragraph in paragraphs_raw:
                    if not isinstance(paragraph, str):
                        continue
                    concept = map_label(paragraph)
                    if concept is not CanonicalConcept.UNKNOWN:
                        mappings.append({"label": paragraph, "concept": concept.value})
    return {"schema_version": 1, "mappings": mappings}
```

Output schema: `{"schema_version": 1, "mappings": [{"label": <original text>, "concept": <CanonicalConcept
value>}, ...]}`, in document order (section order, heading before its paragraphs, paragraph order within a
section) — the same "stable, ordered artifact" discipline as SP-10's `sections` list.

### 3.4 `OntologyStage`

```python
# fdi.pipeline.stages.ontology
class OntologyStage:
    name = "ontology"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        return map_document(ast)


REGISTRY.register(OntologyStage())
```

Reads no external service and calls no upstream I/O beyond `ctx.read("ast")`; `deps=("ast",)` makes the
framework schedule it after AST and hash its `input_hash` from AST's `output_hash` (SP-2's existing
derived-stage machinery — no new framework code needed, exactly as SP-8's `AstStage` needed none beyond
what SP-6/SP-5 had already built).

## 4. Error handling

- `map_label` never raises — any `str` (including `""`) resolves to a `CanonicalConcept`, `UNKNOWN` when
  unrecognized.
- `map_document` never raises on a malformed/partial AST — missing `sections`, a non-list `sections`, a
  section that isn't a `dict`, a missing/non-string `heading`, or a missing/non-list `paragraphs` are all
  treated as "nothing to map here" and skipped, mirroring `build_ast`'s and `SemanticStage`'s defensive
  `isinstance` walks over `object`-typed artifact data. An AST with zero sections → `{"schema_version": 1,
  "mappings": []}`, a trivially valid result, not an error.
- Running `ontology` before `ast` has a recorded artifact → `ctx.read("ast")` (via `latest_run`) asserts the
  upstream run exists; absent, it fails clearly (same DAG-ordering guarantee/failure mode as every other
  derived stage in this codebase).

## 5. Testing

- **Unit — `map_label`** (no Docker): each documented synonym (`"revenue"`, `"sales"`, `"turnover"`, `"pat"`,
  `"cogs"`, …) maps to the expected `CanonicalConcept`; case/whitespace/punctuation variants of the same
  synonym (`"REVENUE"`, `"  Revenue  "`, `"Revenue:"`, `"Revenue."`) map to the same concept as the plain
  form; an unrecognized label (`"Some Random Heading"`, `""`) maps to `UNKNOWN`.
- **Unit — `map_document`** (no Docker): a canned AST with headings and paragraphs mixing recognized labels,
  unrecognized labels, and narrative prose → the expected `mappings` list (order preserved, `UNKNOWN`
  entries absent); an AST with no sections → `{"schema_version": 1, "mappings": []}`; a malformed section
  (missing `paragraphs`) is skipped without raising.
- **Integration** (testcontainers, fake OCR): run `ocr → normalize → ast → ontology` via `run_stage` in a
  local registry (`OcrStage`, `NormalizeStage`, `AstStage`, `OntologyStage`); assert the `ontology` artifact
  is the expected mappings list and its `input_hash` derives from the AST `output_hash` (a rerun is a cache
  hit). Prove the resolver makes `ontology` a successor of `ast`
  (`DagResolver(REGISTRY.all()).successors("ast")` contains `"ontology"`) — and, per the SP-6/SP-8 lesson,
  re-check (during implementation) that no test asserts an exact `successors("ast")` list against the
  GLOBAL registry: as of this writing, `test_quality_stage.py` and `test_semantic_stage.py` already use
  membership assertions (`"quality"`/`"semantic" in resolver.successors("ast")`), and `test_resolver.py`'s
  `successors("a") == ["b"]`-style exact assertions run against a small synthetic registry, not
  `REGISTRY.all()` — so registering `OntologyStage` globally is not expected to break an existing exact-list
  assertion, but confirm this holds (don't skip the check just because it held at spec-writing time).

## 6. Explicitly deferred (YAGNI)

- **An LLM-based (or embedding-based) mapper as an SP-3 measured candidate.** Once a ground-truth dataset of
  (label → canonical concept) pairs exists, wrap the dictionary lookup AND an LLM-based classifier as
  `Candidate`s (same shape as SP-10's semantic-classifier deferral) and let SP-3's leaderboard decide
  whether the added recall is worth the added cost/non-determinism. Not built here.
- **Numeric value extraction + reconciliation.** Pulling the actual number attached to a mapped label
  (`"Revenue  12,345"` → `{concept: REVENUE, value: 12345}`) and cross-checking totals (e.g. `GROSS_PROFIT
  == REVENUE - COST_OF_GOODS_SOLD`) needs real line-item parsing and is the ingredient SP-17's follow-on
  (structure-aware / numeric-total quality checks) is waiting on per the roadmap's SP-17 handoff note.
- **Per-section-type scoping via the SP-10 `semantic` artifact** (§2.4) — widen `OntologyStage.deps` to
  `("ast", "semantic")` and only look for e.g. `REVENUE`/`COST_OF_GOODS_SOLD` inside an
  `INCOME_STATEMENT`-typed section, `TOTAL_ASSETS`/`TOTAL_LIABILITIES` inside a `BALANCE_SHEET`-typed one.
  Reduces false positives from a bare-label match landing in the wrong statement.
- **A richer ontology** (hundreds/thousands of synonyms, multi-language labels, hierarchical concepts,
  maintained as data rather than a Python dict) — grow the starter set only once real corpus coverage
  numbers (via SP-3) show which labels the dictionary is missing.
- **Fuzzy/partial matching** (edit distance, substring/contains matching, stemming) — the normalization in
  §2.3 is deliberately simple; anything fuzzier risks false positives without a measurement harness to
  catch regressions, which is exactly what an LLM/embedding candidate (first bullet) would bring with a
  scorable trade-off instead of an ad hoc heuristic.

## 7. Done criteria

1. `CanonicalConcept` (`fdi.core.ontology`) is a pure `StrEnum` with the 10 documented values; `map_label`
   maps every documented synonym (+ case/whitespace/punctuation variants) to the right concept and an
   unrecognized label to `UNKNOWN`; never raises.
2. `map_document(ast)` returns `{schema_version, mappings:[{label, concept}]}` from an AST's section
   headings and paragraphs, keeping only non-`UNKNOWN` results, in document order; a sectionless/malformed
   AST yields `{"schema_version": 1, "mappings": []}` without raising.
3. `OntologyStage` (`deps=("ast",)`) reads the AST artifact and produces the mappings artifact; a rerun is a
   cache hit; its `input_hash` derives from the AST `output_hash`.
4. `DagResolver(REGISTRY.all()).successors("ast")` includes `"ontology"` (the chain is automatic); verified
   no existing test asserts an exact `successors("ast")` list against the global registry (none does as of
   this writing — the two existing `successors("ast")` assertions are already membership-style).
5. All four static gates green; `core` stays pure; full suite passes.
