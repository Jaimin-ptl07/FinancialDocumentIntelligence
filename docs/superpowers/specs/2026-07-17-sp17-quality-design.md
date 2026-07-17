# SP-17 — Quality Validation

- **Status:** Implemented 2026-07-17 (`fdi.core.quality` + `QualityStage`, `feature/sp-17-quality`).
- **Depends on:** SP-2 (stage/DAG framework), SP-8 (the AST artifact, `fdi.core.ast.build_ast` →
  `{schema_version, title, sections:[{heading, paragraphs:[str]}]}`). References but does not depend
  on SP-7's structure artifact (`{schema_version, blocks:[{role,text}], tables:[...]}`) — see §2.3.
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 table — SP-17 "Quality validation framework",
  phase **enterprise**, weight **medium**, currently ⬜. Phase-2 build order lists it alongside SP-15
  store / SP-16 cross-doc as the last trio, all measured/consumed after the heavy layers exist.

## 1. Purpose

Every stage in the pipeline (OCR → normalize → structure → AST → …) can silently produce a
*structurally broken* artifact without ever raising: an AST with zero sections because normalization
found nothing, a section with a heading swallowed by a role bug, a table whose cell grid was torn by a
bad stitch. Nothing today looks back at a produced artifact and asks "is this internally sound?" SP-17
is that look-back: a **quality validation framework** — pure validators that inspect a document's own
artifacts for internal defects and emit a single, deterministic **quality report** artifact.

Thin vertical slice, pure-core-first (the same shape as SP-6/SP-8): a handful of pure validator
functions + a `validate` aggregator in `fdi.core.quality`, and a `QualityStage` (`deps=("ast",)`) that
runs them over the AST and returns the report. No numeric reconciliation, no cross-document checks, no
UI — those need artifacts and ontology this project hasn't built yet (§6).

### 1.1 Relationship to SP-3 (eval harness) — related but distinct

SP-3 built the machinery to answer *"which algorithm candidate is more accurate?"* — it runs a
candidate over a **ground-truth dataset** and scores `predicted` against a human-authored `expected`.
It is comparative and needs external truth.

SP-17 answers a different question: *"is the artifact this document actually produced internally
consistent?"* — it inspects **one document's own output**, with no ground truth and no candidate
comparison. An AST with no sections is a defect regardless of what any ground-truth dataset says. The
two are complementary, not overlapping: SP-3 picks the best algorithm before it ships; SP-17 watches
what that algorithm produces, per document, forever after. Their `Metric`/`Validator` protocols are
deliberately similar in spirit (pure, deterministic, `fdi.core`) but serve different call sites — a
`Metric` never appears in a pipeline stage; a `Validator` never appears in an eval run.

## 2. Scope decisions (made autonomously; documented for review)

1. **Issues are data, not exceptions.** A validator inspects an artifact and returns a list of `Issue`
   dicts (`{severity, code, message}`) — it never raises for a merely-suspicious artifact (only a
   genuinely malformed input, e.g. `validate` receiving an issue with an unrecognised severity, is a
   raise — see §4). This mirrors SP-3's `Metric` contract (pure, `[0,1]`, deterministic) applied to
   defect-finding instead of scoring.
2. **A `Validator` is a plain callable, not a class hierarchy.** `Validator = Callable[[Mapping[str,
   object]], list[Issue]]`. Adding a new check is adding a function and appending it to a tuple —
   no registry, no plugin system, no YAGNI machinery for a handful of checks.
3. **The thin slice validates the AST only.** `QualityStage` has `deps=("ast",)` and runs the AST
   validators (`ast_has_sections`, `sections_have_content`, `title_present`). `tables_are_rectangular`
   is written and unit-tested in `fdi.core.quality` now (its contract is simple and stable — it needs
   only `column_count` and each cell's `column_index`, both already on SP-7's structure artifact), but
   it is **not wired into `QualityStage`** in this slice, because doing so would require
   `deps=("ast", "structure")`, a second `ctx.read`, and a decision about how AST-level and
   structure-level issues combine in one report — real design work belonging to the follow-on stage,
   not smuggled in as a side effect of "well, the function already exists." The function is ready;
   the day `QualityStage` grows a `structure` dep is a one-line stage change plus a new set of
   integration-test assertions.
