# FOG Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upload a filing, watch the FOG stages run, and read the resulting graph — each fact with the method that produced it, the tags that are contested, the labels nothing mapped, and the health of the whole run.

**Architecture:** No new backend API. `getArtifact(docId, stage)` already reaches `fog_objects`, `fog_mapping` and `fog_reason`. A new pure module `lib/fog.ts` turns those three bodies into view models; three presentational panels render them; `FogViewer` composes them beside the existing `PdfCanvas`.

**Tech Stack:** Next.js (App Router) · React · TypeScript · TanStack Query · Tailwind v4 · Vitest + Testing Library · pdfjs-dist.

**Spec:** [`docs/superpowers/specs/2026-08-07-fog-frontend-design.md`](../specs/2026-08-07-fog-frontend-design.md)

## Global Constraints

- **`lib/fog.ts` is pure and TOTAL.** No fetching, no React, no `Date`/`Math.random`. **Never throws on any input shape** — a malformed or missing artifact degrades to an empty view model, mirroring `fdi.core`'s law. Same posture as the existing `lib/stages.ts`.
- **The design system is MONOCHROME.** `--tether`, `--tick` and `--flag` are all pure ink. Distinction is carried by **weight, glyph and underline — never hue.** Do not introduce a colour for status, severity or method. Tokens live in `frontend/app/globals.css`; use them, never raw hex.
- **`--tether` is PROVENANCE ONLY.** Never a button, never an accent.
- **Three-valued footing.** `passed: true` → `✓`; `passed: false` → `⚑`; `checked: false` → **render nothing**. "Could not check" must never read as failure.
- **`absent ≠ missed`.** An unmapped label always shows its `reason`. `model_call_failed` (a swallowed outage) must never render the same as `model_said_unmapped` (an honest refusal).
- **Basis is always named on screen.** Default `consolidated`, fall back to whichever exists. A number without its basis is not usable.
- **Existing files stay.** `ReportViewer` and `FieldList` remain on disk, unreferenced.
- **Commands** (run from `frontend/`): `npm test` (vitest) · `npm run typecheck` · `npm run lint`. All three clean before every commit.
- **Fixtures come from real artifacts** in `out/fog-run-llm-p6b/`, never invented. Inventing fixtures is how an `aliases`/`note_aliases` mismatch survived three tasks on the backend.
- **Transcribe, do not rewrite.** Where this plan gives code, copy it exactly.

---

## File Structure

| File | Responsibility |
|---|---|
| `frontend/lib/stages.ts` | **Modify.** Add `basis` + the five FOG stages; move `TERMINAL_STAGE` |
| `frontend/lib/fog.ts` | **New.** Types + pure readers over the three artifact bodies |
| `frontend/lib/fog.test.ts` | **New.** Unit tests over real-shaped fixtures |
| `frontend/components/fog/FactsPanel.tsx` | **New.** tag · value · method |
| `frontend/components/fog/AttentionPanel.tsx` | **New.** contested + unmapped |
| `frontend/components/fog/HealthPanel.tsx` | **New.** footing · checks · coverage · mapping · schema |
| `frontend/components/fog/FogViewer.tsx` | **New.** Fetches, selects basis, composes with `PdfCanvas` |
| `frontend/app/documents/[docId]/page.tsx` | **Modify.** Swap `ReportViewer` → `FogViewer` |

---

## Task 1: Stage list

**Files:**
- Modify: `frontend/lib/stages.ts`
- Test: `frontend/lib/stages.test.ts` (**new** — none exists today)

**Interfaces:**
- Produces: `STAGES` gains six entries; `TERMINAL_STAGE === "fog_reason"`

- [ ] **Step 1: Write the failing test**

Create `frontend/lib/stages.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { STAGES, TERMINAL_STAGE, layerStages, summarizeStageCounts } from "./stages";

const names = () => STAGES.map((s) => s.name);

describe("the FOG stages are on the graph", () => {
  it("includes every stage the backend runs", () => {
    for (const name of [
      "basis", "statement_index", "note_index",
      "fog_objects", "fog_mapping", "fog_reason",
    ]) {
      expect(names()).toContain(name);
    }
  });

  it("declares deps that all exist as stages", () => {
    // An unknown dep is treated as depth 0 by layerStages, which silently
    // draws a stage in the wrong column rather than failing.
    for (const stage of STAGES) {
      for (const dep of stage.deps) expect(names()).toContain(dep);
    }
  });

  it("waits for the graph, not the vector index", () => {
    expect(TERMINAL_STAGE).toBe("fog_reason");
  });

  it("puts fog_reason last in the layering", () => {
    const layers = layerStages();
    const depthOf = (n: string) => layers.findIndex((l) => l.includes(n));
    expect(depthOf("fog_reason")).toBeGreaterThan(depthOf("fog_mapping"));
    expect(depthOf("fog_mapping")).toBeGreaterThan(depthOf("fog_objects"));
    expect(depthOf("fog_objects")).toBeGreaterThan(depthOf("statement_index"));
    expect(depthOf("statement_index")).toBeGreaterThan(depthOf("basis"));
  });

  it("counts the new stages in the filings-index total", () => {
    expect(summarizeStageCounts({ done: 3 }).total).toBe(STAGES.length);
  });

  it("has no stage listed twice", () => {
    expect(new Set(names()).size).toBe(names().length);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- stages`
