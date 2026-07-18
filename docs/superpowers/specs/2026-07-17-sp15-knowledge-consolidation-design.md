# SP-15 — Knowledge Store Consolidation

- **Status:** Implemented 2026-07-18.
- **Depends on:** SP-10 (`fdi.core.semantic.SectionType` + `SemanticStage`, `deps=("ast",)` →
  `{schema_version:1, sections:[{heading, type, paragraph_count}]}`, `type` one of the 8
  `SectionType` values), SP-11 (`fdi.core.ontology.CanonicalConcept` (10 members, incl. `UNKNOWN`) +
  `map_document`/`OntologyStage`, `deps=("ast",)` → `{schema_version:1, mappings:[{label,
  concept}]}` — `map_document` already omits `UNKNOWN` mappings itself, so every entry present is a
  real concept), SP-17/SP-17b (`fdi.core.quality.validate_all` + `QualityStage`, now
  `deps=("ast","structure")`, `version=2` → `{schema_version:1, issues, error_count, warning_count,
  ok}`), SP-2 (stage/DAG framework — **confirmed multi-dep support, established fact, re-confirmed
  directly against source for this spec**: `run.py`'s `input_hash = derived_input_hash([facts[d].
  output_hash for d in stage.deps])` is a plain list comprehension over `stage.deps` — already
  N-ary, no 2-dep special-casing anywhere; `resolver.py`'s `successors(name) = sorted(n for n, s in
  self._stages.items() if name in s.deps)` and `_depth(name) = 0 if not deps else 1 + max(_depth(d)
  for d in deps)` are likewise generic over `deps`'s length. Nothing in `run.py`/`resolver.py`/
  `registry.py` assumes 1 or 2 dependencies specifically).
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 table — SP-15 "Knowledge Store
  consolidation", phase 12 "Knowledge Store", weight **light**, currently ⬜. §4 build order lists
  "SP-15 store · SP-16 cross-doc · SP-17 quality" as the last deepening group. SP-12's spec and
  SP-17b's spec each separately claimed "the codebase's first two-dependency stage" while racing to
  land (both since implemented: `ProvenanceStage` `deps=("graph","ocr")`, `QualityStage` v2
  `deps=("ast","structure")` — confirmed by reading `backend/src/fdi/pipeline/stages/quality.py` and
  the registered `stages/__init__.py` directly for this spec). With both two-dependency stages
  already shipped and no other three-or-more-dependency stage registered anywhere in
  `stages/__init__.py` today, **`KnowledgeStage` (`deps=("semantic", "ontology", "quality")`) is
  unambiguously the codebase's first THREE-dependency stage** — no parallel-draft race to note here.

## 1. Purpose

SP-10, SP-11, and SP-17/17b each independently produce a *slice* of what a document "means": SP-10
says which `SectionType` each section is, SP-11 says which `CanonicalConcept`s appear in it, SP-17b
says whether the document is internally sound. Nothing today reads all three together. Phase 12 of
the roadmap ("Knowledge Store") asks for exactly that: **fold a document's already-derived knowledge
artifacts into ONE unified per-document record**, so a later consumer (SP-16's cross-document entity
resolution, a UI summary card, an eval check) can read one artifact instead of joining three.

The full ambition — a persisted, queryable knowledge store spanning every document, numeric
roll-ups, graph/provenance/embedding facts folded in too — needs infrastructure and extraction this
slice does not have (no numeric-value extraction yet — SP-11 §6's own deferral; no external store;
no cross-document layer, which is SP-16's job entirely). SP-15 is therefore the **thin slice**: a
pure `consolidate(semantic, ontology, quality)` function that reads exactly the three artifacts that
already exist and are already this shape, producing one small, deterministic record — the same
"thin-slice, pure-core-first" shape as SP-9/SP-11/SP-12, and, like SP-12 and SP-17b before it, no
new OCR call and no new upstream artifact: SP-15 only *joins* artifacts that already exist.

Thin slice, pure-core-first: `fdi.core.knowledge.consolidate` + exhaustive unit tests, then a
`KnowledgeStage` (`deps=("semantic", "ontology", "quality")`) that auto-chains off ALL THREE via the
existing DAG machinery — no framework change. This is the codebase's **first stage to declare three
dependencies** (see the roadmap note above).

## 2. Scope decisions (autonomous; documented)

1. **`consolidate` takes exactly the three artifacts SP-10/SP-11/SP-17b already produce, in that
   parameter order (`semantic, ontology, quality`) — no graph, provenance, or embedding artifact is
   read in this slice.** Those artifacts carry structural/geometric/vector information, not
   "what does this document mean" information; folding them in is real but separate work, deferred
   to §6 as the value of doing so becomes concrete (e.g. once SP-16 needs a graph-shaped view).
2. **The unified record's shape is a fixed five-key dict, not a passthrough merge of the three
   inputs:** `{schema_version, section_count, section_types, concepts, concept_count, quality}`.
   `section_count`/`section_types` derive from `semantic["sections"]`; `concepts`/`concept_count`
   derive from `ontology["mappings"]`; `quality` is a three-key summary echo (`ok`, `error_count`,
   `warning_count`) of the quality report, not the full `issues` list — the knowledge record answers
   "is this document sound", not "why", which is what the `quality` artifact itself is still for.
3. **`section_types` counts by the semantic artifact's own `type` string, not by re-validating
   against `SectionType`.** `fdi.core.knowledge` does not import `fdi.core.semantic.SectionType` —
   any string-valued `type` on a section is counted as a bucket key. This keeps `knowledge` decoupled
   from `semantic`'s enum (forward-compatible with a future `SectionType` member SP-15 doesn't know
   about, exactly like SP-12 decision 7's "unrecognized type is not an error" posture) and avoids a
   needless import; the semantic artifact is already the sole source of truth for what a valid `type`
   value is.
4. **`concepts` is the SORTED, UNIQUE set of non-`UNKNOWN` concepts, not a multiset/histogram.**
   `map_document` (SP-11) already omits `UNKNOWN` mappings from the `ontology` artifact itself, so in
   practice every `mappings[].concept` present is already non-`UNKNOWN` — but `consolidate` re-checks
   `!= "UNKNOWN"` defensively rather than trusting that invariant blindly (the same "don't trust an
   upstream artifact's own promises without checking" posture SP-12's error-handling section takes
   toward the graph/OCR artifacts). Two paragraphs both mapping to `REVENUE` collapse to one
   `"REVENUE"` entry — a document's knowledge record answers "which concepts appear", not "how many
   times", matching decision 2's "answers what, not detail" framing. Sorted for determinism (Global
   Constraint 6): a `set`'s iteration order is not itself deterministic across runs/interpreters, so
   the pure function must sort before returning, not incidentally rely on insertion order.
5. **A section whose `type` is missing/non-`str`, or an ontology mapping whose `concept` is
   missing/non-`str`, degrades independently at its own granularity — never drops the whole
   artifact.** A malformed section still counts toward `section_count` (it IS a section) but
   contributes no bucket to `section_types` (it has no usable type); a malformed mapping contributes
   nothing to `concepts` at all (there is no partial "half a concept"). This mirrors SP-12's
   per-field (not per-record) degrade posture.
6. **The `quality` echo degrades to `{"ok": False, "error_count": 0, "warning_count": 0}` when the
   `quality` artifact is missing/malformed** — `False`/`0`/`0` is the same "empty input → zero
   counts / empty collections" contract every other field in the record follows (§ purpose statement,
   confirmed in Done Criteria 1), not a special case. A `quality["ok"]` that is present but non-`bool`
   (e.g. a stray `1`), or an `error_count`/`warning_count` that is present but non-`int`, degrades the
   same way, field by field — never coerced (a non-`bool` `"yes"` does NOT become `True`).
7. **`KnowledgeStage.deps = ("semantic", "ontology", "quality")`, in that fixed order — the order is
   part of the artifact's cache identity, exactly as SP-12 decision 5 and SP-17b decision 3 already
   established for their own dep tuples.** `run.py` computes `input_hash =
   derived_input_hash([facts["semantic"].output_hash, facts["ontology"].output_hash,
   facts["quality"].output_hash])` — reordering the tuple later would silently change every
   document's `knowledge` artifact key. The order chosen ("what type is it" → "what concepts does it
   contain" → "is it sound") mirrors the record's own field order (decision 2) and the roadmap's
   phase-7/8/enterprise ordering of the underlying sub-projects — not arbitrary.
8. **`KnowledgeStage` is a DERIVED stage** (`name="knowledge"`, `version=1`, `deps=("semantic",
   "ontology", "quality")`) — a pure function of three upstream artifacts, no injected service, no
   external call (unlike `SemanticStage`, which itself needs an injected `SemanticClient` — that
   non-determinism lives entirely upstream of `knowledge`, which only reads its already-materialized,
   already-cached output). Because it depends on all three, the SP-2 resolver makes it a successor of
   **all three**: `resolver.successors("semantic")`, `resolver.successors("ontology")`, and
   `resolver.successors("quality")` all gain `"knowledge"`. `run_stage("ocr", ...)` already
   auto-enqueues the whole graph; `knowledge` now additionally waits on `semantic`, `ontology`, AND
   `quality` (which itself now waits on `ast` AND `structure`, post-SP-17b) all finishing before it
   can run — handled entirely by the existing generic `stale`/`plan`/`successors` machinery over an
   N-length `deps` tuple, no new code path.

## 3. Architecture

### 3.1 Layering (honours the CI-enforced `api | workers → pipeline → platform → core` contract)

- **`fdi.core.knowledge`** — `consolidate(semantic: Mapping[str, object], ontology: Mapping[str,
  object], quality: Mapping[str, object]) -> dict[str, object]` (pure; no I/O; no import of
  `fdi.core.semantic`/`fdi.core.ontology`/`fdi.core.quality` — decision 3's decoupling means
  `consolidate` treats all three inputs as plain `Mapping[str, object]`, the same "read defensively,
  never import the producer's types" posture `build_provenance` already takes toward `graph`/`ocr`).
- **`fdi.pipeline.stages.knowledge`** — `KnowledgeStage`, self-registered into `REGISTRY`.

### 3.2 `consolidate` algorithm

```
section_types = {}                        # dict[str, int], insertion order = semantic["sections"] order
section_count = 0
for section in semantic.get("sections", []):   # skip non-Mapping entries entirely
    if not isinstance(section, Mapping):
        continue
    section_count += 1                    # a malformed section still counts as "a section"
    type_ = section.get("type")
    if isinstance(type_, str):
        section_types[type_] = section_types.get(type_, 0) + 1
    # non-str/missing type: section_count still incremented, no bucket added