4. **`validate` is the single aggregation point.** Every validator's issues flow through one function
   that computes `error_count`, `warning_count`, and `ok` (`ok = error_count == 0` — warnings never
   gate `ok`; a warning is "worth a look", not "broken"). Consumers never hand-count issues themselves.
5. **The quality report is itself a plain artifact**, `{schema_version, issues, error_count,
   warning_count, ok}`, stored/hashed exactly like every other stage output (SP-2 content-addressing).
   It is not written back into the AST or mutated in place — validation is read-only over its input.
6. **Determinism (Global Constraint 6) holds.** `validate` and every validator are pure functions of
   their artifact argument; given the same AST, the same report comes out byte-identical. No clock, no
   RNG, no I/O in `fdi.core.quality`.

## 3. Architecture

### 3.1 Layering (honours the CI-enforced `api | workers → pipeline → platform → core` contract)

- **`fdi.core.quality`** — `Issue` (a `dict[str, object]` shape, built by an `_issue` helper — the
  project does not use `TypedDict`/`dataclass` for artifact-body shapes; `AstStage`/`StructureStage`
  return plain dicts too, so `Issue` follows that precedent rather than introducing a new one),
  `Validator` (`Callable[[Mapping[str, object]], list[Issue]]`), the validator functions, the
  `AST_VALIDATORS` tuple, and `validate(artifact, validators) -> dict[str, object]`. Pure; no I/O.
- **`fdi.pipeline.stages.quality`** — `QualityStage`, self-registered into `REGISTRY` (same pattern as
  `AstStage`/`NormalizeStage`/`StructureStage`).

### 3.2 The quality report schema

```json
{
  "schema_version": 1,
  "issues": [
    {"severity": "error", "code": "ast.no_sections", "message": "AST has no sections."},
    {"severity": "warning", "code": "ast.missing_title", "message": "Document has no title."}
  ],
  "error_count": 1,
  "warning_count": 1,
  "ok": false
}
```
`ok` is `error_count == 0`. An artifact with only warnings is still `ok: true` — warnings surface for
review, they do not fail the document.

### 3.3 Validators

| function | operates over | severity | fires when |
|---|---|---|---|
| `ast_has_sections` | AST | `error` | `ast["sections"]` is empty |
| `sections_have_content` | AST | `warning` (per offending section) | a section has `heading is None` **and** an empty `paragraphs` list — an entirely empty section |
| `title_present` | AST | `warning` | `ast["title"] is None` |
| `tables_are_rectangular` | structure (not yet wired, §2.3) | `warning` (per offending table) | any cell's `column_index >= column_count` for that table — a torn grid |

All four take a `Mapping[str, object]` and return `list[Issue]`; none raise on a merely-empty/absent
key (they use `.get(..., default)` + `isinstance`/`cast`, the same defensive style `build_ast` already
uses for a missing `"blocks"` key) — a validator's job is to describe defects, not to add new ways for
the pipeline to crash on data upstream code already tolerated.

`AST_VALIDATORS: tuple[Validator, ...] = (ast_has_sections, sections_have_content, title_present)` is
the tuple `QualityStage` runs. `tables_are_rectangular` is exported from the module and fully tested,
but deliberately not in this tuple (§2.3).

### 3.4 `validate`

```python
def validate(artifact: Mapping[str, object], validators: Sequence[Validator]) -> dict[str, object]:
    issues: list[Issue] = [issue for validator in validators for issue in validator(artifact)]
    for issue in issues:
        if issue["severity"] not in {"error", "warning"}:
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
```
Composable over *any* artifact + validator list — `QualityStage` calls `validate(ast, AST_VALIDATORS)`,
and the follow-on structure-aware stage will call it a second time (or with a combined validator tuple)
once it has a `structure` dep.

### 3.5 `QualityStage`

```python
class QualityStage:
    name = "quality"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        return validate(ast, AST_VALIDATORS)
```
Reads no external service; `deps=("ast",)` makes the SP-2 resolver schedule `quality` as a successor of
`ast` and hash its `input_hash` from the AST's `output_hash`, exactly as `AstStage` chains off
`normalize` and `NormalizeStage` chains off `ocr`. Running `ocr` therefore auto-enqueues the whole
chain: `ocr → normalize → ast → quality`.

## 4. Error handling

