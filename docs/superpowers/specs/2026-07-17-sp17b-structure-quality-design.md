# SP-17b — Structure-aware Quality (SP-17 follow-on)

- **Status:** Implemented 2026-07-17.
- **Depends on:** SP-17 (`fdi.core.quality` — `Issue`, `Validator`, `_issue`, `AST_VALIDATORS`,
  `validate`, and the already-written-but-unwired `tables_are_rectangular`; `QualityStage` v1,
  `deps=("ast",)`), SP-7c (the `structure` artifact `{schema_version, blocks, tables}` that
  `tables_are_rectangular` consumes), SP-2 (stage/DAG framework — confirmed multi-dep support:
  `run.py`'s `input_hash = derived_input_hash([facts[d].output_hash for d in stage.deps])` and
  `resolver.py`'s `successors(name)` both already iterate `stage.deps`/`s.deps` generically; no
  framework change is needed to give a stage two deps).
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 table — add an **SP-17b** row directly
  under SP-17, phase **enterprise**, weight **light** (this is a focused wiring task, not new
  research), currently ⬜. The roadmap's top status line currently lists "structure-aware quality
  (SP-17 follow-on)" in its **Next** bullet and §"What SP-17 shipped" names the exact follow-on
  requirement this spec satisfies — both get updated once SP-17b ships (plan Task 3).

## 1. Purpose

SP-17 shipped a quality-validation core and a `QualityStage` that validates the AST — but it
explicitly deferred one thing (spec §2.3/§6): `tables_are_rectangular`, a structure-artifact
validator, was written and unit-tested but never wired into the stage, because doing so needed
`deps=("ast", "structure")`, a second `ctx.read`, and — the real design question — **a decision on
how two artifacts' issue lists merge into one report**. SP-17b is that follow-on, named verbatim in
SP-17's own handoff note. It does exactly one thing: **wire the already-written
`tables_are_rectangular` structure validator into `QualityStage`, producing ONE quality report
spanning AST-level and structure-level defects.**

