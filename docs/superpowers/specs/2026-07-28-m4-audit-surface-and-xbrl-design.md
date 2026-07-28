# Milestone 4 — Audit Surface + XBRL Export

**Date:** 2026-07-28
**Parent vision:** `2026-07-28-audit-grade-financial-intelligence-vision.md`
**Depends on:** M1–M3
**Status:** spec + implementation (this milestone)

## Goal

Package the Fact graph into what a bank actually consumes: a coverage/completeness
report, an XBRL instance, an end-to-end factsheet API, and a frontend audit
surface — all reading the same self-explaining, self-validating bundle.

## Deliverables

### Pure core (fully unit-tested)
1. **`fdi.core.coverage`** — classify every taxonomy tag as found / derived /
   missing, with the explicit `missing_extracted` and `unresolved_derived`
   lists a bank audits, plus a completeness %.
2. **`fdi.core.xbrl`** — `export_instance` emits a well-formed XBRL instance
   (context, INR unit, one numeric fact per tag; tag preserved in a `concept`
   attribute so odd tags like `TOTRE/SOPL` stay valid XML). `calculation_arcs`
   emits the calculation-linkbase relationships from resolved derivations.
3. **`fdi.core.factsheet.rows_from_schema` / `document_factsheet`** — build the
   whole bundle from a v4 schema + extraction records alone (no runtime
   taxonomy file): derive → validate → calibrate → coverage. A stray `derived`
   record can never seed the Fact map — the engine always recomputes it.

### API
4. **`POST /factsheet/document`** — runs the same extraction as
   `/extract/document`, then layers the engines. `?format=json` (default)
   returns the full bundle + `calculation_arcs`; `?format=xbrl` returns the
   XBRL instance as `application/xml`. Same 503/404/409 guards as extract.

### Frontend
5. **`AuditFactSheet`** — presentational audit surface: completeness header,
   the CARO/Schedule-III ratios, and the reconciliation/identity checks, in the
   Statutory Ledger idiom (ink glyph ✓/! + rule weight for pass/fail; `--tether`
   stays reserved for provenance). Decoded-bundle in, no data fetching.

## Auditability / immutability note

The factsheet is a pure, deterministic function of (schema, extraction records).
Extraction is already content-addressed and cached in the artifact store, so the
bundle is reproducible and effectively immutable per (doc, schema) — no new
versioning system is introduced. A future pass can persist each bundle as its own
artifact for a signed, append-only trail.

## Testing / acceptance

- coverage / xbrl / document_factsheet: pure unit suites, incl. the real v4
  schema (508 tags) end-to-end and the "derived record never seeds" guard.
- API: app imports, OpenAPI builds with `/factsheet/document` and its `format`
  param; mypy + ruff + import contract clean. (Live extraction needs Azure +
  a report, verified at cutover.)
- Frontend: vitest renders ratios, flags a failed check, handles empty checks.

## Out of scope / follow-up

Live cutover of the running seed from v3.2.0 → v4 (needs a real report for
accuracy verification); persisted signed factsheet artifacts; the full 11-ratio
Schedule III set; Dataset-1/2 redirection rollups; drill-down wiring of the
frontend to real bounding boxes (the viewer already renders boxes — connecting
derived → inputs → box is a UI follow-up).
