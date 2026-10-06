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
