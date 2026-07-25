# TR-1 P4a — schema v3.2 `basis_preference` (report)

## Scope

Per design spec `docs/superpowers/specs/2026-07-22-table-representation-design.md`
sec 6.1: add a per-UNIT `basis_preference` (`standalone` | `consolidated` |
`both`, default `standalone`) to the field-extraction schema and its pure
parser. No pipeline/retrieval changes (later phase).

## Changes

1. `docs/architecture/field-extraction-schema.json`
   - `version`: `"3.1.0"` -> `"3.2.0"`.
   - Added `"basis_preference": "standalone"` to exactly one unit (`RU_BS`,
     the Balance Sheet primary-statement unit) as a demonstration/regression
     anchor. No other structural change.

2. `backend/src/fdi/core/extractor.py`
   - `FieldSpec`: added `basis_preference: str = "standalone"` as the last
     defaulted member (after `locator_keywords`), so positional/hand-built
     construction is unaffected.
   - `UnitSpec`: added the same member with the same default.
   - New pure helper `_basis_preference(value) -> str`: returns `value` if it
     is one of `{"standalone", "consolidated", "both"}`, else `"standalone"`
     (covers absent key, wrong type, or an invalid string like `"garbage"`).
     Never raises.
   - `_field_spec_from`: reads `unit.get("basis_preference")` through the new
     helper and sets it on the returned `FieldSpec`.
   - `group_fields_by_unit`: carries `basis_preference` from the group's
     first field onto the constructed `UnitSpec` (same pattern as
     `unit_title`/`retrieval_query`/`locator_keywords`).
   - Module docstring: added a short paragraph documenting the v3.2 addition
     and its scope boundary (retrieval filtering is explicitly a later
     phase, not built here).

3. `backend/tests/unit/test_extractor.py` — added:
   - `test_parse_field_specs_basis_preference_consolidated`
   - `test_parse_field_specs_basis_preference_absent_defaults_to_standalone`
   - `test_parse_field_specs_basis_preference_invalid_value_defaults_to_standalone`
   - `test_parse_field_specs_basis_preference_both`
   - `test_group_fields_by_unit_carries_basis_preference_from_first_field`
   - Extended `test_field_spec_constructs_without_the_new_members` to assert
     the new member defaults to `"standalone"` on a hand-built `FieldSpec`.
   - Extended `test_group_fields_by_unit_carries_unit_metadata` to assert
     `unit.basis_preference == "standalone"`.
   - Extended `test_parse_field_specs_against_the_committed_schema_file` to
     assert `schema["version"] == "3.2.0"` and that every `RU_BS` field's
     `basis_preference == "standalone"`. Field-count assertion (190) left
     unchanged, as instructed.

## Verification

- `uv run --directory backend pytest tests/unit/test_extractor.py -q` -> 71
  passed.
- `uv run --directory backend ruff check .` -> All checks passed!
- `uv run --directory backend mypy --strict src` -> Success: no issues found
  in 101 source files.
- `python -c "import json; ...print(d['version'])"` -> `3.2.0`.

## Concerns

None. Change is additive and backward-compatible: every existing v3.1 unit
(no `basis_preference` key) parses to the documented default `standalone`,
and `fdi.core` stays pure (no I/O, no new imports). Basis-aware retrieval
filtering (`extract_unit` consuming this value) is intentionally deferred to
P4b/P3-wire per the phasing table — not touched here.