Expected: FAIL — `STAGES` contains none of the six.

- [ ] **Step 3: Add the stages**

In `frontend/lib/stages.ts`, append inside `STAGES`, after the `index` entry. Every `deps` tuple below is transcribed from `backend/src/fdi/pipeline/stages/*.py`:

```ts
  { name: "basis", deps: ["ast", "provenance"] },
  { name: "statement_index", deps: ["ocr", "structure", "basis"] },
  { name: "note_index", deps: ["ocr", "structure", "basis"] },
  { name: "fog_objects", deps: ["ocr", "statement_index", "note_index", "structure"] },
  { name: "fog_mapping", deps: ["fog_objects"] },
  { name: "fog_reason", deps: ["fog_mapping", "fog_objects"] },
```

Then change the terminal constant and its comment:

```ts
/** The last stage to settle. When this reaches `done` the FINANCIAL OBJECT
 *  GRAPH is readable — facts, their proof, and the health of the run. It used
 *  to be `index`, which only meant the vector store had been written: a filing
 *  could reach it with nothing mapped at all. */
export const TERMINAL_STAGE = "fog_reason";
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- stages`
Expected: PASS, 6 passed

- [ ] **Step 5: Gates and commit**

```bash
cd frontend && npm run typecheck && npm run lint && npm test -- --run && cd ..
git add frontend/lib/stages.ts frontend/lib/stages.test.ts
git commit -m "feat(ui): put the FOG stages on the pipeline graph"
```

---

## Task 2: `lib/fog.ts` — types and the facts reader

**Files:**
- Create: `frontend/lib/fog.ts`
- Create: `frontend/lib/fog.test.ts`

**Interfaces:**
- Produces:
  - `type Basis = string`
  - `type FogFact = { tag: string; value: number; method: string; page: number | null; label: string }`
  - `chooseBasis(reason: unknown): Basis | null` — prefers `"consolidated"`, else the first present, else `null`
  - `readFacts(reason: unknown, mapping: unknown, fog: unknown, basis: Basis): FogFact[]` — sorted by `tag`

- [ ] **Step 1: Write the failing test**

Create `frontend/lib/fog.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { chooseBasis, readFacts } from "./fog";

// Shapes copied from out/fog-run-llm-p6b/TCS/*.json, trimmed.
const reason = {
  schema_version: 4,
  doc_id: "doc-1",
  bases: {
    consolidated: {
      facts: { PBTR: 65487, CT: 16388 },
      contested: [], checks: [], coverage: {}, composed: {}, summary: {},
    },
    standalone: { facts: { PBTR: 62806 }, contested: [], checks: [] },
  },
};
const mapping = {
  bases: {
    consolidated: {
      edges: [
        { type: "MAPS_TO", source: "li:a", target: "PBTR" },
        { type: "MAPS_TO", source: "li:b", target: "CT" },
      ],
      proof: [
        { line_item: "li:a", tag: "PBTR", label: "PROFIT BEFORE TAX",
          method: "lexical:section:name", score: 0.9, unit: "st" },
        { line_item: "li:b", tag: "CT", label: "Current tax",
          method: "llm", score: 0, unit: "nt" },
      ],
      unmapped: [],
    },
  },
};
const fog = {
  bases: {
    consolidated: {
      line_items: [
        { id: "li:a", label: "PROFIT BEFORE TAX", current: 65487, page: 167 },
        { id: "li:b", label: "Current tax", current: 16388, page: 167 },
      ],
      statements: [], notes: [], edges: [],
    },
  },
};

describe("chooseBasis", () => {
  it("prefers consolidated — it is what banks underwrite", () => {
    expect(chooseBasis(reason)).toBe("consolidated");
  });

  it("falls back to whatever the filing has", () => {
    expect(chooseBasis({ bases: { unknown: { facts: {} } } })).toBe("unknown");
  });

  it("returns null rather than inventing a basis", () => {
    expect(chooseBasis({ bases: {} })).toBeNull();
    expect(chooseBasis(null)).toBeNull();
    expect(chooseBasis("nonsense")).toBeNull();
  });
});

describe("readFacts", () => {
  it("joins a fact to the method that produced it", () => {
    const facts = readFacts(reason, mapping, fog, "consolidated");
    const pbtr = facts.find((f) => f.tag === "PBTR");
    expect(pbtr?.value).toBe(65487);
    expect(pbtr?.method).toBe("lexical:section:name");
    expect(pbtr?.label).toBe("PROFIT BEFORE TAX");
    expect(pbtr?.page).toBe(167);
  });

  it("is sorted by tag so the list does not reshuffle between runs", () => {
    const tags = readFacts(reason, mapping, fog, "consolidated").map((f) => f.tag);
    expect(tags).toEqual([...tags].sort());
  });

  it("keeps a fact whose proof is missing rather than dropping the number", () => {
    const facts = readFacts(reason, { bases: { consolidated: {} } }, fog, "consolidated");
    expect(facts).toHaveLength(2);
    expect(facts[0].method).toBe("");
  });

  it("reads the basis it was asked for, not the first one", () => {
    const facts = readFacts(reason, mapping, fog, "standalone");
    expect(facts.map((f) => f.tag)).toEqual(["PBTR"]);
    expect(facts[0].value).toBe(62806);
  });

  it("never throws on a malformed artifact", () => {
    expect(readFacts(null, null, null, "consolidated")).toEqual([]);
    expect(readFacts({ bases: "no" }, {}, {}, "consolidated")).toEqual([]);
    expect(readFacts(reason, mapping, fog, "absent")).toEqual([]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- fog`
