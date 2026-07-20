# SP-20 Frontend UI Implementation Plan

> **STATUS: implemented 2026-07-21.** All eight tasks landed, plus a fifth screen
> (`/analytics`) and a backend `GET /analytics` this plan did not anticipate.
> Frontend gates green: 48 tests, `typecheck`, `lint`, `next build`.
>
> **Where the shipped code deliberately departs from this document.** The plan is
> kept as written for its reasoning; the code is the truth.
>
> 1. **`--tether` was used against its own rule in three places** — the active nav
>    marker (Task 2 Step 6), the selected field in `ModuleTree` (Task 7 Step 5), and
>    the `failed` stage mark (Task 4 Step 5). Red is provenance-only. Shipped:
>    active states use `inset 2px 0 0 0 var(--ink)`; a failed stage is a struck ink
>    chip, legible without borrowing a reserved hue.
> 2. **`FieldEditor`'s aliases textarea could not be typed into.** Feeding
>    `aliases.join("\n")` back as the value while trimming on every keystroke
>    deletes the space and newline mid-word — `"Total assets\nSum of assets"`
>    collapsed to `"TotalassetsSumofassets"`. Shipped: an `AliasesBox` holding raw
>    text, normalizing only on the way out, keyed on `field_path`.
> 3. **The tether drifted off its endpoints on scroll.** Task 8's `useLayoutEffect`
>    re-measured only on `[selected, fields]`. Shipped: re-measures on `resize` and
>    capture-phase `scroll` too, and the tether is withdrawn below 1024px where the
>    viewer stacks.
> 4. **Response shapes.** SP-19 shipped `stage_counts` as a `{status: count}` map
>    (not flat `stages_done`/`stages_total`), wrapped the schema in a
>    `SchemaView` envelope under `schema_document`, and made `bounding_regions`
>    always a list. `lib/types.ts` mirrors what shipped.
> 5. **Design tokens were warmed up** per the user: real elevation, 6px control
>    radius, a dark theme, and a responsive shell (rail above 768px, top bar below).
> 6. **`vitest.config.mts`, not `.ts`** — `package.json` has no `"type": "module"`
>    and vite 7 is ESM-only, so a `.ts` config loads as CJS and throws.
>
> **Still deferred:** persisted extraction results. `POST /extract/document` is
> synchronous and stores nothing, so `/analytics` reports corpus and pipeline
> figures from the database but reads extraction quality fresh, one filing at a
> time. Corpus-wide quality trends need a stored-extraction table first.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the SP-18 thin UI with a four-screen product interface — filings dashboard, ingestion, schema editor, and a side-by-side annual-report viewer that joins each extracted field to the region of the PDF it came from.

**Architecture:** Next.js App Router, client-rendered screens talking to the FastAPI backend over `NEXT_PUBLIC_API_URL`. A hand-rolled design system lives entirely in CSS custom properties in `app/globals.css` (Tailwind v4 `@theme inline`) — no component library, so the UI carries its own identity. TanStack Query owns all server state, including the 1s stage polling that `app/page.tsx` currently does by hand. The PDF is rendered with `pdfjs-dist` directly to a canvas so we control the bounding-region overlay; `react-pdf` is deliberately avoided because it does not give us the overlay coordinate space we need.

**Tech Stack:** Next.js 16.2.10, React 19.2.4, TypeScript 5, Tailwind CSS v4, TanStack Query v5, pdfjs-dist v4, Vitest + React Testing Library + jsdom, MSW v2, pnpm 9.15.9.

## Global Constraints

- **This plan depends on SP-19** (`docs/superpowers/plans/2026-07-20-sp19-ui-backend-enablement.md`). Tasks 5–8 consume endpoints SP-19 creates: `GET /documents` (list-all), `GET /documents/{doc_id}/file`, `bounding_regions` on `ExtractFieldView.source`, and `GET/PUT /schema`. Do not start Task 8 before SP-19 Task B4 lands. Tasks 1–4 have no backend dependency.
- `lib/types.ts` and `test/handlers.ts` assume `GET /documents` returns `{documents: DocumentView[], total: number}` and that `ExtractFieldView.source` gains `bounding_regions`. Before starting Task 5, read the response models SP-19 actually shipped in `backend/src/fdi/api/schemas.py` and reconcile — the backend is the source of truth, not this plan.
- Package manager is **pnpm 9.15.9** via corepack. Never run `npm install` in `frontend/`.
- The `@/*` path alias already exists in `frontend/tsconfig.json` and matches the Vitest alias in Task 1. Do not add a second one.
- All commands in this plan run from `D:\FinancialDocumentIntelligence\frontend`.
- Gates that must pass before every commit: `pnpm lint`, `pnpm typecheck`, `pnpm test --run`.
- API base URL is always read as `process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"`. Never hardcode a URL in a component.
- Backend CORS allows exactly one origin. Until SP-19 Task B6 lands, the dev server **must** run on `http://localhost:3000` — not 3001, not `127.0.0.1:3000`.
- Every color, font size, spacing step, and radius used in a component must come from a CSS variable defined in `app/globals.css`. A literal hex value or a raw `px` font size in a component file is a review rejection.
- Every numeral rendered anywhere in the app — money, counts, confidence, page numbers, version numbers — uses `var(--font-data)` with `font-variant-numeric: tabular-nums`.
- Accessibility floor, not optional: every interactive element is reachable and operable by keyboard with a visible `:focus-visible` ring; all motion is wrapped in `@media (prefers-reduced-motion: no-preference)`; the layout is usable down to 390px wide.
- Commit message convention follows the repo: `feat(ui): ...`, `fix(ui): ...`, `chore(ui): ...`.

---

## Design Direction — "Statutory Ledger"

This section is the contract for Task 2 and the reference for every later task. Implementers: read it before writing any component.

**The subject.** This tool exists so an analyst can decide whether to trust a number a machine read out of an annual report. Everything in the interface serves that one judgment. The visual language therefore comes from the annual report itself — statutory filing paper, ledger ruling, the auditor's tick, and above all the **cross-reference**: in a real annual report every figure on the balance sheet carries a superscript pointing to a note elsewhere in the book. That superscript is exactly what this product automates. It is the motif.

**Color.** Six values plus two states. The background is a cool pale sage-grey, not cream — statutory filing paper, and a deliberate step away from the warm-cream palette that has become the default AI-design answer.

| Token | Value | Role |
|---|---|---|
| `--ink` | `#111820` | Primary text. Near-black with a blue cast. |
| `--ink-soft` | `#59636E` | Secondary text, labels, captions. |
| `--paper` | `#E9ECE6` | App background. Cool pale sage-grey. |
| `--leaf` | `#F8F9F6` | Sheet/card surface — the page laid on the desk. |
| `--rule` | `#C4CABF` | Ledger ruling, borders, dividers. |
| `--tether` | `#B3341C` | **Provenance only.** Reference marks, source links, the tether line. Never decorative, never a generic accent, never a call-to-action color. |
| `--tick` | `#1E5C48` | Verified / stage complete. |
| `--flag` | `#8A6A12` | Needs review / low confidence. Ochre, not red — red is reserved for provenance. |

The discipline that makes this work: `--tether` appears **only** where the UI is asserting "this came from there." If a button is red, the system has lied.

**Type.** Three roles, three families.

- **Display — Archivo**, variable width axis pushed to ~115, weight 700. Wide and official, like statutory signage. Used with restraint: page titles and statement headers only. Never body copy.
- **Body/UI — Public Sans**, 400/500/600. Civic and institutional; it reads as government form rather than startup SaaS.
- **Data — IBM Plex Mono**, 400/500, `tabular-nums`. Every numeral, `field_name`, `chunk_id`, page reference, and confidence score. This is the largest single visual decision in the app: in a financial tool, monospaced tabular figures are not a stylistic flourish, they are what makes columns of money comparable at a glance.

Scale: 11 / 12 / 13 / 15 / 18 / 24 / 34 / 52. Display sizes are 34 and 52 only.

**Layout.** A fixed 216px left spine holds the ledger index (nav + document list). Content occupies the rest. The viewer screen splits that content area into two resizable columns with a draggable rule between them.

**Signature — the tether.** When a field is selected in the viewer, an SVG path in `--tether` is drawn from the field row across the gutter to the highlighted bounding region on the PDF page. It draws itself once, over 240ms, then holds. This is the only orchestrated motion in the application.

Two supporting moves carry the same idea into every other screen:

- **Reference marks.** Every extracted value carries a small mono superscript — `ᵖ84` — set in `--tether`. It is the click target that selects the field and scrolls the PDF. It looks like the note cross-reference in the source document because it *is* one.
- **Confidence as ink density.** A value's text opacity is a function of its confidence: `0.45 + 0.55 × confidence`. A column the model is unsure about literally looks faded on the page, the way a weak carbon copy does. Values below 0.5 additionally get an ochre `--flag` underline. This means an analyst can spot the soft numbers by squinting, before reading a single score.

**What was deliberately rejected.** Numbered `01 / 02 / 03` markers are used in exactly one place — the pipeline stage DAG — because that content genuinely is an ordered dependency sequence; they are not used as decoration anywhere else. Hairline rules are present but functional: they are row separators in financial tables, which is where ruling belongs. There is no ambient animation, no scroll-triggered reveal, and no page-load stagger; the boldness budget is spent entirely on the tether.

---

## File Structure

