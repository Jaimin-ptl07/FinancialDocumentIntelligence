# FOG Phase 5 — Precision and Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise mapping precision by refusing physically impossible mappings, close the cache-key hole that serves stale mappings forever, and wire the validation/confidence/coverage engines that `fog_reason` currently stubs out.

**Architecture:** No new stages. Every change is inside `fog_objects` (quantity kind), `fog_mapping` (kind-aware refusal, cache key), `fog_reason` (validation wiring), or the `Stage` protocol (one optional hook). The FOG's node and edge vocabulary is unchanged.

**Tech Stack:** Python 3.12 · FastAPI · Celery · MinIO · pytest (`asyncio_mode = "auto"`) · `uv` · ruff + mypy.

**Spec:** [`docs/superpowers/specs/2026-08-03-financial-object-graph-design.md`](../specs/2026-08-03-financial-object-graph-design.md)
**Predecessor:** [`2026-08-04-fog-phase-3-4-mapping-and-reasoning.md`](2026-08-04-fog-phase-3-4-mapping-and-reasoning.md)

## Measured starting point (2026-08-05, free tiers only, 0 LLM calls)

| | recall | precision |
|---|---|---|
| TCS consolidated P&L | 72.7% | 100% |
| TCS standalone P&L | 72.7% | 100% |
| TCS liabilities | 50.0% | 100% |
| TCS assets | 28.6% | 100% |
| REMC P&L | 55.6% | 83.3% |
| RIL P&L | 50.0% | 75.0% |
| TCS equity | 0.0% | 0.0% |

**37 of 64 contested TCS tags have `spread` > 1,000**, and the largest (`EQWVR`, `OPBAL`) are ~3.6 **billion** — the weighted-average share count `361,80,87,518` competing with rupee amounts for the same tag. That is Task 1.

## Global Constraints

- **`fdi.core` is pure and TOTAL.** No I/O, no config, no clock/RNG, **never raises on any input shape**. The only sanctioned raise in the FOG is `fog.edge_body` on an unknown edge type. `fdi.core` may not import `fdi.pipeline`/`platform`/`api`/`workers`.
- **Decision 10 — never raise.** Every I/O step in a pipeline module is individually guarded; one unit's failure degrades that unit, never the document. Cost/usage recording is guarded **separately** from the model call (measured scar: a failed usage write poisoned a shared DB session and silently killed retrieval for every later unit, TCS 125 → 71 fields filled).
- **`absent ≠ missed` is a data-structure property.** Anything dropped keeps a recorded reason. Never widen a diagnostic's claim beyond what it can actually distinguish.
- **Determinism.** Everything reaching an artifact body is explicitly sorted; bodies are content-addressed.
- **Any artifact body shape change requires bumping BOTH that artifact's `schema_version` AND the emitting `Stage.version`.** Current versions: `statement_index` v4, `note_index` v3, `fog_objects` v4, `fog_mapping` v3, `fog_reason` v3.
- **The model never parses a number.** Values come from `fdi.core.numbers.parse_amount`; the mapper assigns concepts only.
- **Test runner:** `uv run --directory backend pytest -m "not integration and not e2e" -q` — currently **1121 passed, 11 skipped**.
- **Gates before every commit:** `ruff check .` (100-char lines), `mypy src`, `lint-imports` — all clean.
- **CRLF:** most files are CRLF. `Edit` silently matches nothing if you write `\n` against `\r\n`. When scripting an edit, read bytes and detect the newline first — this has bitten twice.
- **Transcribe, do not rewrite.** Where this plan gives code, copy it exactly.

---

## File Structure

| File | Change |
|---|---|
| `backend/src/fdi/core/quantity.py` | **New.** Classify a line item's quantity kind from its label and magnitude. Pure. |
| `backend/src/fdi/core/fog.py` | Add `kind` to `LineItem` + its body |
| `backend/src/fdi/core/fog_build.py` | Populate `kind` when building face and note items |
| `backend/src/fdi/core/fog_mapping.py` | Refuse a kind-mismatched mapping; raise the lexical acceptance floor |
| `backend/src/fdi/pipeline/stage.py` | Add the optional `key_material` hook to the `Stage` protocol |
| `backend/src/fdi/pipeline/run.py` | Fold `key_material` into `input_hash` |
| `backend/src/fdi/pipeline/stages/fog_mapping.py` | Implement `key_material` from the schema; minors cleanup |
| `backend/src/fdi/core/fog_reason.py` | Wire `validation` / `confidence` / `coverage` |

