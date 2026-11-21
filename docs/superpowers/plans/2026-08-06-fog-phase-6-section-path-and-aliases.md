# FOG Phase 6 — Section Path and Alias Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two defects the first real-LLM run measured — repeated labels that only a section path can disambiguate, and face captions the taxonomy has no alias for.

**Architecture:** No new stages, no new edge types. A face row gains its **section path** (the chain of captions above it, not just the nearest), the FOG carries it, and mapping uses it to separate rows that share a label. Separately, the taxonomy gains report-side aliases harvested from what issuers actually print.

**Tech Stack:** Python 3.12 · pytest (`asyncio_mode = "auto"`) · `uv` · ruff + mypy.

**Spec:** [`docs/superpowers/specs/2026-08-03-financial-object-graph-design.md`](../specs/2026-08-03-financial-object-graph-design.md)
**Predecessor:** [`2026-08-05-fog-phase-5-precision-and-validation.md`](2026-08-05-fog-phase-5-precision-and-validation.md)

## The two measured defects

**D1 — repeated labels, lost section.** `LTOLR` (non-current lease liabilities) expected **9,453**, got **2,077**. TCS's consolidated FOG holds **eight** line items labelled exactly `'Lease liabilities'`. The correct 9,453 *is* on the face — but both face rows (non-current 9,453 and current 1,830) carry the **same** caption `'Financial liabilities'`, because `parse_face_table`'s caption is **last-seen and overwriting**: a run of caption rows (`Liabilities` → `Non-current liabilities` → `Financial liabilities`) collapses to the innermost, and the outer section is discarded. No tier or arbitration tuning can recover it.

**D2 — no alias for the printed caption.** `EQWVR` expected **362**, got **1.0**. The face row `'Share capital'` is **not a claimant at all**: the concept "Equity share capital with voting rights" has no alias matching what TCS actually prints, so note noise wins by default.

## Measured starting point (2026-08-06, 146 real LLM calls)

| | recall | precision |
|---|---|---|
| RIL P&L | 100% | 100% |
| TCS consolidated P&L | 81.8% | 100% |
| TCS standalone P&L | 81.8% | 100% |
| REMC P&L | 66.7% | 85.7% |
| TCS liabilities | 50.0% | **75.0%** ← D1 |
| TCS assets | 28.6% | 100% |
| TCS equity | **0.0%** | **0.0%** ← D2 |

## Global Constraints

- **`fdi.core` is pure and TOTAL.** No I/O, no config, no clock/RNG, **never raises on any input shape**. `fdi.core` may not import `fdi.pipeline`/`platform`/`api`/`workers`.
- **Decision 10 — never raise** in pipeline modules; guard each I/O step individually.
- **`absent ≠ missed`.** Anything dropped keeps a recorded reason.
- **Determinism.** Everything reaching an artifact body is explicitly sorted; bodies are content-addressed.
- **Any artifact body shape change requires bumping BOTH that artifact's `schema_version` AND the emitting `Stage.version`.** Current (verified 2026-08-06): `statement_index` **v4**, `note_index` **v3**, `fog_objects` **v5**, `fog_mapping` **v5**, `fog_reason` **v4**. **Grep and confirm before editing — these have moved every phase.**
- **The model never parses a number.**
- **Test runner:** `uv run --directory backend pytest -m "not integration and not e2e" -q` — currently **1160 passed, 11 skipped**.
- **Gates before every commit:** `ruff check .` (100-char lines), `mypy src`, `lint-imports`, all clean.
- **CRLF:** most files are CRLF. A scripted replace using `\n` against `\r\n` silently matches NOTHING and reports success — this has bitten four times. Read bytes, detect the newline, build strings with it, then **grep to confirm the edit landed**.
- **Stage by explicit path, then run `git status`** and confirm nothing you changed is unstaged. A commit on this branch shipped tests without their source and left HEAD red on a clean checkout.
- **Transcribe, do not rewrite.** Where this plan gives code, copy it exactly.

---

## Task 1: A face row carries its section PATH, not just the nearest caption

**Files:**
- Modify: `backend/src/fdi/core/statements.py`
- Test: `backend/tests/unit/test_statement_sections.py`

