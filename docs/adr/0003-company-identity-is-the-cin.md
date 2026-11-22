---
status: accepted
---

# Company identity is the CIN; `doc_key` identifies a Report

A Company is identified by its Corporate Identity Number (CIN), read from the Report itself, and nothing else: one CIN, one Company, enforced by a unique constraint. The free-text `doc_key` an uploader types stays exactly as it is and keeps meaning what it always has — the key of one Report (one Report, many uploaded versions) — but it is no longer evidence of who issued the Report. This amends the agent-system design's ADR-008 (company-centric document association), which left the identity key open.

The reason is that every other candidate key is a guess. `doc_key` is typed by a person and was already wrong in the corpus (REMC's Report is keyed `rites-2024-25`, its parent's name). Names vary ("RIL", "Reliance Industries Ltd.") and collide. The embedding clustering in `resolve_entities` groups documents that read alike, which is not the same thing as having one issuer. The CIN, by contrast, is a registry-issued identifier that the Companies Act requires a company to print in its own annual report, so it can be read from the Report and quoted as evidence (page and text), like every other fact FDI produces.

## Considered Options

- **CIN** (chosen): legally mandated and globally unique, and auditable from the Report's own text. The catch is that Reports also print their subsidiaries' and registrar's CINs, and the issuer's can be the rarer one, so identification has to rank candidates by context and can be unsure.
- **`doc_key` / uploader-supplied company**: no extraction needed, but it puts a person's typing in charge of identity and was already wrong for one of three Reports.
- **Issuer name**: present in every Report, but not unique and not stable (abbreviations, renames, near-identical group-company names).
- **Embedding clusters (`resolve_entities`)**: automatic, but it measures how alike documents read, not who issued them.

## Consequences

- Identification is a pure `core` function (`fdi.core.issuer`) materialised by a versioned `issuer` stage; the company registry (`fdi.pipeline.companies.CompanyRegistry`) is the only code that creates Companies or files Reports, and the worker, the backfill and the API all go through it.
- A Report whose CIN cannot be chosen with confidence is left unfiled rather than filed under a guess. A Company is never created from a name alone; confirming such Reports by hand is a separate step (S2.3).
- Existing Reports are filed by the same path as new ones (`fdi-companies backfill`), not by a hand-written data migration.
- `doc_key` routes, tools and ground truth keep working unchanged. `resolve_entities` stays callable by today's `/ask` but is no longer an identity source.
- Other identifiers (NSE symbol, BSE code, ISIN) and aliases hang off the Company as secondary keys for search, never as its identity.
