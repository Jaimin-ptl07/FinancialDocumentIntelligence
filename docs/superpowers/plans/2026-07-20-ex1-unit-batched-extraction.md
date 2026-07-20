# EX-1 Unit-Batched Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `extract_document` retrieve and extract once per Retrieval Unit instead of once per field, with the retrieval scoping that the audit harness proved necessary — cutting 198 LLM calls to 18 and closing the gap between measured (87.4%) and shipped accuracy.

**Architecture:** All decision logic lands in `fdi.core.extractor` as pure, total functions (no I/O, no config, no clock/RNG — the module's existing contract). `fdi.pipeline.extractor` gains one new orchestrator, `extract_unit`, which does the I/O; `extract_field` becomes a one-field wrapper over it and `extract_document` keeps its exact signature so `fdi.api.routes` is untouched.

**Tech Stack:** Python 3.13, dataclasses, pytest (`asyncio_mode` from `backend/pyproject.toml`), SQLAlchemy async, `uv` for env management.

**Spec:** `docs/superpowers/specs/2026-07-20-extractor-unit-batching-design.md`

## Global Constraints

- Every function in `fdi.core.extractor` MUST be total: **no raise, on any input shape, anywhere.** Malformed input degrades to a safe default.
- `fdi.core.extractor` MUST NOT import `fdi.pipeline.*`, `fdi.platform.*`, or any other `fdi.core` producer. It reads inputs as plain `Mapping`/`Sequence`.
- `extract_field` and `extract_document` MUST keep their current signatures. `fdi/api/routes.py` MUST NOT be modified.
- `extract_field` / `extract_unit` / `extract_document` MUST never raise (decision 10): every I/O step is guarded individually.
- New `FieldSpec` members MUST carry defaults, so hand-built `FieldSpec`s in existing tests keep constructing.
- Run all commands from the **worktree root**: `D:\FinancialDocumentIntelligence\.claude\worktrees\ex-1-unit-batched-extraction`.
- Fast test loop: `uv run --directory backend pytest -m "not integration and not e2e" -q`. Baseline is **513 passed, 9 skipped**.
- Reranker behaviour MUST NOT change. `rerank_candidates` is not modified by any task in this plan.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/src/fdi/core/extractor.py` | Pure extraction algorithms: specs, grouping, filtering, prompts, assembly | Modify |
| `backend/src/fdi/pipeline/extractor.py` | I/O orchestration: pool → filter → rerank → LLM → assemble | Modify |
| `backend/tests/unit/test_extractor.py` | Pure-function tests | Modify |
| `backend/tests/integration/test_extract_document.py` | End-to-end against real Postgres/MinIO | Modify |

---

### Task 1: `FieldSpec` carries `field_path`, `periods`, and `locator_keywords`

Unit batching needs three things the current `FieldSpec` throws away. `field_path` is the globally-unique key the batched LLM response is keyed by (`field_name` is not guaranteed unique across units). `periods` tells the model which fields are dual-period (145 of 198 fields carry `["current", "comparative_prior"]`). `locator_keywords` is needed alone for unit-level reranking — `FieldSpec.keywords` is `aliases + locator_keywords` deduplicated, so the unit's own keywords cannot be recovered from it.

**Files:**
- Modify: `backend/src/fdi/core/extractor.py:40-61` (`FieldSpec`), `:103-154` (`_field_spec_from`)
- Test: `backend/tests/unit/test_extractor.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `FieldSpec.field_path: str = ""`, `FieldSpec.periods: tuple[str, ...] = ()`, `FieldSpec.locator_keywords: tuple[str, ...] = ()`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/unit/test_extractor.py`:

```python
def test_parse_field_specs_carries_field_path_periods_and_locator_keywords() -> None:
    schema = {
        "modules": [
            {
                "id": 3,
                "key": "module_3_balance_sheet",
                "name": "Balance Sheet",
                "retrieval_units": [
                    {
                        "unit_id": "RU_BS",
                        "title": "Balance Sheet",
                        "locator_keywords": ["balance sheet", "total assets"],
                        "retrieval_query": "Balance Sheet",
                        "fields": [
                            {
                                "field_name": "Total equity",
                                "field_path": "module_3_balance_sheet.equity.total_equity",
                                "aliases": ["Total equity"],
                                "data_type": "decimal",
                                "periods": ["current", "comparative_prior"],
                                "extraction_prompt": "Extract total equity.",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    [spec] = parse_field_specs(schema)
    assert spec.field_path == "module_3_balance_sheet.equity.total_equity"
    assert spec.periods == ("current", "comparative_prior")
    assert spec.locator_keywords == ("balance sheet", "total assets")
    # keywords keeps its existing aliases-then-locator_keywords contract
    assert spec.keywords == ("Total equity", "balance sheet", "total assets")


def test_parse_field_specs_missing_field_path_and_periods_default_empty() -> None:
    schema = {
        "modules": [
            {
                "id": 2,
                "key": "m2",
                "name": "M2",
                "retrieval_units": [
                    {
                        "unit_id": "RU_X",
                        "title": "X",
                        "locator_keywords": [],
                        "retrieval_query": "x",
                        "fields": [
                            {
                                "field_name": "No path",
                                "aliases": [],
                                "data_type": "string",
                                "extraction_prompt": "Extract it.",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    [spec] = parse_field_specs(schema)
    assert spec.field_path == ""
    assert spec.periods == ()
    assert spec.locator_keywords == ()


def test_field_spec_constructs_without_the_new_members() -> None:
    """Hand-built FieldSpecs (tests, synthesizing callers) must keep working."""
    spec = FieldSpec(
        field_name="F", data_type="string", retrieval_query="q",
        keywords=("k",), extraction_prompt="p", enum_values=None,
        module_id=1, module_name="M",
    )
    assert spec.field_path == ""
    assert spec.periods == ()
    assert spec.locator_keywords == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run --directory backend pytest tests/unit/test_extractor.py -k "field_path or new_members" -q
```
Expected: FAIL — `AttributeError: 'FieldSpec' object has no attribute 'field_path'`

- [ ] **Step 3: Add the three members to `FieldSpec`**

In `backend/src/fdi/core/extractor.py`, append to the `FieldSpec` dataclass (after `statement_type`, keeping every new member defaulted so positional construction is unaffected):

```python
    scope: str | None = None
    statement_type: str | None = None
    field_path: str = ""
    periods: tuple[str, ...] = ()
    locator_keywords: tuple[str, ...] = ()
```

- [ ] **Step 4: Populate them in `_field_spec_from`**

In `_field_spec_from`, before the `return FieldSpec(...)`, add:

```python
    field_path = field.get("field_path")
    periods = _str_tuple(field.get("periods"))
    locator_keywords = _str_tuple(unit.get("locator_keywords"))
```

and extend the `return FieldSpec(...)` call with:

```python
        field_path=field_path if isinstance(field_path, str) else "",
        periods=periods,
        locator_keywords=locator_keywords,
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
uv run --directory backend pytest tests/unit/test_extractor.py -q
```
Expected: PASS, all tests in the file.

- [ ] **Step 6: Commit**

```bash
git add backend/src/fdi/core/extractor.py backend/tests/unit/test_extractor.py
git commit -m "feat(core): FieldSpec carries field_path, periods, locator_keywords

Unit-batched extraction keys the batched LLM response by field_path
(field_name is not unique across units), needs periods to tell the model
which fields are dual-period, and needs the unit's locator_keywords alone
for unit-level reranking -- FieldSpec.keywords is aliases+locator_keywords
deduplicated, so the unit's own keywords are not recoverable from it.

All three default to empty, so hand-built FieldSpecs keep constructing."
```

---

### Task 2: `UnitSpec` and grouping

**Files:**
- Modify: `backend/src/fdi/core/extractor.py`
- Test: `backend/tests/unit/test_extractor.py`

**Interfaces:**
- Consumes: `FieldSpec` with `locator_keywords` (Task 1)
- Produces:
  - `class UnitSpec` — frozen dataclass with `unit_id: str`, `unit_title: str`, `retrieval_query: str`, `locator_keywords: tuple[str, ...]`, `fields: tuple[FieldSpec, ...]`
  - `group_fields_by_unit(fields: Sequence[FieldSpec]) -> list[UnitSpec]`
  - `unit_top_k(n_fields: int) -> int`
  - `unit_needs_tables(unit: UnitSpec) -> bool`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/unit/test_extractor.py`:

```python
def _fs(name: str, *, unit_id: str = "U1", data_type: str = "decimal",
        query: str = "q", locators: tuple[str, ...] = ("kw",)) -> FieldSpec:
    return FieldSpec(
        field_name=name, data_type=data_type, retrieval_query=query,
        keywords=(name,), extraction_prompt=f"Extract {name}.", enum_values=None,
        module_id=1, module_name="M", unit_id=unit_id, unit_title=f"T-{unit_id}",
        field_path=f"m.{name}", locator_keywords=locators,
    )


def test_group_fields_by_unit_groups_and_preserves_order() -> None:
    fields = [_fs("a", unit_id="U1"), _fs("b", unit_id="U2"), _fs("c", unit_id="U1")]
    units = group_fields_by_unit(fields)
    assert [u.unit_id for u in units] == ["U1", "U2"]
    assert [f.field_name for f in units[0].fields] == ["a", "c"]
    assert [f.field_name for f in units[1].fields] == ["b"]


def test_group_fields_by_unit_carries_unit_metadata() -> None:
    fields = [_fs("a", unit_id="RU_BS", query="Balance Sheet", locators=("balance sheet",))]
    [unit] = group_fields_by_unit(fields)
    assert unit.unit_id == "RU_BS"
    assert unit.unit_title == "T-RU_BS"
    assert unit.retrieval_query == "Balance Sheet"
    assert unit.locator_keywords == ("balance sheet",)


def test_group_fields_by_unit_blank_unit_id_makes_singleton_units() -> None:
    """A hand-built FieldSpec has unit_id="" -- those must NOT all collapse
    into one bogus group."""
    fields = [_fs("a", unit_id=""), _fs("b", unit_id="")]
    units = group_fields_by_unit(fields)
    assert len(units) == 2
    assert [len(u.fields) for u in units] == [1, 1]


def test_group_fields_by_unit_empty_input() -> None:
    assert group_fields_by_unit([]) == []


def test_unit_top_k_clamps_low_and_high() -> None:
    assert unit_top_k(1) == 20      # max(20, ...) floor
    assert unit_top_k(2) == 20      # 2*2+10 = 14 -> floored to 20
    assert unit_top_k(40) == 60     # 40*2+10 = 90 -> capped to 60
    assert unit_top_k(0) == 20


def test_unit_top_k_linear_in_the_middle() -> None:
    assert unit_top_k(10) == 30     # 10*2+10
    assert unit_top_k(9) == 28      # 9*2+10


def test_unit_needs_tables_true_for_numeric_and_list() -> None:
    for dt in ("decimal", "integer", "list"):
        [unit] = group_fields_by_unit([_fs("a", data_type=dt)])
        assert unit_needs_tables(unit) is True, dt


def test_unit_needs_tables_false_for_all_narrative_unit() -> None:
    fields = [_fs("a", data_type="string"), _fs("b", data_type="date")]
    [unit] = group_fields_by_unit(fields)
    assert unit_needs_tables(unit) is False


def test_unit_needs_tables_true_if_any_field_is_numeric() -> None:
    fields = [_fs("a", data_type="string"), _fs("b", data_type="decimal")]
    [unit] = group_fields_by_unit(fields)
    assert unit_needs_tables(unit) is True
```

Add `UnitSpec`, `group_fields_by_unit`, `unit_needs_tables`, `unit_top_k` to the `from fdi.core.extractor import (...)` block at the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run --directory backend pytest tests/unit/test_extractor.py -k "group_fields or unit_top_k or needs_tables" -q
```
Expected: FAIL — `ImportError: cannot import name 'UnitSpec'`

- [ ] **Step 3: Implement**

In `backend/src/fdi/core/extractor.py`, after the `Candidate` dataclass:

```python
_TABLE_NEEDING_DATA_TYPES = frozenset({"decimal", "integer", "list"})

_UNIT_TOP_K_MIN = 20
_UNIT_TOP_K_MAX = 60


@dataclass(frozen=True)
class UnitSpec:
    """One Retrieval Unit: the fields sharing a single retrieval + a single
    LLM call. Schema v3.1's own retrieval_model states "Retrieve-once,
    extract-many. Retrieval is per Retrieval Unit, not per field."."""

    unit_id: str
    unit_title: str
    retrieval_query: str
    locator_keywords: tuple[str, ...]
    fields: tuple[FieldSpec, ...]


def group_fields_by_unit(fields: Sequence[FieldSpec]) -> list[UnitSpec]:
    """Group `fields` by `unit_id`, order-preserving on first appearance so
    the caller can restore input order afterwards. A field with a blank
    `unit_id` (a hand-built FieldSpec) becomes its OWN singleton unit rather
    than being merged with every other blank-id field into one bogus group.
    Unit metadata is taken from the first field of each group."""
    order: list[str] = []
    grouped: dict[str, list[FieldSpec]] = {}
    for i, field in enumerate(fields):
        key = field.unit_id if field.unit_id else f"\x00singleton:{i}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(field)

    units: list[UnitSpec] = []
    for key in order:
        members = grouped[key]
        head = members[0]
        units.append(UnitSpec(
            unit_id=head.unit_id,
            unit_title=head.unit_title,
            retrieval_query=head.retrieval_query,
            locator_keywords=head.locator_keywords,
            fields=tuple(members),
        ))
    return units


def unit_top_k(n_fields: int) -> int:
    """How many reranked candidates a unit's single LLM call receives:
    `max(20, min(60, n_fields * 2 + 10))`."""
    return max(_UNIT_TOP_K_MIN, min(_UNIT_TOP_K_MAX, n_fields * 2 + 10))


def unit_needs_tables(unit: UnitSpec) -> bool:
    """True when any field wants a number or a list. `list` is deliberate:
    subsidiary/JV disclosures are AOC-1 style TABLES, and treating them as
    narrative strips the very chunks those fields need."""
    return any(f.data_type in _TABLE_NEEDING_DATA_TYPES for f in unit.fields)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run --directory backend pytest tests/unit/test_extractor.py -q
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/fdi/core/extractor.py backend/tests/unit/test_extractor.py
git commit -m "feat(core): UnitSpec + group_fields_by_unit/unit_top_k/unit_needs_tables

Grouping is order-preserving so extract_document can restore input order.
A blank unit_id yields a singleton unit rather than collapsing every
hand-built FieldSpec into one bogus group.

unit_needs_tables counts 'list' as table-needing: AOC-1 subsidiary/JV
disclosures live in tables, and treating them as narrative strips the
chunks those fields need."
```

---

### Task 3: Consolidated-section detection and candidate filtering

The harness hardcoded `page >= 133`. That is document-specific and unshippable. This task derives the boundary from the chunks themselves.

**Files:**
- Modify: `backend/src/fdi/core/extractor.py`
- Test: `backend/tests/unit/test_extractor.py`

**Interfaces:**
- Consumes: `UnitSpec` (Task 2), `Candidate`
- Produces:
  - `find_consolidated_start_page(chunks: Sequence[Mapping[str, object]]) -> int | None`
  - `filter_candidates(candidates: Sequence[Candidate], *, consolidated_start_page: int | None, needs_tables: bool) -> list[Candidate]`

- [ ] **Step 1: Write the failing tests**

```python
def _chunk(chunk_id: str, text: str, page: int | None, heading: str | None = None) -> dict[str, object]:
    return {"chunk_id": chunk_id, "text": text, "page": page, "heading": heading}


def test_find_consolidated_start_page_finds_auditor_report_marker() -> None:
    chunks = [
        _chunk("s1", "INDEPENDENT AUDITOR'S REPORT", 20),
        _chunk("s2", "Balance Sheet as at 31-03-2024", 60),
        _chunk("s3", "INDEPENDENT AUDITOR'S REPORT ON CONSOLIDATED FINANCIAL STATEMENTS", 133),
        _chunk("s4", "Consolidated Balance Sheet", 140),
    ]
    assert find_consolidated_start_page(chunks) == 133


def test_find_consolidated_start_page_takes_the_minimum_page() -> None:
    chunks = [
        _chunk("s1", "Consolidated Balance Sheet", 140),
        _chunk("s2", "Consolidated Statement of Profit and Loss", 133),
    ]
    assert find_consolidated_start_page(chunks) == 133


def test_find_consolidated_start_page_matches_on_heading_too() -> None:
    chunks = [_chunk("s1", "body text", 133, heading="Consolidated Balance Sheet")]
    assert find_consolidated_start_page(chunks) == 133


def test_find_consolidated_start_page_none_when_no_consolidated_section() -> None:
    chunks = [
        _chunk("s1", "Balance Sheet as at 31-03-2024", 60),
        _chunk("s2", "Statement of Profit and Loss", 62),
    ]
    assert find_consolidated_start_page(chunks) is None


def test_find_consolidated_start_page_ignores_marker_with_no_page() -> None:
    chunks = [_chunk("s1", "Consolidated Balance Sheet", None)]
    assert find_consolidated_start_page(chunks) is None


def test_find_consolidated_start_page_malformed_input_never_raises() -> None:
    assert find_consolidated_start_page([]) is None
    assert find_consolidated_start_page([{}]) is None
    assert find_consolidated_start_page([{"text": 123, "page": "x"}]) is None  # type: ignore[list-item]


def _cand(evidence_id: str, text: str, page: int | None) -> Candidate:
    return Candidate(evidence_id=evidence_id, text=text, page=page, score=1.0, source_tool="hybrid")


def test_filter_candidates_drops_at_or_after_consolidated_boundary() -> None:
    cands = [
        _cand("d:s1", "standalone", 60),
        _cand("d:s2", "consolidated", 133),
        _cand("d:s3", "consolidated later", 150),
    ]
    out = filter_candidates(cands, consolidated_start_page=133, needs_tables=True)
    assert [c.evidence_id for c in out] == ["d:s1"]


def test_filter_candidates_boundary_none_is_a_no_op() -> None:
    cands = [_cand("d:s1", "a", 60), _cand("d:s2", "b", 400)]
    out = filter_candidates(cands, consolidated_start_page=None, needs_tables=True)
    assert len(out) == 2


def test_filter_candidates_keeps_candidates_with_no_page() -> None:
    cands = [_cand("d:s1", "no page", None)]
    out = filter_candidates(cands, consolidated_start_page=133, needs_tables=True)
    assert len(out) == 1


def test_filter_candidates_drops_table_chunks_for_narrative_units() -> None:
    cands = [
        _cand("d:s1", "narrative", 10),
        _cand("d:t3", "whole table", 11),
        _cand("d:t3r7", "table row", 11),
    ]
    out = filter_candidates(cands, consolidated_start_page=None, needs_tables=False)
    assert [c.evidence_id for c in out] == ["d:s1"]


def test_filter_candidates_keeps_table_chunks_when_needs_tables() -> None:
    cands = [_cand("d:s1", "narrative", 10), _cand("d:t3r7", "table row", 11)]
    out = filter_candidates(cands, consolidated_start_page=None, needs_tables=True)
    assert len(out) == 2


def test_filter_candidates_applies_both_filters() -> None:
    cands = [
        _cand("d:s1", "keep", 10),
        _cand("d:t1r1", "table dropped", 10),
        _cand("d:s2", "page dropped", 200),
    ]
    out = filter_candidates(cands, consolidated_start_page=133, needs_tables=False)
    assert [c.evidence_id for c in out] == ["d:s1"]


def test_filter_candidates_empty_input() -> None:
    assert filter_candidates([], consolidated_start_page=133, needs_tables=True) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run --directory backend pytest tests/unit/test_extractor.py -k "consolidated or filter_candidates" -q
```
Expected: FAIL — `ImportError: cannot import name 'find_consolidated_start_page'`

- [ ] **Step 3: Implement**

Add `import re` to the imports at the top of `backend/src/fdi/core/extractor.py`, then:

```python
# Markers are deliberately SPECIFIC. A bare "consolidated financial
# statements" also occurs inside standalone-section narrative (policies,
# related-party notes), and matching it would set the boundary too early and
# silently drop standalone content -- the exact failure this filter exists to
# prevent.
_CONSOLIDATED_MARKERS = (
    "independent auditor's report on consolidated financial statements",
    "independent auditors' report on consolidated financial statements",
    "consolidated balance sheet",
    "consolidated statement of profit and loss",
    "consolidated statement of cash flow",
)

_TABLE_CHUNK_RE = re.compile(r"^t\d+(r\d+)?$")


def find_consolidated_start_page(chunks: Sequence[Mapping[str, object]]) -> int | None:
    """First page of the document's Consolidated section, or None when it has
    none (making the filter a no-op).

    Schema v3.1 never requests consolidated data -- the string "consolidat"
    appears ZERO times in it -- while a consolidated section restates every
    primary statement with DIFFERENT values. Returning the minimum page across
    all marker matches lets the caller drop that whole region.

    Never raises: a malformed chunk, a non-str text/heading, or a non-int page
    is skipped."""
    pages: list[int] = []
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        page = chunk.get("page")
        if not isinstance(page, int) or isinstance(page, bool):
            continue
        parts = [chunk.get("heading"), chunk.get("text")]
        haystack = " ".join(p for p in parts if isinstance(p, str)).casefold()
        if not haystack:
            continue
        if any(marker in haystack for marker in _CONSOLIDATED_MARKERS):
            pages.append(page)
    return min(pages) if pages else None


def filter_candidates(
    candidates: Sequence[Candidate],
    *,
    consolidated_start_page: int | None,
    needs_tables: bool,
) -> list[Candidate]:
    """Scope a unit's pool before reranking.

    Drops (a) candidates at or after `consolidated_start_page` -- skipped
    entirely when it is None -- and (b) table chunks (`t{i}` / `t{i}r{j}`)
    when the unit has no numeric/list field, so short narrative paragraphs are
    not crowded out by unrelated numeric schedules sharing a keyword. A
    candidate with `page is None` is always kept: absence of a page is not
    evidence it is consolidated. Never raises."""
    out: list[Candidate] = []
    for c in candidates:
        if (
            consolidated_start_page is not None
            and isinstance(c.page, int)
            and c.page >= consolidated_start_page
        ):
            continue
        if not needs_tables:
            chunk_id = c.evidence_id.split(":", 1)[1] if ":" in c.evidence_id else c.evidence_id
            if _TABLE_CHUNK_RE.match(chunk_id):
                continue
        out.append(c)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run --directory backend pytest tests/unit/test_extractor.py -q
```
Expected: PASS

- [ ] **Step 5: Verify the detector against the REAL document**

The synthetic tests prove the logic; this proves the markers actually fire on
the STTL report, where the boundary is independently known to be page 133.

Run:
```bash
uv run --directory backend python -c "
import pickle, sys
sys.path.insert(0, 'src')
from fdi.core.extractor import find_consolidated_start_page
p = 'C:/Users/JAIMIN/AppData/Local/Temp/claude/D--FinancialDocumentIntelligence/a3033003-a400-4685-b813-45205bda603a/scratchpad/pool_cache.pkl'
chunks = pickle.load(open(p, 'rb'))['chunks']
print('chunks:', len(chunks))
print('consolidated_start_page:', find_consolidated_start_page(chunks))
"
```
Expected: `chunks: 2752` and `consolidated_start_page: 133`.

If it prints `None`, the markers do not match this document's wording — inspect
the real headings before proceeding:
```bash
uv run --directory backend python -c "
import pickle, re
p = 'C:/Users/JAIMIN/AppData/Local/Temp/claude/D--FinancialDocumentIntelligence/a3033003-a400-4685-b813-45205bda603a/scratchpad/pool_cache.pkl'
for c in pickle.load(open(p,'rb'))['chunks']:
    t = str(c.get('text',''))
    if 'consolidat' in t.casefold():
        print(c.get('page'), '|', t[:110].replace(chr(10),' '))
" | sort -n | head -20
```
Widen `_CONSOLIDATED_MARKERS` to the observed wording, keeping markers specific
enough not to match standalone narrative, then re-run Step 4 and this step.

If it prints a page **well below 133**, a marker is matching standalone
narrative — tighten it. Do not proceed until this step prints 133.

- [ ] **Step 6: Commit**

```bash
git add backend/src/fdi/core/extractor.py backend/tests/unit/test_extractor.py
git commit -m "feat(core): find_consolidated_start_page + filter_candidates

Replaces the audit harness's hardcoded 'page >= 133' with detection from
the chunks themselves. Schema v3.1 never requests consolidated data (the
string 'consolidat' appears zero times in it) while the consolidated
section restates every primary statement with DIFFERENT values, so the
whole region is dropped.

Markers are deliberately specific: a bare 'consolidated financial
statements' also occurs in standalone narrative, and matching it would set
the boundary too early and drop standalone content. Verified to return 133
on the STTL FY2023-24 report.

filter_candidates also drops t{i}/t{i}r{j} chunks for units with no
numeric/list field. Candidates with no page are always kept."
```

---

### Task 4: Batched prompt + result assembly, and the parentheses-negative rule

**Files:**
- Modify: `backend/src/fdi/core/extractor.py:233-272` (`build_extraction_messages`), `:285-317` (`assemble_result`)
- Test: `backend/tests/unit/test_extractor.py`

**Interfaces:**
- Consumes: `UnitSpec` (Task 2), `Candidate`
- Produces:
  - `build_unit_messages(unit: UnitSpec, candidates: Sequence[Candidate]) -> tuple[str, str]`
  - `assemble_unit_results(unit: UnitSpec, raw: Mapping[str, object], ranked: Sequence[Candidate]) -> list[dict[str, object]]`
  - Both single-field and batched system prompts include the shared rules, now with parentheses-negative.

- [ ] **Step 1: Write the failing tests**

```python
def test_shared_rules_state_parentheses_are_negative() -> None:
    field = _fs("a")
    system, _ = build_extraction_messages(field, [_cand("d:s1", "t", 1)])
    lowered = system.casefold()
    assert "parenthes" in lowered
    assert "negative" in lowered


def test_build_unit_messages_system_states_parentheses_rule() -> None:
    [unit] = group_fields_by_unit([_fs("a")])
    system, _ = build_unit_messages(unit, [_cand("d:s1", "t", 1)])
    lowered = system.casefold()
    assert "parenthes" in lowered
    assert "negative" in lowered


def test_build_unit_messages_user_lists_every_field_and_candidate() -> None:
    fields = [_fs("alpha"), _fs("beta")]
    [unit] = group_fields_by_unit(fields)
    _, user = build_unit_messages(unit, [_cand("d:s1", "row text", 7)])
    assert "m.alpha" in user
    assert "m.beta" in user
    assert "Extract alpha." in user
    assert "Extract beta." in user
    assert "[d:s1]" in user
    assert "row text" in user
    assert "page 7" in user


def test_build_unit_messages_marks_dual_period_fields() -> None:
    dual = FieldSpec(
        field_name="total_equity", data_type="decimal", retrieval_query="q",
        keywords=("k",), extraction_prompt="Extract equity.", enum_values=None,
        module_id=1, module_name="M", unit_id="U1", unit_title="T",
        field_path="m.total_equity", periods=("current", "comparative_prior"),
    )
    [unit] = group_fields_by_unit([dual])
    system, user = build_unit_messages(unit, [_cand("d:s1", "t", 1)])
    assert "current" in user and "prior" in user
    assert "field_path" in system


def test_assemble_unit_results_maps_response_by_field_path() -> None:
    fields = [_fs("alpha"), _fs("beta")]
    [unit] = group_fields_by_unit(fields)
    ranked = [_cand("d:s1", "t", 3)]
    raw = {
        "m.alpha": {"value": 1.5, "source": {"page": 9, "snippet": "s",
                                             "evidence_ids": ["d:s1"]}, "confidence": 0.9},
        "m.beta": {"value": "x", "source": {"page": 4, "snippet": "y",
                                            "evidence_ids": ["d:s1"]}, "confidence": 0.5},
    }
    out = assemble_unit_results(unit, raw, ranked)
    assert [r["field_name"] for r in out] == ["alpha", "beta"]
    assert out[0]["value"] == 1.5
    assert out[0]["confidence"] == 0.9
    assert out[1]["value"] == "x"


def test_assemble_unit_results_missing_field_path_degrades_to_well_formed() -> None:
    fields = [_fs("alpha"), _fs("beta")]
    [unit] = group_fields_by_unit(fields)
    ranked = [_cand("d:s1", "fallback text", 3)]
    raw = {"m.alpha": {"value": 1.0, "confidence": 0.8}}
    out = assemble_unit_results(unit, raw, ranked)
    assert len(out) == 2
    assert out[1]["field_name"] == "beta"
    assert out[1]["value"] is None
    assert out[1]["confidence"] == 0.0
    assert out[1]["source"]["page"] == 3


def test_assemble_unit_results_malformed_raw_yields_one_record_per_field() -> None:
    fields = [_fs("alpha"), _fs("beta")]
    [unit] = group_fields_by_unit(fields)
    for raw in ({}, {"m.alpha": "not a dict"}, {"unrelated": {"value": 1}}):
        out = assemble_unit_results(unit, raw, [])
        assert len(out) == 2
        assert all(r["value"] is None for r in out)
        assert all(r["confidence"] == 0.0 for r in out)


def test_assemble_unit_results_falls_back_to_field_name_key() -> None:
    """A model that echoes field_name instead of field_path still resolves."""
    [unit] = group_fields_by_unit([_fs("alpha")])
    out = assemble_unit_results(unit, {"alpha": {"value": 7, "confidence": 1.0}}, [])
    assert out[0]["value"] == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run --directory backend pytest tests/unit/test_extractor.py -k "parenthes or unit_messages or unit_results" -q
```
Expected: FAIL — `ImportError: cannot import name 'build_unit_messages'`

- [ ] **Step 3: Factor the shared rules and add the parentheses rule**

In `backend/src/fdi/core/extractor.py`, add above `build_extraction_messages`:

```python
_SHARED_EXTRACTION_RULES = (
    "Use ONLY the context given below -- never prior knowledge of a specific "
    "company or filing. "
    "When a context row/line shows more than one period column (financial "
    "statements commonly print the current year then the prior year side "
    "by side), the FIRST / most-recent period column is the CURRENT "
    "reporting-year value and the second is the prior-year comparative. "
    "When the same figure appears in more than one place, prefer the "
    "value from the PRIMARY financial statement (Balance Sheet, "
    "Statement of Profit and Loss, or Cash Flow Statement) over a note, "
    "schedule, or other disclosure that restates it. "
    "A number printed in PARENTHESES is NEGATIVE -- accounting notation. "
    "\"(12.97)\" means -12.97, and \"(1,234)\" means -1234. Return it as a "
    "negative number, never as its positive magnitude. "
    "The field may be labeled differently than its name in the "
    "document -- recognize LABEL SYNONYMS and extract a value whose "
    "MEANING matches even when the wording differs (e.g. \"closing cash "
    "balance\" = \"Cash and cash equivalents\" at year-end; \"revenue\" "
    "= \"Revenue from operations\"). "
    "Apart from parenthesis-negation, return the number exactly as printed "
    "in the context -- do not rescale, convert, or reinterpret units. "
)
```

Then rewrite `build_extraction_messages`'s `system` to reuse it:

```python
    system = (
        "You extract ONE field's value from financial-document context. "
        + _SHARED_EXTRACTION_RULES
        + "Respond with a single JSON object, nothing else, "
        "shaped exactly like this: "
        '{"value": <the extracted value, or null if not present in the '
        'context>, "source": {"page": <int or null>, "snippet": <verbatim '
        'supporting text or null>, "evidence_ids": [<the evidence_id(s) you '
        'used>]}, "confidence": <float 0.0-1.0>}. '
        "If the context does not contain the answer, set value to null and "
        "confidence to 0.0 -- never guess."
    )
```

> Note: the existing test `test_build_extraction_messages_system_states_extraction_guidance` asserts on this prompt's wording. Run it in Step 5 and, if it asserts on a phrase this edit reworded, update the assertion to match the new text — do not weaken it to a trivial check.

- [ ] **Step 4: Implement `build_unit_messages` and `assemble_unit_results`**

```python
def build_unit_messages(
    unit: UnitSpec, candidates: Sequence[Candidate]
) -> tuple[str, str]:
    """(system, user) for ONE batched call covering every field in `unit`.
    The response is a JSON object keyed by `field_path` -- globally unique,
    unlike field_name."""
    system = (
        "You extract MULTIPLE fields from ONE shared block of "
        "financial-document context, all belonging to the SAME Retrieval Unit "
        "(e.g. one statement or one note). "
        + _SHARED_EXTRACTION_RULES
        + "Respond with a single JSON object, nothing else, whose KEYS are "
        "exactly the field_path values listed below and whose values are "
        'shaped like: {"value": <extracted value -- for a dual-period field '
        'this MUST be {"current": <num or null>, "prior": <num or null>}, '
        'otherwise a scalar or null>, "source": {"page": <int or null>, '
        '"snippet": <verbatim supporting text or null>, "evidence_ids": '
        '[<ids you used>]}, "confidence": <float 0.0-1.0>}. '
        "Include EVERY field_path as a key. If a field is not present in the "
        "context, set its value to null (or {\"current\": null, \"prior\": "
        "null} if dual-period) and confidence to 0.0 -- never guess."
    )

    lines = [f"Retrieval Unit: {unit.unit_title}", "", "Fields to extract:"]
    for f in unit.fields:
        key = f.field_path or f.field_name
        dual = " (dual-period: return {current, prior})" if f.periods else ""
        aliases = f", aliases={list(f.keywords)}" if f.keywords else ""
        lines.append(
            f'- field_path="{key}" label="{f.field_name}" '
            f"data_type={f.data_type}{dual}{aliases}: {f.extraction_prompt}"
        )
    lines.append("")
    lines.append("Context:")
    for c in candidates:
        lines.append(f"[{c.evidence_id}] (page {c.page}): {c.text}")
    return system, "\n".join(lines)


def assemble_unit_results(
    unit: UnitSpec, raw: Mapping[str, object], ranked: Sequence[Candidate]
) -> list[dict[str, object]]:
    """One output_contract record per field in `unit`, in unit order.

    Looks each field up by `field_path`, falling back to `field_name` (a model
    that echoes the label still resolves). A missing or non-Mapping entry
    degrades through `assemble_result`'s existing fallbacks, so a partial or
    garbled batched response still yields one well-formed record per field.
    Never raises."""
    out: list[dict[str, object]] = []
    for field in unit.fields:
        entry: object = None
        if isinstance(raw, Mapping):
            if field.field_path:
                entry = raw.get(field.field_path)
            if entry is None:
                entry = raw.get(field.field_name)
        field_raw = entry if isinstance(entry, Mapping) else {}
        out.append(assemble_result(field, field_raw, ranked))
    return out
```

- [ ] **Step 5: Run the whole unit suite**

Run:
```bash
uv run --directory backend pytest tests/unit/test_extractor.py -q
```
Expected: PASS. If `test_build_extraction_messages_system_states_extraction_guidance` fails, update its assertion to the reworded phrasing (see the note in Step 3).

- [ ] **Step 6: Commit**

```bash
git add backend/src/fdi/core/extractor.py backend/tests/unit/test_extractor.py
git commit -m "feat(core): build_unit_messages + assemble_unit_results; parentheses rule

Batched prompt keyed by field_path (globally unique, unlike field_name),
marking dual-period fields explicitly. assemble_unit_results degrades a
partial or garbled response through assemble_result's existing fallbacks,
so every field still gets a well-formed record.

Extraction rules shared by the single-field and batched prompts are
factored into one constant, and gain an explicit accounting-parentheses
rule: (12.97) means -12.97. Targets the three observed sign inversions
(total_oci, other_comprehensive_income_cumulative, income_tax_on_oci)."
```

---

### Task 5: `extract_unit` orchestrator and rewiring

**Files:**
- Modify: `backend/src/fdi/pipeline/extractor.py:90-143`
- Test: `backend/tests/integration/test_extract_document.py` (Task 6)

**Interfaces:**
- Consumes: everything from Tasks 1–4
- Produces:
  - `extract_unit(*, ctx: ExtractContext, unit: UnitSpec, client: ExtractorClient, consolidated_start_page: int | None = None) -> list[dict[str, object]]`
  - `extract_field` and `extract_document` keep their exact current signatures.

- [ ] **Step 1: Replace the constants and both functions**

In `backend/src/fdi/pipeline/extractor.py`, extend the core import block:

```python
from fdi.core.extractor import (
    Candidate,
    FieldSpec,
    UnitSpec,
    assemble_result,
    build_extraction_messages,
    build_unit_messages,
    assemble_unit_results,
    filter_candidates,
    find_consolidated_start_page,
    group_fields_by_unit,
    rerank_candidates,
    unit_needs_tables,
    unit_top_k,
)
from fdi.pipeline.retrieval import _read_chunks, hybrid_search
from fdi.platform.db.repository import RegistryRepository
```

Replace the `_POOL_K` / `_EXTRACT_TOP_K` block and both functions with:

```python
_POOL_K = 60
_EXTRACT_TOP_K = 20

# The pool is over-fetched RELATIVE to top_k, not fixed. The filters run
# BETWEEN pooling and reranking, so a fixed 60 against an adaptive top_k of up
# to 60 would leave large units (RU_BS) with fewer post-filter candidates than
# top_k -- strictly less context than the harness supplied, a silent recall
# regression. top_k*2 mirrors the harness's fuse(k=top_k*2).
_POOL_OVERFETCH = 2


async def _pool_for(
    *, ctx: ExtractContext, query: str, k: int
) -> list[Candidate]:
    """hybrid_search -> Candidate list, scoped to this document/version.
    Never raises (decision 10): a failed search degrades to an empty pool."""
    hits: list[dict[str, object]] = []
    if query.strip():
        with contextlib.suppress(Exception):
            hits = await hybrid_search(
                session=ctx.session, store=ctx.store, embedder=ctx.embedder,
                vector_store=ctx.vector_store, query=query.strip(), k=k,
                filters={"doc_key": ctx.doc_key, "version": ctx.version},
            )
    return [
        Candidate(
            evidence_id=f"{h['doc_id']}:{h['chunk_id']}",
            text=str(h.get("text", "")),
            page=cast(int | None, h.get("page")),
            score=float(cast(float, h.get("score", 0.0))),
            source_tool="hybrid",
        )
        for h in hits
    ]


async def extract_unit(
    *,
    ctx: ExtractContext,
    unit: UnitSpec,
    client: ExtractorClient,
    consolidated_start_page: int | None = None,
) -> list[dict[str, object]]:
    """ONE retrieval + ONE LLM call for every field in `unit` -- schema v3.1's
    "Retrieve-once, extract-many". Returns one output_contract record per
    field, in unit order. Never raises (decision 10): a failed search or
    client call degrades THIS unit's fields to well-formed null records and
    never affects another unit."""
    top_k = unit_top_k(len(unit.fields))
    pool = await _pool_for(ctx=ctx, query=unit.retrieval_query, k=top_k * _POOL_OVERFETCH)

    scoped = filter_candidates(
        pool,
        consolidated_start_page=consolidated_start_page,
        needs_tables=unit_needs_tables(unit),
    )
    ranked = rerank_candidates(scoped, unit.locator_keywords, top_k=top_k)
    if not ranked:
        return assemble_unit_results(unit, {}, ranked)

    system, user = build_unit_messages(unit, ranked)
    raw: Mapping[str, object] = {}
    with contextlib.suppress(Exception):  # never raise (decision 10)
        raw = client.extract(system=system, user=user)
    return assemble_unit_results(unit, raw, ranked)


async def extract_field(
    *, ctx: ExtractContext, field: FieldSpec, client: ExtractorClient
) -> dict[str, object]:
    """Single-field extraction, preserved for callers that hold one FieldSpec.
    Pools with the single-field constants (_POOL_K / _EXTRACT_TOP_K) and the
    single-field prompt, exactly as before -- it is NOT routed through
    extract_unit, so its behaviour is unchanged. Never raises."""
    pool = await _pool_for(ctx=ctx, query=field.retrieval_query, k=_POOL_K)
    ranked = rerank_candidates(pool, field.keywords, top_k=_EXTRACT_TOP_K)
    if not ranked:
        return assemble_result(field, {}, ranked)

    system, user = build_extraction_messages(field, ranked)
    raw: Mapping[str, object] = {}
    with contextlib.suppress(Exception):  # never raise (decision 10)
        raw = client.extract(system=system, user=user)
    return assemble_result(field, raw, ranked)


async def _consolidated_start_page(ctx: ExtractContext) -> int | None:
    """Read this document's `chunks` artifact ONCE and derive the boundary.
    Never raises: a missing/unreadable artifact yields None, so extraction
    proceeds unscoped rather than failing."""
    with contextlib.suppress(Exception):
        chunks = await _read_chunks(RegistryRepository(ctx.session), ctx.store, ctx.doc_id)
        return find_consolidated_start_page(chunks)
    return None


async def extract_document(
    *, ctx: ExtractContext, fields: Sequence[FieldSpec], client: ExtractorClient
) -> dict[str, object]:
    """Group `fields` into Retrieval Units and run ONE retrieval + ONE LLM
    call per unit, then restore the caller's field order. Signature unchanged
    -- `doc_key`/`module` are still attached by the route. Never raises."""
    boundary = await _consolidated_start_page(ctx)

    units = group_fields_by_unit(fields)
    by_name: dict[int, dict[str, object]] = {}
    cursor = 0
    for unit in units:
        records = await extract_unit(
            ctx=ctx, unit=unit, client=client, consolidated_start_page=boundary,
        )
        for record in records:
            by_name[cursor] = record
            cursor += 1

    # restore input order: group_fields_by_unit is order-preserving, so
    # rebuilding the flat unit-order list and mapping it back by identity is
    # exact even when two fields share a field_name.
    unit_order: list[FieldSpec] = [f for unit in units for f in unit.fields]
    position = {id(f): i for i, f in enumerate(unit_order)}
    results = [by_name[position[id(f)]] for f in fields]

    return {
        "schema_version": 1, "doc_id": ctx.doc_id,
        "field_count": len(results), "fields": results,
    }
```

- [ ] **Step 2: Run the full fast suite**

Run:
```bash
uv run --directory backend pytest -m "not integration and not e2e" -q
```
Expected: PASS, at least 513 passed (new unit tests added on top).

- [ ] **Step 3: Typecheck and lint**

Run:
```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
```
Expected: both clean. If mypy objects to importing the private `_read_chunks`, rename it to `read_chunks` in `backend/src/fdi/pipeline/retrieval.py`, update its two internal call sites (`resolve_chunk`, `resolve_chunks`, `index_chunks_fts`), and re-run.

- [ ] **Step 4: Commit**

```bash
git add backend/src/fdi/pipeline/extractor.py backend/src/fdi/pipeline/retrieval.py
git commit -m "feat(pipeline): extract_unit -- one retrieval + one LLM call per unit

extract_document now groups FieldSpecs into Retrieval Units and runs one
retrieval + one LLM call each (198 -> 18 for schema v3.1), applying the
consolidated-section and narrative-only-table filters and an adaptive
top_k. Input field order is restored by identity, so the route contract
is unchanged.

The pool is over-fetched relative to top_k (top_k*2), not fixed: the
filters run between pooling and reranking, so a fixed 60 against an
adaptive top_k of up to 60 would leave large units with less context than
top_k -- a silent recall regression.

extract_field keeps the single-field constants and prompt and is NOT
routed through extract_unit, so its behaviour is byte-identical."
```

---

### Task 6: Integration test — one LLM call per unit

**Files:**
- Modify: `backend/tests/integration/test_extract_document.py`

**Interfaces:**
- Consumes: `extract_document`, `FakeExtractorClient` (Task 5)
- Produces: the regression that proves batching

- [ ] **Step 0: Fix the EXISTING test's scripted responses (it will break)**

`test_extract_document_pools_keyword_candidates_only` (line ~189) scripts
**single-field-shaped** results (`{"value": ..., "source": ..., "confidence": ...}`).
Under batching, `assemble_unit_results` looks each field up **by key** inside
the response, finds nothing, and degrades every value to `None` — so this test
fails on its value assertions.

Its call-count assertion is unaffected: the three fields sit in three distinct
units (`RU_OVERVIEW`, `RU_BS`, `RU_NOWHERE`), and `RU_NOWHERE`'s pool is empty
so it short-circuits before the client — still exactly 2 calls.

The test schema's fields have **no `field_path`**, so `FieldSpec.field_path` is
`""` and `assemble_unit_results` falls back to `field_name`. Re-key each
scripted result by its field name:

```python
    fake = FakeExtractorClient([
        {"Company overview text": {
            "value": "ACME is a manufacturer.",
            "source": {"page": 1, "snippet": "ACME Ltd is a manufacturing company",
                       "evidence_ids": [f"{doc.id}:s0"]},
            "confidence": 0.9}},
        {"Total assets": {
            "value": 12345.0,
            "source": {"page": None, "snippet": "Total assets 12,345",
                       "evidence_ids": [f"{doc.id}:t0r1"]},
            "confidence": 0.95}},
        # The third field's unit has an empty pool -- extract_unit
        # short-circuits BEFORE calling the client, so only 2 are consumed.
    ])
```

Leave every assertion below it unchanged — they must all still pass, including
`len(fake.calls) == 2`, `"12,345" in fake.calls[1][1]`, and the fully-`None`
record for `Unmatchable field`.

Do **not** touch `test_extract_field_blank_query_pool_is_empty_and_well_formed`:
`extract_field` keeps the single-field prompt and response shape.

Run it now to confirm the whole file is green before adding the new test:
```bash
uv run --directory backend pytest tests/integration/test_extract_document.py -q
```

- [ ] **Step 1: Write the failing test**

`FakeExtractorClient` records every call in `.calls`, so call count is directly assertable. Add to `backend/tests/integration/test_extract_document.py`:

Fixtures are `session_factory`, `store`, `processed_doc` — the same three the
existing tests in this file use. `processed_doc` yields `(doc, marker)`.

```python
async def test_extract_document_makes_one_llm_call_per_unit(
    session_factory, store, processed_doc
) -> None:
    """THE batching regression: 5 fields across 2 units must produce 2
    client.extract calls, not 5 -- and come back in INPUT order."""
    doc, marker = processed_doc

    def _f(name: str, unit_id: str) -> FieldSpec:
        return FieldSpec(
            field_name=name, data_type="string",
            retrieval_query=f"manufacturing company {marker}",
            keywords=(name,), extraction_prompt=f"Extract {name}.", enum_values=None,
            module_id=1, module_name="M", unit_id=unit_id, unit_title=f"T-{unit_id}",
            field_path=f"m.{name}", locator_keywords=("manufacturing",),
        )

    # interleaved on purpose: input order (a,b,c,d,e) != unit order (a,b,e,c,d)
    fields = [
        _f("a", "U1"), _f("b", "U1"), _f("c", "U2"), _f("d", "U2"), _f("e", "U1"),
    ]
    fake = FakeExtractorClient([
        {"m.a": {"value": "A", "confidence": 1.0},
         "m.b": {"value": "B", "confidence": 1.0},
         "m.e": {"value": "E", "confidence": 1.0}},
        {"m.c": {"value": "C", "confidence": 1.0},
         "m.d": {"value": "D", "confidence": 1.0}},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=None, vector_store=None,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        result = await extract_document(ctx=ctx, fields=fields, client=fake)

    assert len(fake.calls) == 2, f"expected 2 unit calls, got {len(fake.calls)}"
    assert result["field_count"] == 5
    # input order preserved, NOT unit order
    assert [r["field_name"] for r in result["fields"]] == ["a", "b", "c", "d", "e"]
    assert [r["value"] for r in result["fields"]] == ["A", "B", "C", "D", "E"]
```

Add `FieldSpec` to the `from fdi.core.extractor import (...)` block at the top
of the file if it is not already imported.

Both units share a `retrieval_query` that the `processed_doc` fixture's content
actually matches, so both pools are non-empty and both units reach the client —
if a pool were empty the unit would short-circuit and the count would be 1, and
the test would pass for the wrong reason.

- [ ] **Step 2: Run it**

Run:
```bash
uv run --directory backend pytest tests/integration/test_extract_document.py -q
```
Expected: PASS (requires Docker for Postgres/MinIO testcontainers). If Docker is
not running, start it — this test is the whole point of the task and must not be
skipped.

- [ ] **Step 3: Run the full suite**

Run:
```bash
uv run --directory backend pytest -q
```
Expected: no failures. Pre-existing skips (9) are fine.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/integration/test_extract_document.py
git commit -m "test(pipeline): assert one LLM call per Retrieval Unit

5 fields across 2 units must produce 2 client.extract calls, not 5, and
must come back in INPUT order rather than unit order."
```

---

## Verification (after all tasks)

Not a code task — the accuracy assessment, per the spec's §2 decision that no
scoring script is written. Run the system, dump its output, and read the JSON
against the schema's `example` values directly.

- [ ] Bring up the stack: `docker compose -f docker/compose.yml --profile core --profile vector up -d`
- [ ] Seed the `ocr` artifact from the cached Azure DI result (`D:\FinancialDocIntelligence\data\documents\0c0c02af-f5e5-4b67-9723-7c8360e80e4a\azure_result.json`) through `normalize_ocr`, written under the `ocr` stage's content-addressed key so `run_stage` sees a cache hit. **0 Azure DI calls.**
- [ ] Seed `semantic` as `{"schema_version": 1, "sections": []}` — the 87.4% benchmark was itself built this way, `section_type` is inert for extraction, and running it live costs 510 Azure OpenAI calls while making the chunk set differ from the benchmark's. **0 semantic calls.**
- [ ] Let the DAG run `structure` → `chunks` → `embedding` → `index`, then `POST /documents/{doc_id}/index_fts` (FTS indexing is NOT in the DAG — without it hybrid search silently degrades to vector-only).
- [ ] `POST /extract/document` and save the response JSON. **Expect 18 Azure OpenAI calls.**
- [ ] Read the output against `docs/architecture/field-extraction-schema.json` `example` values and report per-unit accuracy, noting that ISO-8601 dates (`2024-03-31`) are CORRECT against `dd-mm-yyyy` examples.

## Self-Review

**Spec coverage:** §3.1 `FieldSpec`/`UnitSpec`/`group_fields_by_unit`/`unit_top_k`/`unit_needs_tables` → Tasks 1–2. `find_consolidated_start_page`/`filter_candidates` → Task 3. `build_unit_messages`/`assemble_unit_results` → Task 4. §3.2 orchestration → Task 5. §3.3 parentheses rule → Task 4. §4 error handling → guarded in Tasks 5 (`contextlib.suppress` per I/O step, `_consolidated_start_page` → None). §5 testing → Tasks 1–6 plus the Verification block. §2 exclusions honoured: no scoring module, no `_UNIT_KEYWORD_BOOST`, `rerank_candidates` untouched.

**Gap found and closed:** the spec's §3.1 did not mention `field_path` or `periods`, but the batched response must be keyed by something globally unique (`field_name` is not) and the model must be told which fields are dual-period. Task 1 adds all three members rather than just `locator_keywords`.

**Breaking-change audit:** `test_extract_document_pools_keyword_candidates_only`
scripts single-field-shaped responses that batching would resolve to `None`.
Task 6 Step 0 re-keys them by field name and states why the call-count
assertion (2) is unaffected. `extract_field`'s tests are untouched by design.

**Type consistency:** `UnitSpec` members (`unit_id`, `unit_title`, `retrieval_query`, `locator_keywords`, `fields`) are used identically in Tasks 2–5. `filter_candidates` is keyword-only with `consolidated_start_page`/`needs_tables` at every call site. `assemble_unit_results(unit, raw, ranked)` positional in both Task 4 and Task 5.