**Interfaces:**
- Produces: `FaceRow.section_path: tuple[str, ...]` (outermost first), appended **last** after `source_row` so every existing positional construction keeps working. `parse_face_table` populates it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_statement_sections.py`:

```python
"""A caption is LAST-SEEN and overwriting, so a run of caption rows collapses to
the innermost and the outer section is lost.

MEASURED ON TCS: the balance sheet prints `Liabilities` / `Non-current
liabilities` / `Financial liabilities` as three consecutive amount-less rows.
Both the non-current lease liability (9,453) and the current one (1,830) ended
up with caption 'Financial liabilities', so nothing could tell them apart and
LTOLR resolved to 2,077 from an unrelated note."""

from fdi.core.statements import parse_face_table


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_BS = {
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2026"),
        _cell(1, 0, "EQUITY AND LIABILITIES"),
        _cell(2, 0, "Equity"),
        _cell(3, 0, "Share capital"), _cell(3, 2, "362"),
        _cell(4, 0, "Liabilities"),
        _cell(5, 0, "Non-current liabilities"),
        _cell(6, 0, "Financial liabilities"),
        _cell(7, 0, "Lease liabilities"), _cell(7, 2, "9,453"),
        _cell(8, 0, "Current liabilities"),
        _cell(9, 0, "Financial liabilities"),
        _cell(10, 0, "Lease liabilities"), _cell(10, 2, "1,830"),
    ],
}


def _rows():
    return {(r.label, r.current): r for r in parse_face_table(_BS)}


def test_two_rows_sharing_a_label_get_different_section_paths():
    rows = _rows()
    non_current = rows[("Lease liabilities", 9453.0)]
    current = rows[("Lease liabilities", 1830.0)]
    assert non_current.section_path != current.section_path


def test_the_path_keeps_the_OUTER_section_the_caption_discarded():
    rows = _rows()
    assert "Non-current liabilities" in rows[("Lease liabilities", 9453.0)].section_path
    assert "Current liabilities" in rows[("Lease liabilities", 1830.0)].section_path


def test_the_path_is_outermost_first():
    path = _rows()[("Lease liabilities", 9453.0)].section_path
    assert path.index("Non-current liabilities") < path.index("Financial liabilities")


def test_the_nearest_caption_is_still_available_unchanged():
    """`caption` keeps its existing meaning; the path is additive."""
    assert _rows()[("Lease liabilities", 9453.0)].caption == "Financial liabilities"


def test_a_sibling_section_does_not_leak_into_the_next():
    """Once `Current liabilities` opens, `Non-current liabilities` must be gone."""
    assert "Non-current liabilities" not in _rows()[("Lease liabilities", 1830.0)].section_path


def test_a_row_under_no_caption_has_an_empty_path():
    table = {"cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "2026"),
        _cell(1, 0, "Revenue"), _cell(1, 1, "100"),
    ]}
    assert parse_face_table(table)[0].section_path == ()


def test_a_deeper_caption_does_not_pop_a_shallower_one():
    """Depth is unknown from a flat table, so the path only ever GROWS until a
    known top-level section resets it -- guessing nesting would be worse than
    carrying an extra level."""
    rows = _rows()
    assert "Financial liabilities" in rows[("Lease liabilities", 9453.0)].section_path


