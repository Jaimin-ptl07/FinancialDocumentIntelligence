# SP-8 — Document AST (naive)

- **Status:** Approved (autonomously, per standing mandate) 2026-07-17. Not yet implemented.
- **Depends on:** SP-6 (unified document), SP-2 (stage framework).
- **Roadmap:** phase 5 "Document AST — hierarchical tree · sections · page spans". Build order §4 puts a NAIVE AST here (before the heavy SP-7 structural work), then deepens later.

## 1. Purpose

Build a hierarchical document tree — `title → sections → paragraphs` — from the unified document's ordered
blocks + canonical roles (SP-6). This is the **naive** AST: it uses only the role labels OCR already gives
(title / sectionHeading / body / …), deferring reading-order correction, nested subsections, and table
placement to SP-7 (which will enrich OCR + reprocess). The AST is what SP-9 (graph) and SP-10 (semantic)
walk instead of a flat block list.

Thin slice: a pure `build_ast` + an `AstStage` (`deps=("normalize",)`) that auto-chains off Normalize.

## 2. Scope decisions (autonomous; documented)

1. **Naive means role-driven, single-level sections.** Walk the ordered blocks: the first `title` block is the
   document title; each `heading` block opens a new section; `body`/`footnote` blocks become the current
   section's paragraphs; `header`/`footer`/`page_number` blocks are dropped as noise (SP-7 will do proper
   header/footer detection — the naive version just trusts OCR's role). Body before the first heading forms a
   preamble section (`heading: null`).
2. **The AST is a stable, versioned artifact** owned by `fdi.core`. Downstream commits to it. Deeper nesting
   (subsections, section page-spans, table nodes) is a future `schema_version`/SP-7 concern.
3. **`AstStage` is a derived stage** (`deps=("normalize",)`), auto-scheduled after Normalize by the DAG
   (`normalize → ast`), `input_hash` from Normalize's `output_hash`. Determinism: pure function of the
   unified doc.

## 3. Architecture

- **`fdi.core.ast.build_ast(unified: Mapping[str, object]) -> dict[str, object]`** (pure) →
  `{"schema_version": 1, "title": str | None, "sections": [{"heading": str | None, "paragraphs": [str]}]}`.
- **`fdi.pipeline.stages.ast.AstStage`** (`name="ast"`, `version=1`, `deps=("normalize",)`) — `run(ctx)` reads
  `ctx.read("normalize")` and returns `build_ast(...)`. Self-registered.

### `build_ast` algorithm
```
title = None; sections = []; current = None
for b in unified.get("blocks", []):
    role, text = b["role"], b["text"]
    if role == "title":            title = title if title is not None else text; continue
    if role in {"header","footer","page_number"}:   continue          # naive: drop noise
    if role == "heading":          current = {"heading": text, "paragraphs": []}; sections.append(current); continue
    # body / footnote / anything else -> a paragraph in the current (or a preamble) section
    if current is None:            current = {"heading": None, "paragraphs": []}; sections.append(current)
    current["paragraphs"].append(text)
return {"schema_version": 1, "title": title, "sections": sections}
```

## 4. Error handling

- Missing `blocks` → an empty document AST (`title: null, sections: []`) — a document with no text is valid,
  not an error (a scanned cover page could yield nothing). No raise.
- Malformed block (missing `role`/`text`) → the naive builder uses `b["role"]`/`b["text"]`; the upstream
  Normalize guarantees the shape, so a KeyError here is a real upstream bug and should surface (not be
  silently swallowed).

## 5. Testing

- **Unit** (no Docker): `build_ast` over a canned unified doc — title captured, headings open sections, body
  grouped under the right section, header/footer/page_number dropped, preamble (body-before-heading) →
  `heading: null` section, no-title → `title: null`, empty blocks → empty AST.
- **Integration** (testcontainers, fake OCR): run `ocr → normalize → ast` via `run_stage` in a local registry;
  assert the `ast` artifact is the expected tree and that `DagResolver(REGISTRY.all()).successors("normalize")`
  includes `"ast"`.

## 6. Explicitly deferred (YAGNI)

- Reading-order correction, header/footer *detection* (vs trusting OCR roles), multi-level subsection nesting,
  table nodes in the tree, per-section page spans → SP-7 (structural) + a later AST `schema_version`.
- Semantic labels (which section is the balance sheet, MD&A, …) → SP-10.

## 7. Done criteria

1. `build_ast(unified)` returns `{schema_version, title, sections[{heading, paragraphs[]}]}` per the algorithm
   (title captured; headings open sections; body grouped; header/footer/page_number dropped; preamble +
   no-title + empty-blocks cases correct).
2. `AstStage` (`deps=("normalize",)`) reads the unified doc and produces the AST; a rerun is a cache hit; its
   `input_hash` derives from Normalize's `output_hash`.
3. `DagResolver(REGISTRY.all()).successors("normalize")` includes `"ast"`.
4. All four static gates green; `core` stays pure; full suite passes.
