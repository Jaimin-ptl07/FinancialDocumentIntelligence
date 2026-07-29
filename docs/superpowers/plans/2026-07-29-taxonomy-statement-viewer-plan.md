# Taxonomy Statement Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a root-level `/taxonomy` page where a user picks an already-extracted filing and sees its figures as a Screener-style statement table per sheet (P&L/Assets/Liabilities/Equity), with the true 4-level parent/child hierarchy, current/prior values, confidence, and click-through to PDF evidence in a new tab.

**Architecture:** Backend wires the existing v4.1 hierarchical schema as the live extraction schema, exposes each extracted field's taxonomy `tag` on the wire (it was already computed internally but silently dropped by the response model), and adds a pure `tree_from_schema` function that nests `POST /factsheet/document`'s already-computed facts into the schema's tree shape. Frontend adds two new routes (`/taxonomy` picker, `/taxonomy/[docId]` viewer), a new `StatementTable` tree/table component, and a small extension to the existing `ReportViewer` so a provenance link opened in a new tab pre-selects the right field by tag.

**Tech Stack:** FastAPI/Pydantic/Python 3.13 (backend), Next.js 16 App Router/React 19/TypeScript/TanStack Query/Vitest (frontend). No new dependencies.

## Global Constraints

- No new third-party UI library — this app has a bespoke Tailwind v4 + CSS-custom-property design system ("Monochrome Ledger"); every new element must use existing classes/vars (`.sheet-flat`, `.eyebrow`, `.data`, `.btn`, `.input`, `--sp-*`, `--text-*`, `--ink*`, `--leaf*`, `--rule*`, `--tether`, `--flag`).
- `--tether`/`--flag` colors are reserved for provenance/needs-review signaling respectively — never introduce a new hue for status; use weight, glyphs (✓/!/Σ/▸/▾), or underline style instead, per the existing rule in `frontend/app/globals.css`.
- Frontend types in `frontend/lib/types.ts` mirror backend Pydantic/dict shapes field-for-field, no camelCase translation.
- Backend: never write to the on-disk schema file at runtime (`PUT /schema` persists to the artifact bucket only) — this plan's schema-path change is a build-time/env default change, not a runtime write.
- Existing tests must stay green; any assertion that hardcodes old v3.2.0 schema numbers reachable via the *default* Settings path must be updated in the same task that changes the default.
- Matching a field across two independently-fetched result sets (the taxonomy tree and a document's own extraction) must use its taxonomy `tag`, never `field_name` — the codebase repeatedly notes `field_name` collides across modules (e.g. `frontend/components/viewer/FieldList.tsx:18-23`), so name-based cross-referencing is a latent wrong-evidence bug, not just a style nit.

---

### Task 1: Wire the v4.1 schema as the live default

**Files:**
- Modify: `backend/src/fdi/platform/config.py:23` (`_SCHEMA_RELPATH`)
- Modify: `backend/Dockerfile:39` (COPY instruction)
- Modify: `.env:98`, `.env.example:137`, `docker/.env:98` (`FDI_EXTRACTION_SCHEMA_PATH`)
- Modify (test, update first): `backend/tests/unit/test_config.py:118-134` (`test_extraction_schema_path_default`)
- Modify (test, update first): `backend/tests/integration/test_schema_endpoints.py:60-139`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Settings.extraction_schema_path` now defaults to `docs/architecture/field-extraction-schema.v4_1.json` (verified: version `"4.1.0"`, `module_count=4`, `unit_count=4`, `field_count=402` via `parse_field_specs`). Every later task in this plan reads the schema through the existing `load_schema()`/`GET /schema`/`POST /extract/document`/`POST /factsheet/document` machinery unchanged — only the default *file* moves.

- [ ] **Step 1: Update the failing config test first**

Edit `backend/tests/unit/test_config.py`, in `test_extraction_schema_path_default` (around line 133):

```python
    assert p.as_posix().endswith(
        "docs/architecture/field-extraction-schema.v4_1.json"
    )
```

- [ ] **Step 2: Run it to confirm it fails against the current default**

Run: `cd backend && python3 -m pytest tests/unit/test_config.py::test_extraction_schema_path_default -v`
Expected: FAIL — the current default still ends with `field-extraction-schema.json` (no `.v4_1`).

- [ ] **Step 3: Point the default at the v4.1 file**

Edit `backend/src/fdi/platform/config.py` line 23:

```python
_SCHEMA_RELPATH = Path("docs") / "architecture" / "field-extraction-schema.v4_1.json"
```

- [ ] **Step 4: Update the three env files**

In `.env`, `.env.example`, and `docker/.env`, change the line:

```
FDI_EXTRACTION_SCHEMA_PATH=docs/architecture/field-extraction-schema.json
```

to:

```
FDI_EXTRACTION_SCHEMA_PATH=docs/architecture/field-extraction-schema.v4_1.json
```

- [ ] **Step 5: Update the Dockerfile COPY so the deployed image ships the right file**

Edit `backend/Dockerfile` line 39, from:

```dockerfile
COPY --chown=fdi:fdi docs/architecture/field-extraction-schema.json /app/docs/architecture/field-extraction-schema.json
```

to:

```dockerfile
COPY --chown=fdi:fdi docs/architecture/field-extraction-schema.v4_1.json /app/docs/architecture/field-extraction-schema.v4_1.json
```

- [ ] **Step 6: Run the config test to confirm it passes**

Run: `cd backend && python3 -m pytest tests/unit/test_config.py::test_extraction_schema_path_default -v`
Expected: PASS

- [ ] **Step 7: Update the integration test assertions that hardcode v3.2.0 numbers**

Edit `backend/tests/integration/test_schema_endpoints.py`. Three assertions read through the now-changed default:

In `test_get_schema_falls_back_to_the_on_disk_seed` (around line 60-68):

```python
async def test_get_schema_falls_back_to_the_on_disk_seed(make_client) -> None:
    async with make_client() as client:
        r = await client.get("/schema")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "seed"
    assert body["version"] == "4.1.0"
    assert body["field_count"] == 402
    assert len(body["schema_document"]["modules"]) == 4
```

In `test_put_schema_persists_and_bumps_the_patch_version` (around line 81):

```python
        assert put.json()["version"] == "4.1.1"
```

In `test_post_schema_validate_reports_without_writing` (around line 133-135):

```python
    assert ok.json() == {
        "valid": True, "errors": [], "module_count": 4, "unit_count": 4, "field_count": 402,
    }
```

- [ ] **Step 8: Run the full unit + integration config/schema suites**

Run: `cd backend && python3 -m pytest tests/unit/test_config.py tests/integration/test_schema_endpoints.py -v`
Expected: PASS (integration tests require the Postgres/MinIO/RabbitMQ/Redis fixtures this suite already depends on — run them the same way the project's existing integration suite is run; if that infrastructure isn't available in this environment, at minimum confirm `tests/unit/test_config.py` passes and visually re-check the integration edits above against the file for correctness).

- [ ] **Step 9: Commit**

```bash
git add backend/src/fdi/platform/config.py backend/Dockerfile .env .env.example docker/.env \
  backend/tests/unit/test_config.py backend/tests/integration/test_schema_endpoints.py
git commit -m "feat(schema): wire v4.1 four-level taxonomy schema as the live default"
```

---

### Task 2: Add `tree_from_schema` to the factsheet engine

**Files:**
- Modify: `backend/src/fdi/core/factsheet.py`
- Create: `backend/tests/unit/test_factsheet_tree.py`
- Modify: `backend/tests/unit/test_reasoned_v41.py` (add one real-file sanity test)

**Interfaces:**
- Consumes: nothing new (pure function over the schema dict shape already used by `rows_from_schema`, plus the `facts: dict[str, dict[str, object]]` shape `build_factsheet` already returns: `{tag: {"value", "confidence", "origin", "band", "page"?}}`).
- Produces: `tree_from_schema(schema: Mapping[str, object], facts: Mapping[str, Mapping[str, object]]) -> list[dict[str, object]]`, returning one entry per module: `{"key": str, "name": str, "statement": str, "tree": list[TreeNode]}` where a `TreeNode` is `{"field_name": str, "tag": str | None, "path": str, "origin": str | None, "role": str | None, "value": object | None, "confidence": float | None, "band": str | None, "page": int | None, "children": list[TreeNode]}`. Also: `document_factsheet`'s returned bundle gains a `"statements"` key holding this list, and each extracted fact now carries `"page"` (the source page number, `None` for derived facts or when absent). Task 3 (frontend types) and Task 7 (frontend viewer) consume this exact shape.

- [ ] **Step 1: Write the failing unit tests for `tree_from_schema`**

Create `backend/tests/unit/test_factsheet_tree.py`:

```python
"""tree_from_schema: the nested (not flattened) walk of a v4/v4.1 schema, with
facts attached by tag -- what the taxonomy viewer page renders directly."""

from fdi.core.factsheet import build_factsheet, document_factsheet, tree_from_schema

_SCHEMA = {
    "modules": [
        {
            "key": "module_assets",
            "name": "Assets",
            "statement": "assets",
            "retrieval_units": [
                {
                    "unit_id": "AS",
                    "fields": [
                        {
                            "field_name": "Raw materials",
                            "tag": "RM",
                            "origin": "derived",
                            "role": "line",
                            "derivation": {"method": "sum", "inputs": ["IMP", "IND"]},
                            "fields": [
                                {
                                    "field_name": "Raw materials - Imported",
                                    "tag": "IMP",
                                    "origin": "extracted",
                                    "role": "line",
                                    "field_path": "assets.IMP",
                                },
                                {
                                    "field_name": "Raw materials - Indigenous",
                                    "tag": "IND",
                                    "origin": "extracted",
                                    "role": "line",
                                    "field_path": "assets.IND",
                                },
                            ],
                        },
                        {
                            "field_name": "Not extracted this run",
                            "tag": "MISSING",
                            "origin": "extracted",
                            "role": "line",
                            "field_path": "assets.MISSING",
                        },
                    ],
                }
            ],
        }
    ]
}


def test_tree_preserves_nesting_and_attaches_facts_by_tag():
    facts = {
        "IMP": {"value": 3.0, "confidence": 0.9, "origin": "extracted",
                 "band": "high", "page": 12},
        "IND": {"value": 4.0, "confidence": 0.8, "origin": "extracted",
                 "band": "high", "page": 12},
        "RM": {"value": 7.0, "confidence": 0.8, "origin": "derived", "band": "high"},
    }
    tree = tree_from_schema(_SCHEMA, facts)
    assert len(tree) == 1
    module = tree[0]
    assert module["key"] == "module_assets"
    assert module["name"] == "Assets"
    assert module["statement"] == "assets"
    assert len(module["tree"]) == 2  # RM (with children), MISSING

    rm = module["tree"][0]
    assert rm["tag"] == "RM"
    assert rm["value"] == 7.0
    assert rm["origin"] == "derived"
    assert rm["page"] is None  # a derived node has no single source page
    assert len(rm["children"]) == 2
    imp, ind = rm["children"]
    assert imp["tag"] == "IMP"
    assert imp["value"] == 3.0
    assert imp["page"] == 12
    assert ind["tag"] == "IND"
    assert ind["value"] == 4.0


def test_a_tag_with_no_fact_renders_as_none_not_missing_from_the_tree():
    tree = tree_from_schema(_SCHEMA, {})
    module = tree[0]
    missing_node = module["tree"][1]
    assert missing_node["tag"] == "MISSING"
    assert missing_node["value"] is None
    assert missing_node["confidence"] is None
    assert missing_node["band"] is None


def test_paths_are_unique_and_stable_for_every_node_including_parents():
    tree = tree_from_schema(_SCHEMA, {})
    module = tree[0]
    rm = module["tree"][0]
    assert rm["path"] == "assets.AS.0"
    assert rm["children"][0]["path"] == "assets.AS.0.0"
    assert rm["children"][1]["path"] == "assets.AS.0.1"
    assert module["tree"][1]["path"] == "assets.AS.1"


def test_document_factsheet_carries_page_through_to_the_tree():
    records = [
        {"tag": "IMP", "origin": "extracted", "value": 3.0, "confidence": 0.9,
         "source": {"page": 12, "snippet": "x", "evidence_ids": []}},
        {"tag": "IND", "origin": "extracted", "value": 4.0, "confidence": 0.8,
         "source": {"page": 12, "snippet": "x", "evidence_ids": []}},
    ]
    fs = document_factsheet(_SCHEMA, records)
    assert "statements" in fs
    rm = fs["statements"][0]["tree"][0]
    assert rm["value"] == 7.0  # derived from IMP + IND
    imp = rm["children"][0]
    assert imp["page"] == 12


def test_build_factsheet_alone_has_no_statements_key():
    # tree_from_schema needs the schema, which build_factsheet (unlike
    # document_factsheet) never receives -- statements is assembled one
    # level up.
    fs = build_factsheet(
        [{"statement": "assets", "rows": [{"tag": "IMP", "origin": "extracted"}]}],
        {"IMP": {"value": 1.0, "confidence": 0.9}},
    )
    assert "statements" not in fs
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/unit/test_factsheet_tree.py -v`
Expected: FAIL with `ImportError: cannot import name 'tree_from_schema'`

- [ ] **Step 3: Implement `tree_from_schema` and thread `page` through `document_factsheet`**

Edit `backend/src/fdi/core/factsheet.py`. Add the new function after `_schema_row` (after line 80) and before `build_factsheet`:

```python
def tree_from_schema(
    schema: Mapping[str, object], facts: Mapping[str, Mapping[str, object]]
) -> list[dict[str, object]]:
    """The same modules -> units -> nested fields walk as rows_from_schema, but
    NESTED (not flattened) and with each tagged node's resolved Fact attached.
    This is what the taxonomy viewer renders directly: one entry per module,
    each holding its statement's tree with values/confidence/origin/page baked
    in by tag. A node with a tag but no matching fact (never extracted, or a
    derived subtotal that didn't resolve) still appears, with value/confidence
    /band/page all None -- absence is coverage information, not an error."""
    out: list[dict[str, object]] = []
    modules = schema.get("modules")
    if not isinstance(modules, list):
        return out
    for m in modules:
        if not isinstance(m, Mapping):
            continue
        statement = m.get("statement") or m.get("key")
        statement_key = statement if isinstance(statement, str) else "module"
        nodes: list[dict[str, object]] = []
        units = m.get("retrieval_units")
        if isinstance(units, list):
            for u in units:
                if not isinstance(u, Mapping):
                    continue
                unit_id = u.get("unit_id")
                unit_key = unit_id if isinstance(unit_id, str) else "unit"
                fields = u.get("fields")
                if isinstance(fields, list):
                    for i, f in enumerate(fields):
                        if isinstance(f, Mapping):
                            nodes.append(
                                _tree_node(f, f"{statement_key}.{unit_key}.{i}", facts)
                            )
        out.append({
            "key": m.get("key"),
            "name": m.get("name"),
            "statement": statement,
            "tree": nodes,
        })
    return out


def _tree_node(
    f: Mapping[str, object], path: str, facts: Mapping[str, Mapping[str, object]]
) -> dict[str, object]:
    tag = f.get("tag")
    tag = tag if isinstance(tag, str) and tag else None
    fact = facts.get(tag) if tag is not None else None

    children_out: list[dict[str, object]] = []
    children_in = f.get("fields")
    if isinstance(children_in, list):
        for i, child in enumerate(children_in):
            if isinstance(child, Mapping):
                children_out.append(_tree_node(child, f"{path}.{i}", facts))

    return {
        "field_name": f.get("field_name"),
        "tag": tag,
        "path": path,
        "origin": f.get("origin"),
        "role": f.get("role"),
        "value": fact.get("value") if fact else None,
        "confidence": fact.get("confidence") if fact else None,
        "band": fact.get("band") if fact else None,
        "page": fact.get("page") if fact else None,
        "children": children_out,
    }
```

Now thread `page` through `document_factsheet` and add the `statements` key. Replace the body of `document_factsheet` (lines 143-167):

```python
def document_factsheet(
    schema: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    *,
    tolerance: float = 1.0,
) -> dict[str, object]:
    """End-to-end factsheet from a v4 schema + extraction records (the API entry
    point). `records` are extraction output_contract records ({tag, value,
    confidence, source, ...}); only extracted, tagged, valued records seed the
    Fact map, and the derivation engine produces the rest. Adds a coverage
    report and the nested per-statement tree (tree_from_schema) the taxonomy
    viewer renders directly."""
    statements = rows_from_schema(schema)
    extracted: dict[str, dict[str, object]] = {}
    for rec in records:
        tag = rec.get("tag")
        if not isinstance(tag, str) or not tag or rec.get("origin") == "derived":
            continue
        if rec.get("value") is None:
            continue
        source = rec.get("source")
        page = source.get("page") if isinstance(source, Mapping) else None
        extracted[tag] = {
            "value": rec.get("value"),
            "confidence": rec.get("confidence", 0.0),
            "page": page,
        }

    bundle = build_factsheet(statements, extracted, tolerance=tolerance)
    facts = bundle["facts"]
    if isinstance(facts, dict):
        bundle["coverage"] = coverage_report(statements, facts)
        bundle["statements"] = tree_from_schema(schema, facts)
    return bundle
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/unit/test_factsheet_tree.py -v`
Expected: PASS

- [ ] **Step 5: Add one real-file sanity check alongside the other v4.1 end-to-end tests**

Edit `backend/tests/unit/test_reasoned_v41.py`, add at the end of the file:

```python
def test_v41_tree_from_schema_matches_the_four_sheets():
    if not _V41.exists():
        return
    schema = json.loads(_V41.read_text("utf-8"))
    statements = rows_from_schema(schema)
    records = [{"tag": r["tag"], "origin": "extracted", "value": 1.0, "confidence": 0.9,
                "source": {"page": 1, "snippet": "", "evidence_ids": []}}
               for st in statements for r in st["rows"] if r["origin"] == "extracted"]
    fs = document_factsheet(schema, records)
    trees = fs["statements"]
    assert [t["statement"] for t in trees] == [
        "profit_and_loss", "assets", "liabilities", "equity",
    ]
    # every node's path is unique within the whole document
    paths = []

    def collect(nodes):
        for n in nodes:
            paths.append(n["path"])
            collect(n["children"])

    for t in trees:
        collect(t["tree"])
    assert len(paths) == len(set(paths))
    # a known derived subtotal carries its resolved value into the tree, nested
    def find(nodes, tag):
        for n in nodes:
            if n["tag"] == tag:
                return n
            found = find(n["children"], tag)
            if found:
                return found
        return None

    assets_tree = trees[1]["tree"]
    grstotta = find(assets_tree, "GRSTOTTA")
    assert grstotta is not None
    assert grstotta["value"] == 7.0
    assert grstotta["origin"] == "derived"
```

- [ ] **Step 6: Run the full factsheet-related unit suite**

Run: `cd backend && python3 -m pytest tests/unit/test_factsheet_tree.py tests/unit/test_reasoned_v41.py tests/unit/test_document_factsheet.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/src/fdi/core/factsheet.py backend/tests/unit/test_factsheet_tree.py \
  backend/tests/unit/test_reasoned_v41.py
git commit -m "feat(factsheet): nest schema+facts into a per-statement tree (tree_from_schema)"
```

---

### Task 3: Frontend types and API client for the factsheet tree

**Files:**
- Modify: `frontend/lib/types.ts`
- Modify: `frontend/lib/api.ts`
- Create: `frontend/lib/api.factsheet.test.ts`

**Interfaces:**
- Consumes: the exact JSON shape Task 2 produces (`document_factsheet`'s bundle, including `statements`, `summary`, `ratios`, `validations`, `coverage`, plus the existing `doc_key`/`calculation_arcs` the route already adds).
- Produces: `TaxonomyTreeNode`, `TaxonomyStatementView`, `FactsheetDocumentView` types, and `factsheetDocument(docKey: string, force?: boolean): Promise<FactsheetDocumentView>`. Tasks 6 and 7 import these directly.

- [ ] **Step 1: Write the failing API client test**

Create `frontend/lib/api.factsheet.test.ts`:

```ts
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import { factsheetDocument } from "./api";

const API = "http://localhost:8000";

describe("factsheetDocument", () => {
  it("posts doc_key and returns the parsed bundle", async () => {
    server.use(
      http.post(`${API}/factsheet/document`, async ({ request }) => {
        const body = (await request.json()) as { doc_key: string; force: boolean };
        expect(body).toEqual({ doc_key: "annual-report-2025.pdf", force: false });
        return HttpResponse.json({
          summary: {
            extracted: 1, derived_ok: 0, derived_unresolved: 0, ratios_ok: 0,
            checks_total: 0, checks_failed: 0,
            bands: { high: 1, needs_review: 0, low: 0, failed: 0 },
          },
          ratios: [],
          validations: [],
          coverage: {
            totals: { expected: 1, found_extracted: 1, derived_ok: 0,
                      missing_extracted: 0, completeness_pct: 100 },
          },
          statements: [
            { key: "module_assets", name: "Assets", statement: "assets", tree: [] },
          ],
          doc_key: "annual-report-2025.pdf",
        });
      }),
    );

    const result = await factsheetDocument("annual-report-2025.pdf");
    expect(result.doc_key).toBe("annual-report-2025.pdf");
    expect(result.statements[0].name).toBe("Assets");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run lib/api.factsheet.test.ts`
Expected: FAIL — `factsheetDocument` does not exist / TypeScript error.

- [ ] **Step 3: Add the types**

Edit `frontend/lib/types.ts`, append at the end of the file:

```ts
/** One node in a statement's tree (from tree_from_schema, backend/src/fdi/
 *  core/factsheet.py). `value` is `unknown` for the same reason
 *  ExtractFieldView.value is: a scalar for a non-dual-period field, or
 *  `{current, prior}` for one that is -- see isDualPeriod in lib/format.ts.
 *  A node with `tag === null` is a pure section/group heading; a node can
 *  carry BOTH a tag/value AND children (a derived subtotal that also breaks
 *  out its inputs, e.g. Raw materials = Imported + Indigenous). */
export interface TaxonomyTreeNode {
  field_name: string;
  tag: string | null;
  path: string;
  // Verified against the real v4.1 file: every node's `origin` is exactly
  // "extracted", "derived", or absent (pure structural group headers) --
  // never any other value.
  origin: "extracted" | "derived" | null;
  role: string | null;
  value: unknown;
  confidence: number | null;
  band: "high" | "needs_review" | "low" | "failed" | null;
  page: number | null;
  children: TaxonomyTreeNode[];
}

export interface TaxonomyStatementView {
  key: string;
  name: string;
  statement: string;
  tree: TaxonomyTreeNode[];
}

/** Structurally identical to AuditFactSheet's own FactSheetBundle
 *  (components/analytics/AuditFactSheet.tsx) plus the fields that component
 *  doesn't need -- kept as a separate type rather than importing that one so
 *  AuditFactSheet stays a self-contained, independently-testable
 *  presentational component per its own file header. */
export interface FactsheetRatioView {
  key: string;
  label: string;
  value: number | null;
  status: string;
  caro_clause: string;
  explanation: string;
}

export interface FactsheetValidationView {
  rule_id: string;
  kind: string;
  expected: number | string;
  actual: number | string;
  delta: number;
  severity: string;
  passed: boolean;
}

export interface FactsheetDocumentView {
  summary: {
    extracted: number;
    derived_ok: number;
    derived_unresolved: number;
    ratios_ok: number;
    checks_total: number;
    checks_failed: number;
    bands: { high: number; needs_review: number; low: number; failed: number };
  };
  ratios: FactsheetRatioView[];
  validations: FactsheetValidationView[];
  coverage: {
    totals: {
      expected: number;
      found_extracted: number;
      derived_ok: number;
      missing_extracted: number;
      completeness_pct: number;
    };
  };
  statements: TaxonomyStatementView[];
  doc_key: string;
}
```

- [ ] **Step 4: Add the API function**

Edit `frontend/lib/api.ts`. Add `FactsheetDocumentView` to the type-only import block at the top (line 1-13):

```ts
import type {
  AnalyticsView,
  CorpusCostView,
  DocumentCostView,
  DocumentListView,
  DocumentUsageEventsView,
  DocumentView,
  ExtractDocumentView,
  ExtractionSchema,
  FactsheetDocumentView,
  SchemaValidationView,
  SchemaView,
  StagesView,
} from "./types";
```

Add the function after `extractDocument` (after line 86):

```ts
/** POST /factsheet/document: extraction + derivation/validation/ratios/coverage
 *  in one call, plus the nested per-statement tree the taxonomy viewer renders.
 *  Cache-first like extractDocument -- `force` re-runs extraction from scratch. */
export const factsheetDocument = (docKey: string, force?: boolean) =>
  request<FactsheetDocumentView>("/factsheet/document", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ doc_key: docKey, force: force ?? false }),
  });
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd frontend && npx vitest run lib/api.factsheet.test.ts`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add frontend/lib/types.ts frontend/lib/api.ts frontend/lib/api.factsheet.test.ts
git commit -m "feat(taxonomy): add FactsheetDocumentView types and factsheetDocument API client"
```

---

### Task 4: Expose each extracted field's taxonomy `tag` on the wire

**Files:**
- Modify: `backend/src/fdi/api/schemas.py:489-493` (`ExtractFieldView`)
- Modify: `backend/tests/integration/test_api.py` (extend the existing extract/document happy-path test)
- Modify: `frontend/lib/types.ts` (`ExtractFieldView`)

**Interfaces:**
- Consumes: `fdi.core.extractor.assemble_result`'s output dict, which already includes `"tag": field.tag, "origin": field.origin` (`backend/src/fdi/core/extractor.py:536-543`) — today silently dropped by `ExtractFieldView.model_validate(...)` because the Pydantic model doesn't declare those keys.
- Produces: `ExtractFieldView.tag: str | None` and `ExtractFieldView.origin: str | None`, round-tripped through `POST /extract/document`. Task 5 (`ReportViewer`) matches an incoming deep link's `tag` against `fields[].tag` — the only reliable cross-reference, since `field_name` collides across modules.

- [ ] **Step 1: Extend the existing happy-path integration test first**

Edit `backend/tests/integration/test_api.py`, in `test_extract_document_happy_path_with_fake_extractor_client_override` (around line 1876), add after the existing assertion:

```python
    assert body["fields"][0]["field_name"] == "Company overview text"
    # This test's schema has no "tag" key on its one field (pre-taxonomy v3.1
    # shape) -- proves the new key round-trips as null rather than being
    # dropped, same as a real tagged v4.1 field would round-trip its tag.
    assert body["fields"][0]["tag"] is None
    assert body["fields"][0]["origin"] == "extracted"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/integration/test_api.py::test_extract_document_happy_path_with_fake_extractor_client_override -v`
Expected: FAIL — `KeyError: 'tag'` (the response body has no such key today).

If the integration fixtures (Postgres/MinIO/RabbitMQ/Redis) aren't available in this environment, skip straight to Step 3 and rely on Step 5's verification instead.

- [ ] **Step 3: Add the fields to `ExtractFieldView`**

Edit `backend/src/fdi/api/schemas.py`, replace the `ExtractFieldView` class (around line 489-493):

```python
class ExtractFieldView(BaseModel):
    field_name: str
    # The taxonomy tag and its extracted/derived origin -- assemble_result
    # (core/extractor.py) always sets both, but this model dropped them
    # silently until now (Pydantic ignores undeclared keys by default). A
    # deep link from the taxonomy viewer matches a field by `tag`, since
    # `field_name` is not unique across modules.
    tag: str | None = None
    origin: str | None = None
    value: object
    source: ExtractSourceView
    confidence: float
```

- [ ] **Step 4: Run the integration test to verify it passes**

Run: `cd backend && python3 -m pytest tests/integration/test_api.py::test_extract_document_happy_path_with_fake_extractor_client_override -v`
Expected: PASS

- [ ] **Step 5: Mirror the new fields in the frontend type**

Edit `frontend/lib/types.ts`, replace the `ExtractFieldView` interface (around line 67-72):

```ts
export interface ExtractFieldView {
  field_name: string;
  /** The taxonomy tag and its extracted/derived origin -- null for a field
   *  extracted under a pre-taxonomy (v3.1-shaped) schema. A deep link from
   *  the taxonomy viewer matches on `tag`, never `field_name` (which
   *  collides across modules -- see fieldId's own comment in lib/format.ts). */
  tag: string | null;
  origin: "extracted" | "derived" | null;
  value: unknown;
  source: ExtractSourceView;
  confidence: number;
}
```

- [ ] **Step 6: Run the full backend extract-route suite and the frontend type-check**

Run: `cd backend && python3 -m pytest tests/integration/test_api.py -k extract_document -v`
Run: `cd frontend && npx tsc --noEmit`
Expected: PASS on both (the `tsc` check catches any consumer of `ExtractFieldView` that destructures it positionally in a way the new required fields would break — none do, per Task 3/5's own reads of `FieldList.tsx`/`Value.tsx`, which only ever spread/read named fields).

- [ ] **Step 7: Commit**

```bash
git add backend/src/fdi/api/schemas.py backend/tests/integration/test_api.py frontend/lib/types.ts
git commit -m "feat(extract): expose each field's taxonomy tag/origin in POST /extract/document"
```

---

### Task 5: `ReportViewer` pre-selects a field from a `?tag=` deep link

**Files:**
- Modify: `frontend/test/setup.ts`
- Modify: `frontend/components/viewer/ReportViewer.tsx`
- Modify: `frontend/components/viewer/ReportViewer.test.tsx`

**Interfaces:**
- Consumes: `useSearchParams()` from `next/navigation`; `ExtractFieldView.tag` (Task 4).
- Produces: opening `/documents/[docId]?tag=<TAG>` pre-selects the matching field and turns the PDF to its page, exactly as if the user had clicked it in `FieldList`. `StatementTable` (Task 6) is the only caller of this contract in this plan.

- [ ] **Step 1: Give tests control over `useSearchParams`**

`frontend/test/setup.ts` currently mocks `useSearchParams` to always return an empty `URLSearchParams`, with no way for an individual test to change that — extend it the same way `setPathname` already works for `usePathname`.

Edit `frontend/test/setup.ts`. Replace the `usePathname`/`useSearchParams` section:

```ts
let pathname = "/";
export function setPathname(next: string) {
  pathname = next;
}

let searchParams = new URLSearchParams();
export function setSearchParams(next: URLSearchParams | string) {
  searchParams = typeof next === "string" ? new URLSearchParams(next) : next;
}

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    back: vi.fn(),
    refresh: vi.fn(),
    prefetch: vi.fn(),
  }),
  useSearchParams: () => searchParams,
  useParams: () => ({}),
}));
```

And reset it alongside `pathname` in `afterEach`:

```ts
afterEach(() => {
  server.resetHandlers();
  cleanup();
  pathname = "/";
  searchParams = new URLSearchParams();
});
```

- [ ] **Step 2: Write the failing test**

Add to `frontend/components/viewer/ReportViewer.test.tsx` (extend the existing `@/test/setup` import to include `setSearchParams`):

```ts
import { server, setSearchParams } from "@/test/setup";
```

Add a new test in the `describe("ReportViewer", ...)` block:

```ts
  it("pre-selects the field named by a ?tag= deep link and turns to its page", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () => HttpResponse.json(record())),
      http.post(`${API}/extract/document`, () =>
        HttpResponse.json({
          doc_key: "annual-report-2025.pdf",
          doc_id: "doc-1",
          module: null,
          field_count: 2,
          fields: [
            {
              field_name: "Cash on hand", tag: "COH", origin: "extracted",
              value: 10, confidence: 0.9,
              source: { page: 3, snippet: "", evidence_ids: [],
                        bounding_regions: { current: [], prior: [] } },
            },
            {
              field_name: "Cash equivalents", tag: "CEQ", origin: "extracted",
              value: 20, confidence: 0.9,
              source: { page: 7, snippet: "", evidence_ids: [],
                        bounding_regions: { current: [], prior: [] } },
            },
          ],
        }),
      ),
    );
    setSearchParams("tag=CEQ");
    renderViewer("doc-1");

    expect(await screen.findByRole("button", { name: /Cash equivalents/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/viewer/ReportViewer.test.tsx`
Expected: FAIL — no such pre-selection happens yet.

- [ ] **Step 4: Implement the pre-selection in `ReportViewer`**

Edit `frontend/components/viewer/ReportViewer.tsx`. `useEffect` and `useRef` are already imported on line 3 (`import { useCallback, useEffect, useMemo, useRef, useState } from "react";`) — only `useSearchParams` needs adding, with the other imports near line 6:

```tsx
import { useSearchParams } from "next/navigation";
```

Add, right after the `active` computation (after line 52, before the `select` callback):

```tsx
  const searchParams = useSearchParams();
  // A deep link from the taxonomy viewer (StatementTable's provenance links)
  // arrives as ?tag=<TAG>. Applied once, on the render where `fields` first
  // becomes non-empty -- re-applying on every render would fight a user's
  // own subsequent click.
  const appliedDeepLinkRef = useRef(false);
  useEffect(() => {
    if (appliedDeepLinkRef.current || fields.length === 0) return;
    const wantedTag = searchParams.get("tag");
    if (!wantedTag) return;
    appliedDeepLinkRef.current = true;
    const index = fields.findIndex((f) => f.tag === wantedTag);
    if (index === -1) return;
    const target = fields[index];
    setSelected(toFieldId(index, target.field_name));
    if (target.source.page) setPage(target.source.page);
  }, [fields, searchParams]);
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd frontend && npx vitest run components/viewer/ReportViewer.test.tsx`
Expected: PASS (all tests in the file, including the pre-existing ones)

- [ ] **Step 6: Commit**

```bash
git add frontend/test/setup.ts frontend/components/viewer/ReportViewer.tsx \
  frontend/components/viewer/ReportViewer.test.tsx
git commit -m "feat(viewer): pre-select a field from a ?tag= deep link"
```

---

### Task 6: `StatementTable` component

**Files:**
- Create: `frontend/components/taxonomy/StatementTable.tsx`
- Create: `frontend/components/taxonomy/StatementTable.test.tsx`

**Interfaces:**
- Consumes: `TaxonomyTreeNode[]` (Task 3), `formatAmount`/`formatConfidence`/`inkOpacity`/`isDualPeriod` (`frontend/lib/format.ts`), `RefMark` (`frontend/components/data/RefMark.tsx`).
- Produces: `StatementTable({ tree, docId }: { tree: TaxonomyTreeNode[]; docId: string })`. Task 7 renders one per active statement tab.

- [ ] **Step 1: Write the failing component tests**

Create `frontend/components/taxonomy/StatementTable.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { TaxonomyTreeNode } from "@/lib/types";
import { StatementTable } from "./StatementTable";

const tree: TaxonomyTreeNode[] = [
  {
    field_name: "Raw materials",
    tag: "RM",
    path: "assets.AS.0",
    origin: "derived",
    role: "line",
    value: 7,
    confidence: 0.8,
    band: "high",
    page: null,
    children: [
      {
        field_name: "Raw materials - Imported",
        tag: "IMP",
        path: "assets.AS.0.0",
        origin: "extracted",
        role: "line",
        value: { current: 3, prior: 2 },
        confidence: 0.9,
        band: "high",
        page: 12,
        children: [],
      },
      {
        field_name: "Raw materials - Indigenous",
        tag: "IND",
        path: "assets.AS.0.1",
        origin: "extracted",
        role: "line",
        value: { current: 4, prior: 5 },
        confidence: 0.4,
        band: "needs_review",
        page: 12,
        children: [],
      },
    ],
  },
  {
    field_name: "Not extracted this run",
    tag: "MISSING",
    path: "assets.AS.1",
    origin: "extracted",
    role: "line",
    value: null,
    confidence: null,
    band: null,
    page: null,
    children: [],
  },
];

describe("StatementTable", () => {
  it("shows a parent row and its depth-1/2 children by default", () => {
    render(<StatementTable tree={tree} docId="doc-1" />);
    expect(screen.getByText("Raw materials")).toBeInTheDocument();
    // Depth-1 children of a depth-0 root stay visible under the default
    // expand-to-depth-2 rule (see plan Task 6, Step 3).
    expect(screen.getByText("Raw materials - Imported")).toBeInTheDocument();
    expect(screen.getByText("Raw materials - Indigenous")).toBeInTheDocument();
  });

  it("collapses and re-expands a subtree on toggle", async () => {
    const user = userEvent.setup();
    render(<StatementTable tree={tree} docId="doc-1" />);
    const toggle = screen.getByRole("button", { name: "Collapse Raw materials" });
    await user.click(toggle);
    expect(screen.queryByText("Raw materials - Imported")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand Raw materials" }));
    expect(screen.getByText("Raw materials - Imported")).toBeInTheDocument();
  });

  it("renders current/prior/delta for a dual-period value", () => {
    render(<StatementTable tree={tree} docId="doc-1" />);
    const row = screen.getByText("Raw materials - Imported").closest("tr")!;
    expect(within(row).getByText("3.00")).toBeInTheDocument();
    expect(within(row).getByText("2.00")).toBeInTheDocument();
    expect(within(row).getByText("50.0%")).toBeInTheDocument(); // (3-2)/2
  });

  it("shows an em-dash for a tag with no value", () => {
    render(<StatementTable tree={tree} docId="doc-1" />);
    const row = screen.getByText("Not extracted this run").closest("tr")!;
    expect(within(row).getByText("—")).toBeInTheDocument();
  });

  it("filters rows by particulars or tag, pruning empty branches", async () => {
    const user = userEvent.setup();
    render(<StatementTable tree={tree} docId="doc-1" />);
    await user.type(screen.getByPlaceholderText("Find a line item or tag"), "IND");
    expect(screen.getByText("Raw materials - Indigenous")).toBeInTheDocument();
    expect(screen.queryByText("Raw materials - Imported")).not.toBeInTheDocument();
  });

  it("opens the source document in a new tab, keyed by tag, from a provenance link", async () => {
    const user = userEvent.setup();
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<StatementTable tree={tree} docId="doc-1" />);
    await user.click(
      screen.getByRole("button", {
        name: "Open evidence for Raw materials - Imported on page 12",
      }),
    );
    expect(openSpy).toHaveBeenCalledWith("/documents/doc-1?tag=IMP", "_blank");
    openSpy.mockRestore();
  });

  it("gives a derived node no provenance link", () => {
    render(<StatementTable tree={tree} docId="doc-1" />);
    const row = screen.getByText("Raw materials").closest("tr")!;
    expect(within(row).queryByRole("button", { name: /Open evidence/ })).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run components/taxonomy/StatementTable.test.tsx`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `StatementTable`**

Create `frontend/components/taxonomy/StatementTable.tsx`:

```tsx
"use client";

import { useMemo, useState } from "react";
import type { TaxonomyTreeNode } from "@/lib/types";
import { formatAmount, formatConfidence, inkOpacity, isDualPeriod } from "@/lib/format";
import { RefMark } from "@/components/data/RefMark";

const INDENT_PX = 20;
// Nodes at this depth or deeper start collapsed -- a statement's tree is
// ~100 tags deep across up to 4 levels; showing everything at once buries
// the top-line figures a banking user actually starts from.
const DEFAULT_EXPAND_DEPTH = 2;

function allParentPaths(nodes: TaxonomyTreeNode[], out: Set<string>): void {
  for (const node of nodes) {
    if (node.children.length > 0) out.add(node.path);
    allParentPaths(node.children, out);
  }
}

function defaultCollapsed(nodes: TaxonomyTreeNode[], depth: number, out: Set<string>): void {
  for (const node of nodes) {
    if (node.children.length > 0 && depth >= DEFAULT_EXPAND_DEPTH) out.add(node.path);
    defaultCollapsed(node.children, depth + 1, out);
  }
}

function matchesQuery(node: TaxonomyTreeNode, needle: string): boolean {
  return (
    node.field_name.toLowerCase().includes(needle) ||
    (node.tag ?? "").toLowerCase().includes(needle)
  );
}

/** Same shape as ModuleTree's filterModules: a node survives if it matches
 *  itself or has a matching descendant, so the table shows the shape of the
 *  answer rather than a column of empty branches. */
function filterTree(nodes: TaxonomyTreeNode[], needle: string): TaxonomyTreeNode[] {
  if (!needle) return nodes;
  const out: TaxonomyTreeNode[] = [];
  for (const node of nodes) {
    const children = filterTree(node.children, needle);
    if (matchesQuery(node, needle) || children.length > 0) {
      out.push({ ...node, children });
    }
  }
  return out;
}

function numericCurrent(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  if (isDualPeriod(raw) && typeof raw.current === "number") return raw.current;
  return null;
}

function numericPrior(raw: unknown): number | null {
  if (isDualPeriod(raw) && typeof raw.prior === "number") return raw.prior;
  return null;
}

function deltaPercent(current: number | null, prior: number | null): string {
  if (current === null || prior === null || prior === 0) return "—";
  return `${(((current - prior) / Math.abs(prior)) * 100).toFixed(1)}%`;
}

const thStyle = {
  padding: "var(--sp-2) var(--sp-4)",
  fontSize: "var(--text-2xs)",
} as const;

export function StatementTable({
  tree,
  docId,
}: {
  tree: TaxonomyTreeNode[];
  docId: string;
}) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    defaultCollapsed(tree, 0, initial);
    return initial;
  });

  const needle = query.trim().toLowerCase();
  const visible = useMemo(() => filterTree(tree, needle), [tree, needle]);

  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const expandAll = () => setCollapsed(new Set());
  const collapseAll = () => {
    const all = new Set<string>();
    allParentPaths(tree, all);
    setCollapsed(all);
  };

  const rows: { node: TaxonomyTreeNode; depth: number }[] = [];
  const collect = (nodes: TaxonomyTreeNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({ node, depth });
      if (node.children.length > 0 && !collapsed.has(node.path)) {
        collect(node.children, depth + 1);
      }
    }
  };
  collect(visible, 0);

  return (
    <div className="sheet-flat">
      <div
        className="flex items-center"
        style={{
          gap: "var(--sp-3)",
          padding: "var(--sp-3) var(--sp-4)",
          borderBottom: "var(--hairline)",
        }}
      >
        <input
          className="input"
          placeholder="Find a line item or tag"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ maxWidth: "24rem" }}
        />
        <button type="button" className="btn" onClick={expandAll}>
          Expand all
        </button>
        <button type="button" className="btn" onClick={collapseAll}>
          Collapse all
        </button>
      </div>

      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ borderBottom: "1px solid var(--rule)" }}>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "left" }}>Particulars</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "left" }}>Tag</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "right" }}>Current</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "right" }}>Prior</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "right" }}>Δ%</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "right" }}>Confidence</th>
            <th className="eyebrow" style={thStyle}>
              <span className="sr-only">Evidence</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={7} style={{ padding: "var(--sp-6)", color: "var(--ink-soft)" }}>
                No line item answers to that name or tag.
              </td>
            </tr>
          ) : (
            rows.map(({ node, depth }) => (
              <StatementRow
                key={node.path}
                node={node}
                depth={depth}
                docId={docId}
                collapsed={collapsed.has(node.path)}
                onToggle={() => toggle(node.path)}
              />
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function StatementRow({
  node,
  depth,
  docId,
  collapsed,
  onToggle,
}: {
  node: TaxonomyTreeNode;
  depth: number;
  docId: string;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const current = numericCurrent(node.value);
  const prior = numericPrior(node.value);
  const hasChildren = node.children.length > 0;
  const isDerived = node.origin === "derived";
  const isTagged = node.tag !== null;
  const missing = isTagged && node.value === null;
  const cell = { padding: "var(--sp-2) var(--sp-4)" } as const;

  return (
    <tr style={{ borderTop: "1px solid var(--rule-soft)" }}>
      <td style={{ ...cell, paddingLeft: `calc(var(--sp-4) + ${depth * INDENT_PX}px)` }}>
        <span className="flex items-baseline" style={{ gap: "var(--sp-2)" }}>
          {hasChildren ? (
            <button
              type="button"
              onClick={onToggle}
              aria-expanded={!collapsed}
              aria-label={`${collapsed ? "Expand" : "Collapse"} ${node.field_name}`}
              className="data"
              style={{ width: "1.2em", flexShrink: 0, color: "var(--ink-faint)", cursor: "pointer" }}
            >
              {collapsed ? "▸" : "▾"}
            </button>
          ) : (
            <span aria-hidden="true" style={{ width: "1.2em", flexShrink: 0 }} />
          )}
          <span style={{ fontWeight: hasChildren ? 600 : 400, color: "var(--ink-soft)" }}>
            {node.field_name}
          </span>
          {isDerived ? (
            <span
              aria-hidden="true"
              className="data"
              title={`Derived (${node.field_name})`}
              style={{ fontSize: "var(--text-2xs)", color: "var(--ink-faint)" }}
            >
              Σ
            </span>
          ) : null}
        </span>
      </td>
      <td className="data" style={{ ...cell, fontSize: "var(--text-xs)", color: "var(--ink-faint)" }}>
        {node.tag ?? ""}
      </td>
      <td
        className="data"
        style={{
          ...cell,
          textAlign: "right",
          opacity: node.confidence !== null ? inkOpacity(node.confidence) : 1,
        }}
      >
        {!isTagged ? "" : missing ? "—" : formatAmount(node.value)}
      </td>
      <td className="data" style={{ ...cell, textAlign: "right", color: "var(--ink-faint)" }}>
        {!isTagged ? "" : prior !== null ? formatAmount(prior) : "—"}
      </td>
      <td className="data" style={{ ...cell, textAlign: "right", color: "var(--ink-faint)" }}>
        {!isTagged ? "" : deltaPercent(current, prior)}
      </td>
      <td className="data" style={{ ...cell, textAlign: "right", fontSize: "var(--text-2xs)" }}>
        {!isTagged || node.confidence === null
          ? ""
          : node.band === "failed"
            ? "!"
            : node.band === "high"
              ? "✓"
              : formatConfidence(node.confidence)}
      </td>
      <td style={cell}>
        {node.origin === "extracted" && node.page !== null && node.tag !== null ? (
          <RefMark
            page={node.page}
            label={`Open evidence for ${node.field_name} on page ${node.page}`}
            onClick={() =>
              window.open(`/documents/${docId}?tag=${encodeURIComponent(node.tag!)}`, "_blank")
            }
          />
        ) : null}
      </td>
    </tr>
  );
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run components/taxonomy/StatementTable.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/components/taxonomy/StatementTable.tsx frontend/components/taxonomy/StatementTable.test.tsx
git commit -m "feat(taxonomy): add StatementTable — expandable statement tree with provenance links"
```

---

### Task 7: `/taxonomy/[docId]` detail page

**Files:**
- Create: `frontend/components/taxonomy/TaxonomyViewer.tsx`
- Create: `frontend/components/taxonomy/TaxonomyViewer.test.tsx`
- Create: `frontend/app/taxonomy/[docId]/page.tsx`

**Interfaces:**
- Consumes: `factsheetDocument` (Task 3), `StatementTable` (Task 6), `AuditFactSheet` (`frontend/components/analytics/AuditFactSheet.tsx`, already built — its `FactSheetBundle` prop type is structurally satisfied by `FactsheetDocumentView`), `getDocument`/`filingLabel` (existing), `PageHeader` (existing).
- Produces: `TaxonomyViewer({ docId }: { docId: string })`, mounted by the route shell.

- [ ] **Step 1: Write the failing test**

Create `frontend/components/taxonomy/TaxonomyViewer.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import { TaxonomyViewer } from "./TaxonomyViewer";

const API = "http://localhost:8000";

function renderViewer(docId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TaxonomyViewer docId={docId} />
    </QueryClientProvider>,
  );
}

const bundle = {
  summary: {
    extracted: 2, derived_ok: 0, derived_unresolved: 0, ratios_ok: 0,
    checks_total: 0, checks_failed: 0,
    bands: { high: 2, needs_review: 0, low: 0, failed: 0 },
  },
  ratios: [],
  validations: [],
  coverage: {
    totals: { expected: 2, found_extracted: 2, derived_ok: 0,
              missing_extracted: 0, completeness_pct: 100 },
  },
  statements: [
    {
      key: "module_profit_and_loss", name: "Profit and Loss", statement: "profit_and_loss",
      tree: [
        { field_name: "Revenue from Sale of Products", tag: "RFSP",
          path: "profit_and_loss.PR.0", origin: "extracted", role: "line",
          value: 10, confidence: 0.9, band: "high", page: 4, children: [] },
      ],
    },
    {
      key: "module_assets", name: "Assets", statement: "assets",
      tree: [
        { field_name: "Cash on hand", tag: "COH", path: "assets.AS.0",
          origin: "extracted", role: "line", value: 5, confidence: 0.9,
          band: "high", page: 6, children: [] },
      ],
    },
  ],
  doc_key: "annual-report-2025.pdf",
};

describe("TaxonomyViewer", () => {
  it("shows the P&L statement by default and switches tabs", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json({
          id: "doc-1", checksum: "abc", filename: "annual-report-2025.pdf",
          byte_size: 100, doc_key: "annual-report-2025.pdf", version: 1,
          content_type: "application/pdf",
        }),
      ),
      http.post(`${API}/factsheet/document`, () => HttpResponse.json(bundle)),
    );
    const user = userEvent.setup();
    renderViewer("doc-1");

    expect(await screen.findByText("Revenue from Sale of Products")).toBeInTheDocument();
    expect(screen.queryByText("Cash on hand")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Assets" }));
    expect(await screen.findByText("Cash on hand")).toBeInTheDocument();
    expect(screen.queryByText("Revenue from Sale of Products")).not.toBeInTheDocument();
  });

  it("shows the completeness stat from AuditFactSheet", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json({
          id: "doc-1", checksum: "abc", filename: "annual-report-2025.pdf",
          byte_size: 100, doc_key: "annual-report-2025.pdf", version: 1,
          content_type: "application/pdf",
        }),
      ),
      http.post(`${API}/factsheet/document`, () => HttpResponse.json(bundle)),
    );
    renderViewer("doc-1");
    expect(await screen.findByText("100%")).toBeInTheDocument();
    expect(screen.getByText("Completeness")).toBeInTheDocument();
  });

  it("shows an inline error if the factsheet call fails", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json({
          id: "doc-1", checksum: "abc", filename: "annual-report-2025.pdf",
          byte_size: 100, doc_key: "annual-report-2025.pdf", version: 1,
          content_type: "application/pdf",
        }),
      ),
      http.post(`${API}/factsheet/document`, () => HttpResponse.json(
        { detail: "this document's pipeline has not finished indexing yet -- retry later" },
        { status: 409 },
      )),
    );
    renderViewer("doc-1");
    expect(await screen.findByRole("alert")).toHaveTextContent(/not finished indexing/);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/taxonomy/TaxonomyViewer.test.tsx`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `TaxonomyViewer`**

Create `frontend/components/taxonomy/TaxonomyViewer.tsx`:

```tsx
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { factsheetDocument, getDocument } from "@/lib/api";
import { filingLabel } from "@/lib/documents";
import { PageHeader } from "@/components/shell/PageHeader";
import { AuditFactSheet } from "@/components/analytics/AuditFactSheet";
import { StatementTable } from "./StatementTable";

export function TaxonomyViewer({ docId }: { docId: string }) {
  const { data: filing } = useQuery({
    queryKey: ["document", docId],
    queryFn: () => getDocument(docId),
  });

  const factsheet = useQuery({
    queryKey: ["factsheet", docId, filing?.doc_key],
    queryFn: () => factsheetDocument(filing!.doc_key ?? docId),
    enabled: Boolean(filing?.doc_key),
  });

  const [activeStatement, setActiveStatement] = useState<string | null>(null);
  const statements = factsheet.data?.statements ?? [];
  const active = statements.find((s) => s.statement === activeStatement) ?? statements[0] ?? null;

  return (
    <div className="flex flex-col" style={{ minHeight: "100vh" }}>
      <PageHeader
        eyebrow={filing ? `Version ${filing.version ?? "—"}` : "Filing"}
        title={filingLabel(filing)}
        lede="Every tagged line item from the taxonomy, laid out the way the source statements are, with figures read from this filing where they were found."
      />

      <div style={{ padding: "var(--sp-8)" }}>
        {factsheet.error ? (
          <p role="alert" className="sheet" style={{ padding: "var(--sp-6)" }}>
            {(factsheet.error as Error).message}
          </p>
        ) : factsheet.isPending ? (
          <p className="eyebrow">Reading the factsheet</p>
        ) : (
          <>
            <div style={{ marginBottom: "var(--sp-6)" }}>
              <AuditFactSheet data={factsheet.data} />
            </div>

            <div
              className="flex"
              style={{ gap: "var(--sp-1)", marginBottom: "var(--sp-4)" }}
            >
              {statements.map((s) => {
                const isActive = active?.statement === s.statement;
                return (
                  <button
                    key={s.statement}
                    type="button"
                    onClick={() => setActiveStatement(s.statement)}
                    aria-pressed={isActive}
                    className="data"
                    style={{
                      fontSize: "var(--text-xs)",
                      padding: "var(--sp-2) var(--sp-4)",
                      borderRadius: "var(--radius-sm)",
                      border: "1px solid",
                      borderColor: isActive ? "var(--ink)" : "var(--rule)",
                      background: isActive ? "var(--ink)" : "transparent",
                      color: isActive ? "var(--leaf)" : "var(--ink-soft)",
                      cursor: "pointer",
                    }}
                  >
                    {s.name}
                  </button>
                );
              })}
            </div>

            {active ? <StatementTable tree={active.tree} docId={docId} /> : null}
          </>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run components/taxonomy/TaxonomyViewer.test.tsx`
Expected: PASS

- [ ] **Step 5: Add the route shell**

Create `frontend/app/taxonomy/[docId]/page.tsx`:

```tsx
"use client";

import { use } from "react";
import { TaxonomyViewer } from "@/components/taxonomy/TaxonomyViewer";

/** The route shell. It exists only to unwrap the async `params` promise; the
 *  screen itself takes a plain `docId` so it can be rendered — and tested —
 *  without one. Mirrors app/documents/[docId]/page.tsx. */
export default function TaxonomyDocumentPage({
  params,
}: {
  params: Promise<{ docId: string }>;
}) {
  const { docId } = use(params);
  return <TaxonomyViewer docId={docId} />;
}
```

- [ ] **Step 6: Commit**

```bash
git add frontend/components/taxonomy/TaxonomyViewer.tsx frontend/components/taxonomy/TaxonomyViewer.test.tsx \
  frontend/app/taxonomy/\[docId\]/page.tsx
git commit -m "feat(taxonomy): add /taxonomy/[docId] statement viewer page"
```

---

### Task 8: `/taxonomy` picker page

**Files:**
- Create: `frontend/app/taxonomy/page.tsx`
- Create: `frontend/app/taxonomy/page.test.tsx`

**Interfaces:**
- Consumes: `listDocuments` (existing), `summarizeStageCounts` (`frontend/lib/stages.ts`, existing), `filingLabel` (existing), `PageHeader` (existing).
- Produces: the `/taxonomy` route — the nav entry (Task 9) will link to.

- [ ] **Step 1: Write the failing test**

Create `frontend/app/taxonomy/page.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import TaxonomyPickerPage from "./page";

const API = "http://localhost:8000";

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TaxonomyPickerPage />
    </QueryClientProvider>,
  );
}

const doc = (over: Record<string, unknown> = {}) => ({
  id: "doc-1",
  checksum: "abc123",
  filename: "annual-report-2025.pdf",
  byte_size: 100,
  doc_key: "annual-report-2025.pdf",
  version: 1,
  content_type: "application/pdf",
  stage_counts: { done: 15 },
  ...over,
});

describe("TaxonomyPickerPage", () => {
  it("lists only filings whose pipeline has fully completed", async () => {
    server.use(
      http.get(`${API}/documents`, () =>
        HttpResponse.json({
          documents: [
            doc(),
            doc({ id: "doc-2", filename: "half-done.pdf", stage_counts: { done: 3 } }),
          ],
          total: 2, limit: 50, offset: 0,
        }),
      ),
    );
    renderPage();
    expect(await screen.findByRole("link", { name: /annual-report-2025\.pdf/ })).toHaveAttribute(
      "href",
      "/taxonomy/doc-1",
    );
    expect(screen.queryByText("half-done.pdf")).not.toBeInTheDocument();
  });

  it("shows an empty state when no filing has completed its pipeline", async () => {
    server.use(
      http.get(`${API}/documents`, () =>
        HttpResponse.json({ documents: [], total: 0, limit: 50, offset: 0 }),
      ),
    );
    renderPage();
    expect(await screen.findByText(/No filings are ready yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Filings" })).toHaveAttribute("href", "/");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run app/taxonomy/page.test.tsx`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the picker page**

Create `frontend/app/taxonomy/page.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { listDocuments } from "@/lib/api";
import { filingLabel } from "@/lib/documents";
import { summarizeStageCounts } from "@/lib/stages";
import { PageHeader } from "@/components/shell/PageHeader";

export default function TaxonomyPickerPage() {
  const { data, error, isPending } = useQuery({
    queryKey: ["documents"],
    queryFn: () => listDocuments(),
  });

  const ready = (data?.documents ?? []).filter((d) => {
    const { done, failed, total } = summarizeStageCounts(d.stage_counts);
    return failed === 0 && done >= total;
  });

  return (
    <>
      <PageHeader
        eyebrow="Taxonomy"
        title="Statement viewer"
        lede="Pick a filing whose pipeline has finished to see its figures laid out against the full taxonomy hierarchy."
      />

      <div style={{ padding: "var(--sp-8)" }}>
        {error ? (
          <p className="sheet" style={{ padding: "var(--sp-6)" }}>
            {(error as Error).message}
          </p>
        ) : isPending ? (
          <p className="eyebrow">Loading filings</p>
        ) : ready.length === 0 ? (
          <div
            className="sheet"
            style={{ padding: "var(--sp-16)", textAlign: "center", boxShadow: "var(--shadow-md)" }}
          >
            <p className="display" style={{ fontSize: "var(--text-lg)", marginBottom: "var(--sp-3)" }}>
              No filings are ready yet
            </p>
            <p style={{ color: "var(--ink-soft)", marginBottom: "var(--sp-6)" }}>
              A filing needs to finish its whole pipeline before its figures can be read against the taxonomy.
            </p>
            <Link href="/" className="btn btn-primary">
              Filings
            </Link>
          </div>
        ) : (
          <table
            className="sheet"
            style={{
              width: "100%", textAlign: "left", borderCollapse: "collapse",
              overflow: "hidden", boxShadow: "var(--shadow-md)",
            }}
          >
            <caption className="sr-only">Filings ready for the taxonomy viewer</caption>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--rule)" }}>
                <th className="eyebrow" style={{ padding: "var(--sp-3) var(--sp-4)" }}>Filing</th>
                <th className="eyebrow" style={{ padding: "var(--sp-3) var(--sp-4)" }}>Version</th>
              </tr>
            </thead>
            <tbody>
              {ready.map((d) => (
                <tr key={d.id} style={{ borderTop: "1px solid var(--rule-soft)" }}>
                  <td style={{ padding: "var(--sp-3) var(--sp-4)" }}>
                    <Link
                      href={`/taxonomy/${d.id}`}
                      style={{ fontWeight: 500, textDecoration: "underline", textUnderlineOffset: "3px" }}
                    >
                      {filingLabel(d)}
                    </Link>
                  </td>
                  <td className="data" style={{ padding: "var(--sp-3) var(--sp-4)", fontSize: "var(--text-sm)" }}>
                    v{d.version ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run app/taxonomy/page.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/app/taxonomy/page.tsx frontend/app/taxonomy/page.test.tsx
git commit -m "feat(taxonomy): add /taxonomy picker page"
```

---

### Task 9: Register the nav entry

**Files:**
- Modify: `frontend/components/shell/Spine.tsx:9-15`
- Modify: `frontend/components/shell/Spine.test.tsx`

**Interfaces:**
- Consumes: nothing new.
- Produces: a "Taxonomy" link visible in the app rail, `href="/taxonomy"`.

- [ ] **Step 1: Update the failing nav test**

Edit `frontend/components/shell/Spine.test.tsx`, in the first `it` block, add after the "Extraction schema" assertion (after line 18):

```ts
    expect(screen.getByRole("link", { name: "Taxonomy" })).toHaveAttribute(
      "href",
      "/taxonomy",
    );
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/shell/Spine.test.tsx`
Expected: FAIL — no "Taxonomy" link exists yet.

- [ ] **Step 3: Add the destination**

Edit `frontend/components/shell/Spine.tsx` lines 9-15:

```ts
const DESTINATIONS = [
  { href: "/", label: "Filings" },
  { href: "/ingest", label: "Add a filing" },
  { href: "/schema", label: "Extraction schema" },
  { href: "/taxonomy", label: "Taxonomy" },
  { href: "/analytics", label: "Quality" },
  { href: "/costing", label: "Costing" },
] as const;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run components/shell/Spine.test.tsx`
Expected: PASS

- [ ] **Step 5: Run the full frontend test suite as a final check**

Run: `cd frontend && npx vitest run`
Expected: PASS (all suites, including every test added in Tasks 3-9)

- [ ] **Step 6: Commit**

```bash
git add frontend/components/shell/Spine.tsx frontend/components/shell/Spine.test.tsx
git commit -m "feat(nav): add Taxonomy destination to the app rail"
```

---

## Manual verification (after all tasks land)

1. Start the backend and frontend dev servers.
2. Extract at least one filing fully (pipeline complete through `index`), then visit `/taxonomy`.
3. Confirm the filing appears in the picker; click it.
4. Confirm all 4 statement tabs render, hierarchy depth matches the source `Q3VENI LATEST TAXANOMY.xlsx`, and the summary strip shows real completeness/ratio/validation figures.
5. Click a provenance glyph on an extracted leaf; confirm a new tab opens on `/documents/[docId]` with that field selected and the PDF turned to the right page with its region highlighted.
6. Confirm `/schema` still works unchanged (now showing the v4.1 document instead of v3.2, since it reads through the same live schema).