def test_malformed_input_never_raises():
    assert parse_face_table({}) == []
    assert parse_face_table({"cells": "nope"}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_statement_sections.py -q`
Expected: FAIL — `FaceRow` has no attribute `section_path`.

- [ ] **Step 3: Implement**

In `backend/src/fdi/core/statements.py`:

1. Add to `FaceRow`, **after** every existing field: `section_path: tuple[str, ...] = ()`.

2. Add the section vocabulary and the path tracker above `parse_face_table`:

```python
# The Schedule III blocks that RESET the path rather than nesting inside it.
# A flat table carries no indentation, so depth is genuinely unknowable; what
# IS knowable is that a statement's top-level blocks are mutually exclusive.
# MEASURED: without this, `Non-current liabilities` and `Current liabilities`
# both collapse into the caption `Financial liabilities`, and the two lease
# rows become indistinguishable.
_TOP_SECTIONS: tuple[str, ...] = (
    "non-current assets", "current assets",
    "non-current liabilities", "current liabilities",
    "assets", "equity and liabilities", "equity", "liabilities",
    "income", "expenses", "expenditure",
)


def _section_reset(label: str) -> str:
    """The top-level block `label` opens, or "" if it opens none.

    Longest match wins: "non-current liabilities" must not be read as
    "liabilities", which would reset the path to the wrong block."""
    key = label.strip().lower().rstrip(":")
    best = ""
    for section in _TOP_SECTIONS:
        if key == section and len(section) > len(best):
            best = section
    return best
```

3. In `parse_face_table`, replace the single `caption` variable with a path. Where the current code does `caption = label` on an amount-less row, instead:

```python
        if not amounts:
            # A row with no amount anywhere is a CAPTION.
            if _section_reset(label):
                path = [label]        # a top-level block starts a fresh path
            else:
                path.append(label)    # otherwise it nests under what is open
            caption = label
            continue
```

Initialise `path: list[str] = []` and `caption = ""` together, and pass `section_path=tuple(path)` when constructing each `FaceRow`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_statement_sections.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Regression check**

Run: `uv run --directory backend pytest tests/unit -k "statement or footing or fog" -q`
Expected: PASS. `caption`'s behaviour is unchanged, so nothing that reads it should move.

- [ ] **Step 6: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/statements.py backend/tests/unit/test_statement_sections.py
git status --short
git commit -m "feat(statements): a face row carries its section path, not just the nearest caption"
```

---

## Task 2: Carry the section path onto every LineItem and into mapping

**Files:**
- Modify: `backend/src/fdi/core/fog.py`, `backend/src/fdi/core/fog_build.py`, `backend/src/fdi/core/fog_mapping.py`
- Modify: `backend/src/fdi/pipeline/stages/fog_objects.py`, `backend/src/fdi/pipeline/stages/fog_mapping.py` (version bumps)
- Test: `backend/tests/unit/test_fog_face.py`, `backend/tests/unit/test_fog_mapping_prompt.py`

**Interfaces:**
- Produces: `LineItem.section_path: tuple[str, ...]` (appended last, defaulted); `line_item_body` emits `"section_path"` as a **list** (JSON has no tuples); `MapUnit.sections: tuple[tuple[str, ...], ...]` parallel to `labels`; `build_map_prompt` renders each label with its section.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/unit/test_fog_mapping_prompt.py`:

```python
def test_the_prompt_shows_the_section_of_each_label():
    """MEASURED ON TCS: eight line items are labelled exactly 'Lease
    liabilities'. Without its section, the model is asked to distinguish rows
    that are literally identical strings, and LTOLR resolved to 2,077 instead
    of 9,453."""
    unit = MapUnit(
        unit_id="st:consolidated:balance_sheet", kind="face", title="Balance sheet",
        labels=("Lease liabilities", "Lease liabilities"),
        candidates=(_c("LTOLR", "Lease liabilities - non-current"),
                    _c("STLL", "Lease liabilities - current")),
        sections=(("Non-current liabilities", "Financial liabilities"),
                  ("Current liabilities", "Financial liabilities")),
    )
    _system, user, _dropped = build_map_prompt(unit)
    assert "Non-current liabilities" in user
    assert "Current liabilities" in user


def test_a_label_with_no_section_still_renders():
    unit = MapUnit("u", "note", "t", ("Travel",), (_c("TCEXP", "Travel"),))
    _system, user, _dropped = build_map_prompt(unit)
    assert "Travel" in user
```

Append to `backend/tests/unit/test_fog_face.py`:

```python
def test_a_face_line_item_carries_its_section_path():
    items, _ = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert isinstance(revenue.section_path, tuple)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_prompt.py tests/unit/test_fog_face.py -q`
Expected: FAIL — `MapUnit` has no `sections`; `LineItem` has no `section_path`.

- [ ] **Step 3: Implement**

1. `core/fog.py` — add `section_path: tuple[str, ...] = ()` to `LineItem` (last), and `"section_path": list(item.section_path)` to `line_item_body`. **A tuple is not JSON-serialisable as itself**; emit a list, and keep it sorted-stable by construction (it is ordered, not a set — do NOT sort it, order carries meaning).
2. `core/fog_build.py` — in `face_line_items`, pass `section_path=row.section_path`. In `note_line_items`, pass `section_path=()` (a note has no statement section); state that in a comment.
3. `core/fog_mapping.py` — add `sections: tuple[tuple[str, ...], ...] = ()` to `MapUnit` (last). In `units_from_fog`, build it from each item's `section_path` in the same order as `labels`. In `build_map_prompt`, render a label as `f"  {label}  [section: {' > '.join(section)}]"` when it has one, plain otherwise.
4. Bump `fog_objects` and `fog_mapping` — both `Stage.version` and artifact `schema_version` — and update every assertion. **Grep for the current values first.**

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_prompt.py tests/unit/test_fog_face.py -q`
Expected: PASS

- [ ] **Step 5: Full suite, gates, commit**

```bash
cd backend && uv run pytest -m "not integration and not e2e" -q \
  && uv run ruff check . && uv run mypy src && uv run lint-imports && cd ..
git add backend/src/fdi/core/fog.py backend/src/fdi/core/fog_build.py \
        backend/src/fdi/core/fog_mapping.py backend/src/fdi/pipeline/stages/fog_objects.py \
        backend/src/fdi/pipeline/stages/fog_mapping.py backend/tests/unit
git status --short
git commit -m "feat(fog): carry the section path onto LineItems and into the mapping prompt"
```

---

## Task 3: Section-aware lexical disambiguation

**Files:**
- Modify: `backend/src/fdi/core/fog_mapping.py`
- Test: `backend/tests/unit/test_fog_mapping_tiers.py`

**Interfaces:**
- `resolve_lexical` consults the section when a concept name carries a qualifier the section confirms or contradicts.

**Why this is separate from Task 2:** Task 2 lets the *model* see the section. This makes the *free* tier use it, so the common case never reaches the paid tier at all.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/unit/test_fog_mapping_tiers.py`:

```python
def test_a_section_qualifier_picks_the_right_concept_of_a_matching_pair():
    """MEASURED ON TCS: 'Lease liabilities' appears under both Non-current and
    Current liabilities, and the taxonomy has a concept for each. The label
    alone is a tie; the section breaks it, for free."""
    unit = MapUnit(
        unit_id="st", kind="face", title="Balance sheet",
        labels=("Lease liabilities",),
        candidates=(_c("LTOLR", "Lease liabilities - non-current"),
                    _c("STLL", "Lease liabilities - current")),
        sections=(("Non-current liabilities", "Financial liabilities"),),
    )
    assert resolve_lexical(unit)["Lease liabilities"].tag == "LTOLR"


def test_the_other_section_picks_the_other_concept():
    unit = MapUnit(
        unit_id="st", kind="face", title="Balance sheet",
        labels=("Lease liabilities",),
        candidates=(_c("LTOLR", "Lease liabilities - non-current"),
                    _c("STLL", "Lease liabilities - current")),
        sections=(("Current liabilities", "Financial liabilities"),),
    )
    assert resolve_lexical(unit)["Lease liabilities"].tag == "STLL"


def test_a_contradicting_section_refuses_rather_than_guessing():
    """If the only candidate's qualifier contradicts the section, silence beats
    a confident wrong answer."""
    unit = MapUnit(
        unit_id="st", kind="face", title="Balance sheet",
        labels=("Lease liabilities",),
        candidates=(_c("STLL", "Lease liabilities - current"),),
        sections=(("Non-current liabilities",),),
    )
    assert resolve_lexical(unit) == {}


def test_no_section_leaves_existing_behaviour_untouched():
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Travel expenses",), candidates=_CANDS,
    )
    assert resolve_lexical(unit)["Travel expenses"].tag == "TCEXP"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_tiers.py -q`
Expected: FAIL on the section tests.

- [ ] **Step 3: Implement**

Add to `core/fog_mapping.py`:

```python
# A concept name may carry a period qualifier the SECTION can confirm or
# contradict. These are the only two that matter on an Indian balance sheet,
# and they are mutually exclusive by construction.
_QUALIFIERS: tuple[tuple[str, str], ...] = (
    ("non-current", "non-current"),
    ("current", "current"),
)


