# Financial Document Intelligence (FDI)

A platform that turns public annual-report PDFs into structured, provenance-linked financial facts — OCR, semantic chunking, retrieval, extraction, and a financial object graph — with a "Statutory Ledger" UI.

## Deployment

**Gated demo**:
The deployed FDI v2 environment — a public HTTPS URL behind a login wall, used only by invited people. The gate protects operating cost (Azure DI/OpenAI spend), not user data. Designed to open up: the same deployment becomes a Public demo without re-architecting the gate.
_Avoid_: "production deployment" (unqualified), "SaaS", "public beta"

**Public demo**:
The Gated demo with the login wall removed — anyone may visit and upload. Rate limits and cost caps, not the gate, become the primary safeguard.
_Avoid_: "open beta", "launch"

## Access

**Invitee**:
A person whose account an admin creates directly. The only way identity enters the Gated demo — there is no self-signup.
_Avoid_: "customer", "subscriber", "registered user"

**Production**:
An FDI environment that runs unattended: deployed off a developer machine, reachable by its intended audience, and observable when it fails. The Gated demo is FDI's first Production environment.
_Avoid_: using "production" to mean merely "runs in Docker"

**Public filing**:
The only class of document the Gated demo accepts — annual reports and filings published on public exchanges.
_Avoid_: "client document", "private financials" (out of scope by decision, 2026-10-07)

## Companies

**Company**:
The issuer behind a set of Public filings, identified by its corporate registration number (CIN) and known by many names (aliases, tickers). One Company publishes many reports across years. Shared by every Invitee — Public filings carry no per-user ownership.
_Avoid_: "entity" (overloaded with cross-document entity clustering), "tenant", "client"

**Report**:
One Company's annual report for one Fiscal year. Uploading a corrected or re-downloaded copy adds a new version of the same Report, not a new Report. A Report usually holds both standalone and consolidated statements, so basis belongs to its statements, not to the Report.
_Avoid_: "document" when the Report is meant (a document is one uploaded PDF), "filing" for a single version

**Fiscal year**:
The reporting period a Report covers, written `FY2025-26` for the year ending 31 March 2026 (the Indian April–March year). Distinct from when the Report was published.
_Avoid_: "FY26", "2025" (ambiguous between the year a period starts, ends or is published)

## Extraction

**Extraction schema**:
A named list of Fields an Invitee wants read out of Reports. Published versions never change; editing produces the next version. FDI ships one standard Extraction schema; an Invitee's own schemas start empty or as a copy of it.
_Avoid_: "template", "form", "the schema" (ambiguous with database schemas)

**Field**:
One thing to read from a Report — its name, meaning, data type, unit and instructions. A Field's identity survives across versions; its definition may change between them.
_Avoid_: "column", "attribute", "tag" (a tag is the standard schema's code for a Field, e.g. `PATR`)

**Extraction run**:
One request to read a set of Fields from a set of Reports with one Extraction schema version. Only Fields whose result is missing or out of date cost anything.
_Avoid_: "job" (a job is how any long request is queued, not what it does)

**Extracted value**:
What an Extraction run read for one Field in one Report, with its source location. An Invitee may correct it; the value as read is kept beside the correction.
_Avoid_: "fact" (a financial fact is the canonical, cross-Report observation — S5)

## Research

**Evidence**:
A located piece of one Report version — a passage, a table, a table cell, a page, or a financial fact — that an answer can point at. Its identity comes from where it is in the Report, never from how it was found or how well it ranked, so the same passage found two ways is the same Evidence.
_Avoid_: "chunk" (a chunk is how text is indexed, one possible kind of Evidence), "source", "context"

**Workspace**:
An Invitee's working set of Companies — a view for focusing research, never an access boundary. Two Workspaces holding the same Company see the same reports and facts.
_Avoid_: "tenant", "organization", "account"
