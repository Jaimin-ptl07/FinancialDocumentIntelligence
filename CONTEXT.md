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

## Financial knowledge

**Concept**:
A line of financial meaning from FDI's standard taxonomy — "Profit after tax", "Trade receivables" — identified by its tag (e.g. `PATR`). Different Reports print it under different labels; the Concept is what they share.
_Avoid_: "field" (a Field is what an Extraction schema asks for; it may name a Concept), "line item" (a printed row)

**Observation**:
One Report's printed value for one Concept, Fiscal year and basis. A Report observes its own Fiscal year (the current column) and usually the previous one (the comparative column), so one Fiscal year can have several Observations.
_Avoid_: "fact" for a single Report's number

**Financial fact**:
The value FDI stands behind for one Company, Concept, Fiscal year and basis, chosen from its Observations by a fixed rule, with every Observation kept beside it.
_Avoid_: "extracted value" (that is one Extraction run's read of one Field)

**Restatement**:
A later Report printing a different value for an earlier Fiscal year than that year's own Report did. Both Observations are kept; the Financial fact follows the latest Report, and says it was restated.
_Avoid_: "correction", "error" (a restatement is the issuer's own revision, not a mistake by FDI)

**Metric**:
A named calculation over Financial facts — operating margin, revenue growth, debt to equity — with a fixed formula. Computed by FDI, never by the model.
_Avoid_: "ratio" when the calculation is not a ratio (growth), "KPI"

## Agents

**Agent run**:
One execution of the agent system answering one request from an Invitee — planned, delegated, checked and answered, or failed, cancelled or awaiting approval. It survives a worker crash and continues where it stopped.
_Avoid_: "job" (the queueing mechanism), "session", "conversation" (one conversation holds many Agent runs)

**Agent task**:
One piece of an Agent run handed to one specialist agent, with its own status and result.
_Avoid_: "step", "subtask"

**Run event**:
A progress record an Agent run emits for the Invitee to follow — a specialist started, a tool was called, Evidence was found, the run finished. Never the model's private reasoning.
_Avoid_: "log", "trace", "thought"

**Conversation**:
An Invitee's ongoing thread of questions and answers, usually about one Company. Each question starts an Agent run; later questions can build on what earlier runs verified.
_Avoid_: "chat session", "thread" (a thread is the runtime's checkpoint identity)

**Claim**:
One checkable statement inside an answer — a figure ("TCS's FY2025-26 consolidated PAT was ₹49,454 crore") or a qualitative statement ("margins fell because of higher employee costs") — tied to the Evidence it rests on. Every Claim is verified, unsupported, contradicted or inconclusive.
_Avoid_: "fact" (a Financial fact is FDI's stored value; a Claim is something an answer asserts), "finding" for a single statement

**Approval**:
An Agent run's request for an Invitee's yes or no before an action that changes or deletes data. The run waits until it is answered.
_Avoid_: "confirmation dialog", "review" (review is for Extracted values)

## Research

**Evidence**:
A located piece of one Report version — a passage, a table, a table cell, a page, or a financial fact — that an answer can point at. Its identity comes from where it is in the Report, never from how it was found or how well it ranked, so the same passage found two ways is the same Evidence.
_Avoid_: "chunk" (a chunk is how text is indexed, one possible kind of Evidence), "source", "context"

**Workspace**:
An Invitee's working set of Companies — a view for focusing research, never an access boundary. Two Workspaces holding the same Company see the same reports and facts.
_Avoid_: "tenant", "organization", "account"