No new validator logic, no new artifact shape, no new severity — `tables_are_rectangular` keeps its
existing `warning` severity (a torn table warns, it doesn't fail `ok`). The only new surface is the
aggregation: a document's quality report today only ever saw the AST; after SP-17b it sees the AST
**and** the structure artifact's tables, merged into the same `{schema_version, issues, error_count,
warning_count, ok}` shape SP-17 already established. Consumers of the `quality` artifact key need no
changes — they read a richer report through the exact same path.

## 2. Scope decisions (made autonomously; documented for review)

1. **The merge point is a new pure function, `validate_all`, not a change to `validate`'s
   signature.** `validate(artifact, validators)` is SP-17's established single-artifact contract and
   several call sites (today: `QualityStage` itself; conceptually: any future ad-hoc single-artifact
   check) may still want it. `validate_all(pairs: Sequence[tuple[Mapping[str, object],
   Sequence[Validator]]]) -> dict[str, object]` generalizes it to *N* artifacts, each with its own
   validator tuple. **`validate` is now expressed in terms of `validate_all`** (`validate(artifact,
   validators) = validate_all([(artifact, validators)])`) rather than duplicating the
   issue-flattening/counting/raise logic in two places — one aggregation body, two call shapes. This
   keeps the single-pair case byte-identical to SP-17's `validate` (same tests still pass unchanged)
   while adding the multi-artifact case `QualityStage` needs.
2. **Ordering is deterministic and pair-order-then-validator-order:** `validate_all` flattens issues
   pair-by-pair, in the order pairs are given, and within a pair in validator-tuple order (exactly
   `validate`'s existing per-validator flattening, just repeated across pairs). `QualityStage` calls
   `validate_all([(ast, AST_VALIDATORS), (structure, STRUCTURE_VALIDATORS)])` — AST issues always
   precede structure issues in the merged report. This is a documented, tested contract, not an
   incidental side effect of dict/list iteration.
3. **`QualityStage.deps = ("ast", "structure")`, in that order — and the order is now part of the
   artifact's cache identity, not just report ordering.** SP-2's `run.py` computes `input_hash =
   derived_input_hash([facts[d].output_hash for d in stage.deps])`: a plain ordered hash of the deps'
   output hashes in `deps` tuple order. Putting `ast` first (matching SP-17's existing single dep and
   the merge order in decision 2) means the `quality` input hash is
   `derived_input_hash([ast.output_hash, structure.output_hash])`. Reordering the tuple later would
   silently change every document's `quality` artifact key — so `("ast", "structure")` is a
   deliberate, fixed choice, not an arbitrary one.
4. **`QualityStage.version` bumps 1 → 2.** The stage's *behavior* changed (it now reads a second
   artifact and runs a second validator family) even though its output *shape* did not — SP-2's
   convention (see `StructureStage` 1→2 when it grew cross-page stitching, `OcrStage` bumps for
   enrichment) is that a stage's `version` tracks its own logic changing, independent of whether
   `deps` also changed. The version bump means every document gets a fresh `quality` run (new
   `artifact_key`) rather than a stale v1 report being mistaken for a v2 one.
5. **`STRUCTURE_VALIDATORS = (tables_are_rectangular,)` is added to `fdi.core.quality`,
   parallel to `AST_VALIDATORS`.** A one-element tuple today; the natural home for any future
   structure-shape check the same way `AST_VALIDATORS` is the home for AST-shape checks. No registry,
   no dynamic dispatch — same "a validator is a plain callable in a tuple" philosophy SP-17 set.
6. **Severity policy is unchanged.** `tables_are_rectangular` keeps emitting `warning` — a torn table
   is "worth a look", not "broken". `validate_all`'s `ok = error_count == 0` composes correctly with
   this: a document with a torn table but a sound AST is still `ok: true`, exactly as SP-17 intended
   for warnings generally (§2 of the SP-17 spec, decision 4). SP-17b does not revisit this policy —
   see §6 (deferred).
7. **Determinism (Global Constraint 6) holds.** `validate_all` is a pure function of its `pairs`
   argument; no clock, no RNG, no I/O. `QualityStage.run` still only reads two artifacts and returns a
   plain dict — the same shape of purity SP-17 established, now over two inputs instead of one.

## 3. Architecture

### 3.1 Layering (honours the CI-enforced `api | workers → pipeline → platform → core` contract)

- **`fdi.core.quality`** (extended, not replaced) — adds `STRUCTURE_VALIDATORS` and `validate_all`;
  `validate` becomes a thin wrapper over `validate_all`. Everything SP-17 put here (`Issue`,
  `Validator`, `_issue`, `ast_has_sections`, `sections_have_content`, `title_present`,
  `tables_are_rectangular`, `AST_VALIDATORS`) is untouched. Still pure; still no I/O.
- **`fdi.pipeline.stages.quality`** (extended) — `QualityStage.deps` grows to `("ast", "structure")`,
  `version` 1→2, `run` reads both artifacts and calls `validate_all`. Already self-registered into
  `REGISTRY`; `backend/src/fdi/pipeline/stages/__init__.py` already imports `quality` (and
  `structure`) for their registration side effects — no import-list change needed.

### 3.2 The quality report schema — unchanged shape, richer contents

```json
{
  "schema_version": 1,
  "issues": [
    {"severity": "error", "code": "ast.no_sections", "message": "AST has no sections."},
    {"severity": "warning", "code": "ast.missing_title", "message": "Document has no title."},
    {"severity": "warning", "code": "structure.torn_table",
     "message": "Table at index 0 has a cell referencing a column_index >= its column_count (2)."}
  ],
  "error_count": 1,
  "warning_count": 2,
  "ok": false
}
```
Same four top-level keys SP-17 shipped (`schema_version` inside the report body stays `1` — it
describes the *report's own shape*, which has not changed; `QualityStage.version` is the thing that
bumped, because that describes the *stage's* behavior). AST issues (`ast.*` codes) always precede
structure issues (`structure.*` codes) — decision 2.

### 3.3 `validate_all` — the new aggregation point

```python
def validate_all(
    pairs: Sequence[tuple[Mapping[str, object], Sequence[Validator]]],
) -> dict[str, object]:
    issues: list[Issue] = [
        issue
        for artifact, validators in pairs
        for validator in validators
        for issue in validator(artifact)
    ]
    for issue in issues:
        if issue["severity"] not in _KNOWN_SEVERITIES:
            raise ValueError(f"unknown issue severity: {issue['severity']!r}")
    error_count = sum(1 for issue in issues if issue["severity"] == "error")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    return {
        "schema_version": 1,
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
        "ok": error_count == 0,
    }