---

## Task 1: Quantity kind — a share count is not a rupee amount

**Files:**
- Create: `backend/src/fdi/core/quantity.py`
- Test: `backend/tests/unit/test_quantity.py`

**Interfaces:**
- Consumes: nothing (leaf module)
- Produces: `KINDS: frozenset[str]`; `classify_quantity(label: str, value: float | None) -> str` returning one of `"currency" | "count" | "per_share" | "ratio" | "unknown"`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_quantity.py`:

```python
"""A share COUNT is not a rupee AMOUNT, and mapping one onto the other is how
EQWVR came back as 260 with a contested spread of 3.6 BILLION -- the weighted
average share count 361,80,87,518 competing with share capital 362."""

from fdi.core.quantity import KINDS, classify_quantity


def test_a_share_count_is_a_count():
    assert classify_quantity("Weighted average number of equity shares", 3618087518.0) == "count"
    assert classify_quantity("Number of shares outstanding", 1000000.0) == "count"


def test_earnings_per_share_is_per_share_not_currency():
    """EPS is rupees, but rupees PER SHARE -- it must never satisfy a concept
    expecting a total."""
    assert classify_quantity("Earnings per equity share - Basic and diluted", 136.01) == "per_share"
    assert classify_quantity("Basic (in Rs)", 19.59) == "per_share"


def test_a_percentage_or_ratio_is_a_ratio():
    assert classify_quantity("Effective tax rate (%)", 24.5) == "ratio"
    assert classify_quantity("Current ratio", 1.8) == "ratio"


def test_an_ordinary_statement_line_is_currency():
    assert classify_quantity("Revenue from operations", 267021.0) == "currency"
    assert classify_quantity("Total equity", 108478.0) == "currency"


def test_the_label_decides_before_the_magnitude():
    """A large count and a large amount are both large; only the label separates
    them, so magnitude must never override an explicit label signal."""
    assert classify_quantity("Number of equity shares", 12.0) == "count"


def test_a_face_value_per_share_is_per_share():
    assert classify_quantity("Face value per share", 10.0) == "per_share"


def test_shares_alone_does_not_make_a_count():
    """'Sale of shares' is a rupee amount. Only counting language does."""
    assert classify_quantity("Sale of equity shares", 500.0) == "currency"


def test_an_absent_value_still_classifies_on_its_label():
    assert classify_quantity("Number of equity shares", None) == "count"


def test_an_unlabelled_row_is_unknown_not_currency():
    """Guessing currency would let anything satisfy a monetary concept."""
    assert classify_quantity("", 5.0) == "unknown"


def test_every_result_is_in_the_closed_vocabulary():
    for label in ("Revenue", "Number of shares", "EPS per share", "Tax rate (%)", ""):
        assert classify_quantity(label, 1.0) in KINDS


def test_malformed_input_never_raises():
    assert classify_quantity(None, None) == "unknown"  # type: ignore[arg-type]
    assert classify_quantity(123, "x") == "unknown"  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_quantity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.quantity'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/src/fdi/core/quantity.py`:

