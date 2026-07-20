# SP-19 — UI Backend Enablement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six backend gaps that block a real frontend UI: revive field extraction against schema v3.1, list all documents, serve the source PDF, return bounding boxes on extracted fields, make the extraction schema editable over HTTP, and make CORS origins configurable.

**Architecture:** Everything here is additive to the existing layered stack (`api | workers` → `pipeline` → `platform` → `core`). Pure algorithms — schema parsing (B1), the chunk→geometry join (B4), schema validation (B5) — land in `fdi.core` where they are unit-testable against frozen fixtures with no Docker. All artifact/blob/DB reads stay in `fdi.pipeline` / `fdi.platform`. The routes in `fdi.api.routes` stay thin: resolve, call, map to a Pydantic view. The extraction schema stops being a read-only file and becomes a MinIO object (`schema/field-extraction-schema.json`) with the repo's committed file as the immutable v3.1.0 seed/fallback.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2 / pydantic-settings, SQLAlchemy 2 (async, asyncpg), Alembic, MinIO (S3 API), Celery, structlog, pytest + pytest-asyncio (`asyncio_mode = "auto"`), testcontainers (Postgres/MinIO/RabbitMQ/Redis), ruff, mypy `--strict`, import-linter.

## Global Constraints

Every task's requirements implicitly include this section.

1. **Python 3.13**, `requires-python = ">=3.13,<3.14"` (`backend/pyproject.toml`). Dependencies are **uv-managed**; never hand-edit `uv.lock`, use `uv add` / `uv sync`.
2. **Lint gate:** `uv run --directory backend ruff check .` — clean. Line length 100; rule set `["E", "F", "I", "UP", "B", "ASYNC", "C4", "SIM"]`.
3. **Type gate:** `uv run --directory backend mypy --strict src` — clean. `plugins = ["pydantic.mypy"]`.
4. **Import gate:** `uv run --directory backend lint-imports --config .importlinter` — must report `Contracts: 1 kept, 0 broken`.
5. **Encoding:** set `PYTHONUTF8=1` for every command. Windows console is cp1252 and an unconfigured structlog/rich render raises `UnicodeEncodeError` that escapes the code that was logging.
6. **Fast test gate:** `uv run --directory backend pytest -m "not integration and not e2e" -q`.
7. **Integration test gate** (needs Docker): `uv run --directory backend pytest -m integration -q`.
8. **The import-linter layering contract** (`backend/.importlinter`, verbatim structure):
   - `root_package = fdi`, `containers = fdi`, contract type `layers`, `exhaustive = true`.
   - Layers ordered high → low: `api | workers`, then `pipeline`, then `platform`, then `core`. `|` means INDEPENDENT SIBLINGS: `api` and `workers` may **not** import each other.
   - The single `ignore_imports` entry is exactly `fdi.api.app -> fdi.workers.tasks` (the composition root).
   - **Consequence, binding on Task 4:** `fdi.core` may import **nothing** else under `fdi` — not `fdi.pipeline`, not `fdi.platform`, and by this project's own convention (see `fdi/core/retrieval.py`'s module docstring) not other `fdi.core` producers either. So the chunk→node→provenance join is a **pure function in `fdi.core.provenance`** that takes the already-read `provenance` and `structure` artifacts as plain `Mapping`s; the artifact I/O lives in `fdi.pipeline.extractor`.
   - Comments in `.importlinter` must each be on their own standalone line (configparser does not strip trailing inline comments).
9. **Never-raises contract (decision 10)** of the extractor: `fdi.core.extractor` is total on any input shape, and `fdi.pipeline.extractor.extract_field`/`extract_document` guard every I/O step individually. Task 4 must not weaken this.
10. **Commit message convention** (from `git log --oneline -25`): `type(scope): summary`, types `feat` / `fix` / `docs` / `test`, scopes `core` / `pipeline` / `api` / `platform` / `config` / `extractor`. Use `--` (double hyphen), not an em dash, in commit subjects.
11. **Config trio:** any new `Settings` field must be added in all three places — `backend/src/fdi/platform/config.py`, `.env.example`, and `docker/compose.yml`'s `x-backend-env` block.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `backend/src/fdi/core/extractor.py` | `FieldSpec` + `parse_field_specs` migrated to schema v3.1 | 1 |
| `backend/src/fdi/core/provenance.py` | `resolve_chunk_regions` / `resolve_evidence_regions` — pure chunk→geometry join | 4 |
| `backend/src/fdi/core/schema.py` | `validate_extraction_schema` / `bump_patch_version` — pure schema validation | 5 |
| `backend/src/fdi/platform/db/repository.py` | `DocumentRepository.list_latest_per_doc_key`, `RegistryRepository.latest_run_status_counts` | 2 |
| `backend/src/fdi/platform/config.py` | `cors_origins` setting | 6 |
| `backend/src/fdi/pipeline/schema_store.py` | Read-through schema load (MinIO object, on-disk seed fallback) + save | 5 |
| `backend/src/fdi/pipeline/extractor.py` | Attaches `bounding_regions` to every extracted field's `source` | 4 |
| `backend/src/fdi/api/schemas.py` | New/extended Pydantic view models | 2,3,4,5 |
| `backend/src/fdi/api/routes.py` | `GET /documents` list-all, `GET /documents/{id}/file`, `GET|PUT /schema`, `POST /schema/validate` | 2,3,5 |
| `backend/src/fdi/api/app.py` | Settings-driven CORS allowlist | 6 |

---

### Task 1: B1 — migrate `parse_field_specs` to schema v3.1

The single highest-priority fix: the working-tree schema `docs/architecture/field-extraction-schema.json` is v3.1.0 and nests fields under `modules[*].retrieval_units[*].fields`. The shipped parser reads `modules[*].fields` and filters on `rag_extractable is True and source_type == "document_content"` — keys that no longer exist. Verified: `parse_field_specs(v3.1_schema)` returns `[]`, i.e. `POST /extract/document` extracts nothing at all.

**v3.1 shape, verified against the committed file** (16 modules, 18 retrieval units, 198 fields):

- module keys: `id`, `key`, `name`, `retrieval_unit_count`, `retrieval_units`
- retrieval-unit keys: `unit_id`, `title`, `scope`, `statement_type` (optional), `locator_keywords`, `retrieval_query`, `context_hint`, `references` (optional), `validation` (optional), `field_count`, `fields`
- field keys: `field_name`, `field_path`, `canonical_label`, `aliases`, `data_type`, `description`, `extraction_prompt`, `example`, `normalization`, plus optional `group`, `item_fields`, `periods`
- `scope` ∈ `{front_matter, primary_statement, note, back_matter}`; `statement_type` ∈ `{BS, PL, CF}`
- There is **no** `rag_extractable`, **no** `source_type`, **no** `keywords`, **no** `enum_values`, and **no per-field `retrieval_query`** anywhere in the committed file.

**Decisions this task locks in (all stated in the new docstring):**

1. **No `rag_extractable` / `source_type` filter survives.** In v3.1 a field only exists if it sits inside a retrieval unit, and a retrieval unit *is* document context by construction (`retrieval_model.concept`: "Retrieve-once, extract-many. Retrieval is per Retrieval Unit"). Pipeline-sourced metadata fields simply are not in this file any more. Inventing a replacement filter would drop all 198 fields again.
2. **`retrieval_query` comes from the retrieval UNIT**, falling back to a per-field `retrieval_query` if a future schema revision adds one. Unit-first, because retrieval is per-unit in v3.1.
3. **`keywords` = the field's `aliases` followed by the unit's `locator_keywords`**, order-preserving deduplicated. `keyword_overlap` divides by `len(keywords)`, so duplicates would silently dilute the rerank boost.
4. **The `module` selector matches on `key` OR `name`** (exact, either one). v3.1 has no `folder` key, so the old `name`-or-`folder` pair becomes `key`-or-`name`. `key` is the stable identifier (`module_3_balance_sheet`), `name` is the human label (`Balance Sheet`); accepting both keeps every existing caller working and is what a UI dropdown will send. The public signature `parse_field_specs(schema, module=None)` is **unchanged**, so `fdi/api/routes.py:853` needs no edit.
5. **The five new `FieldSpec` members carry defaults** (`module_key=""`, `unit_id=""`, `unit_title=""`, `scope=None`, `statement_type=None`) so hand-built `FieldSpec(...)` call sites — e.g. `tests/integration/test_extract_document.py`'s blank-query guard test — keep compiling.
6. **The "never raises, skips malformed" contract is preserved** and widened: a non-Mapping module / retrieval unit / field is skipped; a malformed or missing `modules` / `retrieval_units` / `fields` list yields `[]`; a field missing a non-empty `field_name`, `data_type`, or `extraction_prompt`, or with no non-blank effective `retrieval_query`, is skipped.

**Files:**
- Modify: `backend/src/fdi/core/extractor.py:1-21` (module docstring), `:29-38` (`FieldSpec`), `:50-51` (`_module_matches`), `:54-91` (`_field_spec_from`), `:94-126` (`parse_field_specs`)
- Modify (tests): `backend/tests/unit/test_extractor.py:1-95` — replace `_schema()` and the seven `parse_field_specs` tests; `:148-153` — `_field()` helper
- Modify (tests): `backend/tests/integration/test_extract_document.py:62-97` — replace `_schema(marker)`; `:222-226` — the hand-built `FieldSpec`
- Unchanged: `backend/src/fdi/api/routes.py:853`, `backend/src/fdi/pipeline/extractor.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  ```python
  @dataclass(frozen=True)
  class FieldSpec:
      field_name: str
      data_type: str
      retrieval_query: str
      keywords: tuple[str, ...]
      extraction_prompt: str
      enum_values: tuple[str, ...] | None
      module_id: int
      module_name: str
      module_key: str = ""
      unit_id: str = ""
      unit_title: str = ""
      scope: str | None = None
      statement_type: str | None = None

  def parse_field_specs(
      schema: Mapping[str, object], module: str | None = None
  ) -> list[FieldSpec]: ...
  ```
  Task 4 consumes nothing from this task; Task 5 consumes `parse_field_specs` indirectly through the route.

- [ ] **Step 1: Write the failing tests**

Replace lines 1–95 of `backend/tests/unit/test_extractor.py` (the imports, `_schema()`, and the seven `parse_field_specs` tests) with:

```python
from fdi.core.extractor import (
    Candidate,
    FieldSpec,
    assemble_result,
    build_extraction_messages,
    is_numeric,
    keyword_overlap,
    parse_field_specs,
    rerank_candidates,
)


def _schema() -> dict[str, object]:
    """A v3.1.0-shaped schema: modules -> retrieval_units -> fields, with
    `aliases` per field and `locator_keywords`/`retrieval_query` per unit.
    Mirrors docs/architecture/field-extraction-schema.json exactly (no
    rag_extractable, no source_type, no per-field retrieval_query)."""
    return {
        "version": "3.1.0",
        "modules": [
            {
                "id": 2,
                "key": "module_2_company_info",
                "name": "Company Information",
                "retrieval_units": [
                    {
                        "unit_id": "RU_COMPANY",
                        "title": "Corporate Information",
                        "scope": "front_matter",
                        "locator_keywords": ["corporate information", "CIN"],
                        "retrieval_query": "Corporate Information panel: name, CIN, address",
                        "fields": [
                            {
                                "field_name": "Company name",
                                "canonical_label": "Company name",
                                "aliases": ["Company name", "Name of the company"],
                                "data_type": "string",
                                "extraction_prompt": "Extract the Company name.",
                            },
                            {
                                # MALFORMED: no extraction_prompt -- SKIPPED, never raises
                                "field_name": "Broken field",
                                "aliases": [],
                                "data_type": "string",
                            },
                        ],
                    },
                ],
            },
            {
                "id": 3,
                "key": "module_3_balance_sheet",
                "name": "Balance Sheet",
                "retrieval_units": [
                    {
                        "unit_id": "RU_BS",
                        "title": "Balance Sheet",
                        "scope": "primary_statement",
                        "statement_type": "BS",
                        "locator_keywords": ["balance sheet", "total assets"],
                        "retrieval_query": "Standalone Balance Sheet as at the reporting date",
                        "fields": [
                            {
                                "field_name": "Total assets",
                                "canonical_label": "Total assets",
                                "aliases": ["Total assets", "TOTAL ASSETS"],
                                "data_type": "decimal",
                                "extraction_prompt": "Extract the total assets value.",
                            },
                        ],
                    },
                ],
            },
        ],
    }


def test_parse_field_specs_flattens_modules_units_fields() -> None:
    specs = parse_field_specs(_schema())
    assert [s.field_name for s in specs] == ["Company name", "Total assets"]


def test_parse_field_specs_skips_malformed_field() -> None:
    specs = parse_field_specs(_schema())
    assert "Broken field" not in [s.field_name for s in specs]


def test_parse_field_specs_module_filter_by_key() -> None:
    specs = parse_field_specs(_schema(), module="module_3_balance_sheet")
    assert [s.field_name for s in specs] == ["Total assets"]
    assert specs[0].module_id == 3
    assert specs[0].module_key == "module_3_balance_sheet"
    assert specs[0].module_name == "Balance Sheet"


def test_parse_field_specs_module_filter_by_name() -> None:
    specs = parse_field_specs(_schema(), module="Company Information")
    assert [s.field_name for s in specs] == ["Company name"]


def test_parse_field_specs_module_filter_no_match_is_empty() -> None:
    assert parse_field_specs(_schema(), module="no-such-module") == []


def test_parse_field_specs_empty_or_malformed_schema_never_raises() -> None:
    assert parse_field_specs({}) == []
    assert parse_field_specs({"modules": "not-a-list"}) == []
    assert parse_field_specs({"modules": [{"retrieval_units": "nope"}]}) == []
    assert parse_field_specs({"modules": [{"retrieval_units": [1, "x", None]}]}) == []
    assert parse_field_specs({"modules": [{"retrieval_units": [{"fields": "nope"}]}]}) == []
    assert parse_field_specs({"modules": [{"retrieval_units": [{"fields": [1, None]}]}]}) == []


def test_parse_field_specs_retrieval_query_comes_from_the_unit() -> None:
    spec = parse_field_specs(_schema(), module="module_3_balance_sheet")[0]
    assert spec.retrieval_query == "Standalone Balance Sheet as at the reporting date"
    assert spec.unit_id == "RU_BS"
    assert spec.unit_title == "Balance Sheet"
    assert spec.scope == "primary_statement"
    assert spec.statement_type == "BS"