def _qualifier(text: str) -> str:
    """The period qualifier `text` asserts, or "".

    "non-current" is tested FIRST and wins: it CONTAINS "current", so testing
    in the other order would label every non-current concept "current" and
    invert the disambiguation this exists to provide."""
    key = text.lower()
    for needle, name in _QUALIFIERS:
        if needle in key:
            return name
    return ""
```

In `resolve_lexical`, after a candidate is matched and before accepting it, compare `_qualifier(candidate.name)` with the qualifier of the row's section (join the section path and take its qualifier). If both are non-empty and differ, **do not accept** — continue to the next-best candidate rather than returning a contradicted match. Record nothing; the residue flows to the model tier as usual.

Order matters and must be asserted by a test: `"non-current"` contains `"current"`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_tiers.py -q`
Expected: PASS

- [ ] **Step 5: Full suite, gates, commit**

```bash
cd backend && uv run pytest -m "not integration and not e2e" -q \
  && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_mapping.py backend/tests/unit/test_fog_mapping_tiers.py
git status --short
git commit -m "feat(fog): break a repeated-label tie with the row's section, for free"
```

---

## Task 4: Alias coverage for the captions issuers actually print

**Files:**
- Create: `scripts/harvest_aliases.py`
- Modify: `docs/architecture/field-extraction-schema.v4_3.json` (and the per-statement source files it merges from)
- Test: `backend/tests/unit/test_alias_coverage.py`