```python
"""What KIND of quantity a reported line carries.

WHY THIS IS NOT READ FROM THE TAXONOMY
--------------------------------------
Every one of v4.3's 453 typed fields declares `data_type: "decimal"`, so the
schema cannot distinguish a share count from a rupee amount. The distinction has
to be inferred from the line ITEM.

MEASURED: TCS's `EQWVR` (equity share capital, 362 crore) was resolved to the
weighted average share count 361,80,87,518, and `contested` reported a spread of
3.6 BILLION. Both claimants are perfectly valid numbers, so no amount of
arbitration tuning can separate them -- only knowing that one is a COUNT can.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

import re

KINDS = frozenset({"currency", "count", "per_share", "ratio", "unknown"})

# "number of ... shares", "no. of shares", "share count". Deliberately requires
# COUNTING language: "Sale of equity shares" is a rupee amount, not a count.
_COUNT = re.compile(
    r"\b(number|numbers|no\.?|count)\s+of\b|\bshare\s+count\b|\bweighted\s+average\s+number\b",
    re.IGNORECASE,
)
_PER_SHARE = re.compile(
    r"\bper\s+(equity\s+)?share\b|\bper\s+unit\b|\bEPS\b|\bface\s+value\b",
    re.IGNORECASE,
)
_RATIO = re.compile(r"%|\bratio\b|\brate\b|\bpercentage\b|\byield\b", re.IGNORECASE)


def classify_quantity(label: str, value: float | None) -> str:
    """The kind of quantity `label` reports.

    The LABEL decides, never the magnitude: a large count and a large amount are
    both large, and letting size arbitrate would misclassify a genuinely big
    rupee figure. `value` is accepted for future refinement and deliberately
    unused today rather than used badly.

    An unlabelled row is "unknown", NOT "currency" -- defaulting to currency
    would let any stray number satisfy a monetary concept, which is the failure
    this function exists to prevent. Total: never raises."""
    if not isinstance(label, str) or not label.strip():
        return "unknown"
    if _COUNT.search(label):
        return "count"
    if _PER_SHARE.search(label):
        return "per_share"
    if _RATIO.search(label):
        return "ratio"
    return "currency"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_quantity.py -q`
Expected: PASS, 11 passed

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/quantity.py backend/tests/unit/test_quantity.py
git commit -m "feat(fog): classify a line's quantity kind — a share count is not an amount"
```

---

## Task 2: Carry `kind` on every LineItem

**Files:**
- Modify: `backend/src/fdi/core/fog.py`
- Modify: `backend/src/fdi/core/fog_build.py`
- Modify: `backend/src/fdi/pipeline/stages/fog_objects.py` (version bump)
- Test: `backend/tests/unit/test_fog_model.py`, `backend/tests/unit/test_fog_face.py`, `backend/tests/unit/test_fog_notes.py`

**Interfaces:**
- Consumes: Task 1's `classify_quantity`
- Produces: `LineItem.kind: str` (appended last, after `source_row`, so existing positional construction is unaffected); `line_item_body` emits `"kind"`

- [ ] **Step 1: Add the field**

In `backend/src/fdi/core/fog.py`, append `kind: str = "unknown"` to `LineItem` **after** `source_row`, and add `"kind": item.kind` to `line_item_body`'s returned mapping. A default keeps every existing construction site valid; `fog_build` sets it explicitly.

- [ ] **Step 2: Populate it**

In `backend/src/fdi/core/fog_build.py`, import `classify_quantity` and pass `kind=classify_quantity(row.label, row.current)` in `face_line_items`, and `kind=classify_quantity(label, current)` in `note_line_items`.

- [ ] **Step 3: Write the tests**

Append to `backend/tests/unit/test_fog_face.py`:

```python
def test_a_face_row_carries_its_quantity_kind():
    items, _ = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert revenue.kind == "currency"
```

Append to `backend/tests/unit/test_fog_notes.py`:

```python
def test_a_share_count_note_row_is_not_currency():
    """MEASURED: the weighted average share count competed with share capital
    for EQWVR and won, with a contested spread of 3.6 billion."""
    from fdi.core.fog_build import build_note_index, note_line_items

    table = {
        "bounding_regions": [{"page_number": 300}],
        "spans": [{"offset": 2150, "length": 60}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
            _cell(1, 0, "Weighted average number of equity shares"),
            _cell(1, 1, "3,61,80,87,518"),
        ],
    }
    head = {
        "content": "18. Earnings per share", "role": "sectionHeading",
        "bounding_regions": [{"page_number": 300}],
        "spans": [{"offset": 2000, "length": 22}],
    }
    ocr = {"paragraphs": [head], "tables": [table]}
    basis = {"default_basis": "unknown",
             "regions": [{"basis": "consolidated", "start_page": 300, "end_page": 400}]}
    items, _ = note_line_items(ocr, build_note_index(ocr, basis), basis="consolidated")
    assert items[0].kind == "count"