```
frontend/
  app/
    layout.tsx                   Fonts, providers, app shell (MODIFY — currently create-next-app default)
    globals.css                  The entire design system (MODIFY — currently 26 lines of stubs)
    page.tsx                     Dashboard / filings index (REPLACE — currently the SP-18 thin UI)
    ingest/page.tsx              Upload + live pipeline DAG
    schema/page.tsx              Schema editor
    documents/[docId]/page.tsx   Report viewer
  lib/
    api.ts                       Typed fetch client — one function per endpoint
    types.ts                     Backend view-model types, mirroring api/schemas.py
    format.ts                    Money/date/confidence formatting, pure
    stages.ts                    The 16-stage DAG topology + layering, pure
  components/
    shell/Spine.tsx              Left navigation rail
    shell/PageHeader.tsx         Display-type page title block
    data/Value.tsx               A value + reference mark + ink-density styling
    data/RefMark.tsx             The ᵖ84 superscript
    data/ConfidenceBar.tsx       Ledger-rule confidence indicator
    pipeline/StageGraph.tsx      DAG ribbon, used by dashboard + ingest
    viewer/PdfCanvas.tsx         pdfjs-dist canvas + bbox overlay
    viewer/FieldList.tsx         Fields grouped by retrieval unit
    viewer/Tether.tsx            The signature SVG connector
    schema/ModuleTree.tsx        Modules → retrieval units → fields navigation
    schema/FieldEditor.tsx       Per-field form
  test/
    setup.ts                     Vitest + RTL + MSW wiring
    handlers.ts                  MSW request handlers
```

---

### Task 1: Toolchain and test harness

Nothing in this plan can be verified without a test runner, and the frontend has none. This task adds every dependency the plan needs and proves the harness runs.

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/vitest.config.ts`
- Create: `frontend/test/setup.ts`
- Create: `frontend/test/handlers.ts`
- Create: `frontend/lib/format.ts`
- Create: `frontend/lib/format.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `pnpm test`, `pnpm typecheck` scripts; `test/handlers.ts` exporting `handlers: RequestHandler[]`; `lib/format.ts` exporting `formatAmount(value: unknown): string`, `inkOpacity(confidence: number): number`, `formatConfidence(confidence: number): string`.

- [ ] **Step 1: Install dependencies**

```bash
pnpm add @tanstack/react-query@^5 pdfjs-dist@^4 clsx@^2
pnpm add -D vitest@^3 @vitejs/plugin-react@^5 jsdom@^26 @testing-library/react@^16 @testing-library/user-event@^14 @testing-library/jest-dom@^6 msw@^2
```

- [ ] **Step 2: Add scripts to `package.json`**

Add to the `"scripts"` object:

```json
    "test": "vitest",
    "typecheck": "tsc --noEmit"
```

- [ ] **Step 3: Create `vitest.config.ts`**

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./test/setup.ts"],
    globals: true,
  },
  resolve: {
    alias: { "@": path.resolve(__dirname, ".") },
  },
});
```

- [ ] **Step 4: Create `test/handlers.ts`**

```ts
import { http, HttpResponse } from "msw";

const API = "http://localhost:8000";

export const handlers = [
  http.get(`${API}/documents`, () =>
    HttpResponse.json({
      documents: [
        {
          id: "doc-1",
          checksum: "abc123",
          filename: "annual-report-2025.pdf",
          byte_size: 4_200_000,
          doc_key: "annual-report-2025.pdf",
          version: 1,
          content_type: "application/pdf",
        },
      ],
      total: 1,
    }),
  ),
  http.get(`${API}/documents/:docId/stages`, () =>
    HttpResponse.json({
      stages: [
        { name: "ocr", status: "done", error: null },
        { name: "ast", status: "running", error: null },
      ],
    }),
  ),
];
```

- [ ] **Step 5: Create `test/setup.ts`**

```ts
import "@testing-library/jest-dom/vitest";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll } from "vitest";
import { cleanup } from "@testing-library/react";
import { handlers } from "./handlers";

export const server = setupServer(...handlers);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  cleanup();
});
afterAll(() => server.close());
```

- [ ] **Step 6: Write the failing test — `lib/format.test.ts`**

```ts
import { describe, expect, it } from "vitest";
import { formatAmount, formatConfidence, inkOpacity } from "./format";

describe("formatAmount", () => {
  it("groups an integer in the Indian numbering system", () => {
    expect(formatAmount(124850)).toBe("1,24,850.00");
  });

  it("renders a null value as an em dash", () => {
    expect(formatAmount(null)).toBe("—");
  });

  it("passes a non-numeric value through unchanged", () => {
    expect(formatAmount("Reliance Industries Limited")).toBe(
      "Reliance Industries Limited",
    );
  });
});

describe("inkOpacity", () => {
  it("renders a fully confident value at full ink", () => {
    expect(inkOpacity(1)).toBe(1);
  });

  it("renders a zero-confidence value at the floor, still legible", () => {
    expect(inkOpacity(0)).toBe(0.45);
  });

  it("clamps a confidence outside 0..1", () => {
    expect(inkOpacity(-3)).toBe(0.45);
    expect(inkOpacity(9)).toBe(1);
  });
});

describe("formatConfidence", () => {
  it("renders two decimal places", () => {
    expect(formatConfidence(0.9234)).toBe("0.92");
  });
});
```

- [ ] **Step 7: Run the test and watch it fail**

Run: `pnpm test --run lib/format.test.ts`
Expected: FAIL — `Failed to resolve import "./format"`.

- [ ] **Step 8: Write `lib/format.ts`**

```ts
const INDIAN_GROUPING = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Extracted values arrive as `unknown` — the schema's data_type says what
 *  the field SHOULD be, but the LLM may return anything, and a failed
 *  extraction returns null. Numbers are grouped lakh/crore style because
 *  every source document in this corpus is an Indian statutory filing. */
export function formatAmount(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number" && Number.isFinite(value)) {
    return INDIAN_GROUPING.format(value);
  }
  return String(value);
}

/** Confidence rendered as ink density: a value the model is unsure about
 *  literally looks faded. Floored at 0.45 so it stays legible — a fully
 *  transparent number would be a bug, not a signal. */
export function inkOpacity(confidence: number): number {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(confidence) ? confidence : 0));
  return Number((0.45 + 0.55 * clamped).toFixed(4));
}

export function formatConfidence(confidence: number): string {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(confidence) ? confidence : 0));
  return clamped.toFixed(2);
}
```

- [ ] **Step 9: Run the test and watch it pass**

Run: `pnpm test --run lib/format.test.ts`
Expected: PASS — 7 tests.

- [ ] **Step 10: Commit**

```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/vitest.config.ts frontend/test frontend/lib
git commit -m "chore(ui): add vitest/RTL/MSW harness and value formatting helpers"
```

---

### Task 2: Design tokens and app shell

This is where the design direction becomes code. Everything after this task consumes these variables and adds no new ones.

**Files:**
- Modify: `frontend/app/globals.css` (replace all 26 lines)
- Modify: `frontend/app/layout.tsx` (replace fonts and metadata)
- Create: `frontend/components/shell/Spine.tsx`
- Create: `frontend/components/shell/PageHeader.tsx`
- Create: `frontend/components/shell/Spine.test.tsx`

**Interfaces:**
- Consumes: nothing from Task 1 except the test harness.
- Produces: the token set below; `<Spine />` (no props); `<PageHeader title: string, eyebrow?: string, children?: ReactNode />`.

- [ ] **Step 1: Replace `app/globals.css`**

```css
@import "tailwindcss";

:root {
  /* Color — see the Design Direction section of the SP-20 plan.
     --tether is provenance-only: it marks "this came from there",
     and is never used for a button, a link, or an accent. */
  --ink: #111820;
  --ink-soft: #59636e;
  --paper: #e9ece6;
  --leaf: #f8f9f6;
  --rule: #c4cabf;
  --tether: #b3341c;
  --tick: #1e5c48;
  --flag: #8a6a12;

  /* Type */
  --font-display: var(--font-archivo), system-ui, sans-serif;
  --font-body: var(--font-public-sans), system-ui, sans-serif;
  --font-data: var(--font-plex-mono), ui-monospace, monospace;

  --text-2xs: 0.6875rem; /* 11 — reference marks */
  --text-xs: 0.75rem;    /* 12 — captions, chunk ids */
  --text-sm: 0.8125rem;  /* 13 — table data, labels */
  --text-base: 0.9375rem;/* 15 — body */
  --text-md: 1.125rem;   /* 18 — section headings */
  --text-lg: 1.5rem;     /* 24 — card titles */
  --text-xl: 2.125rem;   /* 34 — display */
  --text-2xl: 3.25rem;   /* 52 — display */

  /* Space — 4px base */
  --sp-1: 0.25rem;
  --sp-2: 0.5rem;
  --sp-3: 0.75rem;
  --sp-4: 1rem;
  --sp-6: 1.5rem;
  --sp-8: 2rem;
  --sp-12: 3rem;

  /* Structure */
  --spine-w: 216px;
  --radius: 2px; /* statutory paper does not have rounded corners */
  --hairline: 1px solid var(--rule);
}

@theme inline {
  --color-ink: var(--ink);
  --color-ink-soft: var(--ink-soft);
  --color-paper: var(--paper);
  --color-leaf: var(--leaf);
  --color-rule: var(--rule);
  --color-tether: var(--tether);
  --color-tick: var(--tick);
  --color-flag: var(--flag);
  --font-display: var(--font-display);
  --font-body: var(--font-body);
  --font-data: var(--font-data);
}

body {
  background: var(--paper);
  color: var(--ink);
  font-family: var(--font-body);
  font-size: var(--text-base);
  line-height: 1.5;
}

/* Display type: Archivo pushed wide on its width axis. Restraint is the
   rule — this class appears on page titles and statement headers only. */
