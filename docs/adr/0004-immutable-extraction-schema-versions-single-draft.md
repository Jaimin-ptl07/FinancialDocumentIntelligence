---
status: accepted
---

# Immutable Extraction schema versions with a single mutable draft

An Extraction schema is a row an Invitee owns, not a file on the server. Its Fields live in **published versions that never change** and in **at most one mutable draft** per schema. Field create, update and delete edit the draft; publishing freezes the draft as the next version number. Every Extracted value records the Field definition it was read with, so a result always points at the exact definition that produced it. This amends the agent-system design's ADR-009, which had every Field operation create a new version.

A version per Field edit would make an Invitee's ten-minute editing session produce ten versions that nobody asked for. A history like that hides the changes that matter, and every version would need its own runs before it became comparable. A draft gives the Invitee room to change things freely. Publishing is the one deliberate act that makes a definition citable.

## Decisions

- **Field identity is separate from its definition.** A Field keeps one id across versions (and across copies of a schema). Its *definition hash* covers exactly what changes the read: name, meaning, data type, unit, instructions, aliases, periods, note aliases, allowed values, statement, and its retrieval group's query, keywords, title and basis. Position, `required` and validation rules are excluded, because they change how a value is judged, not how it is read.
- **The per-Field result cache is keyed by** Report version (document id) × Field identity × definition hash × retrieval configuration version × prompt version × model deployment. Only a Field whose key has no fresh read is sent to the model. Rewording a Field re-reads that Field alone. Changing the prompt, the retrieval configuration or the model re-reads everything. The old cache was keyed by a hash of Field *names*, so adding one Field re-extracted all of them, and rewording a Field never invalidated its stale result.
- **Which Fields are stale is a pure function** (`fdi.core.extraction_plan.plan_extraction`). The orchestrator only does what the plan says.
- **The value as read is never overwritten.** A read is an immutable row. An Invitee's accept or correct is a review row beside it, recorded with who and when. A forced re-read supersedes the old read (it is kept, marked superseded) rather than replacing it.
- **Review status is computed when a value is shown, never stored.** "Needs review" comes from the version's validation rules and confidence threshold, applied when the value is viewed. Tightening a rule therefore re-judges old reads without paying for new ones.
- **Derived Fields are computed when a value is shown, from the effective inputs** (an accepted or corrected value where there is one). They are never sent to the model. A correction to an input therefore carries through to every total built on it.
- **The FDI standard schema is imported, never hand-edited.** The v4.3 files are imported as a read-only, FDI-owned schema (owner null). Its Field ids are derived from the standard tag, so a later standard version keeps each Field's identity. Re-importing an unchanged file is a no-op.
- **Draft revisions only ever grow.** Publishing does not delete the draft. It advances it (same content, now equal to the new version) with the same compare-and-set every edit uses. A tab that is stale from before a publish therefore stays stale, and an edit that lands during a publish aborts that publish instead of being lost.
- **A review belongs to the read it judged.** Forcing a re-read produces a new read with no review, and the Report view shows the new value as read. The earlier accept or correct stays with the superseded read and its history (`GET /extracted-values/{id}/reviews`). Forcing a re-read is how an Invitee asks for a fresh look, so it starts the value's review over.
- **Ownership is about editing only.** Only the owner edits a schema. Every Invitee may read, copy and run any schema, and may accept or correct any Extracted value (Public filings, no isolation: #16; every review records who). Extraction runs are private to the Invitee who started them, like jobs.

## Considered Options

- **Single draft + immutable versions** (chosen).
- **Version per Field operation** (design ADR-009 as written): every keystroke-sized edit becomes a citable version, and the history fills with noise.
- **Mutable schema, versioned results only**: simplest to build, but a result could no longer say which definition produced it, which defeats the audit trail.

## Consequences

- `extraction_schema`, `extraction_schema_version`, `extraction_field`, `extraction_schema_draft`, `field_read`, `value_review`, `extraction_run`, `extraction_run_report` and `extraction_run_item` replace the one-JSON-object-per-Report extraction cache in object storage (migration 0019, additive).
- `POST /extract/document` keeps its response shape and keeps reading the configured schema document. It is served by the same per-Field cache: adding one Field to that schema extracts only that Field. It still writes its MinIO extraction record for existing readers (`score_pipeline.py`, the factsheet), but it no longer reads that record as a cache.
- Concurrent runs that read the same Field for the same Report converge on one read (a unique index on the live key). The worst case is paying for a duplicate call, never storing two live values.