```

Update `test_fog_model.py`'s `_ITEM` construction to pass `kind="currency"`, and assert `line_item_body(_ITEM)["kind"] == "currency"`.

- [ ] **Step 4: Bump the version**

`fog_objects` v4 → v5 and the FOG `schema_version` 4 → 5 in `core/fog.py` and `core/fog_build.py`, plus the assertions in `test_fog_objects_stage.py`, `test_statement_index.py` and `test_fog_model.py`. The body gained a field; `run.py` keys on `Stage.version`, so without the bump every document is served its pre-`kind` artifact forever.

- [ ] **Step 5: Run and commit**

Run: `uv run --directory backend pytest -m "not integration and not e2e" -q` — expect the existing 1121 plus the new tests, zero failures.

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog.py backend/src/fdi/core/fog_build.py \
        backend/src/fdi/pipeline/stages/fog_objects.py backend/tests/unit
git commit -m "feat(fog): carry quantity kind on every LineItem; fog_objects v5"
```

---

## Task 3: Refuse a kind-mismatched mapping, and raise the lexical floor

**Files:**
- Modify: `backend/src/fdi/core/fog_mapping.py`
- Modify: `backend/src/fdi/pipeline/stages/fog_mapping.py` (version bump)
- Test: `backend/tests/unit/test_fog_mapping_tiers.py`

**Interfaces:**
- Consumes: `LineItem.kind`
- Produces: `MapUnit.kinds: tuple[str, ...]` (parallel to `labels`); `LEXICAL_FLOOR: float`; `resolve_lexical` gains kind-refusal and the floor

**Two changes, both precision-only.** Recall may fall; that is acceptable and must be measured, not assumed.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/unit/test_fog_mapping_tiers.py`:

```python
def test_a_count_is_never_mapped_onto_a_currency_concept():
    """MEASURED: the weighted average share count 361,80,87,518 won EQWVR
    (equity share capital, 362) with a contested spread of 3.6 billion. Both are
    valid numbers, so arbitration cannot separate them -- only the KIND can."""
    unit = MapUnit(
        unit_id="nt:consolidated:18", kind="note", title="Earnings per share",
        labels=("Weighted average number of equity shares",),
        kinds=("count",),
        candidates=(_c("EQWVR", "Equity share capital with voting rights",
                       ("weighted average number of equity shares",)),),
    )
    assert resolve_lexical(unit) == {}


def test_a_currency_line_still_maps_onto_a_currency_concept():
    unit = MapUnit(
        unit_id="st", kind="face", title="Balance sheet",
        labels=("Share capital",), kinds=("currency",),
        candidates=(_c("EQWVR", "Equity share capital with voting rights",
                       ("share capital",)),),
    )
    assert resolve_lexical(unit)["Share capital"].tag == "EQWVR"


def test_a_weak_token_overlap_is_refused_rather_than_guessed():
    """Below the floor, silence beats a guess: an unmapped label is visible and
    a wrong value is not."""
    from fdi.core.fog_mapping import LEXICAL_FLOOR

    assert LEXICAL_FLOOR > 0.5
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Something only faintly related",), kinds=("currency",),
        candidates=(_c("TCEXP", "Travel and conveyance"),),
    )
    assert resolve_lexical(unit) == {}


def test_an_exact_hit_is_never_refused_by_the_floor():
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Travel expenses",), kinds=("currency",), candidates=_CANDS,
    )
    assert resolve_lexical(unit)["Travel expenses"].tag == "TCEXP"