concepts_set = set()
for mapping in ontology.get("mappings", []):   # skip non-Mapping entries entirely
    if not isinstance(mapping, Mapping):
        continue
    concept = mapping.get("concept")
    if isinstance(concept, str) and concept != "UNKNOWN":
        concepts_set.add(concept)
concepts = sorted(concepts_set)            # deterministic (decision 4)

ok = quality.get("ok")
error_count = quality.get("error_count")
warning_count = quality.get("warning_count")

return {
    "schema_version": 1,
    "section_count": section_count,
    "section_types": section_types,
    "concepts": concepts,
    "concept_count": len(concepts),
    "quality": {
        "ok": ok if isinstance(ok, bool) else False,
        "error_count": error_count if isinstance(error_count, int) else 0,
        "warning_count": warning_count if isinstance(warning_count, int) else 0,
    },
}
```

Example output for a two-section, one-concept, clean document:

```json
{
  "schema_version": 1,
  "section_count": 2,
  "section_types": {"BALANCE_SHEET": 1, "DIRECTORS_REPORT": 1},
  "concepts": ["TOTAL_ASSETS"],
  "concept_count": 1,
  "quality": {"ok": true, "error_count": 0, "warning_count": 0}
}
```

### 3.3 `KnowledgeStage`

```python
class KnowledgeStage:
    name = "knowledge"
    version = 1
    deps: tuple[str, ...] = ("semantic", "ontology", "quality")

    async def run(self, ctx: StageContext) -> object:
        semantic = cast(Mapping[str, object], await ctx.read("semantic"))
        ontology = cast(Mapping[str, object], await ctx.read("ontology"))
        quality = cast(Mapping[str, object], await ctx.read("quality"))
        return consolidate(semantic, ontology, quality)