- An AST missing keys the validators expect (`sections`, `title`) → validators treat absence as the
  "worst" structural signal they already model (no sections → the `ast_has_sections` error; no title →
  the `title_present` warning), rather than raising — an oddly-shaped AST is exactly the kind of defect
  this framework exists to surface, not crash on.
- `validate` receiving an `Issue` whose `severity` is not `"error"`/`"warning"` → raises `ValueError`
  (mirrors SP-3's "a metric returning outside `[0,1]` raises" — a validator bug producing a nonsense
  severity must be loud, not silently miscounted into neither bucket).
- `ctx.read("ast")` before an `ast` artifact has been recorded for the document → fails loudly (same
  upstream-missing behaviour as `NormalizeStage`/`AstStage`; the DAG normally guarantees order, a manual
  out-of-order dispatch does not get a silent empty report).

## 5. Testing

- **Unit** (no Docker, `fdi.core.quality`): `ast_has_sections` — empty sections → one `error`; non-empty
  → `[]`. `sections_have_content` — a section with no heading and no paragraphs → one `warning`; a
  section with a heading but no paragraphs → **no** warning (heading alone is not "empty"); a section
  with paragraphs but no heading (a valid preamble section, per SP-8) → no warning. `title_present` —
  `None` → warning; a real title → `[]`. `tables_are_rectangular` — a cell with `column_index >=
  column_count` → warning; a well-formed table → `[]`; multiple tables, only the torn one flagged.
  `validate` — a clean AST run through `AST_VALIDATORS` → `{"issues": [], "error_count": 0,
  "warning_count": 0, "ok": True}`; an AST with empty sections → `ok=False`, `error_count=1`; an AST
  missing only a title → `ok=True` (warnings don't gate `ok`), `warning_count=1`; a validator returning
  an issue with severity `"critical"` → `validate` raises `ValueError`.
- **Integration** (testcontainers, fake OCR, LOCAL `StageRegistry`): register `OcrStage`, `NormalizeStage`,
  `AstStage`, `QualityStage`; assert `"quality" in DagResolver(reg.all()).successors("ast")`; run
  `ocr → normalize → ast → quality` via `run_stage`; read the `quality` artifact by its
  `derived_input_hash([facts["ast"].output_hash])` key and assert the report matches what
  `validate(known_ast, AST_VALIDATORS)` would compute by hand for that fixture (e.g. a title + one
  heading + one non-empty paragraph → `ok=True`, no issues).

## 6. Explicitly deferred (YAGNI)

- **Numeric-total reconciliation** (a balance sheet balances; sub-totals sum to their stated total) —
  needs SP-11's ontology mapping (to know which numbers *are* totals/sub-totals) and real numeric
  extraction; SP-17 today has no numbers to check, only text/structure shape.
- **Cross-document consistency** (the same entity/figure agrees across filings/years) — SP-16's
  concern; needs entity resolution and version linking that don't exist yet.
- **Structure-artifact validators wired into a stage** (`tables_are_rectangular` and any future
  structure-shape checks) — the function exists and is tested now (§2.3); wiring it needs a
  `deps=("ast", "structure")` stage and a decision on how two artifacts' issues merge into one report.
- **A UI surface for the report** — later; the report is a normal artifact, readable via the existing
  artifact-store path, same as every other stage output.
- **Severity beyond `error`/`warning`** (e.g. `info`) — add when a real validator needs a third bucket;
  today's checks sort cleanly into "broken" vs. "worth a look."

## 7. Done criteria

1. `Issue`/`Validator` shapes; `ast_has_sections`, `sections_have_content`, `title_present`,
   `tables_are_rectangular` are pure, each matching the table in §3.3, and none raises on an
   absent/empty key.
2. `validate(artifact, validators)` returns `{schema_version, issues, error_count, warning_count, ok}`
   with `ok = error_count == 0`; raises `ValueError` on an issue with an unrecognised severity.
3. `QualityStage` (`deps=("ast",)`) reads the AST artifact and returns `validate(ast, AST_VALIDATORS)`;
   a rerun is a cache hit; its `input_hash` derives from the AST's `output_hash`.
4. `DagResolver(REGISTRY.all()).successors("ast")` includes `"quality"` (the chain `ocr → normalize →
   ast → quality` is automatic from a single `run_stage("ocr", ...)`).
5. All four static gates green (`ruff`, `mypy --strict`, `PYTHONUTF8=1 lint-imports` — 1 kept, 0
   broken); `core` stays pure; full suite passes.