def test_an_unknown_kind_is_permitted_not_refused():
    """A row we could not classify must not become unmappable -- that would turn
    a classification gap into silent data loss."""
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Travel expenses",), kinds=("unknown",), candidates=_CANDS,
    )
    assert resolve_lexical(unit)["Travel expenses"].tag == "TCEXP"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_tiers.py -q`
Expected: FAIL — `MapUnit` has no `kinds` field.

- [ ] **Step 3: Implement**

In `backend/src/fdi/core/fog_mapping.py`:

1. Add `kinds: tuple[str, ...] = ()` to `MapUnit`, **after** `candidates`, so existing construction is unaffected.
2. Add the floor and the refusal:

```python
# Below this, `lexical_match` is guessing from token overlap rather than
# recognising a name. MEASURED: sub-floor overlaps produced every remaining
# wrong value (REMC 1, RIL 2) by winning uncontested. Silence is better --
# an unmapped label is visible in the artifact, a wrong number is not.
LEXICAL_FLOOR = 0.75

# A concept expecting money can never be satisfied by a share count, a
# per-share figure or a ratio. "unknown" is permitted: a classification gap
# must not become silent data loss.
_CURRENCY_ONLY = frozenset({"count", "per_share", "ratio"})
```

and inside `resolve_lexical`, before accepting a match:

```python
    for index, label in enumerate(unit.labels):
        kind = unit.kinds[index] if index < len(unit.kinds) else "unknown"
        if kind in _CURRENCY_ONLY:
            continue
        candidate, method, score = lexical_match(label, unit.candidates)
        if candidate is not None and score >= LEXICAL_FLOOR:
            out[label] = Resolution(label, candidate.tag, f"lexical:{method}", score)
```

3. In `units_from_fog`, build `kinds` alongside `labels` from each item's `kind` field, in the same order.

- [ ] **Step 4: Bump `fog_mapping` v3 → v4**

Its proof records and unmapped set change, and its inputs now include kinds. Update `test_fog_mapping_stage.py`'s version assertion.

- [ ] **Step 5: Run and commit**

Run the full suite. **Record the recall change** — some labels previously mapped by weak overlap will now be unmapped, and that is the intended trade.

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_mapping.py backend/src/fdi/pipeline/stages/fog_mapping.py backend/tests/unit
git commit -m "feat(fog): refuse kind-mismatched mappings and weak overlaps"
```

---

## Task 4: Put the schema in the cache key (review finding I4)

**Files:**
- Modify: `backend/src/fdi/pipeline/stage.py`
- Modify: `backend/src/fdi/pipeline/run.py`
- Modify: `backend/src/fdi/pipeline/stages/fog_mapping.py`
- Test: `backend/tests/unit/test_stage_key_material.py`

**Interfaces:**
- Produces: `Stage.key_material(services: Mapping[str, object]) -> str` — optional, defaults to `""`; `run.py` folds a non-empty result into `input_hash`

**The hole:** `input_hash = derived_input_hash([facts[d].output_hash for d in stage.deps])` uses **declared deps only**. The taxonomy schema arrives via `ctx.services`, so `PUT /schema` changes what the mapping *would* produce while the old artifact is served forever.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_stage_key_material.py`:

```python
"""A stage whose behaviour depends on an injected SERVICE must fold that service
into its cache key, or the framework serves a stale artifact forever."""

from fdi.pipeline.stages.fog_mapping import FogMappingStage


def test_a_schema_change_changes_the_key_material():
    stage = FogMappingStage()
    a = stage.key_material({"schema": {"modules": [{"module_id": "pl"}]}})
    b = stage.key_material({"schema": {"modules": [{"module_id": "bs"}]}})
    assert a and b and a != b


def test_the_same_schema_yields_identical_key_material():
    """Otherwise every run misses the cache and re-bills the only paid stage."""
    stage = FogMappingStage()
    schema = {"modules": [{"module_id": "pl", "retrieval_units": []}]}
    assert stage.key_material({"schema": schema}) == stage.key_material({"schema": schema})


def test_key_material_does_not_depend_on_dict_ordering():
    stage = FogMappingStage()
    a = stage.key_material({"schema": {"a": 1, "b": 2}})
    b = stage.key_material({"schema": {"b": 2, "a": 1}})
    assert a == b


def test_an_absent_schema_yields_stable_empty_material():
    assert FogMappingStage().key_material({}) == FogMappingStage().key_material({})