def test_parse_field_specs_per_field_retrieval_query_is_the_fallback() -> None:
    """A unit with no usable retrieval_query falls back to the field's own,
    so a future schema revision that pushes the query back down still parses."""
    schema = {
        "modules": [
            {
                "id": 9, "key": "m9", "name": "M9",
                "retrieval_units": [
                    {
                        "unit_id": "RU_9", "title": "Nine", "retrieval_query": "   ",
                        "locator_keywords": ["nine"],
                        "fields": [
                            {
                                "field_name": "F", "data_type": "string",
                                "extraction_prompt": "p", "aliases": ["f"],
                                "retrieval_query": "field level query",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    assert parse_field_specs(schema)[0].retrieval_query == "field level query"


def test_parse_field_specs_field_with_no_query_anywhere_is_skipped() -> None:
    schema = {
        "modules": [
            {
                "id": 9, "key": "m9", "name": "M9",
                "retrieval_units": [
                    {
                        "unit_id": "RU_9", "title": "Nine",
                        "fields": [
                            {
                                "field_name": "F", "data_type": "string",
                                "extraction_prompt": "p", "aliases": [],
                            },
                        ],
                    },
                ],
            },
        ],
    }
    assert parse_field_specs(schema) == []


def test_parse_field_specs_keywords_are_aliases_then_locator_keywords() -> None:
    spec = parse_field_specs(_schema(), module="module_3_balance_sheet")[0]
    assert spec.keywords == ("Total assets", "TOTAL ASSETS", "balance sheet", "total assets")


def test_parse_field_specs_keywords_are_deduplicated_in_order() -> None:
    """keyword_overlap divides by len(keywords), so a duplicate silently
    dilutes the rerank boost -- dedup is a scoring correctness rule, not tidying."""
    schema = {
        "modules": [
            {
                "id": 9, "key": "m9", "name": "M9",
                "retrieval_units": [
                    {
                        "unit_id": "RU_9", "title": "Nine", "retrieval_query": "q",
                        "locator_keywords": ["total assets", "balance sheet"],
                        "fields": [
                            {
                                "field_name": "F", "data_type": "string",
                                "extraction_prompt": "p",
                                "aliases": ["total assets", "assets"],
                            },
                        ],
                    },
                ],
            },
        ],
    }
    assert parse_field_specs(schema)[0].keywords == (
        "total assets", "assets", "balance sheet",
    )


def test_parse_field_specs_field_shape() -> None:
    spec = parse_field_specs(_schema())[0]
    assert spec == FieldSpec(
        field_name="Company name",
        data_type="string",
        retrieval_query="Corporate Information panel: name, CIN, address",
        keywords=("Company name", "Name of the company", "corporate information", "CIN"),
        extraction_prompt="Extract the Company name.",
        enum_values=None,
        module_id=2,
        module_name="Company Information",
        module_key="module_2_company_info",
        unit_id="RU_COMPANY",
        unit_title="Corporate Information",
        scope="front_matter",
        statement_type=None,
    )


def test_parse_field_specs_against_the_committed_schema_file() -> None:
    """The regression that motivated this task: the shipped v2.0 parser
    returned [] against the committed v3.1.0 file, so POST /extract/document
    extracted nothing at all. Reads the real file, not a fixture."""
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "docs" / "architecture" / "field-extraction-schema.json"
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    specs = parse_field_specs(schema)
    assert len(specs) == 198
    assert parse_field_specs(schema, module="module_3_balance_sheet")
    assert all(s.retrieval_query.strip() for s in specs)
    assert all(s.unit_id for s in specs)
```

Then replace `_field()` at `backend/tests/unit/test_extractor.py:148-153` with:

```python
def _field() -> FieldSpec:
    return FieldSpec(
        field_name="Total assets", data_type="decimal",
        retrieval_query="Standalone Balance Sheet as at the reporting date",
        keywords=("total", "assets"), extraction_prompt="Extract the total assets value.",
        enum_values=None, module_id=3, module_name="Balance Sheet",
        module_key="module_3_balance_sheet", unit_id="RU_BS", unit_title="Balance Sheet",
        scope="primary_statement", statement_type="BS",
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_extractor.py -q`
Expected: FAIL. `test_parse_field_specs_flattens_modules_units_fields` fails with `assert [] == ['Company name', 'Total assets']`; `test_parse_field_specs_field_shape` fails with `IndexError: list index out of range`; `test_parse_field_specs_against_the_committed_schema_file` fails with `assert 0 == 198`; `_field()` fails with `TypeError: FieldSpec.__init__() got an unexpected keyword argument 'module_key'`.

- [ ] **Step 3: Write the implementation**

Replace `backend/src/fdi/core/extractor.py:1-126` (module docstring through `parse_field_specs`) with:

```python
"""Schema-driven RAG field extraction -- pure algorithms (design spec sec
3.1). No I/O, no config, no clock/RNG, and no import of fdi.pipeline/
fdi.platform or any other fdi.core producer (fdi.core.retrieval/tables/
agent) -- the field-extraction schema and every candidate are read/built as
plain Mappings/dataclasses this module owns, the same "reads inputs as
plain Mappings" posture fdi.core.retrieval.build_chunks and fdi.core.agent's
extract_citations already establish.

parse_field_specs flattens the schema's THREE-level v3.1 nesting --
modules -> retrieval_units -> fields -- into FieldSpecs. v3.1 replaced
v2.0's flat modules[*].fields, and with it dropped rag_extractable,
source_type, keywords, enum_values, and the per-field retrieval_query. So
the old "keep only rag_extractable=true AND source_type=document_content"
filter is GONE, not reimplemented: in v3.1 a field exists only inside a
retrieval unit, and a retrieval unit IS document context by construction
(the schema's own retrieval_model: "Retrieve-once, extract-many. Retrieval
is per Retrieval Unit, not per field"). Pipeline-sourced metadata fields
are simply absent from the file. retrieval_query is read off the UNIT
(falling back to a per-field one if a later revision adds it back), and
keywords are the field's `aliases` followed by the unit's
`locator_keywords`, order-preserving deduplicated -- keyword_overlap
divides by len(keywords), so a duplicate would silently dilute the boost.

rerank_candidates is the model-free blend reranker (decision 3):
score = retrieval_score + keyword_weight * keyword_overlap(text, keywords),
stable-sorted descending, truncated to top_k. build_extraction_messages/
assemble_result are the two halves of the ExtractorClient contract: the
prompt IN, the LLM's raw dict OUT -> the schema's output_contract record,
with fallbacks so a malformed LLM response degrades to a well-formed record
rather than raising (decisions 8, 10). Every function here is total: no
raise, on any input shape, anywhere.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_NUMERIC_DATA_TYPES = frozenset({"decimal", "integer"})


@dataclass(frozen=True)
class FieldSpec:
    """One extractable field, flattened out of its module and retrieval unit.

    The five unit/module members after `module_name` carry defaults so a
    hand-built FieldSpec (tests, and any future caller that synthesizes one
    outside the parser) still constructs with the v2.0-era argument set.
    """

    field_name: str
    data_type: str
    retrieval_query: str
    keywords: tuple[str, ...]
    extraction_prompt: str
    enum_values: tuple[str, ...] | None
    module_id: int
    module_name: str
    module_key: str = ""
    unit_id: str = ""
    unit_title: str = ""
    scope: str | None = None
    statement_type: str | None = None


@dataclass(frozen=True)
class Candidate:
    evidence_id: str
    text: str
    page: int | None
    score: float
    source_tool: str


def _module_matches(module: Mapping[str, object], selector: str) -> bool:
    """v3.1 modules carry `key` (stable id, e.g. "module_3_balance_sheet")
    and `name` (human label, e.g. "Balance Sheet") -- and no `folder`, which
    v2.0's selector also accepted. Either one matches, exactly: `key` is what
    a stored UI selection should send, `name` is what a person types."""
    return module.get("key") == selector or module.get("name") == selector


def _str_tuple(value: object) -> tuple[str, ...]:
    """`value` filtered to non-empty `str`s, or `()` for anything else."""
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def _dedup(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return tuple(out)


def _non_blank_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _field_spec_from(
    field: Mapping[str, object],
    *,
    unit: Mapping[str, object],
    module_id: int,
    module_name: str,
    module_key: str,
) -> FieldSpec | None:
    """One v3.1 schema field dict (plus its owning retrieval unit) ->
    FieldSpec, or None if malformed (missing/wrong-typed field_name,
    data_type, or extraction_prompt, or no non-blank retrieval_query on
    either the unit or the field) -- parse_field_specs skips a None, never
    raises."""
    field_name = field.get("field_name")
    data_type = field.get("data_type")
    extraction_prompt = field.get("extraction_prompt")
    if not isinstance(field_name, str) or not field_name:
        return None
    if not isinstance(data_type, str) or not data_type:
        return None
    if not isinstance(extraction_prompt, str) or not extraction_prompt:
        return None

    retrieval_query = _non_blank_str(unit.get("retrieval_query")) or _non_blank_str(
        field.get("retrieval_query")
    )
    if retrieval_query is None:
        return None

    keywords = _dedup(
        _str_tuple(field.get("aliases")) + _str_tuple(unit.get("locator_keywords"))
    )

    enum_values_raw = field.get("enum_values")
    enum_values = (
        tuple(v for v in enum_values_raw if isinstance(v, str))
        if isinstance(enum_values_raw, list)
        else None
    )

    unit_id = unit.get("unit_id")
    unit_title = unit.get("title")

    return FieldSpec(
        field_name=field_name, data_type=data_type, retrieval_query=retrieval_query,
        keywords=keywords, extraction_prompt=extraction_prompt, enum_values=enum_values,
        module_id=module_id, module_name=module_name, module_key=module_key,
        unit_id=unit_id if isinstance(unit_id, str) else "",
        unit_title=unit_title if isinstance(unit_title, str) else "",
        scope=_non_blank_str(unit.get("scope")),
        statement_type=_non_blank_str(unit.get("statement_type")),
    )


def parse_field_specs(schema: Mapping[str, object], module: str | None = None) -> list[FieldSpec]:
    """Flatten schema["modules"][*]["retrieval_units"][*]["fields"] into
    FieldSpecs (v3.1 nesting; v2.0's flat modules[*]["fields"] is gone, and
    so are its rag_extractable/source_type filters -- see the module
    docstring). `module`, if given, keeps only the module whose "key" or
    "name" equals it (exact match). Malformed module/unit/field entries are
    skipped, never raise; a malformed/missing "modules", "retrieval_units",
    or "fields" list contributes nothing."""
    modules_raw = schema.get("modules")
    modules = modules_raw if isinstance(modules_raw, list) else []

    specs: list[FieldSpec] = []
    for m in modules:
        if not isinstance(m, Mapping):
            continue
        if module is not None and not _module_matches(m, module):
            continue
        module_id_raw = m.get("id")
        module_id = module_id_raw if isinstance(module_id_raw, int) else -1
        module_name_raw = m.get("name")
        module_name = module_name_raw if isinstance(module_name_raw, str) else ""
        module_key_raw = m.get("key")
        module_key = module_key_raw if isinstance(module_key_raw, str) else ""

        units_raw = m.get("retrieval_units")
        units = units_raw if isinstance(units_raw, list) else []
        for u in units:
            if not isinstance(u, Mapping):
                continue
            fields_raw = u.get("fields")
            fields = fields_raw if isinstance(fields_raw, list) else []
            for f in fields:
                if not isinstance(f, Mapping):
                    continue
                spec = _field_spec_from(
                    f, unit=u, module_id=module_id, module_name=module_name,
                    module_key=module_key,
                )
                if spec is not None:
                    specs.append(spec)
    return specs
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_extractor.py -q`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Update the integration test's schema fixture**

Replace `backend/tests/integration/test_extract_document.py:62-97` (`_schema`) with:

```python
def _schema(marker: str) -> dict[str, object]:
    """v3.1.0-shaped: modules -> retrieval_units -> fields. `retrieval_query`
    is per UNIT, so the three fields need three units (the unmatchable one
    gets its own unit with a deliberately unmatchable query)."""
    return {
        "version": "3.1.0",
        "modules": [
            {
                "id": 1, "key": "module_1_overview", "name": "Overview",
                "retrieval_units": [
                    {
                        "unit_id": "RU_OVERVIEW",
                        "title": "Company Overview",
                        "scope": "front_matter",
                        "locator_keywords": ["manufacturing"],
                        "retrieval_query": f"manufacturing company {marker}",
                        "fields": [
                            {
                                "field_name": "Company overview text",
                                "aliases": ["overview"],
                                "data_type": "text",
                                "extraction_prompt": "Extract the company overview.",
                            },
                        ],
                    },
                ],
            },
            {
                "id": 2, "key": "module_2_balance_sheet", "name": "Balance Sheet",
                "retrieval_units": [
                    {
                        "unit_id": "RU_BS",
                        "title": "Balance Sheet",
                        "scope": "primary_statement",
                        "statement_type": "BS",
                        "locator_keywords": ["total", "assets"],
                        "retrieval_query": "Total assets",
                        "fields": [
                            {
                                "field_name": "Total assets",
                                "aliases": ["Total assets"],
                                "data_type": "decimal",
                                "extraction_prompt": "Extract the total assets value.",
                            },
                        ],
                    },
                    {
                        "unit_id": "RU_NOWHERE",
                        "title": "Nowhere",
                        "scope": "note",
                        "locator_keywords": [],
                        "retrieval_query": "zzzznonexistenttoken9999 impossible query",
                        "fields": [
                            {
                                "field_name": "Unmatchable field",
                                "aliases": [],
                                "data_type": "string",
                                "extraction_prompt": "Extract nothing findable.",
                            },
                        ],
                    },
                ],
            },
        ],
    }
```

And replace the hand-built `FieldSpec` at `backend/tests/integration/test_extract_document.py:222-226` with:

```python
    blank = FieldSpec(
        field_name="Blank query field", data_type="string", retrieval_query="   ",
        keywords=(), extraction_prompt="x", enum_values=None, module_id=9,
        module_name="X", module_key="m9", unit_id="RU_X", unit_title="X",
        scope=None, statement_type=None,
    )
```

- [ ] **Step 6: Run the integration test to verify it passes**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/integration/test_extract_document.py -q`
Expected: PASS, 2 passed. (Needs Docker. `assert len(fields) == 3` still holds: three units, one field each.)

- [ ] **Step 7: Run the full gate set**

Run:
```bash
PYTHONUTF8=1 uv run --directory backend ruff check .
PYTHONUTF8=1 uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
PYTHONUTF8=1 uv run --directory backend pytest -m "not integration and not e2e" -q
```
Expected: ruff `All checks passed!`; mypy `Success: no issues found`; lint-imports `Contracts: 1 kept, 0 broken.`; pytest all passed.

- [ ] **Step 8: Commit**

```bash
git add backend/src/fdi/core/extractor.py backend/tests/unit/test_extractor.py backend/tests/integration/test_extract_document.py
git commit -m "feat(core): parse_field_specs reads schema v3.1 (modules -> retrieval_units -> fields)

The committed field-extraction-schema.json is v3.1.0 and nests fields under
retrieval_units, dropping rag_extractable/source_type/keywords and moving
retrieval_query onto the unit. The v2.0 parser returned [] against it, so
POST /extract/document extracted nothing. FieldSpec gains module_key,
unit_id, unit_title, scope, statement_type; keywords derive from the field's
aliases plus the unit's locator_keywords (deduped); the module selector now
matches key or name. Never-raises/skip-malformed contract preserved."
```

---

### Task 2: B2 — `GET /documents` list-all with stage summaries and pagination

Today `GET /documents` **requires** a `doc_key` query param (`routes.py:362-365`) and returns every version of that one key. A UI has no way to discover what is in the corpus. This task makes `doc_key` optional while keeping the existing behavior byte-identical when it is supplied.

**Decisions:**

1. **One route, two response shapes.** With `?doc_key=` → the existing `DocumentVersionsView` (`{"versions": [...]}`), unchanged. Without → the new `DocumentListView`. The route is declared `response_model=None` with a union return annotation, so FastAPI serializes whichever model is returned instead of coercing one into the other. Splitting into two paths (`/documents` and `/documents/versions`) would break every existing caller of the query-param form.
2. **Latest version per `doc_key`.** Grouped by `coalesce(doc_key, id)` — `doc_key` is nullable (`models.py:56`), and grouping on the bare column would collapse every legacy NULL-`doc_key` row into a single group. A `row_number()` window ranked by `version DESC NULLS LAST, created_at DESC, id` picks the winner deterministically.
3. **`stage_counts` is a `{status: count}` map over the LATEST run per stage name**, not over all runs. A document with 8 stages where `ocr` was retried twice must count `ocr` once. Statuses with a zero count are absent, so the map is `{"done": 7, "failed": 1}`, never a fixed eight-key skeleton. Two round trips total (one for the page of documents, one bulk count for that page's ids) — not N+1.
4. **Pagination:** `limit` (default 50, 1..200) and `offset` (default 0, >= 0), with `total` = the number of distinct doc groups, so the UI can render a page count.

**Files:**
- Modify: `backend/src/fdi/platform/db/repository.py:105` (append `latest_run_status_counts` to `RegistryRepository`), `:233` (append `list_latest_per_doc_key` to `DocumentRepository`)
- Modify: `backend/src/fdi/api/schemas.py:86-88` (after `DocumentVersionsView`)
- Modify: `backend/src/fdi/api/routes.py:362-365` (`list_documents`)
- Create: `backend/tests/integration/test_documents_list.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  ```python
  # fdi.platform.db.repository
  class DocumentRepository:
      async def list_latest_per_doc_key(
          self, *, limit: int, offset: int
      ) -> tuple[list[Document], int]: ...

  class RegistryRepository:
      async def latest_run_status_counts(
          self, doc_ids: Sequence[str]
      ) -> dict[str, dict[str, int]]: ...

  # fdi.api.schemas
  class DocumentSummaryView(BaseModel):
      id: str
      checksum: str | None
      filename: str | None
      byte_size: int | None
      doc_key: str | None
      version: int | None
      content_type: str | None
      stage_counts: dict[str, int]

  class DocumentListView(BaseModel):
      documents: list[DocumentSummaryView]
      total: int
      limit: int
      offset: int
  ```

- [x] **Step 1: Write the failing test**

Create `backend/tests/integration/test_documents_list.py`:

```python
"""GET /documents in its two modes.

With `?doc_key=` it is the version list it has always been (backward
compatibility is the point -- SP-17's /extract/document flow and the
existing frontend both call it that way). Without, it is the corpus list a
UI needs: latest version per doc_key, each with a {status: count} summary
over the LATEST run per stage name, paginated.
"""

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    @asynccontextmanager
    async def _make():
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client

    return _make


async def _seed(session_factory, doc_key: str, versions: int) -> list[str]:
    """`versions` document rows sharing one doc_key, versions 1..n."""
    from fdi.platform.db.repository import DocumentRepository

    ids: list[str] = []
    async with session_factory() as s:
        repo = DocumentRepository(s)
        for v in range(1, versions + 1):
            doc_id = str(uuid.uuid4())
            await repo.create(
                id=doc_id, checksum=uuid.uuid4().hex, filename=f"{doc_key}-v{v}.pdf",
                byte_size=10, content_type="application/pdf", doc_key=doc_key, version=v,
            )
            ids.append(doc_id)
        await s.commit()
    return ids


async def _seed_runs(session_factory, doc_id: str) -> None:
    """ocr: two runs (failed then done -- only `done` must be counted),
    normalize: one running run."""
    from fdi.platform.db.repository import RegistryRepository

    async with session_factory() as s:
        repo = RegistryRepository(s)
        first = str(uuid.uuid4())
        await repo.start_run(first, doc_id, "ocr", 1, "h", None)
        await repo.fail_run(first, "boom")
        await s.commit()
    async with session_factory() as s:
        repo = RegistryRepository(s)
        second = str(uuid.uuid4())
        await repo.start_run(second, doc_id, "ocr", 1, "h", None)
        await repo.finish_run(second, "oh", "artifacts/ocr.json")
        await repo.start_run(str(uuid.uuid4()), doc_id, "normalize", 1, "h", None)
        await s.commit()


@pytest.fixture
def session_factory(migrated_engine):
    from fdi.platform.db.engine import make_session_factory

    return make_session_factory(migrated_engine)


async def test_list_documents_with_doc_key_is_unchanged(make_client, session_factory) -> None:
    key = f"acme-{uuid.uuid4().hex}"
    await _seed(session_factory, key, versions=2)
    async with make_client() as client:
        r = await client.get("/documents", params={"doc_key": key})
    assert r.status_code == 200
    body = r.json()
    assert [v["version"] for v in body["versions"]] == [1, 2]
    assert "documents" not in body


async def test_list_all_documents_returns_latest_version_per_doc_key(
    make_client, session_factory
) -> None:
    key = f"acme-{uuid.uuid4().hex}"
    await _seed(session_factory, key, versions=3)
    async with make_client() as client:
        r = await client.get("/documents", params={"limit": 200})
    assert r.status_code == 200
    body = r.json()
    mine = [d for d in body["documents"] if d["doc_key"] == key]
    assert len(mine) == 1
    assert mine[0]["version"] == 3
    assert mine[0]["filename"] == f"{key}-v3.pdf"


async def test_list_all_documents_carries_latest_run_stage_counts(
    make_client, session_factory
) -> None:
    key = f"acme-{uuid.uuid4().hex}"
    [doc_id] = await _seed(session_factory, key, versions=1)
    await _seed_runs(session_factory, doc_id)
    async with make_client() as client:
        r = await client.get("/documents", params={"limit": 200})
    mine = [d for d in r.json()["documents"] if d["doc_key"] == key]
    # ocr's LATEST run is done (the earlier failed one must not be counted).
    assert mine[0]["stage_counts"] == {"done": 1, "running": 1}


async def test_list_all_documents_paginates(make_client, session_factory) -> None:
    prefix = uuid.uuid4().hex
    for i in range(3):
        await _seed(session_factory, f"{prefix}-{i}", versions=1)
    async with make_client() as client:
        first = (await client.get("/documents", params={"limit": 1, "offset": 0})).json()
        second = (await client.get("/documents", params={"limit": 1, "offset": 1})).json()
    assert len(first["documents"]) == 1
    assert len(second["documents"]) == 1
    assert first["documents"][0]["id"] != second["documents"][0]["id"]
    assert first["limit"] == 1 and first["offset"] == 0
    assert second["offset"] == 1
    assert first["total"] == second["total"] >= 3


async def test_list_all_documents_rejects_a_bad_limit(make_client) -> None:
    async with make_client() as client:
        r = await client.get("/documents", params={"limit": 0})
    assert r.status_code == 422
```

- [x] **Step 2: Run the test to verify it fails**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/integration/test_documents_list.py -q`
Expected: FAIL. The list-all tests fail with `assert 422 == 200` — FastAPI rejects the request because `doc_key` is still a required query param (`{"detail":[{"type":"missing","loc":["query","doc_key"]}]}`).

- [x] **Step 3: Add the repository queries**

Append to `RegistryRepository` in `backend/src/fdi/platform/db/repository.py` (after `latest_runs`, line 105):

```python
    async def latest_run_status_counts(
        self, doc_ids: Sequence[str]
    ) -> dict[str, dict[str, int]]:
        """{doc_id: {status: count}} over the LATEST `stage_run` per
        (doc_id, stage_name), for every id in `doc_ids`.

        Latest-per-stage, not all-runs: a document whose `ocr` was retried
        three times must contribute one `ocr` to the summary, not three --
        otherwise a flaky stage looks like a bigger pipeline. Statuses with
        no rows are simply absent from the inner dict.

        ONE query for the whole page of documents (a `row_number()` window
        partitioned by (doc_id, stage_name)), not one per document: the
        caller is a paginated list route, and N+1 there is the difference
        between one round trip and fifty. An empty `doc_ids` short-circuits
        to `{}` without a query, same posture as `DocumentRepository.get_many`.
        """
        if not doc_ids:
            return {}
        ranked = (
            select(
                StageRun.doc_id.label("doc_id"),
                StageRun.status.label("status"),
                func.row_number()
                .over(
                    partition_by=(StageRun.doc_id, StageRun.stage_name),
                    order_by=(StageRun.created_at.desc(), StageRun.id.desc()),
                )
                .label("rn"),
            )
            .where(StageRun.doc_id.in_(list(doc_ids)))
            .subquery()
        )
        rows = (
            await self._s.execute(
                select(ranked.c.doc_id, ranked.c.status, func.count())
                .where(ranked.c.rn == 1)
                .group_by(ranked.c.doc_id, ranked.c.status)
            )
        ).all()
        counts: dict[str, dict[str, int]] = {}
        for doc_id, status, n in rows:
            counts.setdefault(str(doc_id), {})[str(status)] = int(n)
        return counts
```

Append to `DocumentRepository` in the same file (after `list_by_doc_key`, line 233):

```python
    async def list_latest_per_doc_key(
        self, *, limit: int, offset: int
    ) -> tuple[list[Document], int]:
        """(one page of the corpus, total group count).

        One row per doc_key -- its HIGHEST version -- so a UI's document
        list shows each filing once rather than once per re-upload. Grouped
        on `coalesce(doc_key, id)`, NOT on the bare column: `doc_key` is
        nullable (early intake rows predate it), and grouping on NULL would
        collapse every such legacy row into one phantom document. Ranking is
        `version DESC NULLS LAST, created_at DESC, id` so the winner is
        deterministic even when two rows share a version.

        `total` counts GROUPS, not rows -- it is the denominator for the
        page count the UI renders, and must not move when someone uploads a
        new version of a document already in the list.
        """
        group = func.coalesce(Document.doc_key, Document.id)
        ranked = (
            select(
                Document.id.label("id"),
                func.row_number()
                .over(
                    partition_by=group,
                    order_by=(
                        Document.version.desc().nullslast(),
                        Document.created_at.desc(),
                        Document.id,
                    ),
                )
                .label("rn"),
            )
            .subquery()
        )
        latest_ids = select(ranked.c.id).where(ranked.c.rn == 1).subquery()
        total = (
            await self._s.execute(select(func.count()).select_from(latest_ids))
        ).scalar_one()
        rows = (
            await self._s.execute(
                select(Document)
                .where(Document.id.in_(select(latest_ids.c.id)))
                .order_by(Document.created_at.desc(), Document.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        return list(rows), int(total)
```

Add `Sequence` to the imports at the top of `repository.py`:

```python
from collections.abc import Sequence
```

- [x] **Step 4: Add the view models**

Insert into `backend/src/fdi/api/schemas.py` immediately after `DocumentVersionsView` (line 88):

```python
class DocumentSummaryView(BaseModel):
    """One corpus-list row: the latest version of a doc_key, plus a
    {status: count} summary over the latest run per stage name."""

    id: str
    checksum: str | None
    filename: str | None
    byte_size: int | None
    doc_key: str | None
    version: int | None
    content_type: str | None
    stage_counts: dict[str, int]


class DocumentListView(BaseModel):
    documents: list[DocumentSummaryView]
    total: int
    limit: int
    offset: int
```

- [x] **Step 5: Rewrite the route**

Replace `backend/src/fdi/api/routes.py:362-365` with:

```python
@router.get("/documents", response_model=None)
async def list_documents(
    session: SessionDep,
    doc_key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DocumentVersionsView | DocumentListView:
    """Two modes on one path.

    `?doc_key=` -> every version of that key, ascending, exactly as this
    route has always behaved (SP-17's extractor flow and the SP-18 frontend
    both call it that way; changing the shape would break them).

    No `doc_key` -> the corpus list a real UI needs: the latest version per
    doc_key, each with a stage summary, paginated. `response_model=None` +
    a union return annotation is what lets one path serve both: a declared
    `response_model` would coerce one shape into the other and silently drop
    half of each.
    """
    repo = DocumentRepository(session)
    if doc_key is not None:
        docs = await repo.list_by_doc_key(doc_key)
        return DocumentVersionsView(versions=[_view(d) for d in docs])

    rows, total = await repo.list_latest_per_doc_key(limit=limit, offset=offset)
    counts = await RegistryRepository(session).latest_run_status_counts([d.id for d in rows])
    return DocumentListView(
        documents=[
            DocumentSummaryView(
                id=d.id,
                checksum=d.checksum,
                filename=d.filename,
                byte_size=d.byte_size,
                doc_key=d.doc_key,
                version=d.version,
                content_type=d.content_type,
                stage_counts=counts.get(d.id, {}),
            )
            for d in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
```

Add `DocumentListView` and `DocumentSummaryView` to the `fdi.api.schemas` import block in `routes.py` (alphabetical, between `DocumentStructureView` and `DocumentVersionsView`):

```python
    DocumentListView,
    DocumentStructureView,
    DocumentSummaryView,
    DocumentVersionsView,
```

- [x] **Step 6: Run the test to verify it passes**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/integration/test_documents_list.py -q`
Expected: PASS, 5 passed.

- [x] **Step 7: Run the full gate set**

Run:
```bash
PYTHONUTF8=1 uv run --directory backend ruff check .
PYTHONUTF8=1 uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
PYTHONUTF8=1 uv run --directory backend pytest -m "not integration and not e2e" -q
```
Expected: all green; lint-imports `Contracts: 1 kept, 0 broken.`

- [x] **Step 8: Commit**

```bash
git add backend/src/fdi/platform/db/repository.py backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/integration/test_documents_list.py
git commit -m "feat(api): GET /documents lists the corpus (latest per doc_key, stage counts, paginated)

doc_key becomes optional. With it, the route returns the version list it
always did. Without it, one row per doc_key -- its highest version -- each
carrying a {status: count} summary over the LATEST run per stage name,
with limit/offset/total. Grouping is on coalesce(doc_key, id) so legacy
NULL-doc_key rows do not collapse into one phantom document; stage counts
are one windowed query for the whole page, not N+1."
```

---

### Task 3: B3 — `GET /documents/{doc_id}/file` streams the source PDF

No endpoint serves the uploaded PDF. `intake_document` writes it to the artifacts bucket at `documents/{checksum}.pdf` (`fdi/pipeline/intake.py:23-24, 43`), and `Document.checksum` is the only pointer to it.

**Decisions:**

1. **Proxy through the API, not a presigned MinIO URL.** SP-18 §2 decision 1 already settled this: "a browser fetching `localhost:9000` cross-origin would need MinIO CORS config; proxying through the already-CORS'd API is simpler and keeps the UI to one origin." A PDF viewer embedded in the UI is exactly the case that decision was made for. Keep the posture.
2. **HTTP range support is DEFERRED, deliberately.** The minio SDK's `get_object` does accept `offset`/`length`, so byte ranges are *reachable* — but `ArtifactStore.get_raw` is built around one contract, "read the whole body, then `close()`/`release_conn()`" (see its own comment at `artifact_store.py:76-83` about what breaks the moment the body stops being read whole). Doing this properly means a new `ArtifactStore.get_range` returning a streaming handle, plus `Range` header parsing, `If-Range`, multi-range rejection, and `206`/`416` responses — a change to the storage layer's contract that is out of scope here and not needed by any viewer this UI ships with (`pdf.js` fetches whole documents fine when the server advertises no ranges). So: **no `Accept-Ranges` header is emitted**, which is the correct signal for "ranges unsupported", and the whole body is returned with `200`. Revisit when a document exceeds a size where whole-body transfer is felt.
3. **Three distinct 404 causes, three distinct messages:** unknown document; document row with no `checksum` (an early-intake row that predates content addressing); and object absent from the bucket. `store.exists` is checked before the read so a missing object is a 404, not a 500 from a raw `S3Error`.
4. **`Content-Disposition: inline`** with an ASCII-sanitized `filename=` plus an RFC 5987 `filename*=UTF-8''...` for the real name, so a filing named `ACME — FY24.pdf` neither breaks the header nor loses its name on download.

**Files:**
- Modify: `backend/src/fdi/api/routes.py:452` (append after `document_artifact`)
- Create: `backend/tests/integration/test_document_file.py`

**Interfaces:**
- Consumes: `fdi.pipeline.intake.document_key(checksum: str) -> str` (existing, `intake.py:23`); `ArtifactStore.exists(key) -> bool`, `ArtifactStore.get_bytes(key) -> bytes` (existing).
- Produces: `GET /documents/{doc_id}/file` → `200` with `Content-Type: application/pdf`, `Content-Disposition: inline; filename="..."; filename*=UTF-8''...`, `Content-Length`, body = the exact uploaded bytes; `404` otherwise. No new Python symbol other tasks depend on.

- [x] **Step 1: Write the failing test**

Create `backend/tests/integration/test_document_file.py`:

```python
"""GET /documents/{id}/file -- the source PDF, proxied through the API.

Proxied, not presigned: SP-18 sec 2 decision 1 (the browser would need MinIO
CORS configured to fetch localhost:9000 directly). An embedded PDF viewer is
the case that decision was made for.
"""

import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings

pytestmark = pytest.mark.integration

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


@pytest.fixture
def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    @asynccontextmanager
    async def _make():
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            # `artifacts` is the default bucket the app opens (this fixture
            # never sets FDI_S3_BUCKET). Lifespan does not create it -- only
            # /ready's probe calls ensure_bucket -- so intake's put_bytes
            # would 404 without this.
            await app.state.artifact_store.ensure_bucket()
            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client

    return _make


async def test_file_returns_the_uploaded_pdf_bytes_inline(make_client) -> None:
    async with make_client() as client:
        upload = await client.post(
            "/documents",
            files={"file": ("ACME — FY24.pdf", PDF_BYTES, "application/pdf")},
            data={"doc_key": f"acme-{uuid.uuid4().hex}"},
        )
        assert upload.status_code == 201
        doc_id = upload.json()["id"]

        r = await client.get(f"/documents/{doc_id}/file")

    assert r.status_code == 200
    assert r.content == PDF_BYTES
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-length"] == str(len(PDF_BYTES))
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("inline;")
    # Non-ASCII survives via RFC 5987, and the plain filename= stays ASCII-safe.
    assert f"filename*=UTF-8''{quote('ACME — FY24.pdf')}" in disposition
    assert 'filename="ACME _ FY24.pdf"' in disposition


async def test_file_does_not_advertise_range_support(make_client) -> None:
    """Range support is deliberately deferred (ArtifactStore reads whole
    bodies). Emitting no Accept-Ranges is the correct signal -- advertising
    `bytes` while ignoring the header is what makes a viewer render garbage."""
    async with make_client() as client:
        upload = await client.post(
            "/documents",
            files={"file": ("a.pdf", PDF_BYTES, "application/pdf")},
            data={"doc_key": f"acme-{uuid.uuid4().hex}"},
        )
        doc_id = upload.json()["id"]
        r = await client.get(f"/documents/{doc_id}/file")
    assert "accept-ranges" not in r.headers


async def test_file_unknown_document_is_404(make_client) -> None:
    async with make_client() as client:
        r = await client.get(f"/documents/{uuid.uuid4()}/file")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


async def test_file_document_without_a_checksum_is_404(make_client, migrated_engine) -> None:
    """An early-intake row (RegistryRepository.create_document) has no
    checksum, so there is no content-addressed blob to serve."""
    from fdi.platform.db.engine import make_session_factory
    from fdi.platform.db.repository import RegistryRepository

    doc_id = str(uuid.uuid4())
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as s:
        await RegistryRepository(s).create_document(doc_id)
        await s.commit()

    async with make_client() as client:
        r = await client.get(f"/documents/{doc_id}/file")
    assert r.status_code == 404
    assert "no source file" in r.json()["detail"]
```

- [x] **Step 2: Run the test to verify it fails**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/integration/test_document_file.py -q`
Expected: FAIL, 4 failed with `assert 404 == 200` and `assert 405 == 404` — the path `/documents/{doc_id}/file` does not exist, so FastAPI never routes it.

- [x] **Step 3: Write the route**

Append to `backend/src/fdi/api/routes.py` immediately after `document_artifact` (line 452):

```python
def _content_disposition(filename: str | None) -> str:
    """`inline` + BOTH filename forms (RFC 6266).

    `filename=` must be a quoted ASCII token: a real filing named
    "ACME -- FY24.pdf" with an em dash puts a non-latin-1 byte into a header
    value, which the ASGI server rejects outright. So the plain form is
    sanitized (every non-ASCII / quote / control byte -> "_") and the true
    name is carried alongside in the RFC 5987 `filename*` form, which every
    current browser prefers when present. Absent filename -> "document.pdf".
    """
    name = filename or "document.pdf"
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


@router.get("/documents/{doc_id}/file")
async def document_file(doc_id: str, session: SessionDep, store: StoreDep) -> Response:
    """The source PDF, proxied through this API rather than presigned.

    SP-18 sec 2 decision 1: a browser fetching MinIO directly would need
    MinIO's own CORS configured, so artifact bodies come through the
    already-CORS'd API instead. An embedded PDF viewer is exactly that case,
    so it keeps the same posture -- `presigned_get_url` stays available on
    /jobs for direct downloads.

    HTTP range support is DEFERRED, not forgotten: `ArtifactStore.get_raw`
    is built around reading a whole body and then close()/release_conn()
    (see its own comment on what leaks once the body stops being read
    whole), so ranges need a new streaming `get_range` on the storage layer
    plus Range/If-Range parsing and 206/416 handling. Until then this
    deliberately emits NO `Accept-Ranges` header -- advertising `bytes` and
    then ignoring the header is what makes a viewer render garbage.
    """
    doc = await DocumentRepository(session).get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    if not doc.checksum:
        raise HTTPException(
            status_code=404, detail=f"no source file for document {doc_id} (no checksum)"
        )
    key = document_key(doc.checksum)
    if not await store.exists(key):
        raise HTTPException(
            status_code=404, detail=f"no source file for document {doc_id} at {key}"
        )
    data = await store.get_bytes(key)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition(doc.filename),
            "Content-Length": str(len(data)),
        },
    )
```

Add the two new imports to `backend/src/fdi/api/routes.py`: `from urllib.parse import quote` (after `import uuid`, keeping ruff's `I` ordering — stdlib block) and `document_key` to the existing intake import at line 95:

```python
from fdi.pipeline.intake import document_key, intake_document
```

- [x] **Step 4: Run the test to verify it passes**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/integration/test_document_file.py -q`
Expected: PASS, 4 passed.

- [x] **Step 5: Run the full gate set**

Run:
```bash
PYTHONUTF8=1 uv run --directory backend ruff check .
PYTHONUTF8=1 uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
PYTHONUTF8=1 uv run --directory backend pytest -m "not integration and not e2e" -q
```
Expected: all green.

- [x] **Step 6: Commit**

```bash
git add backend/src/fdi/api/routes.py backend/tests/integration/test_document_file.py
git commit -m "feat(api): GET /documents/{id}/file -- proxy the source PDF

Streams documents/{checksum}.pdf from the artifact bucket through the API,
not via a presigned MinIO URL: SP-18 sec 2 decision 1 keeps artifact bodies
on one CORS'd origin. Content-Type application/pdf, Content-Disposition
inline with both an ASCII-sanitized filename= and an RFC 5987 filename*.
Range support deferred with no Accept-Ranges header -- ArtifactStore reads
whole bodies, and advertising bytes without honoring Range renders garbage."
```

---

### Task 4: B4 — bounding regions on extraction results

Extracted fields carry `page`, `snippet`, `evidence_ids` but no geometry, so a UI cannot highlight the answer on the page. The geometry exists; it just has never been joined.

**The real mapping, worked out from the code — including where it is lossy.**

An `evidence_id` is `f"{doc_id}:{chunk_id}"` (`fdi/pipeline/extractor.py:113`). `build_chunks` (`fdi/core/retrieval.py:150-205`) emits exactly three chunk-id shapes:

| chunk_id | emitted by | geometry source | fidelity |
|---|---|---|---|
| `s{i}` | `build_chunks`, one per AST section | `provenance["node_provenance"]` entry with `node_id == "s{i}"` | **LOSSY** — see below |
| `t{i}r{j}` | `_table_chunks`, one per data row | `structure["tables"][i]["grid"]["cell_regions"][j]`, flattened over the row's cells | exact (per-cell geometry, Phase 2) |
| `t{i}` | `_table_chunks`, one capped whole-table chunk | `structure["tables"][i]["bounding_regions"]` | exact (whole table, one region per source page after a cross-page merge) |

**Where the join is lossy or absent — stated, not papered over:**

1. **`s{i}` returns the SECTION HEADING's box, not the section body's.** `build_graph` (`fdi/core/graph.py:126-134`) sets the `s{i}` node's `source_index` from the section's `heading_source_index`, and `build_provenance` (`fdi/core/provenance.py:89`) resolves that to exactly one OCR paragraph's `bounding_regions`. The chunk's *text*, however, is `heading + "\n" + "\n".join(paragraphs)`. The body paragraphs each have their own `s{i}p{j}` provenance node with their own boxes — but no chunk maps to a paragraph, so there is no non-invented way to know which paragraph a value came from. **Degraded behavior: return the heading's regions.** They correctly identify the section and the page; they under-cover the region. Do not synthesize a bounding box by unioning the paragraph nodes — that would claim a precision the extraction does not have.
2. **A section whose heading has no usable `source_index`** (non-int, out of range, or a paragraph with empty `bounding_regions`) is **omitted entirely** from `node_provenance` by `build_provenance`'s conservative policy. **Degraded behavior: `[]`.**
3. **Table nodes carry NO provenance entry at all.** `build_graph:190` emits `t{i}` nodes with `"source_index": None` (they have no position in `ocr["paragraphs"]`), so `build_provenance` skips them. This is why table geometry is read from `structure`, not from `provenance`. That is not a workaround — `structure`'s per-cell `cell_regions`/`cell_pages` (`fdi/core/table_grid.py:52-73, 90-92`) were added in Phase 2 explicitly to mirror `node_provenance`'s `page` + `bounding_regions` shape.
4. **`t{i}r{j}` where the row's cells carry no geometry** (OCR that omitted cell `boundingRegions`) falls back to the whole-table regions rather than returning `[]` — a page-level answer beats no answer, and the whole-table box is a true superset of the row.
5. **`f{k}` figure nodes and `s{i}p{j}` paragraph nodes never appear as chunk ids**, so they never appear in an `evidence_id`. Any id that does not match `s\d+`, `t\d+`, or `t\d+r\d+` — including an id the LLM hallucinated — resolves to `[]`.
6. **A missing `provenance` or `structure` artifact** (a document processed before those stages ran) resolves everything to `[]`. The route must not 500 on that.

**Decisions:**

1. **The join is a pure function in `fdi.core.provenance`.** Global Constraint 8 forbids `fdi.core` importing `fdi.pipeline`/`fdi.platform`, so it takes the already-read `provenance` and `structure` artifacts as plain `Mapping`s, exactly as `build_chunks` already does. The artifact reads live in `fdi.pipeline.extractor`.
2. **Always a list, never `null`.** `bounding_regions` is `[]` when unresolvable. One shape for the UI to branch on, and it matches `assemble_result`'s existing habit of returning `evidence_ids: []` rather than `None`.
3. **Never raises (decision 10 is binding).** The pure function is total by construction; the pipeline-side artifact reads are wrapped in `contextlib.suppress(Exception)` exactly like `hybrid_search`/`client.extract` already are, so a dead MinIO degrades this one enrichment, not the document's whole extraction batch.
4. **Regions are unioned across a field's `evidence_ids`, order-preserving, deduplicated** on `(page_number, polygon)`. A field cited from a table row and its whole-table chunk must not paint the same box twice.
5. **Read the two artifacts ONCE per `extract_document` call**, not per field — 198 fields would otherwise mean 396 MinIO round trips.

**Files:**
- Modify: `backend/src/fdi/core/provenance.py:1-7` (docstring), append `resolve_chunk_regions` / `resolve_evidence_regions`
- Modify: `backend/src/fdi/pipeline/extractor.py:131-143` (`extract_document`)
- Modify: `backend/src/fdi/api/schemas.py:434-437` (`ExtractSourceView`)
- Create: `backend/tests/unit/test_chunk_regions.py`
- Modify: `backend/tests/integration/test_extract_document.py` (fixture geometry + assertions)

**Interfaces:**
- Consumes: `FieldSpec` (Task 1) only indirectly, via `extract_document`'s existing signature — unchanged.
- Produces:
  ```python
  # fdi.core.provenance
  def resolve_chunk_regions(
      chunk_id: str,
      provenance: Mapping[str, object],
      structure: Mapping[str, object],
  ) -> list[dict[str, object]]: ...

  def resolve_evidence_regions(
      doc_id: str,
      evidence_ids: Sequence[object],
      provenance: Mapping[str, object],
      structure: Mapping[str, object],
  ) -> list[dict[str, object]]: ...
  ```
  Every extracted record's `source` gains `"bounding_regions": list[dict[str, object]]`.

- [x] **Step 1: Write the failing unit test**

Create `backend/tests/unit/test_chunk_regions.py`:

```python
"""The chunk_id -> geometry join, and every place it is lossy.

`s{i}` resolves through the graph node of the same id into the `provenance`
artifact -- but that node's source_index is the section's HEADING paragraph
(core.graph.build_graph), so the box covers the heading, not the body. Table
chunks have no provenance node at all (build_graph gives t{i} nodes
source_index=None, and build_provenance omits those), so `t{i}`/`t{i}r{j}`
resolve through `structure` instead -- per-cell for a row, whole-table for a
table. Everything else is [].
"""

from fdi.core.provenance import resolve_chunk_regions, resolve_evidence_regions

HEAD = [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
ROW = [{"page_number": 4, "polygon": [2, 2, 3, 2, 3, 3, 2, 3]}]
TABLE = [{"page_number": 4, "polygon": [0, 0, 9, 0, 9, 9, 0, 9]}]


def _provenance() -> dict[str, object]:
    return {
        "schema_version": 1,
        "node_provenance": [
            {"node_id": "doc", "page": 1, "bounding_regions": HEAD},
            {"node_id": "s0", "page": 1, "bounding_regions": HEAD},
            {"node_id": "s0p1", "page": 2, "bounding_regions": ROW},
        ],
    }


def _structure() -> dict[str, object]:
    return {
        "schema_version": 3,
        "tables": [
            {
                "bounding_regions": TABLE,
                "grid": {
                    "n_rows": 2,
                    "n_cols": 2,
                    "header_row_count": 1,
                    "column_headers": ["Particulars", "FY24"],
                    "grid": [["Particulars", "FY24"], ["Total assets", "12,345"]],
                    "cell_regions": [[[], []], [ROW, ROW]],
                    "cell_pages": [[None, None], [4, 4]],
                },
            },
        ],
    }


def test_section_chunk_resolves_to_its_heading_paragraph_box() -> None:
    assert resolve_chunk_regions("s0", _provenance(), _structure()) == HEAD


def test_section_chunk_with_no_provenance_entry_is_empty() -> None:
    """build_provenance OMITS a node whose heading paragraph has no usable
    bounding_regions -- a conservative policy this join must not paper over."""
    assert resolve_chunk_regions("s7", _provenance(), _structure()) == []


def test_table_row_chunk_resolves_to_that_row_s_cell_regions() -> None:
    """Deduplicated: both cells in row 1 carry the same region."""
    assert resolve_chunk_regions("t0r1", _provenance(), _structure()) == ROW


def test_table_row_chunk_without_cell_geometry_falls_back_to_the_table() -> None:
    structure = _structure()
    structure["tables"][0]["grid"]["cell_regions"] = [[[], []], [[], []]]
    assert resolve_chunk_regions("t0r1", _provenance(), structure) == TABLE


def test_whole_table_chunk_resolves_to_the_table_box() -> None:
    assert resolve_chunk_regions("t0", _provenance(), _structure()) == TABLE


def test_out_of_range_table_or_row_index_is_empty() -> None:
    assert resolve_chunk_regions("t9", _provenance(), _structure()) == []
    assert resolve_chunk_regions("t0r9", _provenance(), _structure()) == []


def test_paragraph_figure_and_garbage_ids_are_empty() -> None:
    """s{i}p{j} and f{k} nodes exist in the graph but are never chunk ids, so
    they never reach here from a real evidence_id; a hallucinated id must
    also degrade, not raise."""
    for chunk_id in ("s0p1", "f3", "", "nonsense", "s", "t", "trx", "s-1"):
        assert resolve_chunk_regions(chunk_id, _provenance(), _structure()) == []


def test_missing_artifacts_never_raise() -> None:
    for chunk_id in ("s0", "t0", "t0r1"):
        assert resolve_chunk_regions(chunk_id, {}, {}) == []
    assert resolve_chunk_regions("s0", {"node_provenance": "nope"}, {"tables": 7}) == []
    assert resolve_chunk_regions("t0", {}, {"tables": [None]}) == []
    assert resolve_chunk_regions("t0r1", {}, {"tables": [{"grid": "nope"}]}) == []


def test_resolve_evidence_regions_strips_the_doc_prefix_and_unions() -> None:
    out = resolve_evidence_regions(
        "d1", ["d1:s0", "d1:t0r1"], _provenance(), _structure()
    )
    assert out == HEAD + ROW


def test_resolve_evidence_regions_deduplicates_across_evidence_ids() -> None:
    """A field cited from both a row and its whole table must not paint two
    boxes for one answer -- but the row box and the table box are different,
    so only true repeats collapse."""
    out = resolve_evidence_regions(
        "d1", ["d1:t0r1", "d1:t0r1", "d1:t0"], _provenance(), _structure()
    )
    assert out == ROW + TABLE


def test_resolve_evidence_regions_tolerates_junk_ids() -> None:
    assert resolve_evidence_regions("d1", [], _provenance(), _structure()) == []
    assert resolve_evidence_regions("d1", [None, 7, "d1:"], _provenance(), _structure()) == []
    # A bare chunk id with no "{doc_id}:" prefix still resolves.
    assert resolve_evidence_regions("d1", ["s0"], _provenance(), _structure()) == HEAD
    # An id belonging to a DIFFERENT document is not this document's geometry.
    assert resolve_evidence_regions("d1", ["d2:s0"], _provenance(), _structure()) == []
```

- [x] **Step 2: Run the test to verify it fails**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_chunk_regions.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'resolve_chunk_regions' from 'fdi.core.provenance'`.

- [x] **Step 3: Write the pure join**

Append to `backend/src/fdi/core/provenance.py`:

```python
_SECTION_RE = re.compile(r"^s(\d+)$")
_TABLE_RE = re.compile(r"^t(\d+)$")
_TABLE_ROW_RE = re.compile(r"^t(\d+)r(\d+)$")


def _region_key(region: Mapping[str, object]) -> tuple[object, ...]:
    """A hashable identity for a bounding region: its page plus its polygon
    coordinates. Used only to deduplicate; never persisted."""
    polygon = region.get("polygon")
    coords = tuple(polygon) if isinstance(polygon, list) else ()
    return (region.get("page_number"), coords)


def _dedup_regions(regions: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[object, ...]] = set()
    out: list[dict[str, object]] = []
    for r in regions:
        key = _region_key(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _regions_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(r) for r in value if isinstance(r, Mapping)]


def _node_regions(provenance: Mapping[str, object], node_id: str) -> list[dict[str, object]]:
    entries = provenance.get("node_provenance")
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("node_id") != node_id:
            continue
        return _regions_list(entry.get("bounding_regions"))
    return []


def _table_at(structure: Mapping[str, object], index: int) -> Mapping[str, object] | None:
    tables = structure.get("tables")
    if not isinstance(tables, list) or index < 0 or index >= len(tables):
        return None
    table = tables[index]
    return table if isinstance(table, Mapping) else None


def _table_row_regions(table: Mapping[str, object], row: int) -> list[dict[str, object]]:
    """Every cell region on grid row `row`, flattened and deduplicated. A
    spanning cell repeats its regions into each cell it covers (see
    core.table_grid.canonicalize_table), so dedup is required, not tidy."""
    grid = table.get("grid")
    if not isinstance(grid, Mapping):
        return []
    cell_regions = grid.get("cell_regions")
    if not isinstance(cell_regions, list) or row < 0 or row >= len(cell_regions):
        return []
    row_cells = cell_regions[row]
    if not isinstance(row_cells, list):
        return []
    flat: list[dict[str, object]] = []
    for cell in row_cells:
        flat.extend(_regions_list(cell))
    return _dedup_regions(flat)


def resolve_chunk_regions(
    chunk_id: str,
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
) -> list[dict[str, object]]:
    """The bounding geometry behind ONE `chunks` record, by its chunk_id.

    Three id shapes exist (core.retrieval.build_chunks) and each resolves
    through a different artifact, because the graph does not carry table
    geometry at all:

    * `s{i}` -> the `provenance` artifact's node_provenance entry with the
      SAME id. LOSSY BY CONSTRUCTION: core.graph.build_graph sets the `s{i}`
      node's source_index from the section's `heading_source_index`, so this
      is the HEADING paragraph's box -- while the chunk's text is the heading
      plus every body paragraph. The body paragraphs have their own `s{i}p{j}`
      nodes with their own boxes, but no chunk maps to a paragraph, so there
      is no non-invented way to say which one a value came from. The heading
      box names the right section and the right page and under-covers the
      region; unioning the paragraph nodes to fake a fuller box would claim a
      precision the extraction does not have. A section whose heading has no
      usable geometry is OMITTED from node_provenance entirely (this module's
      conservative policy, unchanged) and therefore resolves to [].

    * `t{i}r{j}` -> `structure["tables"][i]["grid"]["cell_regions"][j]`,
      flattened across the row's cells. Falls back to the whole-table regions
      when the row has no per-cell geometry (older OCR): a page-level answer
      beats none, and the table box is a true superset of the row.

    * `t{i}` -> `structure["tables"][i]["bounding_regions"]` (one region per
      source page after a cross-page merge).

    Anything else -- `s{i}p{j}`, `f{k}`, a malformed or hallucinated id --
    resolves to []. Pure and total: no I/O, never raises, whatever the two
    artifacts contain (including being entirely absent).
    """
    row_match = _TABLE_ROW_RE.match(chunk_id)
    if row_match is not None:
        table = _table_at(structure, int(row_match.group(1)))
        if table is None:
            return []
        regions = _table_row_regions(table, int(row_match.group(2)))
        return regions or _dedup_regions(_regions_list(table.get("bounding_regions")))

    table_match = _TABLE_RE.match(chunk_id)
    if table_match is not None:
        table = _table_at(structure, int(table_match.group(1)))
        if table is None:
            return []
        return _dedup_regions(_regions_list(table.get("bounding_regions")))

    if _SECTION_RE.match(chunk_id) is not None:
        return _dedup_regions(_node_regions(provenance, chunk_id))

    return []


def resolve_evidence_regions(
    doc_id: str,
    evidence_ids: Sequence[object],
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
) -> list[dict[str, object]]:
    """Union of `resolve_chunk_regions` over an extracted field's
    evidence_ids, in citation order, deduplicated on (page_number, polygon).

    An evidence_id is `f"{doc_id}:{chunk_id}"` (fdi.pipeline.extractor). A
    bare chunk id with no prefix is accepted too; an id prefixed with a
    DIFFERENT document's id is not this document's geometry and contributes
    nothing. Non-str entries are skipped. Pure, total, never raises."""
    prefix = f"{doc_id}:"
    out: list[dict[str, object]] = []
    for raw in evidence_ids:
        if not isinstance(raw, str) or not raw:
            continue
        if raw.startswith(prefix):
            chunk_id = raw[len(prefix):]
        elif ":" in raw:
            continue
        else:
            chunk_id = raw
        out.extend(resolve_chunk_regions(chunk_id, provenance, structure))
    return _dedup_regions(out)
```

Update the imports and docstring at the top of `backend/src/fdi/core/provenance.py` — replace lines 1-10 with:

```python
"""Provenance: trace every SP-9 graph node back to its source OCR paragraph's
page and bounding geometry, and resolve a retrieval chunk id to the geometry
behind it. Pure, deterministic; no I/O.

Node resolution is a DIRECT INDEX LOOKUP: a `section`/`paragraph` node carries
its origin OCR paragraph's position (`source_index`, threaded from `unified` ->
`ast` -> `graph` by Phase 2), so `ocr["paragraphs"][source_index]` is the
node's own paragraph -- never a text/label match, never ambiguous.

Chunk resolution (`resolve_chunk_regions`, `resolve_evidence_regions`) is the
read side an answer-highlighting UI needs. It reads `provenance` and
`structure` as plain Mappings -- no import of fdi.core.retrieval/graph/
table_grid, no import of fdi.pipeline.*/fdi.platform.* -- and is explicit
about where the join is lossy; see `resolve_chunk_regions`'s docstring."""

import re
from collections.abc import Mapping, Sequence
```

- [x] **Step 4: Run the unit test to verify it passes**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_chunk_regions.py -q`
Expected: PASS, 11 passed.

- [x] **Step 5: Wire it into `extract_document`**

Replace `backend/src/fdi/pipeline/extractor.py:131-143` (`extract_document`) with:

```python
async def _read_latest_artifact(
    ctx: ExtractContext, stage: str
) -> Mapping[str, object]:
    """The document's latest `stage` artifact body, or `{}` if there is no
    completed run, no artifact key, or the read fails. Never raises
    (decision 10): geometry is an enrichment, and a dead MinIO must degrade
    the highlight, not the document's whole extraction batch."""
    body: Mapping[str, object] = {}
    with contextlib.suppress(Exception):
        run = await RegistryRepository(ctx.session).latest_run(ctx.doc_id, stage)
        if run is not None and run.artifact_key is not None:
            raw = await ctx.store.get_json(run.artifact_key)
            if isinstance(raw, Mapping):
                body = raw
    return body


def _attach_regions(
    record: dict[str, object],
    *,
    doc_id: str,
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
) -> dict[str, object]:
    """Add `source.bounding_regions` to one assemble_result record.

    Always a list -- [] when unresolvable -- so the UI branches on one shape,
    matching assemble_result's own habit of returning `evidence_ids: []`
    rather than None. Never raises."""
    source = record.get("source")
    if not isinstance(source, dict):
        return record
    evidence_ids = source.get("evidence_ids")
    ids: Sequence[object] = evidence_ids if isinstance(evidence_ids, list) else []
    source["bounding_regions"] = resolve_evidence_regions(
        doc_id, ids, provenance, structure
    )
    return record


async def extract_document(
    *, ctx: ExtractContext, fields: Sequence[FieldSpec], client: ExtractorClient
) -> dict[str, object]:
    """Loop `fields`, calling extract_field for each; always returns one
    record per field, never raises (decision 10). Empty `fields` ->
    field_count 0, fields []. `doc_key`/`module` are NOT part of this
    return -- the route (which already holds both from the request body)
    attaches them; see fdi.api.routes.extract_document_route (Task 5).

    Each record's `source` additionally carries `bounding_regions`: the
    geometry behind its cited chunks, joined by the pure
    fdi.core.provenance.resolve_evidence_regions. The two artifacts it needs
    are read ONCE here, not per field -- the committed schema has 198 fields,
    so a per-field read would be 396 MinIO round trips for one request."""
    provenance = await _read_latest_artifact(ctx, "provenance")
    structure = await _read_latest_artifact(ctx, "structure")

    results = [
        _attach_regions(
            await extract_field(ctx=ctx, field=field, client=client),
            doc_id=ctx.doc_id, provenance=provenance, structure=structure,
        )
        for field in fields
    ]
    return {
        "schema_version": 1, "doc_id": ctx.doc_id,
        "field_count": len(results), "fields": results,
    }
```

Extend the imports at the top of `backend/src/fdi/pipeline/extractor.py` — add `resolve_evidence_regions` and `RegistryRepository`:

```python
from fdi.core.provenance import resolve_evidence_regions
```
(placed after the `fdi.core.extractor` import block, keeping ruff `I` ordering) and

```python
from fdi.platform.db.repository import RegistryRepository
```
(placed after the `fdi.platform.storage.artifact_store` import).

- [x] **Step 6: Extend the view model**

Replace `backend/src/fdi/api/schemas.py:434-437` (`ExtractSourceView`) with:

```python
class BoundingRegionView(BaseModel):
    """One page-scoped bounding region, in the shape `ocr`/`provenance`
    already store: `page_number` plus Azure's flat coordinate list."""

    page_number: int | None = None
    polygon: list[object] = []


class ExtractSourceView(BaseModel):
    page: int | None
    snippet: str | None
    evidence_ids: list[str]
    # ALWAYS a list, [] when the chunk does not map or the provenance/
    # structure artifact is absent -- fdi.core.provenance.resolve_chunk_
    # regions is explicit about which chunk kinds are lossy. Defaulted so an
    # older stored extraction record still validates.
    bounding_regions: list[BoundingRegionView] = []
```

- [x] **Step 7: Extend the integration test**

In `backend/tests/integration/test_extract_document.py`, give the OCR fixture's paragraphs real geometry — replace the `"paragraphs"` entry of the `FakeOcrClient` payload with:

```python
        "paragraphs": [
            {
                "role": "sectionHeading", "content": "Overview",
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
            },
            {
                "content": f"ACME Ltd is a manufacturing company {marker}.",
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
            },
        ],
```

and append these assertions to the end of `test_extract_document_pools_keyword_candidates_only`:

```python
    # B4: geometry rides along on `source`, joined from the provenance
    # (s{i} -> graph node -> OCR paragraph) and structure (t{i}r{j} ->
    # per-cell regions, falling back to the whole table) artifacts.
    assert isinstance(f0["source"]["bounding_regions"], list)
    assert f1["source"]["bounding_regions"] == [{"page_number": 1, "polygon": []}]
    # The unmatchable field cited nothing, so there is nothing to highlight --
    # [] rather than null, and never an exception (decision 10).
    assert f2["source"]["bounding_regions"] == []
```

Also extend the blank-query assertion at the end of `test_extract_field_blank_query_pool_is_empty_and_well_formed` — note `extract_field` is called directly there, so it returns the *un*-enriched record and the expected dict is unchanged. Add one line above the existing `assert out == {...}` to make that explicit:

```python
    # extract_field is the pre-enrichment half: bounding_regions is attached
    # by extract_document, which is what reads the two artifacts.
```

- [x] **Step 8: Run the tests to verify they pass**

Run:
```bash
PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_chunk_regions.py -q
PYTHONUTF8=1 uv run --directory backend pytest tests/integration/test_extract_document.py -q
```
Expected: 11 passed; 2 passed.

- [x] **Step 9: Run the full gate set**

Run:
```bash
PYTHONUTF8=1 uv run --directory backend ruff check .
PYTHONUTF8=1 uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
PYTHONUTF8=1 uv run --directory backend pytest -m "not integration and not e2e" -q
```
Expected: all green. In particular lint-imports must still report `Contracts: 1 kept, 0 broken.` — the new pure function is in `core` and imports nothing from `pipeline`/`platform`.

- [x] **Step 10: Commit**

```bash
git add backend/src/fdi/core/provenance.py backend/src/fdi/pipeline/extractor.py backend/src/fdi/api/schemas.py backend/tests/unit/test_chunk_regions.py backend/tests/integration/test_extract_document.py
git commit -m "feat(core): bounding_regions on extracted fields via a chunk -> geometry join

resolve_chunk_regions maps a chunks-artifact id to its boxes: s{i} through
the provenance artifact's node of the same id, t{i}/t{i}r{j} through the
structure artifact's whole-table and per-cell regions (table graph nodes
carry source_index=None, so provenance omits them). Documented as lossy
where it is: s{i} yields the section HEADING's box, not the body's, because
no chunk maps to a paragraph. Always [] rather than null when unresolvable;
artifacts are read once per document and every read is suppressed, so the
never-raises contract holds."
```

---

### Task 5: B5 — schema read/write endpoints

The UI needs to edit the extraction schema. Today it is read from disk on every `POST /extract/document` (`routes.py:812-816, 851`) at `settings.extraction_schema_path` (`config.py:130-132`).

**Decision — where writes persist, and why not the repo file.**

Writing back to `docs/architecture/field-extraction-schema.json` is wrong for a containerized API, for three independent reasons:

1. `backend/Dockerfile:39` **COPYs the schema into the image at build time**. A write inside the container mutates a layer-owned file that vanishes on the next `docker compose up` — the edit silently disappears.
2. The runtime user is `fdi` (`Dockerfile:31`, `USER fdi`) and the file is copied `--chown=fdi:fdi` — so the write might *succeed*, which is worse: a per-container divergent schema that no two replicas agree on, and that no `git status` shows.
3. It is a **git-tracked source file**. An HTTP endpoint that rewrites the working tree is a supply-chain hazard and makes every deploy a merge conflict.

**Storage: a MinIO object at `schema/field-extraction-schema.json` in the existing artifacts bucket, with the on-disk file as the immutable v3.1.0 seed/fallback.** The bucket is already the durable, shared, replica-agnostic store every artifact uses; `ArtifactStore.put_json`/`get_json` already exist; nothing new to provision. Resolution order on read is: **stored object if present, else the on-disk seed**. Reads never write — a `GET` that seeds the bucket would make the first read of a fresh environment a mutation, and two concurrent cold reads a race. The seed is therefore genuinely immutable: it is only ever read.

**How `parse_field_specs`' caller resolves "current schema":** `extract_document_route` stops calling `_read_schema_file(schema_path)` and calls `load_schema(store=store, seed_path=schema_path)` instead. So a `PUT /schema` takes effect on the very next extraction, with no redeploy and no worker restart — the route is the only reader.

**Optimistic concurrency:** `PUT` carries `base_version`. If it differs from the current document's `version`, the write is rejected `409` with both versions in the detail, so two editors cannot silently clobber each other. On accept the server **stamps** the new `version` itself by bumping the patch component (`3.1.0` → `3.1.1`); a `version` in the request body is ignored. Server-stamped, because a client-chosen version is a second thing to validate and to disagree about, and monotonic patch bumps are exactly what an edit-in-a-UI produces.

**Files:**
- Modify: `backend/src/fdi/core/schema.py` (append `validate_extraction_schema`, `bump_patch_version`)
- Create: `backend/src/fdi/pipeline/schema_store.py`
- Modify: `backend/src/fdi/api/schemas.py` (append the four schema views)
- Modify: `backend/src/fdi/api/routes.py:812-816` (drop `_read_schema_file`), `:851-853` (use `load_schema`), and append the three routes
- Create: `backend/tests/unit/test_schema_validation.py`
- Create: `backend/tests/integration/test_schema_endpoints.py`

**Interfaces:**
- Consumes: `parse_field_specs` (Task 1) — the `field_count` on the validate response comes from it; `ArtifactStore.get_json`/`put_json`/`exists` (existing).
- Produces:
  ```python
  # fdi.core.schema
  def validate_extraction_schema(doc: object) -> list[str]: ...
  def bump_patch_version(version: str) -> str: ...

  # fdi.pipeline.schema_store
  SCHEMA_KEY: str = "schema/field-extraction-schema.json"
  async def load_schema(
      *, store: ArtifactStore, seed_path: str
  ) -> tuple[dict[str, object], str]: ...   # (document, "store" | "seed")
  async def save_schema(*, store: ArtifactStore, doc: Mapping[str, object]) -> None: ...
  ```

- [x] **Step 1: Write the failing unit test**

Create `backend/tests/unit/test_schema_validation.py`:

```python
"""Pure validation of a v3.1 field-extraction schema document.

The gate on PUT /schema: a UI that saves a structurally broken schema would
turn POST /extract/document into a silent no-op (exactly the regression this
program's Task 1 exists to fix), so the write must be refused, loudly, with
a JSON path per problem.
"""

import json
from pathlib import Path

from fdi.core.schema import bump_patch_version, validate_extraction_schema


def _valid() -> dict[str, object]:
    return {
        "version": "3.1.0",
        "modules": [
            {
                "id": 3, "key": "module_3_balance_sheet", "name": "Balance Sheet",
                "retrieval_units": [
                    {
                        "unit_id": "RU_BS",
                        "title": "Balance Sheet",
                        "retrieval_query": "Standalone Balance Sheet",
                        "locator_keywords": ["balance sheet"],
                        "fields": [
                            {
                                "field_name": "Total assets",
                                "aliases": ["Total assets"],
                                "data_type": "decimal",
                                "extraction_prompt": "Extract the total assets value.",
                            },
                        ],
                    },
                ],
            },
        ],
    }


def test_a_valid_document_has_no_errors() -> None:
    assert validate_extraction_schema(_valid()) == []


def test_the_committed_schema_file_is_valid() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "docs" / "architecture" / "field-extraction-schema.json"
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    assert validate_extraction_schema(doc) == []


def test_a_non_object_document_is_one_error() -> None:
    assert validate_extraction_schema([]) == ["schema must be a JSON object"]
    assert validate_extraction_schema("nope") == ["schema must be a JSON object"]
    assert validate_extraction_schema(None) == ["schema must be a JSON object"]


def test_a_bad_version_is_reported() -> None:
    doc = _valid()
    doc["version"] = "three point one"
    errors = validate_extraction_schema(doc)
    assert any('"version"' in e for e in errors)


def test_empty_modules_is_reported() -> None:
    assert '"modules" must be a non-empty list' in validate_extraction_schema(
        {"version": "3.1.0", "modules": []}
    )


def test_a_v2_flat_schema_is_rejected() -> None:
    """The exact shape that silently produced zero fields for months."""
    doc = {
        "version": "2.0.0",
        "modules": [{"id": 1, "key": "m1", "name": "M1", "fields": [{"field_name": "x"}]}],
    }
    errors = validate_extraction_schema(doc)
    assert any("retrieval_units" in e for e in errors)


def test_duplicate_unit_ids_are_reported() -> None:
    doc = _valid()
    unit = doc["modules"][0]["retrieval_units"][0]
    doc["modules"][0]["retrieval_units"] = [unit, dict(unit)]
    errors = validate_extraction_schema(doc)
    assert any("duplicated" in e for e in errors)


def test_a_blank_unit_retrieval_query_is_reported() -> None:
    doc = _valid()
    doc["modules"][0]["retrieval_units"][0]["retrieval_query"] = "   "
    errors = validate_extraction_schema(doc)
    assert any('"retrieval_query" must be a non-blank string' in e for e in errors)


def test_a_field_missing_required_keys_is_reported_by_path() -> None:
    doc = _valid()
    del doc["modules"][0]["retrieval_units"][0]["fields"][0]["extraction_prompt"]
    errors = validate_extraction_schema(doc)
    assert (
        'modules[0].retrieval_units[0].fields[0]."extraction_prompt" '
        "must be a non-empty string" in errors
    )


def test_an_unknown_data_type_is_reported() -> None:
    doc = _valid()
    doc["modules"][0]["retrieval_units"][0]["fields"][0]["data_type"] = "money"
    errors = validate_extraction_schema(doc)
    assert any("'money' is not one of" in e for e in errors)


def test_validation_never_raises_on_junk() -> None:
    for junk in ({"modules": [1, None, "x"]}, {"modules": [{"retrieval_units": [7]}]},
                 {"modules": [{"retrieval_units": [{"fields": [None]}]}]}):
        assert isinstance(validate_extraction_schema(junk), list)


def test_bump_patch_version() -> None:
    assert bump_patch_version("3.1.0") == "3.1.1"
    assert bump_patch_version("3.1.9") == "3.1.10"
    assert bump_patch_version("10.0.0") == "10.0.1"


def test_bump_patch_version_leaves_an_unparseable_version_alone() -> None:
    """Unreachable from PUT /schema (validation rejects a non-semver version
    before the bump), but total rather than raising."""
    assert bump_patch_version("v3") == "v3"
    assert bump_patch_version("") == ""
```

- [x] **Step 2: Run the test to verify it fails**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_schema_validation.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'bump_patch_version' from 'fdi.core.schema'`.

- [x] **Step 3: Write the pure validation**

Append to `backend/src/fdi/core/schema.py`:

```python
_DATA_TYPES: Final = frozenset(
    {"string", "integer", "decimal", "date", "enum", "text", "list", "boolean"}
)
_VERSION_RE: Final = re.compile(r"\d+\.\d+\.\d+")


def bump_patch_version(version: str) -> str:
    """"3.1.0" -> "3.1.1". A version that is not exactly three dot-separated
    integers is returned unchanged -- unreachable from PUT /schema (the
    validator rejects a non-semver `version` before the bump ever runs), but
    total rather than raising, like everything else in fdi.core."""
    parts = version.split(".")
    if len(parts) != 3:
        return version
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return version
    return f"{major}.{minor}.{patch + 1}"


def validate_extraction_schema(doc: object) -> list[str]:
    """Structural validation of a v3.1 field-extraction schema document.

    Returns one human-readable error string per problem, each prefixed with
    the JSON path it was found at; an empty list means valid. Pure and
    total: never raises, whatever `doc` is.

    This is the gate on PUT /schema, and it is deliberately structural
    rather than semantic: the failure it exists to prevent is a saved schema
    that PARSES to zero fields, which turns POST /extract/document into a
    silent no-op with a 200 response. So it checks exactly what
    parse_field_specs needs -- the modules -> retrieval_units -> fields
    nesting, a non-blank unit retrieval_query, and each field's
    field_name/data_type/extraction_prompt -- plus unit_id uniqueness (unit
    ids are how a UI addresses a unit) and a known data_type. It does NOT
    police prompt wording, aliases, examples, or the validation.rules
    strings: those are editorial, and refusing a save over them would make
    the endpoint unusable.
    """
    if not isinstance(doc, Mapping):
        return ["schema must be a JSON object"]

    errors: list[str] = []
    version = doc.get("version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        errors.append('"version" must be a semantic version string like "3.1.0"')

    modules = doc.get("modules")
    if not isinstance(modules, list) or not modules:
        errors.append('"modules" must be a non-empty list')
        return errors

    seen_unit_ids: set[str] = set()
    for mi, module in enumerate(modules):
        where = f"modules[{mi}]"
        if not isinstance(module, Mapping):
            errors.append(f"{where} must be an object")
            continue
        for key in ("key", "name"):
            value = module.get(key)
            if not isinstance(value, str) or not value:
                errors.append(f'{where}."{key}" must be a non-empty string')

        units = module.get("retrieval_units")
        if not isinstance(units, list) or not units:
            errors.append(f'{where}."retrieval_units" must be a non-empty list')
            continue

        for ui, unit in enumerate(units):
            uwhere = f"{where}.retrieval_units[{ui}]"
            if not isinstance(unit, Mapping):
                errors.append(f"{uwhere} must be an object")
                continue

            unit_id = unit.get("unit_id")
            if not isinstance(unit_id, str) or not unit_id:
                errors.append(f'{uwhere}."unit_id" must be a non-empty string')
            elif unit_id in seen_unit_ids:
                errors.append(f'{uwhere}."unit_id" {unit_id!r} is duplicated')
            else:
                seen_unit_ids.add(unit_id)

            query = unit.get("retrieval_query")
            if not isinstance(query, str) or not query.strip():
                errors.append(f'{uwhere}."retrieval_query" must be a non-blank string')

            fields = unit.get("fields")
            if not isinstance(fields, list) or not fields:
                errors.append(f'{uwhere}."fields" must be a non-empty list')
                continue

            for fi, field in enumerate(fields):
                fwhere = f"{uwhere}.fields[{fi}]"
                if not isinstance(field, Mapping):
                    errors.append(f"{fwhere} must be an object")
                    continue
                for key in ("field_name", "data_type", "extraction_prompt"):
                    value = field.get(key)
                    if not isinstance(value, str) or not value:
                        errors.append(f'{fwhere}."{key}" must be a non-empty string')
                data_type = field.get("data_type")
                if isinstance(data_type, str) and data_type and data_type not in _DATA_TYPES:
                    errors.append(
                        f'{fwhere}."data_type" {data_type!r} is not one of '
                        f"{sorted(_DATA_TYPES)}"
                    )

    return errors
```

Replace the import block at `backend/src/fdi/core/schema.py:1-11` with:

```python
"""Pure validation of artifact bodies and of the field-extraction schema
document. No I/O — belongs in fdi.core."""

import math
import re
from collections.abc import Mapping
from typing import Final


class NonFiniteFloatError(ValueError):
    """A float in an artifact body is NaN or +/-Inf, so the body is not valid JSON."""


_PRIMITIVES: Final = (str, bool, int, type(None))
```

- [x] **Step 4: Run the unit test to verify it passes**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_schema_validation.py -q`
Expected: PASS, 13 passed.

- [x] **Step 5: Write the failing integration test**

Create `backend/tests/integration/test_schema_endpoints.py`:

```python
"""GET/PUT /schema and POST /schema/validate.

Writes persist to a MinIO object, NOT back to the repo file: the Dockerfile
COPYs docs/architecture/field-extraction-schema.json into the image at build
time (backend/Dockerfile:39), so a write inside the container mutates a
layer-owned file that vanishes on the next `up` -- or, worse, succeeds and
leaves each replica with a different schema that no `git status` shows. The
on-disk file stays the immutable v3.1.0 seed, read only when the object is
absent.
"""

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    @asynccontextmanager
    async def _make(**overrides: str):
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
            # A per-test bucket, so one test's PUT cannot leak into another's
            # GET through the shared session-scoped MinIO container.
            "FDI_S3_BUCKET": f"schema-{uuid.uuid4().hex[:12]}",
        }
        env.update(overrides)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            # The bucket is NOT created by lifespan (only /ready's probe calls
            # ensure_bucket), and this fixture uses a fresh per-test bucket
            # name, so it must be made here or the first put_json 404s.
            await app.state.artifact_store.ensure_bucket()
            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client

    return _make


async def test_get_schema_falls_back_to_the_on_disk_seed(make_client) -> None:
    async with make_client() as client:
        r = await client.get("/schema")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "seed"
    assert body["version"] == "3.1.0"
    assert body["field_count"] == 198
    assert len(body["schema_document"]["modules"]) == 16


async def test_put_schema_persists_and_bumps_the_patch_version(make_client) -> None:
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        doc = current["schema_document"]
        doc["modules"][0]["retrieval_units"][0]["locator_keywords"].append("edited marker")

        put = await client.put(
            "/schema", json={"base_version": current["version"], "schema_document": doc}
        )
        assert put.status_code == 200
        assert put.json()["version"] == "3.1.1"
        assert put.json()["source"] == "store"

        after = (await client.get("/schema")).json()

    assert after["source"] == "store"
    assert after["version"] == "3.1.1"
    assert "edited marker" in after["schema_document"]["modules"][0][
        "retrieval_units"
    ][0]["locator_keywords"]


async def test_put_schema_with_a_stale_base_version_is_409(make_client) -> None:
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        r = await client.put(
            "/schema",
            json={"base_version": "1.0.0", "schema_document": current["schema_document"]},
        )
    assert r.status_code == 409
    assert "1.0.0" in r.json()["detail"]
    assert current["version"] in r.json()["detail"]


async def test_put_schema_rejects_an_invalid_document(make_client) -> None:
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        broken = {"version": "3.1.0", "modules": [{"key": "m", "name": "M", "fields": []}]}
        r = await client.put(
            "/schema", json={"base_version": current["version"], "schema_document": broken}
        )
        # The rejected write must not have landed.
        after = (await client.get("/schema")).json()
    assert r.status_code == 422
    assert any("retrieval_units" in e for e in r.json()["detail"]["errors"])
    assert after["source"] == "seed"


async def test_post_schema_validate_reports_without_writing(make_client) -> None:
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        ok = await client.post(
            "/schema/validate", json={"schema_document": current["schema_document"]}
        )
        bad = await client.post("/schema/validate", json={"schema_document": {"modules": []}})
        after = (await client.get("/schema")).json()

    assert ok.status_code == 200
    assert ok.json() == {
        "valid": True, "errors": [], "module_count": 16, "unit_count": 18, "field_count": 198,
    }
    assert bad.status_code == 200
    assert bad.json()["valid"] is False
    assert bad.json()["field_count"] == 0
    assert after["source"] == "seed"


async def test_extract_document_reads_the_stored_schema(make_client) -> None:
    """The whole point of making the schema writable: an edit takes effect on
    the next extraction with no redeploy. Proven by shrinking the schema to
    one module and checking /extract/document 404s on an unknown doc_key
    AFTER the schema resolves -- i.e. the stored copy was the one loaded."""
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        doc = current["schema_document"]
        doc["modules"] = doc["modules"][:1]
        put = await client.put(
            "/schema", json={"base_version": current["version"], "schema_document": doc}
        )
        assert put.status_code == 200
        assert put.json()["field_count"] < current["field_count"]

        r = await client.post("/extract/document", json={"doc_key": "no-such-key"})
    assert r.status_code in (404, 503)
```

- [x] **Step 6: Run the integration test to verify it fails**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/integration/test_schema_endpoints.py -q`
Expected: FAIL, 6 failed with `assert 404 == 200` — `/schema` is not a route yet.

- [x] **Step 7: Write the schema store**

Create `backend/src/fdi/pipeline/schema_store.py`:

```python
"""Where the field-extraction schema lives at runtime.

NOT the repo file. `backend/Dockerfile` COPYs
docs/architecture/field-extraction-schema.json into the image at BUILD time,
so a write to that path inside the container mutates a layer-owned file that
disappears on the next `docker compose up` -- and where the write does
succeed (the runtime user owns the copy), each replica ends up with a
different schema that no `git status` shows and no deploy reproduces. It is
also a git-tracked source file, so an HTTP endpoint that rewrites it makes
every deploy a merge conflict.

So the writable copy is an object in the artifact bucket, and the on-disk
file is the IMMUTABLE seed: read when the object is absent, never written,
never overwritten. Resolution is read-through, not read-and-seed -- a GET
that populated the bucket would make the first read of a fresh environment a
mutation, and two concurrent cold reads a race.

Pipeline layer (does I/O): imports platform (ArtifactStore) + core, same
shape as fdi.pipeline.tables. The route is the only reader; a PUT therefore
takes effect on the very next POST /extract/document, with no redeploy.
"""

import asyncio
import json
from collections.abc import Mapping

from fdi.platform.storage.artifact_store import ArtifactStore

SCHEMA_KEY = "schema/field-extraction-schema.json"


def _read_seed(seed_path: str) -> object:
    """Blocking file read, run off the event loop via asyncio.to_thread by
    the caller -- same posture as ArtifactStore's sync-minio-SDK calls."""
    with open(seed_path, encoding="utf-8") as f:
        return json.load(f)


async def load_schema(
    *, store: ArtifactStore, seed_path: str
) -> tuple[dict[str, object], str]:
    """(the current schema document, where it came from).

    "store" when the bucket holds an edited copy, "seed" when it falls back
    to the on-disk file. A stored object that is not a JSON object (corrupt
    or hand-written) falls back to the seed rather than serving a shape
    nothing can parse. An unreadable seed yields ({}, "seed") -- callers
    (parse_field_specs, validate_extraction_schema) are total on {}.
    """
    if await store.exists(SCHEMA_KEY):
        stored = await store.get_json(SCHEMA_KEY)
        if isinstance(stored, dict):
            return stored, "store"

    try:
        seed = await asyncio.to_thread(_read_seed, seed_path)
    except (OSError, ValueError):
        return {}, "seed"
    return (seed, "seed") if isinstance(seed, dict) else ({}, "seed")


async def save_schema(*, store: ArtifactStore, doc: Mapping[str, object]) -> None:
    """Persist `doc` as THE current schema. put_json canonicalizes, so two
    identical saves produce byte-identical objects."""
    await store.put_json(SCHEMA_KEY, dict(doc))
```

- [x] **Step 8: Add the view models**

Append to `backend/src/fdi/api/schemas.py`:

```python
class SchemaView(BaseModel):
    """The current field-extraction schema plus the counts a UI header
    shows. `source` is "store" (an edited copy in the artifact bucket) or
    "seed" (the immutable on-disk file, when nothing has been saved yet).
    The document field is named `schema_document`, not `schema`, because
    `schema` shadows a BaseModel attribute."""

    version: str
    source: str
    module_count: int
    unit_count: int
    field_count: int
    schema_document: dict[str, object]


class SchemaWriteRequest(BaseModel):
    base_version: str
    schema_document: dict[str, object]


class SchemaValidateRequest(BaseModel):
    schema_document: dict[str, object]


class SchemaValidationView(BaseModel):
    valid: bool
    errors: list[str]
    module_count: int
    unit_count: int
    field_count: int
```

- [x] **Step 9: Write the routes**

In `backend/src/fdi/api/routes.py`, delete `_read_schema_file` (lines 812-816) and replace the schema read inside `extract_document_route` (lines 851-853) with:

```python
    schema, _source = await load_schema(store=store, seed_path=schema_path)
    fields = parse_field_specs(schema, body.module)
```

Then append the three new routes to the end of the file:

```python
# --------------------------------------------------------------------------
# SP-19 (B5): the extraction schema is EDITABLE over HTTP.
#
# Writes persist to an object in the artifact bucket, never back to
# docs/architecture/field-extraction-schema.json: backend/Dockerfile COPYs
# that file into the image at build time, so an in-container write vanishes
# on the next `up` -- or succeeds and leaves each replica with a private
# schema. See fdi.pipeline.schema_store's module docstring. The on-disk file
# stays the immutable v3.1.0 seed. POST /extract/document reads through the
# same load_schema, so a PUT takes effect on the very next extraction.
# --------------------------------------------------------------------------


def _schema_counts(schema: Mapping[str, object]) -> tuple[int, int, int]:
    """(module_count, unit_count, field_count). field_count is what
    parse_field_specs ACTUALLY yields, not the schema's own `field_count`
    hints -- the number that matters is how many fields extraction will
    attempt, which is the number that silently went to zero when the schema
    moved to v3.1."""
    modules_raw = schema.get("modules")
    modules = [m for m in modules_raw if isinstance(m, Mapping)] if isinstance(
        modules_raw, list
    ) else []
    unit_count = 0
    for m in modules:
        units = m.get("retrieval_units")
        if isinstance(units, list):
            unit_count += sum(1 for u in units if isinstance(u, Mapping))
    return len(modules), unit_count, len(parse_field_specs(schema))


def _schema_view(schema: Mapping[str, object], source: str) -> SchemaView:
    version = schema.get("version")
    module_count, unit_count, field_count = _schema_counts(schema)
    return SchemaView(
        version=version if isinstance(version, str) else "",
        source=source,
        module_count=module_count,
        unit_count=unit_count,
        field_count=field_count,
        schema_document=dict(schema),
    )


@router.get("/schema", response_model=SchemaView)
async def get_schema(store: StoreDep, schema_path: ExtractionSchemaPathDep) -> SchemaView:
    """The current field-extraction schema document, whole.

    Read-through: the artifact bucket's copy if one has been saved, else the
    on-disk seed. This route never writes -- a GET that seeded the bucket
    would make the first read of a fresh environment a mutation, and two
    concurrent cold reads a race.
    """
    schema, source = await load_schema(store=store, seed_path=schema_path)
    return _schema_view(schema, source)


@router.put("/schema", response_model=SchemaView)
async def put_schema(
    body: SchemaWriteRequest, store: StoreDep, schema_path: ExtractionSchemaPathDep
) -> SchemaView:
    """Replace the schema. Validated, version-stamped, and concurrency-checked.

    `base_version` must equal the CURRENT document's version or the write is
    refused 409 with both versions named -- two people editing the same
    schema in two browser tabs must not silently clobber each other, and the
    loser needs to know what it should have been based on.

    The new version is stamped SERVER-side by bumping the patch component
    (3.1.0 -> 3.1.1); any `version` inside the submitted document is
    overwritten. A client-chosen version is a second thing to validate and to
    disagree about, and a monotonic patch bump is exactly what an edit-in-a-UI
    produces.

    A document that fails structural validation is refused 422 with one
    JSON-path-prefixed error per problem, and NOTHING is written: a saved
    schema that parses to zero fields would turn POST /extract/document into
    a silent no-op with a 200 response, which is the precise failure this
    endpoint's own program exists to have fixed once already.
    """
    current, _source = await load_schema(store=store, seed_path=schema_path)
    current_version = current.get("version")
    current_version_str = current_version if isinstance(current_version, str) else ""
    if body.base_version != current_version_str:
        raise HTTPException(
            status_code=409,
            detail=(
                f"schema was modified: base_version {body.base_version!r} is not the "
                f"current version {current_version_str!r} -- re-read GET /schema and reapply"
            ),
        )

    errors = validate_extraction_schema(body.schema_document)
    if errors:
        raise HTTPException(
            status_code=422, detail={"message": "schema is invalid", "errors": errors}
        )

    new_doc = dict(body.schema_document)
    new_doc["version"] = bump_patch_version(current_version_str)
    await save_schema(store=store, doc=new_doc)
    log.info(
        "schema.updated",
        base_version=body.base_version,
        version=new_doc["version"],
        field_count=len(parse_field_specs(new_doc)),
    )
    return _schema_view(new_doc, "store")


@router.post("/schema/validate", response_model=SchemaValidationView)
async def validate_schema_route(body: SchemaValidateRequest) -> SchemaValidationView:
    """Dry-run the PUT gate. Touches no storage, so a UI can validate on every
    keystroke without a write path anywhere near it."""
    errors = validate_extraction_schema(body.schema_document)
    module_count, unit_count, field_count = _schema_counts(body.schema_document)
    return SchemaValidationView(
        valid=not errors,
        errors=errors,
        module_count=module_count,
        unit_count=unit_count,
        field_count=field_count,
    )
```

Add the new imports to `backend/src/fdi/api/routes.py`:
- to the `fdi.api.schemas` block: `SchemaValidateRequest`, `SchemaValidationView`, `SchemaView`, `SchemaWriteRequest` (alphabetical, after `SearchResultsView`/`SearchResultView`)
- `from fdi.core.schema import bump_patch_version, validate_extraction_schema` (after the `fdi.core.ontology` import)
- `from fdi.pipeline.schema_store import load_schema, save_schema` (after `fdi.pipeline.retrieval`)
- `json` may now be unused at module level — if ruff `F401` flags it, remove `import json` from line 5.

- [x] **Step 10: Run the tests to verify they pass**

Run:
```bash
PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_schema_validation.py -q
PYTHONUTF8=1 uv run --directory backend pytest tests/integration/test_schema_endpoints.py -q
```
Expected: 13 passed; 6 passed.

- [x] **Step 11: Run the full gate set**

Run:
```bash
PYTHONUTF8=1 uv run --directory backend ruff check .
PYTHONUTF8=1 uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
PYTHONUTF8=1 uv run --directory backend pytest -m "not integration and not e2e" -q
```
Expected: all green; lint-imports `Contracts: 1 kept, 0 broken.` (`fdi.pipeline.schema_store` imports only `fdi.platform`, which is the layer below it).

- [x] **Step 12: Commit**

```bash
git add backend/src/fdi/core/schema.py backend/src/fdi/pipeline/schema_store.py backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/unit/test_schema_validation.py backend/tests/integration/test_schema_endpoints.py
git commit -m "feat(api): GET/PUT /schema + POST /schema/validate -- the schema is editable

Writes persist to schema/field-extraction-schema.json in the artifact
bucket, NOT back to the repo file: the Dockerfile COPYs that file in at
build time, so an in-container write vanishes on the next up -- or succeeds
and gives each replica a private schema. The on-disk file becomes the
immutable v3.1.0 seed, read through only when the object is absent.
POST /extract/document resolves the same way, so an edit lands on the next
extraction with no redeploy. PUT is guarded by structural validation
(refusing a schema that would parse to zero fields) and by optimistic
concurrency on base_version, with the new version stamped server-side."
```

---

### Task 6: B6 — configurable CORS origins

`_DEV_ORIGINS = ["http://localhost:3000"]` is hardcoded at `backend/src/fdi/api/app.py:164` and passed to `CORSMiddleware` at `:181`. Its own comment already names the fix: "a real deployment ... needs a configurable origin allowlist -- a Settings field carrying its own x-backend-env / .env.example / consistency-test ripple". This task does that ripple.

**Decisions:**

1. **A `list[str]` Settings field, default `["http://localhost:3000"]`.** Same value as today, so nothing changes for a developer who sets nothing. pydantic-settings parses a complex-typed field from the environment as **JSON**, so the env var is `FDI_CORS_ORIGINS=["http://localhost:3000","https://fdi.example"]`.
2. **Never `["*"]` by default.** `tests/unit/test_cors.py::test_an_unlisted_origin_gets_no_grant` exists precisely because a wildcard passes the two happy-path tests while granting every site on the internet read access from a victim's browser. The setting makes the allowlist configurable, not permissive.
3. **The settings read inside `create_app()` must be guarded.** `fdi/api/app.py:189` ends with a module-level `app = create_app()`, so `create_app` runs at **import** time — during pytest collection, when `tests/conftest.py::_isolate_fdi_env` has deliberately cleared every `FDI_` variable. An unguarded `get_settings()` there would raise `ValidationError: 6 validation errors for Settings` and fail collection of the whole suite. So a `ValidationError` falls back to the historical default. This is narrow and precise: it catches the one failure an unconfigured environment produces, not everything.

**Files:**
- Modify: `backend/src/fdi/platform/config.py:118` (after `entity_sim_threshold`)
- Modify: `backend/src/fdi/api/app.py:152-184`
- Modify: `.env.example:108` (after `FDI_ENTITY_SIM_THRESHOLD`)
- Modify: `docker/compose.yml:44` (after `FDI_ENTITY_SIM_THRESHOLD`)
- Modify: `backend/tests/unit/test_cors.py`, `backend/tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Settings.cors_origins: list[str]` (default `["http://localhost:3000"]`); `create_app()` signature unchanged.

- [x] **Step 1: Write the failing tests**

Append to `backend/tests/unit/test_config.py`:

```python
def test_cors_origins_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    assert Settings().cors_origins == ["http://localhost:3000"]


def test_cors_origins_from_environment_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-settings parses a complex-typed field from the environment as
    JSON, so the var is a JSON array -- not a comma-separated string."""
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(
        "FDI_CORS_ORIGINS", '["http://localhost:3000", "https://fdi.example"]'
    )
    assert Settings().cors_origins == ["http://localhost:3000", "https://fdi.example"]
```

Append to `backend/tests/unit/test_cors.py`:

```python
async def test_a_configured_origin_is_granted_and_the_default_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FDI_CORS_ORIGINS REPLACES the allowlist, it does not extend it.

    A deployment that serves the UI from https://fdi.example must not keep
    granting localhost:3000 -- an allowlist that only ever grows is not an
    allowlist. Built through create_app() with the env set, which is the only
    way the middleware's origins are decided.
    """
    for key, value in {
        "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
        "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
        "FDI_RESULT_BACKEND": "redis://h:6379/0",
        "FDI_S3_ENDPOINT": "h:9000",
        "FDI_S3_ACCESS_KEY": "ak",
        "FDI_S3_SECRET_KEY": "sk",
        "FDI_CORS_ORIGINS": '["https://fdi.example"]',
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        granted = await c.get("/health", headers={"Origin": "https://fdi.example"})
        revoked = await c.get("/health", headers={"Origin": DEV_ORIGIN})

    assert granted.headers["access-control-allow-origin"] == "https://fdi.example"
    assert "access-control-allow-origin" not in revoked.headers


async def test_an_unconfigured_environment_still_grants_the_dev_origin() -> None:
    """create_app() runs at IMPORT time (`app = create_app()` at the bottom of
    fdi/api/app.py) and in every test whose fixtures clear FDI_* -- so an
    unbuildable Settings must fall back to the historical dev default, not
    raise and fail collection of the whole suite."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health", headers={"Origin": DEV_ORIGIN})
    assert r.headers["access-control-allow-origin"] == DEV_ORIGIN
```

Add the imports `test_cors.py` now needs, right after `from fdi.api.app import create_app`:

```python
from fdi.platform.config import get_settings
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_cors.py tests/unit/test_config.py -q`
Expected: FAIL. `test_cors_origins_default` fails with `AttributeError: 'Settings' object has no attribute 'cors_origins'`; `test_cors_origins_from_environment_is_json` fails with `ValidationError: Unknown FDI_ environment variable(s): ['FDI_CORS_ORIGINS']`; `test_a_configured_origin_is_granted_and_the_default_is_replaced` fails the same way.

- [x] **Step 3: Add the setting**

Insert into `backend/src/fdi/platform/config.py` immediately after `entity_sim_threshold` (line 118):

```python
    # Browser origins allowed to read this API's responses cross-origin. The
    # default is the Next.js dev server (SP-1/SP-18) -- the exact value that
    # was hardcoded in fdi.api.app until SP-19 -- so an unset environment
    # behaves identically to before.
    #
    # This is a REPLACEMENT, not an extension: setting it drops localhost:3000
    # unless it is listed again. An allowlist that only ever grows is not an
    # allowlist, and a production deployment must be able to say "only this
    # origin".
    #
    # pydantic-settings parses a complex-typed field from the environment as
    # JSON, so the variable is a JSON array -- `FDI_CORS_ORIGINS=["https://
    # fdi.example"]`, not a comma-separated string. Never default this to
    # ["*"]: a wildcard passes every happy-path CORS test while granting every
    # site on the internet read access to this API from a victim's browser
    # (see tests/unit/test_cors.py's unlisted-origin test).
    cors_origins: list[str] = ["http://localhost:3000"]
```

- [x] **Step 4: Read the setting in the app factory**

Replace `backend/src/fdi/api/app.py:152-184` with:

```python
# The historical hardcoded value, now the FALLBACK rather than the policy: the
# Next.js dev server (SP-1/SP-18), which fetches this API cross-origin. Without
# CORS the browser lets the request go out but forbids the JS from READING the
# response -- even a simple GET -- so the UI's job view never populates and
# SP-1 done-criterion 6 silently fails, while curl/httpx (which enforce no
# origin policy) pass. That gap is exactly why the e2e httpx test cannot catch
# this and a real browser must.
_DEFAULT_CORS_ORIGINS = ["http://localhost:3000"]


def _cors_origins() -> list[str]:
    """`FDI_CORS_ORIGINS`, or the dev default when Settings cannot be built.

    The guard is structural, not defensive padding. `app = create_app()` at
    the bottom of this module means create_app runs at IMPORT time -- during
    pytest COLLECTION, where tests/conftest.py::_isolate_fdi_env has
    deliberately cleared every FDI_ variable so a developer's stray FDI_FOO=1
    cannot fail unrelated tests. An unguarded get_settings() here would raise
    `6 validation errors for Settings` and take the whole suite down with it.

    Narrow on purpose: ValidationError is the ONE failure an unconfigured
    environment produces (the required-field errors, and the unknown-FDI_-var
    ValueError that the model_validator raises and pydantic wraps). Anything
    else is a real bug and must still propagate.
    """
    try:
        return list(get_settings().cors_origins)
    except ValidationError:
        return list(_DEFAULT_CORS_ORIGINS)


def create_app() -> FastAPI:
    app = FastAPI(title="Financial Document Intelligence", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    # Must sit INSIDE ServerErrorMiddleware (which is always outermost) so that
    # the id is still bound when `errors._unhandled` renders a 500 and reaches
    # for `instance`. add_middleware() prepends, so this is exactly where a
    # user middleware goes; there is no arrangement that puts it outside.
    app.add_middleware(CorrelationIdMiddleware)
    # Added last, so it is the OUTERMOST user middleware: a preflight OPTIONS is
    # answered here and short-circuits before it can reach correlation binding or
    # routing, and the ACAO header is attached to every response on the way out,
    # including the ones the correlation middleware wrapped.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
```

Add the import to `backend/src/fdi/api/app.py` (after `from minio import Minio`, keeping ruff `I` ordering — third-party block):

```python
from pydantic import ValidationError
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `PYTHONUTF8=1 uv run --directory backend pytest tests/unit/test_cors.py tests/unit/test_config.py -q`
Expected: PASS.

- [x] **Step 6: Complete the config trio**

Insert into `.env.example` after line 108 (`FDI_ENTITY_SIM_THRESHOLD=0.85`):

```
# Browser origins allowed to read this API's responses cross-origin. A JSON
# ARRAY, not a comma-separated list: pydantic-settings parses a complex-typed
# field from the environment as JSON. This REPLACES the allowlist -- listing
# a production origin drops localhost:3000 unless it is listed again. Never
# set this to ["*"]: that grants every site on the internet read access to
# this API from a logged-in user's browser.
FDI_CORS_ORIGINS=["http://localhost:3000"]
```

Insert into `docker/compose.yml` after line 44 (`FDI_ENTITY_SIM_THRESHOLD: ${FDI_ENTITY_SIM_THRESHOLD}`):

```yaml
  FDI_CORS_ORIGINS: ${FDI_CORS_ORIGINS}
```

- [x] **Step 7: Run the full gate set**

Run:
```bash
PYTHONUTF8=1 uv run --directory backend ruff check .
PYTHONUTF8=1 uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
PYTHONUTF8=1 uv run --directory backend pytest -m "not integration and not e2e" -q
```
Expected: all green.

- [x] **Step 8: Commit**

```bash
git add backend/src/fdi/platform/config.py backend/src/fdi/api/app.py .env.example docker/compose.yml backend/tests/unit/test_cors.py backend/tests/unit/test_config.py
git commit -m "feat(config): FDI_CORS_ORIGINS -- configurable browser allowlist (trio)

The hardcoded [\"http://localhost:3000\"] becomes the DEFAULT of a Settings
field, so an unset environment behaves exactly as before and a deployment
can name its own origin. A JSON array (pydantic-settings parses complex
fields from env as JSON); replaces rather than extends the allowlist; never
defaults to \"*\". The read in create_app() is guarded on ValidationError
because create_app runs at import time and under fixtures that clear FDI_*."
```

---

## Final verification

- [ ] **Run every gate, including integration**

```bash
PYTHONUTF8=1 uv run --directory backend ruff check .
PYTHONUTF8=1 uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
PYTHONUTF8=1 uv run --directory backend pytest -m "not integration and not e2e" -q
PYTHONUTF8=1 uv run --directory backend pytest -m integration -q
```

Expected: ruff `All checks passed!`; mypy `Success: no issues found`; lint-imports `Contracts: 1 kept, 0 broken.`; both pytest runs green.

- [ ] **Smoke-check the revived extraction path against the real schema**

```bash
PYTHONUTF8=1 uv run --directory backend python -c "import json, sys; sys.path.insert(0, 'src'); from fdi.core.extractor import parse_field_specs; d = json.load(open('../docs/architecture/field-extraction-schema.json', encoding='utf-8')); print(len(parse_field_specs(d)))"
```

Expected: `198` (it printed `0` before Task 1).