**Interfaces:**
- Produces: `propose_aliases(fog: Mapping, schema: Mapping, *, basis: str) -> list[dict[str, object]]` — for each **unmapped face label**, the concepts whose names are closest, as a REVIEW LIST.

**This task does not auto-edit the taxonomy.** An alias is a semantic assertion — "this printed caption means this concept" — and inventing them from string similarity is how a wrong mapping becomes permanent and invisible. The script proposes; a human confirms; the confirmed set is committed.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_alias_coverage.py`:

```python
"""MEASURED ON TCS: `EQWVR` expected 362, got 1.0. The face row 'Share capital'
is not a claimant at all -- the concept "Equity share capital with voting
rights" has no alias matching what TCS prints, so note noise wins by default.
The gap is ALIAS COVERAGE; no tier or arbitration change can reach it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from harvest_aliases import propose_aliases  # noqa: E402

_SCHEMA = {"modules": [{
    "module_id": "equity", "statement": "equity",
    "retrieval_units": [{"unit_id": "RU", "fields": [
        {"tag": "EQWVR", "field_name": "Equity share capital with voting rights",
         "origin": "extracted", "note_aliases": []},
    ]}],
}]}
_FOG = {"bases": {"consolidated": {
    "statements": [{"id": "st:consolidated:balance_sheet", "statement": "balance_sheet"}],
    "notes": [],
    "line_items": [{"id": "li:a", "parent": "st:consolidated:balance_sheet",
                    "origin": "face", "label": "Share capital", "current": 362.0,
                    "section_path": ["Equity"], "kind": "currency"}],
    "edges": [],
}}}


def test_an_unmapped_face_label_is_proposed_against_the_closest_concepts():
    proposals = propose_aliases(_FOG, _SCHEMA, basis="consolidated")
    assert proposals
    top = proposals[0]
    assert top["label"] == "Share capital"
    assert "EQWVR" in [c["tag"] for c in top["candidates"]]


def test_a_proposal_carries_the_evidence_a_human_needs_to_judge():
    top = propose_aliases(_FOG, _SCHEMA, basis="consolidated")[0]
    assert top["value"] == 362.0
    assert top["section_path"] == ["Equity"]


def test_note_labels_are_not_proposed():
    """Only FACE captions are worth aliasing: a note's rows are detail, and
    aliasing them would bind a concept to one issuer's schedule wording."""
    fog = {"bases": {"consolidated": {
        "statements": [], "notes": [{"id": "nt:consolidated:1", "ref": "1", "title": "x"}],
        "line_items": [{"id": "li:n", "parent": "nt:consolidated:1", "origin": "note",
                        "label": "Something", "current": 1.0, "section_path": [],
                        "kind": "currency"}],
        "edges": [],
    }}}
    assert propose_aliases(fog, _SCHEMA, basis="consolidated") == []


def test_an_already_mapped_label_is_not_proposed():
    schema = {"modules": [{"module_id": "equity", "statement": "equity",
        "retrieval_units": [{"unit_id": "RU", "fields": [
            {"tag": "EQWVR", "field_name": "Equity share capital with voting rights",
             "origin": "extracted", "note_aliases": ["share capital"]},
        ]}]}]}
    assert propose_aliases(_FOG, schema, basis="consolidated") == []


def test_proposals_are_deterministic_and_sorted():
    a = propose_aliases(_FOG, _SCHEMA, basis="consolidated")
    assert a == propose_aliases(_FOG, _SCHEMA, basis="consolidated")