def test_key_material_never_raises_on_a_junk_service_bag():
    for bag in ({}, {"schema": None}, {"schema": "not a mapping"}, {"schema": [1, 2]}):
        assert isinstance(FogMappingStage().key_material(bag), str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_stage_key_material.py -q`
Expected: FAIL — `FogMappingStage` has no `key_material`.

- [ ] **Step 3: Add the hook to the protocol**

In `backend/src/fdi/pipeline/stage.py`, add to the `Stage` Protocol:

```python
    def key_material(self, services: Mapping[str, object]) -> str:
        """Extra cache-key material from INJECTED SERVICES, or "".

        `run_stage` keys an artifact on its declared deps' output hashes plus
        `version`. A stage whose output also depends on something handed to it
        through `services` -- the taxonomy schema, a prompt template -- is
        outside that key, so changing it changes the answer while the stale
        artifact is served forever. Returning a digest of that input closes
        the hole without making services a dependency edge."""
        return ""
```

Give it a default implementation so no existing stage changes.

- [ ] **Step 4: Fold it into `run.py`**

At the `input_hash` computation, after the dep-derived hash is built:

```python
            material = ""
            with contextlib.suppress(Exception):
                material = stage.key_material(services)
            if material:
                input_hash = derived_input_hash([input_hash, material])
```

Guarded because a stage's own hook must never take the DAG down. Apply it to the root-stage branch too, so a root stage with service-dependent output is covered.

- [ ] **Step 5: Implement it on `fog_mapping`**

```python
    def key_material(self, services: Mapping[str, object]) -> str:
        """The taxonomy schema's digest. Canonical JSON so dict ordering cannot
        change the key and cause a spurious re-bill of the only paid stage."""
        schema = services.get("schema")
        if not isinstance(schema, Mapping):
            return ""
        try:
            return sha256_hex(canonical_json_bytes(schema))
        except Exception:  # noqa: BLE001 - a key is never worth failing a run
            return ""
```

Use the existing helpers in `fdi.core.serialization` and `fdi.core.hashing`; read those modules for the real function names rather than assuming.

- [ ] **Step 6: Run and commit**

```bash
cd backend && uv run pytest -m "not integration and not e2e" -q \
  && uv run ruff check . && uv run mypy src && uv run lint-imports && cd ..
git add backend/src/fdi/pipeline/stage.py backend/src/fdi/pipeline/run.py \
        backend/src/fdi/pipeline/stages/fog_mapping.py backend/tests/unit/test_stage_key_material.py
git commit -m "fix(pipeline): fold service-derived inputs into the artifact cache key"
```

---

## Task 5: Wire validation, confidence and coverage

**Files:**
- Modify: `backend/src/fdi/core/fog_reason.py`
- Modify: `backend/src/fdi/pipeline/stages/fog_reason.py` (version bump)
- Test: `backend/tests/unit/test_fog_reason.py`

**Interfaces:**
- Consumes: `fdi.core.validation`, `fdi.core.confidence`, `fdi.core.coverage` — **read all three modules for their real signatures before writing**; earlier tasks in this project found the plan's assumption wrong three times, and those modules are already unit-tested and must not be modified.
- Produces: `reason()`'s `checks` populated, and `coverage` computed by `core/coverage` rather than inline

- [ ] **Step 1: Read the three modules**

Note the exact entry points and the shapes they return. `core/validation.py` produces per-rule results with `(rule, tags, expected, actual, delta, tolerance, severity, passed)`; `core/confidence.py` exposes `triangulate` and `calibrate`; `core/coverage.py` reports found / derived / genuinely absent.

- [ ] **Step 2: Write the failing test**

Append to `backend/tests/unit/test_fog_reason.py`:

```python
def test_validation_runs_and_reports_its_checks():
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert isinstance(out["checks"], list)
    assert out["summary"] != {} or out["checks"] == []


def test_a_partial_composition_never_satisfies_a_reported_total():
    """The measured rule: an incomplete sum may not vouch for a total. Showing
    the number with its coverage is honest; letting it claim the books tie is
    not."""
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    for record in out["composed"].values():
        if not record["complete"]:
            assert record["coverage"] < 1.0
```

Add at least one test per engine asserting its section is genuinely populated for a fixture that should trigger it — a balance sheet where `A = L + E` holds, and one where it does not.

- [ ] **Step 3: Implement**

Replace `reason()`'s `checks: []` and inline `coverage` block with real calls. Keep every existing key so no consumer breaks. **Do not invent rules**: use what `core/validation.py` already defines.

- [ ] **Step 4: Bump `fog_reason` v3 → v4** and update its assertions.

- [ ] **Step 5: Run and commit**

```bash
cd backend && uv run pytest -m "not integration and not e2e" -q \
  && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_reason.py backend/src/fdi/pipeline/stages/fog_reason.py backend/tests/unit
git commit -m "feat(fog): wire validation, confidence and coverage into fog_reason"
```

---

## Task 6: The review's minors

**Files:** `backend/src/fdi/pipeline/stages/fog_mapping.py`, `backend/src/fdi/core/fog_reason.py`, `backend/src/fdi/core/fog_mapping.py`
**Test:** the existing suites for each

Four fixes, each with a test that fails first:

- [ ] **`result.usage` is read outside the Decision-10 guard.** Move the read inside the `try`, so a client returning a malformed result degrades that unit rather than raising.
- [ ] **`llm_calls` counts failed calls**, so the artifact and `GET /documents/{id}/cost` disagree. Split into `llm_calls` (attempted) and `llm_calls_billed` (returned usage), and assert they differ on a failing client.
- [ ] **The paid prompt carries all 519 concepts** (~12.3k tokens/unit, ~₹218/doc) because `build_map_prompt` bypasses `MAX_CANDIDATES`. Cap it, and **log what was dropped** — a silent cap reads as "considered everything".
- [ ] **`_arbitrate` is computed twice** per `reason()` call (once for facts, once for `contested`). Compute once and pass it.

Also: **the memory key drops `basis`.** `MemoryKey` accepts `(label, section, basis)` and `resolve_from_memory` passes only two, so a consolidated mapping can be recalled for a standalone label. Pass the basis through `MapUnit`.

```bash
git commit -m "fix(fog): the review's minors — guard, billing count, prompt cap, double work, memory basis"
```

---

## Task 7: Phase 5 gate — re-measure

**Files:** `.superpowers/sdd/<plan>/progress.md`, `docs/architecture/pipeline-end-to-end.md`

**No new code.**

- [ ] **Step 1: Re-run the measurement harness** against all three issuers with the shipped `field-extraction-schema.v4_3.json`.

- [ ] **Step 2: Record, per issuer and basis:** recall, precision, contested count, contested-with-large-spread count, unmapped count **by reason**, and LLM calls.

- [ ] **Step 3: Check the gate**

| Check | Expectation |
|---|---|
| TCS equity precision | **> 0%** — the share-count mis-mapping is the specific defect Tasks 1–3 target |
| REMC and RIL precision | ≥ 90%, up from 83.3% / 75.0% |
| TCS P&L precision | still 100% — no regression on what already worked |
| Contested tags with spread > 1,000 | materially below 37 |
| Recall | **record honestly, even if it falls.** The floor and kind-refusal trade recall for precision by design; an unmapped label is visible, a wrong number is not. |

- [ ] **Step 4: Update §12** of `pipeline-end-to-end.md` to reflect what is now live.

- [ ] **Step 5: Commit.**

---

## What this plan deliberately does NOT do

- **No route changes.** `/extract/document` and `/factsheet/document` keep serving the old similarity path.
- **Nothing is deleted.** The cutover stays gated on beating a baseline that does not exist yet — ground truth is still face-level only.
- **No durable mapping memory.** `store=None` remains; the tier is tested and inert. It changes cost, not correctness.
- **No `COMPOSES` / `RECONCILES_WITH` edges.** Composition still produces values, not graph edges.
- **No policy engine.** Domain 2.

## Follow-on

| Plan | Content |
|---|---|
| 6 | Note-level ground truth → the first real baseline · routes read the FOG · durable memory · stage removals · delete the old path once the baseline is beaten |