.display {
  font-family: var(--font-display);
  font-variation-settings: "wdth" 115;
  font-weight: 700;
  letter-spacing: -0.015em;
  line-height: 1.05;
}

/* Every numeral in the application. Tabular figures are what make a
   column of money comparable at a glance. */
.data {
  font-family: var(--font-data);
  font-variant-numeric: tabular-nums;
  font-feature-settings: "zero" 1;
}

.eyebrow {
  font-family: var(--font-data);
  font-size: var(--text-2xs);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-soft);
}

.sheet {
  background: var(--leaf);
  border: var(--hairline);
  border-radius: var(--radius);
}

:focus-visible {
  outline: 2px solid var(--tether);
  outline-offset: 2px;
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

- [ ] **Step 2: Replace `app/layout.tsx`**

```tsx
import type { Metadata } from "next";
import { Archivo, IBM_Plex_Mono, Public_Sans } from "next/font/google";
import "./globals.css";
import { Spine } from "@/components/shell/Spine";
import { Providers } from "./providers";

const archivo = Archivo({
  variable: "--font-archivo",
  subsets: ["latin"],
  axes: ["wdth"],
});

const publicSans = Public_Sans({
  variable: "--font-public-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "Financial Document Intelligence",
  description: "Read, extract, and verify figures from annual reports.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${archivo.variable} ${publicSans.variable} ${plexMono.variable} h-full antialiased`}
    >
      <body className="min-h-full">
        <Providers>
          <div className="flex min-h-screen">
            <Spine />
            <main className="flex-1 min-w-0">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
```

- [ ] **Step 3: Create `app/providers.tsx`**

```tsx
"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { staleTime: 5_000, retry: 1 } },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
```

- [ ] **Step 4: Write the failing test — `components/shell/Spine.test.tsx`**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Spine } from "./Spine";

describe("Spine", () => {
  it("names each destination by what the analyst does there", () => {
    render(<Spine />);
    expect(screen.getByRole("link", { name: "Filings" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Add a filing" })).toHaveAttribute(
      "href",
      "/ingest",
    );
    expect(screen.getByRole("link", { name: "Extraction schema" })).toHaveAttribute(
      "href",
      "/schema",
    );
  });

  it("exposes the rail as a navigation landmark", () => {
    render(<Spine />);
    expect(screen.getByRole("navigation")).toBeInTheDocument();
  });
});
```

- [ ] **Step 5: Run the test and watch it fail**

Run: `pnpm test --run components/shell/Spine.test.tsx`
Expected: FAIL — `Failed to resolve import "./Spine"`.

- [ ] **Step 6: Create `components/shell/Spine.tsx`**

```tsx
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const DESTINATIONS = [
  { href: "/", label: "Filings" },
  { href: "/ingest", label: "Add a filing" },
  { href: "/schema", label: "Extraction schema" },
] as const;

export function Spine() {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Sections"
      className="shrink-0 border-r border-rule bg-leaf"
      style={{ width: "var(--spine-w)" }}
    >
      <div className="px-4 py-6 border-b border-rule">
        <div className="eyebrow">Financial document</div>
        <div className="display" style={{ fontSize: "var(--text-md)" }}>
          Intelligence
        </div>
      </div>
      <ul className="py-2">
        {DESTINATIONS.map((d) => {
          const active = pathname === d.href;
          return (
            <li key={d.href}>
              <Link
                href={d.href}
                aria-current={active ? "page" : undefined}
                className="block px-4 py-2 text-ink hover:bg-paper"
                style={{
                  fontSize: "var(--text-sm)",
                  fontWeight: active ? 600 : 400,
                  borderLeft: active
                    ? "2px solid var(--tether)"
                    : "2px solid transparent",
                }}
              >
                {d.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
```

- [ ] **Step 7: Create `components/shell/PageHeader.tsx`**

```tsx
export function PageHeader({
  eyebrow,
  title,
  children,
}: {
  eyebrow?: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <header
      className="border-b border-rule px-8 py-6 flex items-end justify-between gap-6"
      style={{ background: "var(--leaf)" }}
    >
      <div>
        {eyebrow ? <div className="eyebrow mb-1">{eyebrow}</div> : null}
        <h1 className="display" style={{ fontSize: "var(--text-xl)" }}>
          {title}
        </h1>
      </div>
      {children ? <div className="flex items-center gap-3">{children}</div> : null}
    </header>
  );
}
```

- [ ] **Step 8: Run the test and watch it pass**

Run: `pnpm test --run components/shell/Spine.test.tsx`
Expected: PASS — 2 tests.

- [ ] **Step 9: Verify the fonts actually load**

Run: `pnpm dev`, open `http://localhost:3000`, and confirm in DevTools that `Archivo`, `Public Sans`, and `IBM Plex Mono` appear under Network → Font. If `next/font/google` rejects `axes: ["wdth"]` for Archivo, drop the `axes` option and remove `font-variation-settings` from `.display`, replacing it with `letter-spacing: 0.01em` — record the change in the commit body.

- [ ] **Step 10: Commit**

```bash
git add frontend/app frontend/components
git commit -m "feat(ui): statutory-ledger design tokens, fonts, and app shell"
```

---

### Task 3: Typed API client

Every screen goes through this file. Types mirror `backend/src/fdi/api/schemas.py` exactly — when they drift, this file is the single place to fix.

**Files:**
- Create: `frontend/lib/types.ts`
- Create: `frontend/lib/api.ts`
- Create: `frontend/lib/api.test.ts`

**Interfaces:**
- Consumes: `test/handlers.ts` from Task 1.
- Produces: `listDocuments()`, `getDocument(docId)`, `getStages(docId)`, `uploadDocument(file, docKey?)`, `startStage(docId, stage)`, `getArtifact(docId, stage)`, `pdfUrl(docId)`, `extractDocument(docKey, module?)`, `getSchema()`, `putSchema(doc, baseVersion)`.

- [ ] **Step 1: Create `lib/types.ts`**

```ts
/** Mirrors backend/src/fdi/api/schemas.py. Keep field names identical to
 *  the Pydantic views — no camelCase translation layer. */

export type StageStatus = "queued" | "running" | "done" | "failed";

export interface DocumentView {
  id: string;
  checksum: string;
  filename: string;
  byte_size: number;
  doc_key: string;
  version: number;
  content_type: string;
}

export interface DocumentListView {
  documents: DocumentView[];
  total: number;
}

export interface StageView {
  name: string;
  status: StageStatus;
  error: string | null;
}

export interface StagesView {
  stages: StageView[];
}

export interface BoundingRegion {
  page_number: number;
  polygon: number[];
}

export interface ExtractFieldView {
  field_name: string;
  value: unknown;
  source: {
    page: number | null;
    snippet: string | null;
    evidence_ids: string[];
    bounding_regions: BoundingRegion[] | null;
  };
  confidence: number;
}

export interface ExtractDocumentView {
  doc_key: string;
  doc_id: string;
  module: string | null;
  field_count: number;
  fields: ExtractFieldView[];
}

export interface SchemaField {
  field_name: string;
  field_path: string;
  canonical_label: string;
  aliases: string[];
  data_type: "decimal" | "integer" | "string" | "date" | "list";
  description: string;
  extraction_prompt: string;
  example?: string;
  normalization?: string;
}

export interface RetrievalUnit {
  unit_id: string;
  title: string;
  scope: "primary_statement" | "note" | "front_matter" | "back_matter";
  statement_type: "BS" | "PL" | "CF" | null;
  locator_keywords: string[];
  retrieval_query: string;
  context_hint?: string;
  validation?: { rules: string[]; tolerance?: number };
  field_count: number;
  fields: SchemaField[];
}

export interface SchemaModule {
  id: number;
  key: string;
  name: string;
  retrieval_unit_count: number;
  retrieval_units: RetrievalUnit[];
}

export interface ExtractionSchema {
  version: string;
  title: string;
  description: string;
  modules: SchemaModule[];
}
```

- [ ] **Step 2: Write the failing test — `lib/api.test.ts`**

```ts
import { describe, expect, it } from "vitest";
import { HttpResponse, http } from "msw";
import { server } from "@/test/setup";
import { getStages, listDocuments, pdfUrl } from "./api";

describe("listDocuments", () => {
  it("returns the filings the backend reports", async () => {
    const result = await listDocuments();
    expect(result.total).toBe(1);
    expect(result.documents[0].doc_key).toBe("annual-report-2025.pdf");
  });

  it("reports the status and the body when the backend rejects the call", async () => {
    server.use(
      http.get("http://localhost:8000/documents", () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 503 }),
      ),
    );
    await expect(listDocuments()).rejects.toThrow(
      "GET /documents failed (503): database unavailable",
    );
  });
});

describe("getStages", () => {
  it("returns one entry per stage that has run", async () => {
    const result = await getStages("doc-1");
    expect(result.stages.map((s) => s.name)).toEqual(["ocr", "ast"]);
  });
});

describe("pdfUrl", () => {
  it("points at the API's own file endpoint, not at object storage", () => {
    expect(pdfUrl("doc-1")).toBe("http://localhost:8000/documents/doc-1/file");
  });
});
```

- [ ] **Step 3: Run the test and watch it fail**

Run: `pnpm test --run lib/api.test.ts`
Expected: FAIL — `Failed to resolve import "./api"`.

- [ ] **Step 4: Create `lib/api.ts`**