def validate(artifact: Mapping[str, object], validators: Sequence[Validator]) -> dict[str, object]:
    return validate_all([(artifact, validators)])
```
`validate_all([(artifact, validators)]) == validate(artifact, validators)` for any single pair — this
is a tested equivalence (Task 1), not an assumption. The union's `error_count`/`warning_count`/`ok`
are computed over *all* issues from *all* pairs, exactly as if every validator in every pair had been
handed the same flat list SP-17's `validate` already builds for one artifact.

### 3.4 `STRUCTURE_VALIDATORS`

```python
STRUCTURE_VALIDATORS: tuple[Validator, ...] = (tables_are_rectangular,)
```
Placed next to `AST_VALIDATORS` in `fdi.core.quality`. One entry today; the extension point for any
future structure-shape check (e.g. a later "every table has at least one header row" validator) is
"append to this tuple", exactly how `AST_VALIDATORS` already works.

### 3.5 `QualityStage`

```python
class QualityStage:
    name = "quality"
    version = 2
    deps: tuple[str, ...] = ("ast", "structure")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        return validate_all([(ast, AST_VALIDATORS), (structure, STRUCTURE_VALIDATORS)])
```
`deps=("ast", "structure")` makes the SP-2 resolver schedule `quality` as a successor of **both**
`ast` and `structure` (`resolver.successors("ast")` and `resolver.successors("structure")` both now
include `"quality"`), and hashes `quality`'s `input_hash` from `derived_input_hash([ast.output_hash,
structure.output_hash])` — the first two-dependency stage in the pipeline (confirmed: every stage
registered before this one — `NormalizeStage`, `StructureStage`, `AstStage`, `OntologyStage`,
`GraphStage`, `SemanticStage`, `LinkingStage`, SP-17's original `QualityStage` — has exactly one dep).
Running `ocr` still auto-enqueues the whole graph; `quality` now waits on two upstream artifacts
instead of one before it can run, which `run_stage`'s existing `input_hash` computation already
handles generically (§2.3 of this spec; no `run.py`/`resolver.py` change needed).

## 4. Error handling

- Same posture as SP-17: validators never raise on a merely-empty/absent key; `tables_are_rectangular`
  already tolerates a `structure` artifact with no `"tables"` key (`.get("tables", [])`) or an empty
  table list — a document with zero tables produces zero structure issues, not an error.
- `validate_all` raises `ValueError` on an issue with an unrecognised severity from **either** pair —
  the check is over the fully-flattened `issues` list, so a bug in a structure validator is caught
  exactly as loudly as a bug in an AST validator (Task 1 tests both).
- `ctx.read("structure")` before a `structure` artifact has been recorded for the document → fails
  loudly, identically to the existing `ctx.read("ast")` upstream-missing behaviour (inherited from the
  stage framework, not re-implemented here); the DAG normally guarantees `structure` and `ast` both
  finish before `quality` runs.

## 5. Testing

- **Unit** (no Docker, `fdi.core.quality`): a clean-AST/clean-structure pair through `validate_all` →
  `{"issues": [], "error_count": 0, "warning_count": 0, "ok": True}`; a sparse-AST/torn-structure pair
  → merged report with AST issues preceding the `structure.torn_table` issue, correct union
  `error_count`/`warning_count`, `ok=False`; single-pair `validate_all` output equals `validate`'s
  output for the same artifact/validators (proves the wrapper is behavior-preserving); a pair whose
  validator returns an unrecognised severity → `validate_all` raises `ValueError` (mirrors SP-17's
  `validate` raise test, now exercised through the new entry point).
- **Integration** (testcontainers, fake OCR, LOCAL `StageRegistry` registering `OcrStage`,
  `NormalizeStage`, `AstStage`, `StructureStage`, `QualityStage`): run `ocr → normalize → ast →
  structure → quality` (order between `ast` and `structure` doesn't matter — neither depends on the
  other — but both must finish before `quality`); assert `"quality" in
  DagResolver(reg.all()).successors("ast")` **and** `"quality" in
  DagResolver(reg.all()).successors("structure")`; a clean document (title + heading + paragraph, zero
  tables) → `ok=True`, no issues; a sparse document (empty AST) with a TORN table (a cell whose
  `column_index >= column_count`) → the merged report's `issues` contains **both** `ast.no_sections`
  (proving the AST validators still ran) **and** `structure.torn_table` (proving the structure
  validator is now wired in), `error_count == 1` (only the AST error), `warning_count == 2`
  (`ast.missing_title` + `structure.torn_table`), `ok == False`. Read the `quality` artifact via
  `artifact_key(doc_id, "quality", 2, derived_input_hash([facts["ast"].output_hash,
  facts["structure"].output_hash]))` — version `2`, two-hash input, per decisions 3-4.

## 6. Explicitly deferred (YAGNI — unchanged from SP-17 §6 except item 1, which this spec resolves)

- **Numeric-total reconciliation** (a balance sheet balances; sub-totals sum to their stated total) —
  still needs SP-11's ontology mapping (to know which numbers *are* totals/sub-totals) and real numeric
  extraction; SP-17b adds no numbers to check, only wires an existing shape check.
- **Cross-document consistency** (the same entity/figure agrees across filings/years) — still SP-16's
  concern; needs entity resolution and version linking that don't exist yet.
- **Severity policy changes** (e.g. promoting `structure.torn_table` to an `error`, or introducing an
  `info` bucket) — not evaluated here; SP-17b's job is wiring, not re-litigating severities SP-17
  already decided. Revisit only when real documents show the current split (torn table = warning) is
  wrong in practice.
- **Provenance-linked issues** (pointing an issue at the exact page/bounding-region that produced it)
  — needs SP-12's provenance graph, which doesn't exist yet; today's `Issue.message` is a human-
  readable index-based description (`"Table at index {index} has a cell..."`), not a geometric pointer.
- **A UI surface for the report** — still later; the report is a normal artifact, readable via the
  existing artifact-store path.

## 7. Done criteria

1. `validate_all(pairs)` returns `{schema_version, issues, error_count, warning_count, ok}` with
   issues flattened pair-by-pair (pair order, then validator order within a pair), `ok = error_count ==
   0`; raises `ValueError` on any pair's issue with an unrecognised severity; `validate(artifact,
   validators) == validate_all([(artifact, validators)])` for every existing SP-17 `validate` test case
   (no regression).
2. `STRUCTURE_VALIDATORS = (tables_are_rectangular,)` exists in `fdi.core.quality`.
3. `QualityStage` has `deps=("ast", "structure")`, `version=2`; `run` reads both artifacts and returns
   `validate_all([(ast, AST_VALIDATORS), (structure, STRUCTURE_VALIDATORS)])`.
4. `DagResolver(REGISTRY.all()).successors("ast")` **and** `.successors("structure")` both include
   `"quality"`; a fresh `quality` artifact key uses `version=2` and
   `derived_input_hash([ast.output_hash, structure.output_hash])`.
5. Integration: a torn-table document's merged report contains both an `ast.*` issue and
   `structure.torn_table` in one `quality` artifact (proves the merge, not just independent wiring).
6. All four static gates green (`ruff`, `mypy --strict`, `PYTHONUTF8=1 lint-imports` — `1 kept, 0
   broken`); `core` stays pure; full suite passes; no existing test asserts an exact-list
   `successors(...)` result that this change would break (confirmed by audit, not assumed).