Expected: FAIL — cannot resolve `./fog`.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/lib/fog.ts`:

```ts
/** Pure readers over the three Financial Object Graph artifacts.
 *
 *  No fetching and no React, matching `lib/stages.ts`: the artifact bodies have
 *  changed shape ten times across backend phases 2-6, and every change should
 *  break one focused test file rather than a component tree.
 *
 *  TOTAL, mirroring `fdi.core`'s own law: every reader takes `unknown` and
 *  degrades a missing or malformed body to an empty result. A panel that
 *  renders nothing is recoverable; a page that throws is not. */

export type Basis = string;

export type FogFact = {
  tag: string;
  value: number;
  /** How the mapping was resolved: "memory" | "lexical:*" | "llm" | "".
   *  Empty means the proof record is absent, NOT that it was guessed. */
  method: string;
  page: number | null;
  label: string;
};

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const isNumber = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v);

const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

/** `doc.bases[basis]`, or `{}`. */
function region(doc: unknown, basis: Basis): Record<string, unknown> {
  if (!isRecord(doc)) return {};
  const bases = doc.bases;
  if (!isRecord(bases)) return {};
  const found = bases[basis];
  return isRecord(found) ? found : {};
}

/** Which basis to show. Consolidated is the product's answer (it is what banks
 *  underwrite); otherwise take whichever the filing actually carries. Returns
 *  null rather than inventing one — a number shown under the wrong basis is
 *  worse than no number. */
export function chooseBasis(reason: unknown): Basis | null {
  if (!isRecord(reason) || !isRecord(reason.bases)) return null;
  const names = Object.keys(reason.bases).sort();
  if (names.length === 0) return null;
  return names.includes("consolidated") ? "consolidated" : names[0];
}

/** Every fact, joined to the proof and the line item behind it.
 *
 *  A fact whose proof record is missing is KEPT with `method: ""`. Dropping it
 *  would hide a real number because its bookkeeping was incomplete. */