```ts
import type {
  DocumentListView,
  DocumentView,
  ExtractDocumentView,
  ExtractionSchema,
  StagesView,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Every failure surfaces the method, path, status, and the backend's own
 *  `detail` string. An error the analyst can act on beats a stack trace. */
async function request<T>(
  path: string,
  init?: RequestInit & { method?: string },
): Promise<T> {
  const method = init?.method ?? "GET";
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* body was not JSON; the status text stands */
    }
    throw new Error(`${method} ${path} failed (${response.status}): ${detail}`);
  }
  return (await response.json()) as T;
}

export const listDocuments = (limit = 50, offset = 0) =>
  request<DocumentListView>(`/documents?limit=${limit}&offset=${offset}`);

export const getDocument = (docId: string) =>
  request<DocumentView>(`/documents/${docId}`);

export const getStages = (docId: string) =>
  request<StagesView>(`/documents/${docId}/stages`);

export const getArtifact = (docId: string, stage: string) =>
  request<unknown>(`/documents/${docId}/artifacts/${stage}`);

export const startStage = (docId: string, stage: string) =>
  request<{ doc_id: string; stage: string }>(`/documents/${docId}/${stage}`, {
    method: "POST",
  });

export const extractDocument = (docKey: string, module?: string) =>
  request<ExtractDocumentView>("/extract/document", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ doc_key: docKey, module: module ?? null }),
  });

export const getSchema = () => request<ExtractionSchema>("/schema");

export const putSchema = (schema: ExtractionSchema, baseVersion: string) =>
  request<ExtractionSchema>("/schema", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ base_version: baseVersion, schema }),
  });

export async function uploadDocument(
  file: File,
  docKey?: string,
): Promise<DocumentView> {
  const form = new FormData();
  form.append("file", file);
  if (docKey) form.append("doc_key", docKey);
  return request<DocumentView>("/documents", { method: "POST", body: form });
}

/** The PDF is proxied through the API rather than served from a presigned
 *  MinIO URL — the SP-18 design decision, kept so the browser only ever
 *  talks to one origin and MinIO needs no CORS configuration. */
export const pdfUrl = (docId: string) => `${API_BASE}/documents/${docId}/file`;
```

- [ ] **Step 5: Run the test and watch it pass**

Run: `pnpm test --run lib/api.test.ts`
Expected: PASS — 4 tests.

- [ ] **Step 6: Commit**

```bash
git add frontend/lib
git commit -m "feat(ui): typed API client mirroring the FastAPI view models"
```

---

### Task 4: Pipeline stage graph

Shared by the dashboard and the ingestion screen. The topology is hardcoded on the client because no endpoint returns it — the layering must be derived, not hand-positioned, so a new stage cannot silently render in the wrong column.

**Files:**
- Create: `frontend/lib/stages.ts`
- Create: `frontend/lib/stages.test.ts`
- Create: `frontend/components/pipeline/StageGraph.tsx`

**Interfaces:**
- Consumes: `StageView`, `StageStatus` from `lib/types.ts`.
- Produces: `STAGES: readonly {name: string; deps: readonly string[]}[]`; `layerStages(): string[][]`; `<StageGraph stages: StageView[] />`.

- [ ] **Step 1: Write the failing test — `lib/stages.test.ts`**

```ts
import { describe, expect, it } from "vitest";
import { STAGES, layerStages } from "./stages";

describe("layerStages", () => {
  it("puts ocr alone in the first layer — everything depends on it", () => {
    expect(layerStages()[0]).toEqual(["ocr"]);
  });

  it("places every stage after all of its dependencies", () => {
    const layers = layerStages();
    const layerOf = new Map<string, number>();
    layers.forEach((layer, i) => layer.forEach((name) => layerOf.set(name, i)));
    for (const stage of STAGES) {
      for (const dep of stage.deps) {
        expect(layerOf.get(stage.name)!).toBeGreaterThan(layerOf.get(dep)!);
      }
    }
  });

  it("lays out every stage exactly once", () => {
    const flat = layerStages().flat();
    expect(flat).toHaveLength(STAGES.length);
    expect(new Set(flat).size).toBe(STAGES.length);
  });
});
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `pnpm test --run lib/stages.test.ts`
Expected: FAIL — `Failed to resolve import "./stages"`.

- [ ] **Step 3: Create `lib/stages.ts`**

Topology transcribed from `backend/src/fdi/pipeline/stages/*.py`. `ping` is the walking-skeleton no-op and is deliberately excluded from the display graph.

```ts
export const STAGES = [
  { name: "ocr", deps: [] },
  { name: "normalize", deps: ["ocr"] },
  { name: "structure", deps: ["ocr"] },
  { name: "linking", deps: ["ocr"] },
  { name: "ast", deps: ["normalize", "ocr"] },
  { name: "semantic", deps: ["ast"] },
  { name: "ontology", deps: ["ast", "structure"] },
  { name: "graph", deps: ["ast", "structure"] },
  { name: "graph_load", deps: ["graph"] },
  { name: "provenance", deps: ["graph", "ocr"] },
  { name: "quality", deps: ["ast", "structure", "ontology"] },
  { name: "knowledge", deps: ["semantic", "ontology", "quality"] },
  { name: "chunks", deps: ["ast", "semantic", "provenance", "structure"] },
  { name: "embedding", deps: ["chunks"] },
  { name: "index", deps: ["embedding", "chunks"] },
] as const satisfies readonly { name: string; deps: readonly string[] }[];

/** Longest-path layering: a stage sits one column right of its deepest
 *  dependency. Derived rather than hand-positioned so adding a stage to
 *  STAGES cannot place it in a column that contradicts its deps. */
export function layerStages(): string[][] {
  const depth = new Map<string, number>();

  const depthOf = (name: string): number => {
    const cached = depth.get(name);
    if (cached !== undefined) return cached;
    const stage = STAGES.find((s) => s.name === name);
    const value =
      !stage || stage.deps.length === 0
        ? 0
        : 1 + Math.max(...stage.deps.map(depthOf));
    depth.set(name, value);
    return value;
  };

  const layers: string[][] = [];
  for (const stage of STAGES) {
    const d = depthOf(stage.name);
    (layers[d] ??= []).push(stage.name);
  }
  return layers;
}
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `pnpm test --run lib/stages.test.ts`
Expected: PASS — 3 tests.

- [ ] **Step 5: Create `components/pipeline/StageGraph.tsx`**

Ordinals appear here — and only here — because the pipeline genuinely is an ordered dependency sequence.