def test_malformed_input_never_raises():
    assert propose_aliases({}, {}, basis="consolidated") == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_alias_coverage.py -q`
Expected: FAIL — no module `harvest_aliases`.

- [ ] **Step 3: Implement `scripts/harvest_aliases.py`**

Reuse `fdi.core.fog_mapping.resolve_lexical` to decide what is already mapped, and `fdi.core.mapping.lexical_match` to rank candidates. Emit, per unmapped face label: `{"label", "value", "section_path", "statement", "candidates": [{"tag", "name", "score"}]}`, sorted by label. Give the module docstring the reason it proposes rather than applies.

Provide a `main()` that reads a FOG artifact and the schema, and writes the review list to a path given on the command line.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_alias_coverage.py -q`
Expected: PASS, 6 passed

- [ ] **Step 5: Generate the review list from the real run**

```bash
uv run --directory backend python ../scripts/harvest_aliases.py \
  --fog out/fog-run-llm-2026-08-06/TCS/fog_objects.json \
  --schema docs/architecture/field-extraction-schema.v4_3.json \
  --basis consolidated --out out/alias-proposals-TCS.json
```

Repeat for REMC and RIL. **Report the counts; do not apply anything yet.**

- [ ] **Step 6: Commit the harvester only**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add scripts/harvest_aliases.py backend/tests/unit/test_alias_coverage.py
git status --short
git commit -m "feat(taxonomy): propose report-side aliases for unmapped face captions"
```

---

## Task 5: Confirm and apply the aliases (human judgement required)

**Files:** `docs/architecture/field-extraction-schema.v4_3*.json`
**Test:** `backend/tests/unit/test_alias_coverage.py`

**This task needs a human.** Each proposal asserts that a printed caption means a taxonomy concept. Applying them from similarity alone would bake a wrong mapping into the schema, where every later measurement inherits it silently.

- [ ] **Step 1: Review each proposal** in `out/alias-proposals-*.json`. Accept only where the caption genuinely names the concept. `Share capital` → `EQWVR` is the motivating case and should be confirmed against the printed balance sheet.

- [ ] **Step 2: Apply the accepted aliases** to the per-statement v4.3 files under `note_aliases`, then regenerate the merged file with `scripts/merge_v4_3_schema.py`.

- [ ] **Step 3: Add a regression test** naming each newly-aliased caption, so a future schema edit that drops it fails loudly:

```python
def test_the_captions_indian_issuers_actually_print_have_aliases():
    """Each of these was measured as an unmapped face caption on a real filing."""
    import json
    from pathlib import Path

    from fdi.core.concepts import all_concepts

    schema = json.loads(
        (Path(__file__).resolve().parents[3] / "docs" / "architecture"
         / "field-extraction-schema.v4_3.json").read_text(encoding="utf-8")
    )
    by_alias = {a.lower() for c in all_concepts(schema) for a in c.aliases}
    for caption in ("share capital",):   # extend as aliases are confirmed
        assert caption in by_alias, f"{caption!r} lost its alias"
```

- [ ] **Step 4: Gates and commit.**

---

## Task 6: Phase 6 gate — re-measure with real LLM calls

**Files:** `.superpowers/sdd/<plan>/progress.md`

**No new code.**

- [ ] **Step 1: Re-run the export bundle** with `--llm` against all three issuers, to a new dated directory.

- [ ] **Step 2: Check the gate**

| Check | Expectation |
|---|---|
| TCS liabilities precision | **back to 100%** — `LTOLR` resolves to 9,453 (D1) |
| TCS equity precision | **> 0%** — `EQWVR` resolves to 362 (D2, needs Task 5 applied) |
| RIL P&L | **still 100% / 100%** — no regression on what is already perfect |
| TCS P&L | **still ≥ 81.8% / 100%** |
| `by_lexical` | **up** — Task 3 moves work off the paid tier |
| LLM calls | **≤ 146** — section-aware free resolution should reduce, not add |

- [ ] **Step 3: Record every number, including any that got worse.** Two of Phase 5's five gate checks failed and saying so is what made this plan target the right things.

- [ ] **Step 4: Commit.**

---

## What this plan deliberately does NOT do

- **No route changes**; `/extract/document` still serves the old similarity path.
- **Nothing deleted.** The cutover stays gated on a baseline that needs note-level ground truth.
- **No durable mapping memory**; `store=None` remains.
- **No auto-applied aliases.** Task 4 proposes, Task 5 is human judgement.

## Follow-on

| Plan | Content |
|---|---|
| 7 | Note-level ground truth → the first real baseline · routes read the FOG · durable memory · stage removals · delete the old path |