export function readFacts(
  reason: unknown,
  mapping: unknown,
  fog: unknown,
  basis: Basis,
): FogFact[] {
  const facts = region(reason, basis).facts;
  if (!isRecord(facts)) return [];

  const proofByTag = new Map<string, Record<string, unknown>>();
  for (const record of arr(region(mapping, basis).proof)) {
    if (isRecord(record) && typeof record.tag === "string") {
      if (!proofByTag.has(record.tag)) proofByTag.set(record.tag, record);
    }
  }

  const itemById = new Map<string, Record<string, unknown>>();
  for (const item of arr(region(fog, basis).line_items)) {
    if (isRecord(item) && typeof item.id === "string") itemById.set(item.id, item);
  }

  const out: FogFact[] = [];
  for (const [tag, raw] of Object.entries(facts)) {
    if (!isNumber(raw)) continue;
    const proof = proofByTag.get(tag);
    const item = proof && typeof proof.line_item === "string"
      ? itemById.get(proof.line_item)
      : undefined;
    out.push({
      tag,
      value: raw,
      method: proof && typeof proof.method === "string" ? proof.method : "",
      label: proof && typeof proof.label === "string" ? proof.label : "",
      page: item && isNumber(item.page) ? item.page : null,
    });
  }
  out.sort((a, b) => a.tag.localeCompare(b.tag));
  return out;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- fog`
Expected: PASS, 9 passed

- [ ] **Step 5: Gates and commit**

```bash
cd frontend && npm run typecheck && npm run lint && cd ..
git add frontend/lib/fog.ts frontend/lib/fog.test.ts
git commit -m "feat(ui): pure readers over the FOG artifacts — facts joined to their proof"
```

---

## Task 3: `lib/fog.ts` — attention and health readers

**Files:**
- Modify: `frontend/lib/fog.ts` (append)
- Modify: `frontend/lib/fog.test.ts` (append)

**Interfaces:**
- Consumes: Task 2's `Basis`, `region`, guards
- Produces:
  - `type Contested = { tag: string; spread: number | null; winnerLabel: string; winnerValue: number | null; losers: { label: string; value: number | null }[] }`
  - `type Unmapped = { reason: string; labels: string[] }`
  - `type Health = { footing: { statement: string; passed: boolean | null; rule: string }[]; checks: Record<string, unknown>[]; coverage: Record<string, unknown>; mapping: Record<string, unknown>; schemaUsable: boolean | null }`
  - `readContested(reason, basis): Contested[]` — **spread descending**
  - `readUnmapped(mapping, basis): Unmapped[]` — grouped by reason, reason ascending
  - `readHealth(reason, mapping, fog, basis): Health`

- [ ] **Step 1: Write the failing test**

Append to `frontend/lib/fog.test.ts`. **Do not add a second `import ... from
"./fog"` line** -- `no-duplicate-imports` will fail lint. Extend the existing
import at the top to `import { chooseBasis, readContested, readFacts,
readHealth, readUnmapped } from "./fog";` and append only the describe blocks:

```ts

const attention = {
  bases: {
    consolidated: {
      facts: {},
      contested: [
        { tag: "SMALL", claimants: 2, spread: 0,
          winner: "li:1", winner_label: "Agrees", winner_value: 11027,
          losers: [{ label: "Also agrees", value: 11027 }] },
        { tag: "BIG", claimants: 3, spread: 11136,
          winner: "li:2", winner_label: "Right-of-use assets", winner_value: 11027,
          losers: [{ label: "Payment for acquiring right-of-use assets", value: -109 }] },
      ],
      checks: [{ rule_id: "identity_balance_sheet", severity: "warn", passed: false, delta: 10 }],
      coverage: { totals: { expected: 4, found_extracted: 3 } },
    },
  },
};

describe("readContested", () => {
  it("puts the widest disagreement first — that is where a human should look", () => {
    expect(readContested(attention, "consolidated").map((c) => c.tag)).toEqual(["BIG", "SMALL"]);
  });

  it("carries the losers, because a winner alone cannot be judged", () => {
    const big = readContested(attention, "consolidated")[0];
    expect(big.winnerLabel).toBe("Right-of-use assets");
    expect(big.losers[0].value).toBe(-109);
  });

  it("never throws", () => {
    expect(readContested(null, "consolidated")).toEqual([]);
    expect(readContested({ bases: { consolidated: { contested: "no" } } }, "consolidated")).toEqual([]);
  });
});

describe("readUnmapped", () => {
  const mapping = {
    bases: {
      consolidated: {
        unmapped: [
          { label: "Facility expenses", reason: "model_said_unmapped" },
          { label: "Travel", reason: "model_call_failed" },
          { label: "Other", reason: "model_said_unmapped" },
        ],
      },
    },
  };

  it("groups by reason so an outage never reads as an honest refusal", () => {
    const groups = readUnmapped(mapping, "consolidated");
    const failed = groups.find((g) => g.reason === "model_call_failed");
    const refused = groups.find((g) => g.reason === "model_said_unmapped");
    expect(failed?.labels).toEqual(["Travel"]);
    expect(refused?.labels).toEqual(["Facility expenses", "Other"]);
  });

  it("never throws", () => {
    expect(readUnmapped(null, "consolidated")).toEqual([]);
  });
});

describe("readHealth", () => {
  const fog = {
    bases: {
      consolidated: {
        statements: [
          { statement: "profit_and_loss",
            footing: { passed: true, checked: true, rule: "income - expenses = pbt" } },
          { statement: "cash_flow", footing: { passed: null, checked: false, rule: "" } },
        ],
        line_items: [], notes: [], edges: [],
      },
    },
  };
  const mapping = {
    schema: { usable: true, face_concepts: 577 },
    bases: { consolidated: { unmapped: [] } },
    diagnostics: { consolidated: { by_lexical: 375, by_llm: 84, llm_calls: 35 } },
  };

  it("keeps could-not-check distinct from failed", () => {
    const footing = readHealth(attention, mapping, fog, "consolidated").footing;
    expect(footing.find((f) => f.statement === "profit_and_loss")?.passed).toBe(true);
    expect(footing.find((f) => f.statement === "cash_flow")?.passed).toBeNull();
  });

  it("surfaces schema usability — an unusable schema maps nothing, silently", () => {
    expect(readHealth(attention, mapping, fog, "consolidated").schemaUsable).toBe(true);
    expect(readHealth(attention, { schema: { usable: false } }, fog, "consolidated").schemaUsable)
      .toBe(false);
  });

  it("carries the mapping diagnostics for the basis shown", () => {
    expect(readHealth(attention, mapping, fog, "consolidated").mapping.llm_calls).toBe(35);
  });

  it("never throws", () => {
    const empty = readHealth(null, null, null, "consolidated");
    expect(empty.footing).toEqual([]);
    expect(empty.schemaUsable).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- fog`
Expected: FAIL — `readContested` is not exported.

- [ ] **Step 3: Append the implementation**

Append to `frontend/lib/fog.ts`:

```ts
export type Contested = {
  tag: string;
  /** Highest claim minus lowest. 0 means the sources corroborate; large means
   *  one of them is wrong. Null when no claimant carried a value. */
  spread: number | null;
  winnerLabel: string;
  winnerValue: number | null;
  losers: { label: string; value: number | null }[];
};

export type Unmapped = { reason: string; labels: string[] };

export type Health = {
  footing: { statement: string; passed: boolean | null; rule: string }[];
  checks: Record<string, unknown>[];
  coverage: Record<string, unknown>;
  mapping: Record<string, unknown>;
  schemaUsable: boolean | null;
};

const num = (v: unknown): number | null => (isNumber(v) ? v : null);
const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** Contested tags, WIDEST DISAGREEMENT FIRST.
 *
 *  Corroboration and mis-mapping are indistinguishable from a fact map alone:
 *  a face row and its note agreeing on 11,027 looks exactly like a face row
 *  losing 11,027 to an unrelated -109. The spread is what separates them, so it
 *  is what the list is ordered by. */
export function readContested(reason: unknown, basis: Basis): Contested[] {
  const out: Contested[] = [];
  for (const raw of arr(region(reason, basis).contested)) {
    if (!isRecord(raw) || typeof raw.tag !== "string") continue;
    out.push({
      tag: raw.tag,
      spread: num(raw.spread),
      winnerLabel: str(raw.winner_label),
      winnerValue: num(raw.winner_value),
      losers: arr(raw.losers)
        .filter(isRecord)
        .map((l) => ({ label: str(l.label), value: num(l.value) })),
    });
  }
  out.sort((a, b) => (b.spread ?? -1) - (a.spread ?? -1) || a.tag.localeCompare(b.tag));
  return out;
}

/** Unmapped labels GROUPED BY REASON.
 *
 *  The grouping is the point. "model_call_failed" is a swallowed outage and
 *  "model_said_unmapped" is the model honestly declining to guess; before the
 *  backend recorded a reason these produced byte-identical output, and a dead
 *  endpoint read exactly like a clean refusal. */
export function readUnmapped(mapping: unknown, basis: Basis): Unmapped[] {
  const groups = new Map<string, string[]>();
  for (const raw of arr(region(mapping, basis).unmapped)) {
    if (!isRecord(raw)) continue;
    const reason = str(raw.reason) || "unknown";
    const label = str(raw.label);
    if (!label) continue;
    const bucket = groups.get(reason);
    if (bucket) bucket.push(label);
    else groups.set(reason, [label]);
  }
  return [...groups.entries()]
    .map(([reason, labels]) => ({ reason, labels: labels.sort() }))
    .sort((a, b) => a.reason.localeCompare(b.reason));
}

/** Footing verdicts, validation checks, coverage, mapping diagnostics and
 *  schema usability for one basis.
 *
 *  `passed` stays THREE-VALUED. `checked: false` means a needed line was absent
 *  or the statement has no rule, which is not a failure and must not render as
 *  one -- discarding a correct face because it labels one line unusually is
 *  worse than not checking it. */
export function readHealth(
  reason: unknown,
  mapping: unknown,
  fog: unknown,
  basis: Basis,
): Health {
  const footing: Health["footing"] = [];
  for (const raw of arr(region(fog, basis).statements)) {
    if (!isRecord(raw)) continue;
    const f = isRecord(raw.footing) ? raw.footing : {};
    footing.push({
      statement: str(raw.statement),
      passed: f.checked === true && typeof f.passed === "boolean" ? f.passed : null,
      rule: str(f.rule),
    });
  }
  footing.sort((a, b) => a.statement.localeCompare(b.statement));

  const schema = isRecord(mapping) && isRecord(mapping.schema) ? mapping.schema : {};
  const diagnostics =
    isRecord(mapping) && isRecord(mapping.diagnostics) ? mapping.diagnostics : {};
  const forBasis = isRecord(diagnostics[basis]) ? diagnostics[basis] : {};
  const coverage = region(reason, basis).coverage;

  return {
    footing,
    checks: arr(region(reason, basis).checks).filter(isRecord),
    coverage: isRecord(coverage) ? coverage : {},
    mapping: forBasis as Record<string, unknown>,
    schemaUsable: typeof schema.usable === "boolean" ? schema.usable : null,
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- fog`
Expected: PASS, 20 passed

- [ ] **Step 5: Gates and commit**

```bash
cd frontend && npm run typecheck && npm run lint && cd ..
git add frontend/lib/fog.ts frontend/lib/fog.test.ts
git commit -m "feat(ui): contested, unmapped and health readers over the FOG"
```

---

## Task 4: The three panels

**Files:**
- Create: `frontend/components/fog/FactsPanel.tsx`
- Create: `frontend/components/fog/AttentionPanel.tsx`
- Create: `frontend/components/fog/HealthPanel.tsx`
- Test: `frontend/components/fog/panels.test.tsx`

**Interfaces:**
- Consumes: `FogFact`, `Contested`, `Unmapped`, `Health` from `lib/fog`
- Produces:
  - `FactsPanel({ facts, selected, onSelect }: { facts: FogFact[]; selected: string | null; onSelect: (f: FogFact) => void })`
  - `AttentionPanel({ contested, unmapped }: { contested: Contested[]; unmapped: Unmapped[] })`
  - `HealthPanel({ health }: { health: Health })`

**Presentational only** — no fetching, no query hooks. Each takes data and renders it.

- [ ] **Step 1: Write the failing test**

Create `frontend/components/fog/panels.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FactsPanel } from "./FactsPanel";
import { AttentionPanel } from "./AttentionPanel";
import { HealthPanel } from "./HealthPanel";

describe("FactsPanel", () => {
  const facts = [
    { tag: "PBTR", value: 65487, method: "lexical:section:name", page: 167, label: "PROFIT BEFORE TAX" },
    { tag: "CT", value: 16388, method: "llm", page: 167, label: "Current tax" },
  ];

  it("shows the tag, the value and HOW it was resolved", () => {
    render(<FactsPanel facts={facts} selected={null} onSelect={() => {}} />);
    expect(screen.getByText("PBTR")).toBeInTheDocument();
    expect(screen.getByText(/65,487/)).toBeInTheDocument();
    expect(screen.getByText(/section/)).toBeInTheDocument();
  });

  it("reports a fact whose proof is missing rather than hiding it", () => {
    render(<FactsPanel facts={[{ ...facts[0], method: "" }]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText(/no proof recorded/i)).toBeInTheDocument();
  });

  it("calls back with the fact so the viewer can navigate to its page", async () => {
    const onSelect = vi.fn();
    render(<FactsPanel facts={facts} selected={null} onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: /PBTR/ }));
    expect(onSelect).toHaveBeenCalledWith(facts[0]);
  });

  it("says so when there is nothing rather than rendering an empty box", () => {
    render(<FactsPanel facts={[]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText(/no facts/i)).toBeInTheDocument();
  });
});

describe("AttentionPanel", () => {
  it("names the winner and the losers of a contested tag", () => {
    render(
      <AttentionPanel
        contested={[{ tag: "RGHTUA", spread: 11136, winnerLabel: "Right-of-use assets",
          winnerValue: 11027, losers: [{ label: "Payment for right-of-use assets", value: -109 }] }]}
        unmapped={[]}
      />,
    );
    expect(screen.getByText("RGHTUA")).toBeInTheDocument();
    expect(screen.getByText(/Right-of-use assets/)).toBeInTheDocument();
    expect(screen.getByText(/Payment for right-of-use assets/)).toBeInTheDocument();
  });

  it("distinguishes an outage from an honest refusal", () => {
    render(
      <AttentionPanel
        contested={[]}
        unmapped={[
          { reason: "model_call_failed", labels: ["Travel"] },
          { reason: "model_said_unmapped", labels: ["Facility expenses"] },
        ]}
      />,
    );
    expect(screen.getByText(/call failed/i)).toBeInTheDocument();
    expect(screen.getByText(/declined to map/i)).toBeInTheDocument();
  });

  it("says nothing needs attention when nothing does", () => {
    render(<AttentionPanel contested={[]} unmapped={[]} />);
    expect(screen.getByText(/nothing contested/i)).toBeInTheDocument();
  });
});

describe("HealthPanel", () => {
  const health = {
    footing: [
      { statement: "profit_and_loss", passed: true, rule: "income - expenses = pbt" },
      { statement: "balance_sheet", passed: false, rule: "equity + liabilities = total" },
      { statement: "cash_flow", passed: null, rule: "" },
    ],
    checks: [], coverage: {}, mapping: { llm_calls: 35 }, schemaUsable: true,
  };

  it("marks a passing face and flags a failing one", () => {
    render(<HealthPanel health={health} />);
    expect(screen.getByText("profit_and_loss").closest("li")).toHaveTextContent("✓");
    expect(screen.getByText("balance_sheet").closest("li")).toHaveTextContent("⚑");
  });

  it("renders NO verdict for a face it could not check", () => {
    render(<HealthPanel health={health} />);
    const cf = screen.getByText("cash_flow").closest("li");
    expect(cf).not.toHaveTextContent("✓");
    expect(cf).not.toHaveTextContent("⚑");
    expect(cf).toHaveTextContent(/not checked/i);
  });

  it("shouts when the schema is unusable — it maps nothing, silently", () => {
    render(<HealthPanel health={{ ...health, schemaUsable: false }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/schema/i);
  });

  it("shows no alert when the schema is fine", () => {
    render(<HealthPanel health={health} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- panels`
Expected: FAIL — cannot resolve `./FactsPanel`.

- [ ] **Step 3: Write the panels**

The three components are fully specified by Step 1's tests, which are complete
and exact -- write the minimal component that makes each assertion pass, in the
existing component style in
`frontend/components/analytics/` and `frontend/components/viewer/` — read one
first for class conventions. Requirements the tests encode:

- `FactsPanel` renders each fact as a `<button>` labelled with its tag, the value formatted with `formatInr`-style Indian grouping (reuse `frontend/lib/format.ts` — read it for the exact export name), and the method. `method === ""` renders "no proof recorded". Empty list renders "No facts".
- Method weight tracks the evidence tier, mirroring `core/fog_reason.py::_tier`: `memory`, `lexical:section*`, and `lexical*` with score ≥ 0.95 are heaviest; `llm` mid; anything else lightest. Use font weight and `--ink-soft`/`--ink`, **never a colour**.
- `AttentionPanel` renders contested entries (tag, spread, winner label + value, losers) then unmapped groups. Reason codes render as prose: `model_call_failed` → "model call failed", `model_said_unmapped` → "model declined to map", `no_client` → "no model configured", `no_candidate` / `no_candidate_left` → "no candidate concept". Both empty renders "Nothing contested".
- `HealthPanel` renders footing as an `<li>` per statement with `✓` / `⚑` / the words "not checked"; then checks, coverage and mapping diagnostics as definition lists. `schemaUsable === false` renders a `role="alert"` naming the schema.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- panels`
Expected: PASS, 11 passed

- [ ] **Step 5: Gates and commit**

```bash
cd frontend && npm run typecheck && npm run lint && cd ..
git add frontend/components/fog
git commit -m "feat(ui): facts, attention and health panels"
```

---

## Task 5: `FogViewer` and the route swap

**Files:**
- Create: `frontend/components/fog/FogViewer.tsx`
- Modify: `frontend/app/documents/[docId]/page.tsx`
- Test: `frontend/components/fog/FogViewer.test.tsx`

**Interfaces:**
- Consumes: everything from Tasks 2–4; `getArtifact`, `getDocument`, `pdfUrl` from `lib/api`; `PdfCanvas` from `components/viewer/PdfCanvas`
- Produces: `FogViewer({ docId }: { docId: string })`

- [ ] **Step 1: Write the failing test**

Create `frontend/components/fog/FogViewer.test.tsx`. Read
`frontend/components/viewer/ReportViewer.test.tsx` FIRST and copy its `vi.mock`
and QueryClient setup verbatim -- including how it disables retries, since a
retrying query makes an "artifact missing" assertion hang rather than fail.

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FogViewer } from "./FogViewer";

vi.mock("@/lib/api", () => ({
  getDocument: vi.fn(async () => ({
    id: "doc-1", filename: "tcs.pdf", doc_key: "tcs", version: 1,
    checksum: "a", byte_size: 1, content_type: "application/pdf",
  })),
  pdfUrl: () => "/x.pdf",
  getArtifact: vi.fn(),
}));
vi.mock("@/components/viewer/PdfCanvas", () => ({
  PdfCanvas: ({ page }: { page: number }) => <div data-testid="pdf">page {page}</div>,
}));

const REASON = {
  bases: { consolidated: { facts: { PBTR: 65487 }, contested: [], checks: [], coverage: {} } },
};
const MAPPING = {
  schema: { usable: true },
  diagnostics: { consolidated: { llm_calls: 35 } },
  bases: { consolidated: { proof: [{ line_item: "li:a", tag: "PBTR",
    label: "PROFIT BEFORE TAX", method: "lexical:name", score: 1 }], unmapped: [] } },
};
const FOG = {
  bases: { consolidated: { statements: [], notes: [], edges: [],
    line_items: [{ id: "li:a", label: "PROFIT BEFORE TAX", current: 65487, page: 167 }] } },
};

async function mount(over: Record<string, unknown> = {}) {
  const { getArtifact } = await import("@/lib/api");
  (getArtifact as ReturnType<typeof vi.fn>).mockImplementation(
    async (_id: string, stage: string) => {
      if (stage in over) {
        const v = over[stage];
        if (v instanceof Error) throw v;
        return v;
      }
      return ({ fog_objects: FOG, fog_mapping: MAPPING, fog_reason: REASON } as
        Record<string, unknown>)[stage];
    },
  );
  render(<FogViewer docId="doc-1" />, { wrapper: makeWrapper() });
}

describe("FogViewer", () => {
  it("names the basis it is showing", async () => {
    await mount();
    expect(await screen.findByText(/consolidated/i)).toBeInTheDocument();
  });

  it("names the stage that has not run when an artifact is missing", async () => {
    await mount({ fog_reason: new Error("404") });
    expect(await screen.findByText(/fog_reason/)).toBeInTheDocument();
    expect(screen.getByText(/not built/i)).toBeInTheDocument();
  });

  it("still renders the facts when a DIFFERENT artifact is malformed", async () => {
    await mount({ fog_mapping: "nonsense" });
    expect(await screen.findByText("PBTR")).toBeInTheDocument();
  });

  it("navigates the PDF to a fact page when the fact is clicked", async () => {
    await mount();
    await userEvent.click(await screen.findByRole("button", { name: /PBTR/ }));
    expect(screen.getByTestId("pdf")).toHaveTextContent("page 167");
  });

  it("refuses to guess a basis when the filing carries none", async () => {
    await mount({ fog_reason: { bases: {} } });
    expect(await screen.findByText(/no basis/i)).toBeInTheDocument();
  });
});
```

`makeWrapper()` is whatever `ReportViewer.test.tsx` uses to supply a
`QueryClient` -- reuse it rather than writing a second one.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- FogViewer`
Expected: FAIL — cannot resolve `./FogViewer`.

- [ ] **Step 3: Write `FogViewer`**

Three `useQuery` calls (`fog_objects`, `fog_mapping`, `fog_reason`) via `getArtifact`, each with its own error state so one failure does not blank the page. Then:

```
basis   = chooseBasis(reason)
facts   = readFacts(reason, mapping, fog, basis)
...
```

Left: `PdfCanvas` at the selected page with `regions={null}` — a FOG line item carries its **address**, not its geometry, and resolving `cell_regions` here would be a second implementation of `core/provenance.py` in TypeScript (spec §4). Right: the three panels stacked in one scrolling column.

Selecting a fact sets the page. Name the basis in the header. When `basis === null`, render "no basis in this filing" rather than defaulting to one.

- [ ] **Step 4: Swap the route**

In `frontend/app/documents/[docId]/page.tsx`, replace the `ReportViewer` import and usage with `FogViewer`. Leave `ReportViewer` and `FieldList` on disk, unreferenced — they are the fallback until the backend cutover.

- [ ] **Step 5: Run the full suite**

Run: `cd frontend && npm test -- --run`
Expected: PASS. `ReportViewer.test.tsx` still passes — the component is untouched.

- [ ] **Step 6: Gates and commit**

```bash
cd frontend && npm run typecheck && npm run lint && npm run build && cd ..
git add frontend/components/fog frontend/app/documents
git commit -m "feat(ui): the FOG viewer replaces the similarity-path report screen"
```

---

## Task 6: End-to-end check against the real stack

**Files:** none — this runs the app.

**No new code.** This proves the wiring against a real filing.

- [ ] **Step 1: Bring the stack up**

```bash
docker compose --env-file .env -f docker/compose.yml -f docker/compose.override.yml up -d
cd frontend && npm run dev
```

The API image must contain the FOG stages. If `docker exec fdi-api-1 python -c "import fdi.core.fog_mapping"` fails, rebuild `api` and `worker` first — an image predating the stages will report every FOG artifact as missing, which the UI will correctly render as "not built yet".

- [ ] **Step 2: Upload a filing** at `/ingest` and watch the graph. Confirm the six new stages appear and that the screen waits for `fog_reason`, not `index`.

- [ ] **Step 3: Open the filing** and confirm: the basis is named; facts list with methods; clicking a fact moves the PDF; contested entries are ordered by spread; unmapped groups read as prose; footing shows `✓`/`⚑`/"not checked" correctly.

- [ ] **Step 4: Record what you saw** in `.superpowers/sdd/<plan>/progress.md`, including anything that rendered wrongly. A screen that looks right on fixtures and wrong on a real filing is the only outcome that matters here.

---

## What this plan deliberately does NOT do

- **No backend change.** No new endpoint, no stage edit.
- **No cell-precise highlighting.** Page-level only; the geometry endpoint is a follow-up (spec §4).
- **Nothing deleted.** `ReportViewer` and `FieldList` stay on disk.
- **No charts.** `ConfidenceHistogram` already exists; more would decorate rather than inform.