```tsx
import type { StageStatus, StageView } from "@/lib/types";
import { layerStages } from "@/lib/stages";

const MARK: Record<StageStatus | "pending", { color: string; glyph: string }> = {
  done: { color: "var(--tick)", glyph: "✓" },
  running: { color: "var(--flag)", glyph: "●" },
  queued: { color: "var(--ink-soft)", glyph: "·" },
  failed: { color: "var(--tether)", glyph: "✕" },
  pending: { color: "var(--rule)", glyph: "·" },
};

export function StageGraph({ stages }: { stages: StageView[] }) {
  const byName = new Map(stages.map((s) => [s.name, s]));
  const layers = layerStages();

  return (
    <div className="flex gap-6 overflow-x-auto pb-2">
      {layers.map((layer, index) => (
        <div key={index} className="shrink-0">
          <div className="eyebrow mb-2">{String(index + 1).padStart(2, "0")}</div>
          <ul className="space-y-1">
            {layer.map((name) => {
              const run = byName.get(name);
              const mark = MARK[run?.status ?? "pending"];
              return (
                <li key={name} className="flex items-center gap-2">
                  <span
                    className="data"
                    aria-hidden="true"
                    style={{ color: mark.color, fontSize: "var(--text-sm)" }}
                  >
                    {mark.glyph}
                  </span>
                  <span
                    className="data"
                    style={{
                      fontSize: "var(--text-xs)",
                      color: run ? "var(--ink)" : "var(--ink-soft)",
                    }}
                    title={run?.error ?? undefined}
                  >
                    {name}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 6: Commit**

```bash
git add frontend/lib/stages.ts frontend/lib/stages.test.ts frontend/components/pipeline
git commit -m "feat(ui): derive and render the 15-stage pipeline DAG"
```

---

### Task 5: Filings dashboard

Replaces the SP-18 thin UI at `app/page.tsx`. It is a ledger index of filings, not a grid of KPI tiles — the analyst's question on opening this app is "which filings are ready to work on," and the screen answers exactly that.

**Depends on:** SP-19 Task B2 (`GET /documents` list-all).

**Files:**
- Modify: `frontend/app/page.tsx` (replace all 81 lines)
- Create: `frontend/components/data/FilingRow.tsx`
- Create: `frontend/app/page.test.tsx`

**Interfaces:**
- Consumes: `listDocuments`, `getStages` from `lib/api.ts`; `<StageGraph />`; `<PageHeader />`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test — `app/page.test.tsx`**

```tsx
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import Dashboard from "./page";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("Dashboard", () => {
  it("lists each filing by the name the analyst uploaded", async () => {
    renderWithQuery(<Dashboard />);
    expect(
      await screen.findByRole("link", { name: /annual-report-2025\.pdf/ }),
    ).toHaveAttribute("href", "/documents/doc-1");
  });

  it("invites the first upload when there are no filings yet", async () => {
    server.use(
      http.get("http://localhost:8000/documents", () =>
        HttpResponse.json({ documents: [], total: 0 }),
      ),
    );
    renderWithQuery(<Dashboard />);
    expect(await screen.findByText("No filings yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add a filing" })).toHaveAttribute(
      "href",
      "/ingest",
    );
  });

  it("explains the failure when the filings cannot be loaded", async () => {
    server.use(
      http.get("http://localhost:8000/documents", () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 503 }),
      ),
    );
    renderWithQuery(<Dashboard />);
    expect(await screen.findByText(/database unavailable/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `pnpm test --run app/page.test.tsx`
Expected: FAIL — the current `app/page.tsx` renders the SP-18 upload form, so `findByRole("link", ...)` times out.

- [ ] **Step 3: Create `components/data/FilingRow.tsx`**

```tsx
"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getStages } from "@/lib/api";
import type { DocumentView } from "@/lib/types";
import { STAGES } from "@/lib/stages";

export function FilingRow({ document }: { document: DocumentView }) {
  const { data } = useQuery({
    queryKey: ["stages", document.id],
    queryFn: () => getStages(document.id),
  });

  const stages = data?.stages ?? [];
  const done = stages.filter((s) => s.status === "done").length;
  const failed = stages.filter((s) => s.status === "failed");

  return (
    <tr className="border-b border-rule">
      <td className="px-4 py-3">
        <Link
          href={`/documents/${document.id}`}
          className="underline underline-offset-2"
          style={{ fontWeight: 500 }}
        >
          {document.filename}
        </Link>
      </td>
      <td className="px-4 py-3 data" style={{ fontSize: "var(--text-sm)" }}>
        v{document.version}
      </td>
      <td className="px-4 py-3 data" style={{ fontSize: "var(--text-sm)" }}>
        {(document.byte_size / 1_048_576).toFixed(1)} MB
      </td>
      <td className="px-4 py-3">
        <span
          className="data"
          style={{
            fontSize: "var(--text-sm)",
            color: failed.length ? "var(--tether)" : "var(--ink)",
          }}
        >
          {done}/{STAGES.length}
        </span>
        {failed.length > 0 ? (
          <span
            className="data ml-2"
            style={{ fontSize: "var(--text-xs)", color: "var(--tether)" }}
          >
            {failed[0].name} failed
          </span>
        ) : null}
      </td>
    </tr>
  );
}
```

- [ ] **Step 4: Replace `app/page.tsx`**

```tsx
"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { listDocuments } from "@/lib/api";
import { PageHeader } from "@/components/shell/PageHeader";
import { FilingRow } from "@/components/data/FilingRow";

export default function Dashboard() {
  const { data, error, isPending } = useQuery({
    queryKey: ["documents"],
    queryFn: () => listDocuments(),
  });

  return (
    <>
      <PageHeader eyebrow="Corpus" title="Filings">
        <Link
          href="/ingest"
          className="px-4 py-2 border border-ink"
          style={{ fontSize: "var(--text-sm)", fontWeight: 500 }}
        >
          Add a filing
        </Link>
      </PageHeader>

      <div className="p-8">
        {error ? (
          <p className="sheet p-6" style={{ color: "var(--tether)" }}>
            {(error as Error).message}
          </p>
        ) : isPending ? (
          <p className="eyebrow">Loading filings</p>
        ) : data.documents.length === 0 ? (
          <div className="sheet p-12 text-center">
            <p className="display mb-2" style={{ fontSize: "var(--text-lg)" }}>
              No filings yet
            </p>
            <p className="mb-6" style={{ color: "var(--ink-soft)" }}>
              Upload an annual report to start reading figures out of it.
            </p>
            <Link
              href="/ingest"
              className="px-4 py-2 border border-ink inline-block"
              style={{ fontSize: "var(--text-sm)", fontWeight: 500 }}
            >
              Add a filing
            </Link>
          </div>
        ) : (
          <table className="sheet w-full text-left">
            <thead>
              <tr className="border-b border-rule">
                <th className="eyebrow px-4 py-3">Filing</th>
                <th className="eyebrow px-4 py-3">Version</th>
                <th className="eyebrow px-4 py-3">Size</th>
                <th className="eyebrow px-4 py-3">Pipeline</th>
              </tr>
            </thead>
            <tbody>
              {data.documents.map((d) => (
                <FilingRow key={d.id} document={d} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
```

- [ ] **Step 5: Run the test and watch it pass**

Run: `pnpm test --run app/page.test.tsx`
Expected: PASS — 3 tests.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/page.tsx frontend/app/page.test.tsx frontend/components/data
git commit -m "feat(ui): filings dashboard replacing the SP-18 thin UI"
```

---

### Task 6: Ingestion screen

Upload, then watch the DAG fill in. The old `app/page.tsx` polled with `setTimeout` for 90 iterations and then gave up silently; this replaces that with a query that stops on its own when the terminal stage settles.

**Files:**
- Create: `frontend/app/ingest/page.tsx`
- Create: `frontend/app/ingest/page.test.tsx`

**Interfaces:**
- Consumes: `uploadDocument`, `startStage`, `getStages` from `lib/api.ts`; `<StageGraph />`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test — `app/ingest/page.test.tsx`**

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import Ingest from "./page";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const pdf = () =>
  new File([new Uint8Array([37, 80, 68, 70])], "report.pdf", {
    type: "application/pdf",
  });

describe("Ingest", () => {
  it("starts the pipeline once the filing is uploaded", async () => {
    server.use(
      http.post("http://localhost:8000/documents", () =>
        HttpResponse.json(
          {
            id: "doc-9",
            checksum: "c",
            filename: "report.pdf",
            byte_size: 4,
            doc_key: "report.pdf",
            version: 1,
            content_type: "application/pdf",
          },
          { status: 201 },
        ),
      ),
      http.post("http://localhost:8000/documents/doc-9/ocr", () =>
        HttpResponse.json({ doc_id: "doc-9", stage: "ocr" }, { status: 202 }),
      ),
    );

    renderWithQuery(<Ingest />);
    await userEvent.upload(screen.getByLabelText("Annual report PDF"), pdf());
    await userEvent.click(screen.getByRole("button", { name: "Start reading" }));

    expect(await screen.findByText("report.pdf")).toBeInTheDocument();
  });

  it("says what went wrong when the upload is rejected", async () => {
    server.use(
      http.post("http://localhost:8000/documents", () =>
        HttpResponse.json({ detail: "file exceeds 50 MB" }, { status: 413 }),
      ),
    );

    renderWithQuery(<Ingest />);
    await userEvent.upload(screen.getByLabelText("Annual report PDF"), pdf());
    await userEvent.click(screen.getByRole("button", { name: "Start reading" }));

    expect(await screen.findByText(/file exceeds 50 MB/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `pnpm test --run app/ingest/page.test.tsx`
Expected: FAIL — `Failed to resolve import "./page"`.

- [ ] **Step 3: Create `app/ingest/page.tsx`**

```tsx
"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { getStages, startStage, uploadDocument } from "@/lib/api";
import type { DocumentView } from "@/lib/types";
import { PageHeader } from "@/components/shell/PageHeader";
import { StageGraph } from "@/components/pipeline/StageGraph";
import { STAGES } from "@/lib/stages";

export default function Ingest() {
  const [file, setFile] = useState<File | null>(null);
  const [document, setDocument] = useState<DocumentView | null>(null);

  const start = useMutation({
    mutationFn: async (chosen: File) => {
      const uploaded = await uploadDocument(chosen);
      await startStage(uploaded.id, "ocr");
      return uploaded;
    },
    onSuccess: setDocument,
  });

  const { data } = useQuery({
    queryKey: ["stages", document?.id],
    queryFn: () => getStages(document!.id),
    enabled: document !== null,
    // Stop polling once the terminal stage settles, or once anything fails.
    refetchInterval: (query) => {
      const stages = query.state.data?.stages ?? [];
      const index = stages.find((s) => s.name === "index");
      const failed = stages.some((s) => s.status === "failed");
      if (failed || index?.status === "done") return false;
      return 1_000;
    },
  });

  const stages = data?.stages ?? [];
  const done = stages.filter((s) => s.status === "done").length;
  const failed = stages.find((s) => s.status === "failed");

  return (
    <>
      <PageHeader eyebrow="Ingestion" title="Add a filing" />

      <div className="p-8 space-y-8">
        <section className="sheet p-6 max-w-2xl">
          <label
            htmlFor="pdf"
            className="eyebrow block mb-2"
            style={{ color: "var(--ink)" }}
          >
            Annual report PDF
          </label>
          <input
            id="pdf"
            type="file"
            accept="application/pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="block mb-4"
            style={{ fontSize: "var(--text-sm)" }}
          />
          <button
            type="button"
            disabled={!file || start.isPending}
            onClick={() => file && start.mutate(file)}
            className="px-4 py-2 border border-ink disabled:opacity-40"
            style={{ fontSize: "var(--text-sm)", fontWeight: 500 }}
          >
            {start.isPending ? "Uploading" : "Start reading"}
          </button>
          {start.error ? (
            <p className="mt-4" style={{ color: "var(--tether)" }}>
              {(start.error as Error).message}
            </p>
          ) : null}
        </section>

        {document ? (
          <section className="sheet p-6">
            <div className="flex items-baseline justify-between mb-6">
              <div>
                <div className="eyebrow">Reading</div>
                <div style={{ fontWeight: 500 }}>{document.filename}</div>
              </div>
              <div className="data" style={{ fontSize: "var(--text-sm)" }}>
                {done}/{STAGES.length} stages
              </div>
            </div>

            <StageGraph stages={stages} />

            {failed ? (
              <p className="mt-6" style={{ color: "var(--tether)" }}>
                {failed.name} failed: {failed.error ?? "no detail reported"}.
                Fix the cause, then reprocess from this stage.
              </p>
            ) : null}

            <Link
              href={`/documents/${document.id}`}
              className="inline-block mt-6 underline underline-offset-2"
              style={{ fontSize: "var(--text-sm)" }}
            >
              Open the filing
            </Link>
          </section>
        ) : null}
      </div>
    </>
  );
}
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `pnpm test --run app/ingest/page.test.tsx`
Expected: PASS — 2 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/ingest
git commit -m "feat(ui): ingestion screen with live pipeline DAG"
```

---

### Task 7: Extraction schema editor

Three panes: modules → retrieval units → fields, with a form for the field's extraction knobs. 16 modules, 18 retrieval units, 198 fields — the navigation has to make that tractable, so the field list is searchable across `field_name`, `canonical_label`, and `aliases`.

**Depends on:** SP-19 Task B5 (`GET /schema`, `PUT /schema`).

**Files:**
- Create: `frontend/app/schema/page.tsx`
- Create: `frontend/components/schema/ModuleTree.tsx`
- Create: `frontend/components/schema/FieldEditor.tsx`
- Create: `frontend/components/schema/FieldEditor.test.tsx`

**Interfaces:**
- Consumes: `getSchema`, `putSchema` from `lib/api.ts`; `SchemaField`, `RetrievalUnit`, `SchemaModule`, `ExtractionSchema` from `lib/types.ts`.
- Produces: `<FieldEditor field: SchemaField, onChange: (next: SchemaField) => void />`; `<ModuleTree modules, selectedPath, onSelect />`.

- [ ] **Step 1: Write the failing test — `components/schema/FieldEditor.test.tsx`**

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SchemaField } from "@/lib/types";
import { FieldEditor } from "./FieldEditor";

const field: SchemaField = {
  field_name: "total_assets",
  field_path: "module_3_balance_sheet.total_assets",
  canonical_label: "Total assets",
  aliases: ["Total assets", "TOTAL ASSETS"],
  data_type: "decimal",
  description: "Total assets as stated in the Balance Sheet.",
  extraction_prompt: "Extract the total assets from the Balance Sheet.",
};

describe("FieldEditor", () => {
  it("reports the edited prompt to its parent", async () => {
    const onChange = vi.fn();
    render(<FieldEditor field={field} onChange={onChange} />);

    await userEvent.type(
      screen.getByLabelText("Extraction prompt"),
      " Return null if absent.",
    );

    expect(onChange).toHaveBeenCalled();
    const last = onChange.mock.calls.at(-1)![0] as SchemaField;
    expect(last.extraction_prompt.startsWith("Extract the total assets")).toBe(true);
  });

  it("edits aliases as one per line, dropping blank lines", async () => {
    const onChange = vi.fn();
    render(<FieldEditor field={field} onChange={onChange} />);

    const aliases = screen.getByLabelText("Aliases");
    await userEvent.clear(aliases);
    await userEvent.type(aliases, "Total assets\n\nSum of assets");

    const last = onChange.mock.calls.at(-1)![0] as SchemaField;
    expect(last.aliases).toEqual(["Total assets", "Sum of assets"]);
  });

  it("shows the field's identity as data, not as prose", () => {
    render(<FieldEditor field={field} onChange={vi.fn()} />);
    expect(screen.getByText("module_3_balance_sheet.total_assets")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `pnpm test --run components/schema/FieldEditor.test.tsx`
Expected: FAIL — `Failed to resolve import "./FieldEditor"`.

- [ ] **Step 3: Create `components/schema/FieldEditor.tsx`**

```tsx
"use client";

import type { SchemaField } from "@/lib/types";

const DATA_TYPES: SchemaField["data_type"][] = [
  "decimal",
  "integer",
  "string",
  "date",
  "list",
];

const labelStyle = { color: "var(--ink)" } as const;
const inputClass = "w-full border border-rule bg-leaf px-3 py-2";

export function FieldEditor({
  field,
  onChange,
}: {
  field: SchemaField;
  onChange: (next: SchemaField) => void;
}) {
  const patch = (part: Partial<SchemaField>) => onChange({ ...field, ...part });

  return (
    <div className="space-y-6">
      <div>
        <div className="eyebrow">Field</div>
        <div className="display" style={{ fontSize: "var(--text-lg)" }}>
          {field.canonical_label}
        </div>
        <div
          className="data"
          style={{ fontSize: "var(--text-xs)", color: "var(--ink-soft)" }}
        >
          {field.field_path}
        </div>
      </div>

      <div>
        <label htmlFor="data_type" className="eyebrow block mb-1" style={labelStyle}>
          Data type
        </label>
        <select
          id="data_type"
          className={inputClass}
          value={field.data_type}
          onChange={(e) =>
            patch({ data_type: e.target.value as SchemaField["data_type"] })
          }
          style={{ fontSize: "var(--text-sm)" }}
        >
          {DATA_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label
          htmlFor="extraction_prompt"
          className="eyebrow block mb-1"
          style={labelStyle}
        >
          Extraction prompt
        </label>
        <p className="mb-2" style={{ fontSize: "var(--text-xs)", color: "var(--ink-soft)" }}>
          Sent to the model with the retrieved context. Say what to return and
          what to return when the figure is absent.
        </p>
        <textarea
          id="extraction_prompt"
          rows={5}
          className={inputClass}
          value={field.extraction_prompt}
          onChange={(e) => patch({ extraction_prompt: e.target.value })}
          style={{ fontSize: "var(--text-sm)" }}
        />
      </div>

      <div>
        <label htmlFor="aliases" className="eyebrow block mb-1" style={labelStyle}>
          Aliases
        </label>
        <p className="mb-2" style={{ fontSize: "var(--text-xs)", color: "var(--ink-soft)" }}>
          One label per line. These are the wordings the figure appears under in
          real filings; they also weight the reranker.
        </p>
        <textarea
          id="aliases"
          rows={4}
          className={`${inputClass} data`}
          value={field.aliases.join("\n")}
          onChange={(e) =>
            patch({
              aliases: e.target.value
                .split("\n")
                .map((a) => a.trim())
                .filter(Boolean),
            })
          }
          style={{ fontSize: "var(--text-sm)" }}
        />
      </div>

      <div>
        <label htmlFor="normalization" className="eyebrow block mb-1" style={labelStyle}>
          Normalization
        </label>
        <input
          id="normalization"
          className={inputClass}
          value={field.normalization ?? ""}
          onChange={(e) => patch({ normalization: e.target.value })}
          style={{ fontSize: "var(--text-sm)" }}
        />
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `pnpm test --run components/schema/FieldEditor.test.tsx`
Expected: PASS — 3 tests.

- [ ] **Step 5: Create `components/schema/ModuleTree.tsx`**

```tsx
"use client";

import type { SchemaModule } from "@/lib/types";

export interface FieldPath {
  moduleKey: string;
  unitId: string;
  fieldName: string;
}

export function ModuleTree({
  modules,
  query,
  selected,
  onSelect,
}: {
  modules: SchemaModule[];
  query: string;
  selected: FieldPath | null;
  onSelect: (path: FieldPath) => void;
}) {
  const needle = query.trim().toLowerCase();
  const matches = (name: string, label: string, aliases: string[]) =>
    !needle ||
    name.toLowerCase().includes(needle) ||
    label.toLowerCase().includes(needle) ||
    aliases.some((a) => a.toLowerCase().includes(needle));

  return (
    <div className="overflow-y-auto">
      {modules.map((module) => {
        const units = module.retrieval_units
          .map((unit) => ({
            unit,
            fields: unit.fields.filter((f) =>
              matches(f.field_name, f.canonical_label, f.aliases),
            ),
          }))
          .filter((u) => u.fields.length > 0);

        if (units.length === 0) return null;

        return (
          <section key={module.key} className="border-b border-rule">
            <h2 className="eyebrow px-4 py-2" style={{ color: "var(--ink)" }}>
              {module.name}
            </h2>
            {units.map(({ unit, fields }) => (
              <div key={unit.unit_id} className="pb-2">
                <div
                  className="data px-4 py-1"
                  style={{ fontSize: "var(--text-xs)", color: "var(--ink-soft)" }}
                >
                  {unit.unit_id} · {unit.title}
                </div>
                <ul>
                  {fields.map((field) => {
                    const active =
                      selected?.moduleKey === module.key &&
                      selected.unitId === unit.unit_id &&
                      selected.fieldName === field.field_name;
                    return (
                      <li key={field.field_path}>
                        <button
                          type="button"
                          onClick={() =>
                            onSelect({
                              moduleKey: module.key,
                              unitId: unit.unit_id,
                              fieldName: field.field_name,
                            })
                          }
                          className="w-full text-left px-6 py-1 hover:bg-paper"
                          style={{
                            fontSize: "var(--text-sm)",
                            fontWeight: active ? 600 : 400,
                            borderLeft: active
                              ? "2px solid var(--tether)"
                              : "2px solid transparent",
                          }}
                        >
                          {field.canonical_label}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </section>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 6: Create `app/schema/page.tsx`**

```tsx
"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getSchema, putSchema } from "@/lib/api";
import type { ExtractionSchema, SchemaField } from "@/lib/types";
import { PageHeader } from "@/components/shell/PageHeader";
import { ModuleTree, type FieldPath } from "@/components/schema/ModuleTree";
import { FieldEditor } from "@/components/schema/FieldEditor";

export default function SchemaScreen() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ExtractionSchema | null>(null);
  const [selected, setSelected] = useState<FieldPath | null>(null);
  const [query, setQuery] = useState("");

  const { data, error, isPending } = useQuery({
    queryKey: ["schema"],
    queryFn: getSchema,
  });

  const schema = draft ?? data ?? null;
  const dirty = draft !== null;

  const save = useMutation({
    mutationFn: () => putSchema(schema!, data!.version),
    onSuccess: (saved) => {
      setDraft(null);
      queryClient.setQueryData(["schema"], saved);
    },
  });

  const field = useMemo<SchemaField | null>(() => {
    if (!schema || !selected) return null;
    return (
      schema.modules
        .find((m) => m.key === selected.moduleKey)
        ?.retrieval_units.find((u) => u.unit_id === selected.unitId)
        ?.fields.find((f) => f.field_name === selected.fieldName) ?? null
    );
  }, [schema, selected]);

  const updateField = (next: SchemaField) => {
    if (!schema || !selected) return;
    setDraft({
      ...schema,
      modules: schema.modules.map((m) =>
        m.key !== selected.moduleKey
          ? m
          : {
              ...m,
              retrieval_units: m.retrieval_units.map((u) =>
                u.unit_id !== selected.unitId
                  ? u
                  : {
                      ...u,
                      fields: u.fields.map((f) =>
                        f.field_name === selected.fieldName ? next : f,
                      ),
                    },
              ),
            },
      ),
    });
  };

  return (
    <>
      <PageHeader eyebrow="Configuration" title="Extraction schema">
        {schema ? (
          <span className="data" style={{ fontSize: "var(--text-sm)" }}>
            v{schema.version}
          </span>
        ) : null}
        <button
          type="button"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate()}
          className="px-4 py-2 border border-ink disabled:opacity-40"
          style={{ fontSize: "var(--text-sm)", fontWeight: 500 }}
        >
          {save.isPending ? "Saving" : "Save changes"}
        </button>
      </PageHeader>

      {error ? (
        <p className="p-8" style={{ color: "var(--tether)" }}>
          {(error as Error).message}
        </p>
      ) : isPending || !schema ? (
        <p className="eyebrow p-8">Loading schema</p>
      ) : (
        <div className="flex" style={{ height: "calc(100vh - 97px)" }}>
          <div className="w-80 shrink-0 border-r border-rule flex flex-col">
            <div className="p-3 border-b border-rule">
              <label htmlFor="find" className="sr-only">
                Find a field
              </label>
              <input
                id="find"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Find a field"
                className="w-full border border-rule bg-leaf px-3 py-2"
                style={{ fontSize: "var(--text-sm)" }}
              />
            </div>
            <ModuleTree
              modules={schema.modules}
              query={query}
              selected={selected}
              onSelect={setSelected}
            />
          </div>

          <div className="flex-1 min-w-0 overflow-y-auto p-8">
            {field ? (
              <div className="sheet p-6 max-w-3xl">
                <FieldEditor field={field} onChange={updateField} />
              </div>
            ) : (
              <p style={{ color: "var(--ink-soft)" }}>
                Pick a field to edit how it is retrieved and read.
              </p>
            )}
            {save.error ? (
              <p className="mt-6" style={{ color: "var(--tether)" }}>
                {(save.error as Error).message}
              </p>
            ) : null}
          </div>
        </div>
      )}
    </>
  );
}
```

- [ ] **Step 7: Run the full suite**

Run: `pnpm test --run && pnpm typecheck && pnpm lint`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add frontend/app/schema frontend/components/schema
git commit -m "feat(ui): extraction schema editor over modules, units, and fields"
```

---

### Task 8: Annual report viewer — PDF and fields side by side

The signature screen. The left column renders the PDF page; the right lists extracted fields grouped by retrieval unit; selecting a field draws the tether between them and highlights the region it was read from.

**Depends on:** SP-19 Tasks B3 (`GET /documents/{doc_id}/file`) and B4 (`bounding_regions` on `ExtractFieldView.source`).

**Files:**
- Create: `frontend/components/data/RefMark.tsx`
- Create: `frontend/components/data/Value.tsx`
- Create: `frontend/components/data/Value.test.tsx`
- Create: `frontend/components/viewer/PdfCanvas.tsx`
- Create: `frontend/components/viewer/Tether.tsx`
- Create: `frontend/components/viewer/FieldList.tsx`
- Create: `frontend/app/documents/[docId]/page.tsx`

**Interfaces:**
- Consumes: `pdfUrl`, `getDocument`, `extractDocument` from `lib/api.ts`; `inkOpacity`, `formatAmount`, `formatConfidence` from `lib/format.ts`; `ExtractFieldView`, `BoundingRegion` from `lib/types.ts`.
- Produces: `<Value field, onSelect />`; `<PdfCanvas url, page, highlight, onRegionRect />`; `<Tether from: DOMRect | null, to: DOMRect | null, container: DOMRect | null />`.

- [ ] **Step 1: Write the failing test — `components/data/Value.test.tsx`**

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ExtractFieldView } from "@/lib/types";
import { Value } from "./Value";

const field = (over: Partial<ExtractFieldView> = {}): ExtractFieldView => ({
  field_name: "total_assets",
  value: 124850,
  source: {
    page: 84,
    snippet: "Total assets 1,24,850",
    evidence_ids: ["doc-1:t3r7"],
    bounding_regions: null,
  },
  confidence: 0.92,
  ...over,
});

describe("Value", () => {
  it("renders the figure grouped for an Indian filing", () => {
    render(<Value field={field()} selected={false} onSelect={vi.fn()} />);
    expect(screen.getByText("1,24,850.00")).toBeInTheDocument();
  });

  it("fades a low-confidence figure — confidence is ink density", () => {
    const { rerender } = render(
      <Value field={field({ confidence: 1 })} selected={false} onSelect={vi.fn()} />,
    );
    const confident = screen.getByText("1,24,850.00");
    expect(confident).toHaveStyle({ opacity: "1" });

    rerender(
      <Value field={field({ confidence: 0 })} selected={false} onSelect={vi.fn()} />,
    );
    expect(screen.getByText("1,24,850.00")).toHaveStyle({ opacity: "0.45" });
  });

  it("offers the page reference as the way into the source", async () => {
    const onSelect = vi.fn();
    render(<Value field={field()} selected={false} onSelect={onSelect} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Show total_assets on page 84" }),
    );
    expect(onSelect).toHaveBeenCalledWith("total_assets");
  });

  it("says plainly when a figure was not found", () => {
    render(
      <Value
        field={field({ value: null, confidence: 0 })}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Not found")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `pnpm test --run components/data/Value.test.tsx`
Expected: FAIL — `Failed to resolve import "./Value"`.

- [ ] **Step 3: Create `components/data/RefMark.tsx`**

```tsx
"use client";

/** The cross-reference mark. In a printed annual report a figure carries a
 *  superscript pointing to the note that explains it; here it points to the
 *  page the machine read it from. Same gesture, made clickable. */
export function RefMark({
  page,
  label,
  onClick,
}: {
  page: number;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className="data align-super ml-1"
      style={{
        fontSize: "var(--text-2xs)",
        color: "var(--tether)",
        textDecoration: "underline",
        textUnderlineOffset: "2px",
      }}
    >
      p{page}
    </button>
  );
}
```

- [ ] **Step 4: Create `components/data/Value.tsx`**

```tsx
"use client";

import type { ExtractFieldView } from "@/lib/types";
import { formatAmount, formatConfidence, inkOpacity } from "@/lib/format";
import { RefMark } from "./RefMark";

const LOW_CONFIDENCE = 0.5;

export function Value({
  field,
  selected,
  onSelect,
}: {
  field: ExtractFieldView;
  selected: boolean;
  onSelect: (fieldName: string) => void;
}) {
  const missing = field.value === null || field.value === undefined;
  const soft = field.confidence < LOW_CONFIDENCE;

  return (
    <div
      data-field={field.field_name}
      className="flex items-baseline justify-between gap-4 px-4 py-2 border-b border-rule"
      style={{ background: selected ? "var(--paper)" : undefined }}
    >
      <span style={{ fontSize: "var(--text-sm)" }}>{field.field_name}</span>

      <span className="flex items-baseline gap-2 shrink-0">
        {missing ? (
          <span style={{ fontSize: "var(--text-sm)", color: "var(--ink-soft)" }}>
            Not found
          </span>
        ) : (
          <span
            className="data"
            style={{
              fontSize: "var(--text-sm)",
              opacity: inkOpacity(field.confidence),
              borderBottom: soft ? "1px solid var(--flag)" : undefined,
            }}
          >
            {formatAmount(field.value)}
          </span>
        )}

        {field.source.page !== null ? (
          <RefMark
            page={field.source.page}
            label={`Show ${field.field_name} on page ${field.source.page}`}
            onClick={() => onSelect(field.field_name)}
          />
        ) : null}

        <span
          className="data"
          style={{ fontSize: "var(--text-2xs)", color: "var(--ink-soft)" }}
        >
          {formatConfidence(field.confidence)}
        </span>
      </span>
    </div>
  );
}
```

- [ ] **Step 5: Run the test and watch it pass**

Run: `pnpm test --run components/data/Value.test.tsx`
Expected: PASS — 4 tests.

- [ ] **Step 6: Create `components/viewer/PdfCanvas.tsx`**

`pdfjs-dist` is used directly rather than `react-pdf` because the overlay must share the canvas's exact coordinate space. Azure Document Intelligence polygons are 8 numbers — four `(x, y)` corners in inches — so they are scaled by `viewport.scale × 72` to reach canvas pixels.

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import type { BoundingRegion } from "@/lib/types";

const SCALE = 1.4;
const POINTS_PER_INCH = 72;

export function PdfCanvas({
  url,
  page,
  regions,
  onHighlightRect,
}: {
  url: string;
  page: number;
  regions: BoundingRegion[] | null;
  onHighlightRect: (rect: DOMRect | null) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url,
        ).toString();

        const doc = await pdfjs.getDocument(url).promise;
        if (cancelled) return;
        const rendered = await doc.getPage(page);
        const viewport = rendered.getViewport({ scale: SCALE });
        const canvas = canvasRef.current;
        if (!canvas || cancelled) return;

        canvas.width = viewport.width;
        canvas.height = viewport.height;
        const context = canvas.getContext("2d");
        if (!context) return;
        await rendered.render({ canvasContext: context, viewport }).promise;
      } catch (cause) {
        if (!cancelled) setError((cause as Error).message);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [url, page]);

  // Report the highlight's on-screen rect so the tether knows where to land.
  useEffect(() => {
    onHighlightRect(highlightRef.current?.getBoundingClientRect() ?? null);
  }, [regions, onHighlightRect]);

  const region = regions?.find((r) => r.page_number === page) ?? null;
  const box = region ? boxOf(region.polygon) : null;

  if (error) {
    return (
      <p className="p-6" style={{ color: "var(--tether)" }}>
        This page could not be rendered: {error}
      </p>
    );
  }

  return (
    <div className="relative inline-block">
      <canvas ref={canvasRef} className="block" />
      {box ? (
        <div
          ref={highlightRef}
          aria-hidden="true"
          className="absolute pointer-events-none"
          style={{
            left: box.x * SCALE * POINTS_PER_INCH,
            top: box.y * SCALE * POINTS_PER_INCH,
            width: box.width * SCALE * POINTS_PER_INCH,
            height: box.height * SCALE * POINTS_PER_INCH,
            border: "1.5px solid var(--tether)",
            background: "color-mix(in srgb, var(--tether) 10%, transparent)",
          }}
        />
      ) : null}
    </div>
  );
}

/** Azure DI polygons are [x1,y1,x2,y2,x3,y3,x4,y4] in inches. Reduce the
 *  four corners to an axis-aligned box — the quads are near-rectangular for
 *  text lines and table cells, and a rotated overlay would be harder to read
 *  than a slightly loose one. */
function boxOf(polygon: number[]) {
  const xs = polygon.filter((_, i) => i % 2 === 0);
  const ys = polygon.filter((_, i) => i % 2 === 1);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
}
```

- [ ] **Step 7: Create `components/viewer/Tether.tsx`**

```tsx
"use client";

/** The signature element: a drawn cross-reference joining a field to the
 *  region of the page it was read from. The only orchestrated motion in the
 *  application — it draws once over 240ms, then holds. */
export function Tether({
  from,
  to,
  container,
}: {
  from: DOMRect | null;
  to: DOMRect | null;
  container: DOMRect | null;
}) {
  if (!from || !to || !container) return null;

  const startX = to.right - container.left;
  const startY = to.top + to.height / 2 - container.top;
  const endX = from.left - container.left;
  const endY = from.top + from.height / 2 - container.top;
  const midX = (startX + endX) / 2;

  const path = `M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`;

  return (
    <svg
      aria-hidden="true"
      className="absolute inset-0 pointer-events-none"
      width={container.width}
      height={container.height}
    >
      <path
        d={path}
        fill="none"
        stroke="var(--tether)"
        strokeWidth={1.25}
        pathLength={1}
        strokeDasharray={1}
        strokeDashoffset={0}
        style={{ animation: "tether-draw 240ms ease-out" }}
      />
      <circle cx={endX} cy={endY} r={2.5} fill="var(--tether)" />
      <style>{`
        @media (prefers-reduced-motion: no-preference) {
          @keyframes tether-draw {
            from { stroke-dashoffset: 1; }
            to { stroke-dashoffset: 0; }
          }
        }
      `}</style>
    </svg>
  );
}
```

- [ ] **Step 8: Create `components/viewer/FieldList.tsx`**

```tsx
"use client";

import type { ExtractFieldView } from "@/lib/types";
import { Value } from "@/components/data/Value";

export function FieldList({
  fields,
  selected,
  onSelect,
}: {
  fields: ExtractFieldView[];
  selected: string | null;
  onSelect: (fieldName: string) => void;
}) {
  const found = fields.filter((f) => f.value !== null && f.value !== undefined);
  const soft = found.filter((f) => f.confidence < 0.5);

  return (
    <div>
      <div className="px-4 py-3 border-b border-rule flex items-baseline gap-4">
        <span className="eyebrow">Fields</span>
        <span className="data" style={{ fontSize: "var(--text-xs)" }}>
          {found.length}/{fields.length} found
        </span>
        {soft.length > 0 ? (
          <span
            className="data"
            style={{ fontSize: "var(--text-xs)", color: "var(--flag)" }}
          >
            {soft.length} need a check
          </span>
        ) : null}
      </div>
      {fields.map((field) => (
        <Value
          key={field.field_name}
          field={field}
          selected={selected === field.field_name}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}
```

- [ ] **Step 9: Create `app/documents/[docId]/page.tsx`**

```tsx
"use client";

import { use, useCallback, useLayoutEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { extractDocument, getDocument, pdfUrl } from "@/lib/api";
import { PageHeader } from "@/components/shell/PageHeader";
import { PdfCanvas } from "@/components/viewer/PdfCanvas";
import { FieldList } from "@/components/viewer/FieldList";
import { Tether } from "@/components/viewer/Tether";

export default function ReportViewer({
  params,
}: {
  params: Promise<{ docId: string }>;
}) {
  const { docId } = use(params);
  const [selected, setSelected] = useState<string | null>(null);
  const [highlightRect, setHighlightRect] = useState<DOMRect | null>(null);
  const [fieldRect, setFieldRect] = useState<DOMRect | null>(null);
  const [containerRect, setContainerRect] = useState<DOMRect | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const { data: document } = useQuery({
    queryKey: ["document", docId],
    queryFn: () => getDocument(docId),
  });

  const extract = useMutation({
    mutationFn: () => extractDocument(document!.doc_key),
  });

  const fields = extract.data?.fields ?? [];
  const active = fields.find((f) => f.field_name === selected) ?? null;

  useLayoutEffect(() => {
    setContainerRect(containerRef.current?.getBoundingClientRect() ?? null);
    if (!selected) {
      setFieldRect(null);
      return;
    }
    const row = containerRef.current?.querySelector(`[data-field="${selected}"]`);
    setFieldRect(row?.getBoundingClientRect() ?? null);
  }, [selected, fields]);

  const onHighlightRect = useCallback(
    (rect: DOMRect | null) => setHighlightRect(rect),
    [],
  );

  return (
    <>
      <PageHeader
        eyebrow={document ? `Version ${document.version}` : "Filing"}
        title={document?.filename ?? "Filing"}
      >
        <button
          type="button"
          disabled={!document || extract.isPending}
          onClick={() => extract.mutate()}
          className="px-4 py-2 border border-ink disabled:opacity-40"
          style={{ fontSize: "var(--text-sm)", fontWeight: 500 }}
        >
          {extract.isPending ? "Reading figures" : "Read figures"}
        </button>
      </PageHeader>

      <div
        ref={containerRef}
        className="relative flex"
        style={{ height: "calc(100vh - 97px)" }}
      >
        <div className="flex-1 min-w-0 overflow-auto p-6 border-r border-rule">
          <PdfCanvas
            url={pdfUrl(docId)}
            page={active?.source.page ?? 1}
            regions={active?.source.bounding_regions ?? null}
            onHighlightRect={onHighlightRect}
          />
        </div>

        <div className="w-[460px] shrink-0 overflow-y-auto">
          {extract.error ? (
            <p className="p-6" style={{ color: "var(--tether)" }}>
              {(extract.error as Error).message}
            </p>
          ) : fields.length === 0 ? (
            <p className="p-6" style={{ color: "var(--ink-soft)" }}>
              Read the figures out of this filing to see them beside the page
              they came from.
            </p>
          ) : (
            <FieldList
              fields={fields}
              selected={selected}
              onSelect={setSelected}
            />
          )}
        </div>

        <Tether from={fieldRect} to={highlightRect} container={containerRect} />
      </div>
    </>
  );
}
```

- [ ] **Step 10: Verify the viewer against a real filing**

Start the backend (`docker compose -f docker/compose.yml --env-file .env --profile core up -d`) and `pnpm dev`. Upload an annual report through `/ingest`, wait for `index` to reach `done`, open the filing, and click **Read figures**. Confirm: the PDF renders, clicking a reference mark jumps to that page, the region is outlined in `--tether`, the tether is drawn between them, and low-confidence values are visibly faded. If `bounding_regions` comes back `null` for every field, SP-19 Task B4 is incomplete — stop and finish it rather than working around it here.

- [ ] **Step 11: Run the full suite and commit**

```bash
pnpm test --run && pnpm typecheck && pnpm lint
git add frontend/app/documents frontend/components/viewer frontend/components/data
git commit -m "feat(ui): side-by-side report viewer with provenance tether"
```

---

## Deferred

Out of scope for SP-20, listed so they are not mistaken for oversights:

- **Ask / Q&A screen** over `POST /ask`, including the `tool_trace` render. The endpoint exists and is the obvious next screen.
- **Version comparison** over `POST /entities/compare`. `docs/architecture/retrieval-roadmap.md:244` requires that the UI render `no_structure` ("not yet processed") differently from `no_match` ("processed, concept not reported") — honor that when it is built.
- **Table browser** over `GET /documents/{doc_id}/tables`.
- **Authentication and multi-tenancy.** The backend has none; every route is unauthenticated. Nothing in this plan should be deployed beyond localhost until that changes.
- **Editing an extracted value.** The viewer shows the machine's reading and lets an analyst judge it; it does not yet let them correct it. That needs a backend write path and a decision about what a correction means for evals.