```

`deps=("semantic", "ontology", "quality")` makes the SP-2 resolver schedule `knowledge` as a
successor of all three and hashes its `input_hash` from `derived_input_hash([semantic.output_hash,
ontology.output_hash, quality.output_hash])` (dep-tuple order — decision 7). Running `ocr` still
auto-enqueues the whole chain (`normalize → ast → {structure, ontology, semantic} ; {ast,structure} →
quality`); `knowledge` additionally waits on `semantic`, `ontology`, and `quality` before it can run,
which the existing `run_stage`/`stale`/`plan` machinery already handles generically over an N-ary
`deps` tuple.

## 4. Error handling

- Missing/non-list `semantic["sections"]` → `section_count = 0`, `section_types = {}` — a
  semantic-less document (e.g. `{}`) yields the same zeros as an explicit `{"sections": []}`.
- A `sections` entry that isn't a `Mapping` → skipped entirely, does not increment `section_count`
  (only a `Mapping` "is a section" for counting purposes).
- A section `Mapping` whose `type` is missing/`None`/non-`str` → `section_count` still increments
  (decision 5) but no `section_types` bucket is added/incremented for it.
- Missing/non-list `ontology["mappings"]` → `concepts = []`, `concept_count = 0`.
- A `mappings` entry that isn't a `Mapping`, or whose `concept` is missing/`None`/non-`str` →
  contributes nothing to `concepts` (skipped, not a partial credit).
- A `mappings` entry whose `concept == "UNKNOWN"` → excluded from `concepts` even though
  `map_document` (SP-11) is not expected to ever emit one — the defensive re-check (decision 4) fires
  regardless of whether the producer's own invariant currently holds.
- Two or more mappings resolving to the same non-`UNKNOWN` concept string → collapse to one entry in
  `concepts` (decision 4's dedup); `concept_count` counts the deduplicated set, not the raw mapping
  count.
- Missing/malformed `quality` (absent `ok`/`error_count`/`warning_count`, or present but wrong type:
  a non-`bool` `ok`, a non-`int` count) → each field independently degrades to its zero value
  (`False`/`0`/`0` respectively) — decision 6; never coerced (e.g. `"1"` does NOT become `1`, `1` does
  NOT become `True` even though `bool` is an `int` subtype in Python — the `isinstance(ok, bool)`
  check is evaluated before any `int`-shaped fallback, so a literal `1` for `ok` degrades to `False`,
  not `True`).
- No raise anywhere in `consolidate` — like `build_provenance`/`build_graph`/`map_document`, any
  malformed/partial input produces a smaller/zeroed record, never an exception. `consolidate({}, {},
  {})` is a fully defined, tested output (Done Criteria 1), not an edge case left to chance.
- Running `knowledge` before `semantic`, `ontology`, and/or `quality` has a recorded artifact for the
  document → `ctx.read(...)` (via `latest_run`) asserts the upstream run exists; absent, it fails
  loudly — the same DAG-ordering guarantee/failure mode every other derived stage in this codebase
  already has (SP-12 §4, SP-17b §4).

## 5. Testing

- **Unit** (no Docker), `backend/tests/unit/test_knowledge.py`:
  - A semantic+ontology+quality triple with 3 sections (2 sharing a `type`) → `section_count == 3`,
    `section_types` correctly bucketed (proves counting-by-type, not just presence).
  - The same triple's `ontology["mappings"]` containing a duplicate concept (two different labels
    mapping to `REVENUE`) → `concepts` is the sorted, DEDUPLICATED list; `UNKNOWN` entries (if any
    slip through) excluded — proves both decision 4 halves.
  - The triple's `quality` echoed into the record's `quality` key exactly (`ok`, `error_count`,
    `warning_count` only — not the full `issues` list).
  - `schema_version == 1` in the output regardless of input.
  - `consolidate({}, {}, {})` → the full zeroed/emptied record (Done Criteria 1's canonical example),
    proving the empty-input contract as one exact-equality assertion, not scattered field checks.
  - `consolidate` called with each artifact present but missing its interesting key (`{"schema_version":
    1}` for all three) → the same zeroed/emptied record — proves "present but empty" and "absent" both
    degrade identically.
  - Malformed `semantic["sections"]` entries (a non-`Mapping` string entry dropped entirely; a
    `Mapping` with a non-`str` `type` counted toward `section_count` but not `section_types`) → both
    behaviors verified in one test (proves decision 5's per-field granularity, not just "doesn't
    crash").
  - Malformed `ontology["mappings"]` entries (non-`Mapping`, missing `concept`, non-`str` `concept`,
    explicit `"UNKNOWN"` `concept`) → all degrade to "contributes nothing", never raises.
  - Malformed `quality` (non-`bool` `ok`, non-`int` `error_count`, `None` `warning_count`) → each
    field independently degrades to its zero value, never coerced.
- **Integration** (testcontainers, fake OCR, fake semantic client), `backend/tests/integration/
  test_knowledge_stage.py`: run `ocr → normalize → ast → structure → semantic → ontology → quality →
  knowledge` via `run_stage` in a **LOCAL** `StageRegistry` (register `OcrStage` + `NormalizeStage` +
  `AstStage` + `StructureStage` + `SemanticStage` + `OntologyStage` + `QualityStage` +
  `KnowledgeStage`, not the global `REGISTRY`) with a `FakeOcrClient` (two headed sections, one
  paragraph mapping to a `CanonicalConcept`, zero tables) and a `FakeSemanticClient`; assert the
  `knowledge` artifact matches the full expected consolidated record (section counts, concepts,
  clean `quality`); assert **all three** of `"knowledge" in resolver.successors("semantic")`,
  `"knowledge" in resolver.successors("ontology")`, and `"knowledge" in resolver.successors("quality")`
  (membership, per the SP-6/SP-8/SP-9/SP-11/SP-12/SP-17b "search-and-fix" precedent — registering
  `KnowledgeStage` globally via `stages/__init__.py` also adds `"knowledge"` to the GLOBAL
  `successors("semantic")`/`successors("ontology")`/`successors("quality")`; any test asserting an
  exact list against the global registry must become a membership check — grep and fix before
  landing, none expected since every prior successor-adding stage already left the suite
  membership-only). Read the `knowledge` key via `artifact_key(doc.id, "knowledge", 1,
  derived_input_hash([facts["semantic"].output_hash, facts["ontology"].output_hash,
  facts["quality"].output_hash]))` — dep-tuple order, decision 7.

## 6. Explicitly deferred (YAGNI)

- **Including the graph, provenance, and embedding artifacts in the knowledge record** — real,
  useful, but a different kind of information (structural/geometric/vector, not "what does this mean")
  than the semantic/ontology/quality triple this slice folds together; add each as its value to a
  concrete consumer becomes clear (decision 1).
- **Numeric roll-ups** (total revenue, sum of line items, balance-sheet reconciliation) — blocked on
  real numeric-value extraction, which SP-11 §6 already defers; `consolidate` has no numbers to roll
  up because `ontology["mappings"]` carries labels/concepts, not extracted values.
- **Persisting the record to an external knowledge store** (a queryable index, a graph database node,
  a search-engine document) — the `knowledge` artifact is, like every other stage output, already
  durably stored via the existing content-addressed artifact store; a SEPARATE store/index for
  cross-document querying is SP-16's concern, not a gap in SP-15.
- **The cross-document layer** (entity resolution across filings, version linking, "same company's
  Revenue across three years") — entirely SP-16's job; this per-document `knowledge` record is
  explicitly SP-16's per-document input, not a partial implementation of it.
- **Re-validating `section_types`' keys against `SectionType`, or `concepts`' entries against
  `CanonicalConcept`** — decision 3 deliberately keeps `fdi.core.knowledge` decoupled from those
  enums; introducing the coupling later (e.g. to reject a corrupt `type` string outright rather than
  just not bucketing it) is a real but separate hardening step, not attempted here.

## 7. Done criteria

1. `consolidate(semantic, ontology, quality)` returns `{schema_version, section_count, section_types,
   concepts, concept_count, quality}` per the algorithm in §3.2: `section_count`/`section_types`
   correctly bucket `semantic["sections"]` by `type`; `concepts` is the sorted, unique, non-`UNKNOWN`
   set of `ontology["mappings"][].concept`; `quality` echoes `{ok, error_count, warning_count}`; all
   §4 error-handling cases correct (per-field degrade, never per-artifact); `consolidate({}, {}, {})`
   == the fully zeroed/emptied record; never raises.
2. `KnowledgeStage` (`deps=("semantic", "ontology", "quality")`, `version=1`) reads all three
   artifacts and produces the knowledge artifact via `consolidate`; a rerun is a cache hit; its
   `input_hash` derives from `derived_input_hash([semantic.output_hash, ontology.output_hash,
   quality.output_hash])` in that order.
3. `DagResolver(REGISTRY.all()).successors("semantic")`, `.successors("ontology")`, AND
   `.successors("quality")` all include `"knowledge"` — the codebase's first stage to declare THREE
   dependencies, exercising SP-2's already-generic N-ary dep machinery (re-confirmed directly against
   `run.py`/`resolver.py` source for this spec, not merely cited) with no framework change.
4. All four static gates green (`ruff`, `mypy --strict`, `lint-imports` 1 kept/0 broken, full suite);
   `core` stays pure (`fdi/core/knowledge.py` has no I/O, and does not import
   `fdi.core.semantic`/`fdi.core.ontology`/`fdi.core.quality` per decision 3); no test asserts an
   exact-list `successors("semantic")`/`successors("ontology")`/`successors("quality")` result that
   this addition would break (confirmed by audit, not assumed); graph/provenance/embedding inclusion,
   numeric roll-ups, external persistence, and the cross-document layer remain explicitly out of
   scope (§6) — the record is SP-16's per-document input, noted as the next handoff in the roadmap.
