# **Financial Document Intelligence Agent — Detailed System Design**

Architecture decisions finalized

We will design this as a **multi-agent, company-centric Financial Document Intelligence Platform**, powered by LangGraph, while preserving your existing document processing and financial intelligence infrastructure.

There is one architectural principle I want to establish before going deeper:

**We are upgrading the intelligence and application layers, not rebuilding the document processing engine.**

Your existing 22-stage DAG, Celery workers, RabbitMQ, PostgreSQL, Milvus, MinIO and Financial Object Graph remain the foundation. We introduce LangGraph as a new orchestration layer that coordinates specialized agents and invokes existing capabilities through well-defined tools.

The result should support two different kinds of work:

1. Deterministic document processing: reliably convert annual reports into searchable and structured knowledge.
2. Agentic financial intelligence: independently research, extract, compare, analyze and generate reports using that knowledge.

This distinction is important because it gives us the flexibility of autonomous agents without sacrificing the reliability of a financial document processing system.

# **1. Finalized architectural decisions**

# **FDI Agent — Architecture baseline**

Approved design

| Decision            | Final choice                      |
| ------------------- | --------------------------------- |
| Agent architecture  | Multi-agent                       |
| Orchestration       | LangGraph                         |
| Knowledge scope     | Company-level workspace           |
| Document processing | Existing 22-stage DAG             |
| Job execution       | Celery + RabbitMQ                 |
| Structured database | PostgreSQL                        |
| Vector retrieval    | Milvus                            |
| Financial knowledge | Existing FOG                      |
| Object storage      | MinIO                             |
| LLM                 | Existing Azure OpenAI integration |
| Embeddings          | Existing BGE-small ONNX           |
| Backend             | FastAPI                           |
| Frontend            | Next.js                           |

**Architecture philosophy**

Multi-agent reasoning + deterministic financial services + hybrid retrieval + structured financial knowledge + evidence-based outputs.

## **1.1 What changes and what stays?**

| Existing component     | Decision | Modification                                           |
| ---------------------- | -------- | ------------------------------------------------------ |
| 22-stage ingestion DAG | Retain   | Expose reusable processing capabilities                |
| Celery                 | Retain   | Execute long-running agent runs and extraction jobs    |
| RabbitMQ               | Retain   | Route agent and processing jobs                        |
| PostgreSQL             | Extend   | Company workspace, schemas, facts, agent state         |
| Milvus                 | Retain   | Company/document-filtered hybrid retrieval             |
| FOG                    | Extend   | Cross-year financial knowledge                         |
| MinIO                  | Retain   | PDFs, artifacts and generated reports                  |
| FastAPI                | Extend   | Agent, research, schema and analytics APIs             |
| Next.js                | Extend   | Workspace, chat, execution timeline                    |
| Existing `/ask` agent  | Evolve   | Migrate tools and logic into LangGraph workflows       |
| Neo4j                  | Optional | Introduce when graph traversal has a concrete use case |

Your existing implementation already has dense and BM25 retrieval with RRF, a schema-driven extraction engine and a seven-tool `/ask` agent. We should reuse these capabilities instead of implementing parallel versions.&#x20;

Pasted text(20261121-141115).txt



# **2. High-level system architecture**

I would organize the entire application into seven layers.

## **Target system architecture**

**L1 — Presentation Layer**

Company Workspace

Document Explorer

AI Research Chat

Schema Builder

Financial Dashboard

Research Reports

Next.js · React · PDF.js · TanStack Query

**L2 — Application and API Layer**

FastAPI · Authentication · Authorization · Workspace Context · API Jobs

## **L3 — Agent Orchestration Layer**

**Supervisor / Planner**

Intent · Plan · Delegation · State · Control

Research Agent

Extraction Agent

Analytics Agent

Comparison Agent

Verification Agent

Reporting Agent

LangGraph · Shared tools · Checkpointing · Human approval

**L4 — Domain Intelligence Services**

Hybrid Retrieval

Extraction Engine

Financial Calculations

Entity Resolution

Concept Mapping

Evidence Verification

**L5 — Knowledge and Memory Layer**

PostgreSQL

Milvus

Financial Object Graph

Redis

Research Memory

Conversation State

**L6 — Agent and Processing Execution**

Celery

RabbitMQ

Redis

LangGraph

Durable execution · Background jobs · Workflow checkpoints

**L7 — Document Processing Infrastructure**

PDF → OCR → Normalize → Structure → AST → FOG → Chunk → Embedding → Index

Existing 22-stage DAG · Azure DI · Azure OpenAI · MinIO

There is one implementation detail worth emphasizing: LangGraph and Celery are not two competing orchestration systems.

- LangGraph controls the reasoning workflow.
- Celery controls durable background execution.
- RabbitMQ transports background jobs.
- PostgreSQL stores business data and workflow records.
- LangGraph checkpoints preserve graph execution state.
- Redis supports transient caching and coordination.

# **3. Component boundaries and responsibilities**

This is where we need to be disciplined.

Each component should have a clear responsibility, explicit inputs and outputs, and a defined ownership boundary.

I would organize the backend around domain modules rather than putting all agent code into one large `agents.py`.

## **3.1 Application layer**

| Component          | Responsibility                        | Owns                      |
| ------------------ | ------------------------------------- | ------------------------- |
| Workspace Service  | Company workspaces and membership     | Workspace relationships   |
| Company Service    | Company identity and identifiers      | Canonical company records |
| Document Service   | Uploads, metadata and document access | Document lifecycle        |
| Schema Service     | User-defined extraction schemas       | Schema versions           |
| Extraction Service | Executes structured extraction        | Extraction runs and facts |
| Research Service   | Research sessions and findings        | Research records          |
| Agent Service      | Creates and manages agent runs        | Agent execution metadata  |
| Analytics Service  | Financial metrics and comparisons     | Analytical results        |
| Report Service     | Creates and exports reports           | Report artifacts          |

The API layer should not implement financial reasoning. It should validate requests, authorize access, call domain services and return results.

## **3.2 Agent orchestration layer**

We will have one supervisor graph and multiple specialized agent graphs.

| Agent              | Responsibility                        | Allowed operations                        |
| ------------------ | ------------------------------------- | ----------------------------------------- |
| Supervisor         | Understand request, plan and delegate | Dispatch sub-agents, inspect results      |
| Research Agent     | Find information and evidence         | Search, inspect tables, sections, notes   |
| Extraction Agent   | Extract requested fields              | Schema lookup, retrieval, extraction      |
| Analytics Agent    | Perform financial calculations        | Read facts, call calculation tools        |
| Comparison Agent   | Compare years and companies           | Historical retrieval, concept matching    |
| Verification Agent | Verify factual claims and results     | Inspect source evidence, validate facts   |
| Reporting Agent    | Generate final artifacts              | Consume verified findings, create reports |

### **Important: specialized agents are not independent services**

I would initially implement them as LangGraph subgraphs or bounded agent nodes inside one orchestration runtime.

They share the same domain services and tool implementations.

We do not need six separately deployed LLM services, six databases or six independent Celery worker pools.

That would add infrastructure complexity without adding meaningful intelligence.

### **Agent autonomy model**

Because you selected multi-agent autonomy, agents should be allowed to perform their own bounded reasoning and tool selection.

For example, the Research Agent can independently decide:

1. Search management discussion.
2. Search the relevant financial statement.
3. Inspect the referenced note.
4. Search historical reports.
5. Return a consolidated evidence package.

However, the supervisor controls the overall task, execution budget, authorization and completion criteria.

This gives us autonomous specialization without an uncontrolled agent swarm.

## **3.3 Domain intelligence services**

This layer is especially important because agents should not directly access PostgreSQL, Milvus or MinIO.

They should interact through domain tools and services.

I would define the following service boundaries.

| Service                      | Main operations                                        |
| ---------------------------- | ------------------------------------------------------ |
| Document Retrieval Service   | Search chunks, sections, tables and pages              |
| Financial Fact Service       | Retrieve and persist canonical financial facts         |
| Schema Engine                | Resolve schema versions and validate field definitions |
| Extraction Engine            | Retrieve context and execute structured extraction     |
| FOG Service                  | Concept mapping, derivation and financial validation   |
| Company Intelligence Service | Resolve company identities and historical reports      |
| Calculation Engine           | Calculate financial ratios and trends                  |
| Evidence Service             | Resolve source references and verify provenance        |
| Report Generation Service    | Produce structured reports and exports                 |

For example, the Analytics Agent should call:

```
calculate_financial_ratio(    company_id="company_123",    metric="operating_margin",    period="FY2025",    basis="consolidated",)
```

It should not generate arbitrary SQL or write its own calculation logic.

Likewise, the Research Agent should call `search_documents()` instead of querying Milvus directly.

This creates a reusable intelligence layer that can power the agents, REST APIs, scheduled jobs and future integrations.

# **4. Detailed multi-agent architecture**

Now let's design the actual LangGraph system.

I recommend a hierarchical multi-agent architecture.

## **Supervisor and specialist agents**

**Supervisor Agent**

Intent · Planning · Delegation · Synthesis

**Research Subgraph**

Search → Inspect → Gather evidence → Return findings

**Extraction Subgraph**

Resolve schema → Retrieve → Extract → Validate

**Analytics Subgraph**

Resolve metrics → Calculate → Validate

**Comparison Subgraph**

Find reports → Normalize → Compare

**Verification Subgraph**

Inspect evidence → Check claims → Flag issues

**Reporting Subgraph**

Synthesize findings → Generate artifact

**Shared Domain Tool Registry**

Retrieval · Facts · Extraction · Calculations · Evidence · Reports

## **4.1 Supervisor Agent**

The supervisor is responsible for the entire user request.

Its responsibilities:

1. Understand user intent.
2. Resolve company, document and reporting period.
3. Determine whether the task is a simple retrieval or a complex investigation.
4. Create an execution plan.
5. Delegate tasks to specialized agents.
6. Manage dependencies and parallel execution.
7. Inspect agent results.
8. Request additional research if evidence is insufficient.
9. Invoke verification.
10. Produce the final answer.

The supervisor should have a bounded execution budget.

For example:

| Parameter                         | Initial policy  |
| --------------------------------- | --------------- |
| Maximum planning iterations       | 3               |
| Maximum specialist dispatches     | 10              |
| Maximum specialist tool calls     | 8 per agent run |
| Maximum supervisor iterations     | 12              |
| Maximum parallel specialist tasks | 3               |
| Maximum retries per task          | 2               |

These are initial application-level limits, not LangGraph defaults. We can tune them through evaluation.

### **Supervisor execution logic**

```
User Request
    |
    v
Intent Classification
    |
    v
Resolve Company Context
    |
    v
Create Research Plan
    |
    v
Dispatch Specialist Agents
    |
    v
Collect Results
    |
    v
Are Results Sufficient?
    |               |
   No              Yes
    |               |
More Research    Verification
    |               |
    +----------> Final Synthesis
                    |
                    v
                 Response
```

## **4.2 Research Agent**

The Research Agent is your main agentic RAG component.

It should be capable of conducting iterative research instead of executing one fixed retrieval query.

### **Internal workflow**

```
Research Request
      |
      v
Understand Question
      |
      v
Generate Search Queries
      |
      v
Hybrid Retrieval
      |
      v
Inspect Retrieved Evidence
      |
      v
Enough Context?
   /       \
 No         Yes
 |           |
Refine       Consolidate
Search       Findings
 |           |
 +-----------+
      |
      v
Evidence Package
```

### **Research tools**

```
search_company_documents()search_document_sections()search_document_content()search_financial_tables()get_document_page()get_table_details()get_note_references()get_financial_fact()compare_concept_across_reports()
```

The existing retrieval implementation should remain underneath these tools.

We should support retrieval filters such as:

```
{    "workspace_id": "workspace_123",    "company_id": "company_123",    "document_ids": ["doc_2024", "doc_2025"],    "financial_years": ["FY2024", "FY2025"],    "sections": ["management_discussion", "financial_statements"],    "basis": "consolidated"}
```

Not every filter needs to be supplied on every request. The retrieval service should apply the appropriate workspace and authorization restrictions regardless of what the LLM requests.

## **4.3 Extraction Agent**

This agent coordinates your existing extraction engine.

The important point is that we should not rebuild the extraction engine inside LangGraph.

Instead:

```
Extraction Agent
       |
       v
Resolve Schema
       |
       v
Determine Fields to Extract
       |
       v
Call Existing Extraction Service
       |
       v
Receive Structured Results
       |
       v
Verification Agent
       |
       v
Persist Results
```

The agent decides:

- Which schema to use.
- Which fields need extraction.
- Whether existing results can be reused.
- Whether additional retrieval is needed.
- Whether ambiguous values require review.

The extraction service handles:

- Retrieval.
- Prompt construction.
- LLM structured output.
- Parsing.
- Deterministic normalization.
- Provenance generation.

This separation is essential.

## **4.4 Analytics Agent**

The Analytics Agent should be able to formulate a calculation plan.

Example request:

"Analyze the company's profitability over the last three years."

Its plan might be:

```
{
  "metrics": [
    "revenue_growth",
    "ebitda_margin",
    "pat_margin",
    "roe"
  ],
  "periods": [
    "FY2023",
    "FY2024",
    "FY2025"
  ],
  "basis": "consolidated"
}
```

The agent retrieves the required facts and invokes deterministic calculation functions.

The output should contain:

- Input values.
- Formula.
- Calculated value.
- Reporting period.
- Units.
- Source references.
- Calculation validation status.

For example:

\\[ \text{Operating Margin} = \frac{\text{Operating Profit}}{\text{Revenue}} \times 100 \\]

The LLM explains the trend; Python computes it.

## **4.5 Comparison Agent**

This agent handles historical and cross-company analysis.

I would explicitly separate two operations:

**Historical comparison:** Same company across reporting years.

**Peer comparison:** Different companies for comparable reporting periods.

The agent must normalize:

- Concept definitions.
- Reporting periods.
- Currency.
- Units.
- Accounting basis.
- Consolidated/standalone classification.

It should return `not_comparable` rather than forcing a comparison when definitions or accounting bases differ.

## **4.6 Verification Agent**

I would make verification a required stage before publishing financial claims.

It should perform deterministic checks first:

1. Does the cited source exist?
2. Does the source belong to the correct report?
3. Is the financial year correct?
4. Is the accounting basis correct?
5. Does the cited evidence support the extracted value?
6. Does the unit match?
7. Do calculations reconcile?

An LLM can then help resolve semantic ambiguity.

A verification result might look like:

```
{
  "claim_id": "claim_001",
  "status": "verified",
  "checks": {
    "source_exists": true,
    "period_matches": true,
    "basis_matches": true,
    "unit_matches": true,
    "value_supported": true
  },
  "evidence_ids": ["evidence_123"]
}
```

## **4.7 Reporting Agent**

The Reporting Agent receives verified results and creates:

- Financial analysis.
- Management discussion summaries.
- Multi-year comparison reports.
- Risk analysis.
- Custom research reports.

It should never have unrestricted access to the entire document corpus when generating a final answer. Its context should be the verified research findings and supporting evidence.

# **5. LangGraph architecture and state management**

This is one of the most important parts of the design.

LangGraph needs a carefully designed state model. If we place every retrieved chunk, every financial fact, every conversation message and every intermediate result into one giant state object, it will become expensive and difficult to maintain.

I recommend three levels of state.

1. Global execution state.
2. Specialist agent state.
3. Persistent domain state.

## **5.1 Global execution state**

The supervisor owns the global execution state.

Proposed Pydantic representation:

```
from typing import Any, Literalfrom pydantic import BaseModel, Fieldclass ExecutionContext(BaseModel):    workspace_id: str    company_id: str | None = None    user_id: str    conversation_id: str | None = None    document_ids: list[str] = Field(default_factory=list)class AgentTask(BaseModel):    task_id: str    agent_type: str    description: str    status: Literal[        "pending",        "running",        "completed",        "failed",        "cancelled",        "needs_review"    ]    dependencies: list[str] = Field(default_factory=list)    result_ref: str | None = Noneclass EvidenceReference(BaseModel):    evidence_id: str    document_id: str    page_number: int    section: str | None = None    artifact_ref: str    claim_ids: list[str] = Field(default_factory=list)
```

This is the conceptual schema. The actual LangGraph state should use `TypedDict` with `Annotated` reducers for fields updated by multiple nodes, particularly parallel task results.

**Important:** Large documents, complete PDF pages and large retrieval results should not live inside the graph state. Store them in PostgreSQL or MinIO and keep references in state.

## **5.2 Specialist agent state**

Each specialist should have its own focused state.

For example:

```
class ResearchAgentState(BaseModel):    task_id: str    research_question: str    search_queries: list[str] = Field(default_factory=list)    retrieved_evidence_ids: list[str] = Field(default_factory=list)    inspected_sources: list[str] = Field(default_factory=list)    findings: list[dict] = Field(default_factory=list)    unresolved_questions: list[str] = Field(default_factory=list)    sufficient_evidence: bool = False    iteration_count: int = 0
```

Extraction Agent:

```
class ExtractionAgentState(BaseModel):    task_id: str    document_id: str    schema_id: str    schema_version_id: str    requested_field_ids: list[str]    extracted_fact_ids: list[str] = Field(default_factory=list)    failed_field_ids: list[str] = Field(default_factory=list)    review_field_ids: list[str] = Field(default_factory=list)    extraction_status: str = "pending"
```

Analytics Agent:

```
class AnalyticsAgentState(BaseModel):    task_id: str    requested_metrics: list[str]    financial_periods: list[str]    source_fact_ids: list[str] = Field(default_factory=list)    calculation_refs: list[str] = Field(default_factory=list)    unresolved_metrics: list[str] = Field(default_factory=list)    validation_status: str = "pending"
```

The same pattern applies to comparison, verification and reporting.

## **5.3 Persistent state versus LangGraph checkpoint**

These are different things.

| Information                           | Where it belongs     |
| ------------------------------------- | -------------------- |
| Current node and execution position   | LangGraph checkpoint |
| Intermediate reasoning workflow state | LangGraph state      |
| User conversation history             | PostgreSQL           |
| Company information                   | PostgreSQL           |
| Financial facts                       | PostgreSQL / FOG     |
| Research findings                     | PostgreSQL           |
| Large evidence artifacts              | MinIO                |
| Retrieved vectors                     | Milvus               |
| Temporary locks and cache             | Redis                |

For persistence, use a PostgreSQL-backed LangGraph checkpointer.

Use:

- `thread_id` = conversation or durable workflow thread.
- `checkpoint_id` = individual graph checkpoint.
- `run_id` = one user-requested execution.
- `task_id` = one specialist task.

Do not confuse these identifiers.

One conversation can contain many runs, and one run can contain many specialist tasks.

### **5.4 Three types of memory**

I would implement memory as follows:

| Memory                   | Storage              | Lifecycle                                                 |
| ------------------------ | -------------------- | --------------------------------------------------------- |
| Conversation memory      | PostgreSQL           | Persistent per conversation                               |
| Research memory          | PostgreSQL + MinIO   | Saved per research session                                |
| Company knowledge memory | PostgreSQL + FOG     | Persistent across reports                                 |
| Execution state          | LangGraph checkpoint | Persistent until workflow completion and retention expiry |
| Short-lived context      | Redis                | TTL-based                                                 |

A particularly useful capability will be research continuity.

For example:

User: "Investigate the company's declining operating margins."

The system stores its research session.

Later:

User: "Now investigate whether increased employee expenses contributed to that decline."

The supervisor can retrieve the earlier findings, inspect their evidence and extend the research.

This is better than embedding every chat message and treating the vector database as conversation memory.

# **6. Company-level knowledge architecture**

Since you selected company-level scope, the data model must revolve around a canonical company entity.

I would introduce a hierarchy like this:

**Workspace**

Access control and ownership boundary

**Company**

Canonical identity · CIN · Ticker · Aliases

**Annual Reports**

FY2023 · FY2024 · FY2025

**Company Knowledge**

Concepts · Entities · Facts

**Financial Intelligence**

Historical facts · Trends · Comparisons · Research

## **6.1 Company resolution**

When a user uploads a report, the system should resolve the company identity.

For example:

```
Reliance Industries Limited
Reliance Industries Ltd.
RIL
RELIANCE
CIN: L17110MH1973PLC019786
```

These should resolve to one canonical company record, subject to identifier verification.

I recommend storing:

- Canonical company name.
- CIN.
- Exchange identifiers.
- Ticker symbols.
- Aliases.
- Company type.
- Country.
- Identification confidence.
- Verification status.

If the company cannot be confidently identified, ask the user to confirm rather than creating duplicate company entities.

## **6.2 Document association**

Every report should have:

- `company_id`
- `financial_year`
- `period_start`
- `period_end`
- `report_type`
- `reporting_basis`
- `currency`
- `document_version`
- `restatement_status`

A report should also be associated with its source PDF and processing run.

A crucial distinction:

**The financial year in which a document was published is not necessarily the financial period it reports.**

Store both document publication metadata and reporting period.

## **6.3 Financial knowledge representation**

The existing FOG should evolve into the canonical financial knowledge layer.

Conceptually:

```
Company
  |
  +-- has_report --> Annual Report FY2025
  |
  +-- has_fact --> Revenue FY2025
  |                  |
  |                  +-- has_concept --> Revenue from Operations
  |                  +-- has_source --> Page 142
  |
  +-- has_fact --> EBITDA FY2025
  |
  +-- has_fact --> Total Debt FY2025
```

The graph can represent relationships such as:

- Financial concept → financial fact.
- Financial fact → source statement.
- Statement line → note reference.
- Company → subsidiary.
- Company → business segment.
- Financial fact → prior-period equivalent.
- Financial fact → derivation.

We do not need Neo4j to be the source of truth for this initial design. The current FOG can continue to own financial relationships and deterministic derivations. Neo4j can later become a query-oriented projection if graph traversals justify it.

# **7. Database design and schema changes**

Now let's get into the actual database architecture.

I would avoid creating an entirely new database or duplicating existing tables.

Instead, extend the existing PostgreSQL schema through additive Alembic migrations.

I will distinguish between existing concepts that should be reused and new tables that should be introduced.

## **7.1 Database domains**

I would divide PostgreSQL into these logical domains:

| Domain              | Responsibility                        |
| ------------------- | ------------------------------------- |
| Identity            | Users, workspaces, memberships        |
| Company             | Companies and identifiers             |
| Documents           | Documents, versions, processing       |
| Extraction          | Schemas, versions, fields, facts      |
| Financial knowledge | Concepts, periods, facts, provenance  |
| Agent execution     | Conversations, runs, tasks, approvals |
| Research            | Sessions, findings, evidence          |
| Analytics           | Calculations and comparisons          |
| Reporting           | Generated reports and exports         |

These can initially remain in the same PostgreSQL database, separated through naming conventions and modules. We don't need microservices or separate databases for each domain.

## **7.2 Proposed entity relationship model**

\#chatgpt-mermaid-\_r_4o5\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(237, 237, 237);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_4o5\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4o5\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4o5\_ .error-icon{fill:rgb(48, 48, 48);}#chatgpt-mermaid-\_r_4o5\_ .error-text{fill:rgb(237, 237, 237);stroke:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4o5\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_4o5\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_4o5\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_4o5\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_4o5\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_4o5\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_4o5\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4o5\_ .marker.cross{stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4o5\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_4o5\_ p{margin:0;}#chatgpt-mermaid-\_r_4o5\_ .entityBox{fill:rgb(9, 23, 44);stroke:rgb(31, 78, 148);}#chatgpt-mermaid-\_r_4o5\_ .relationshipLabelBox{fill:rgb(48, 48, 48);opacity:0.7;background-color:rgb(48, 48, 48);}#chatgpt-mermaid-\_r_4o5\_ .relationshipLabelBox rect{opacity:0.5;}#chatgpt-mermaid-\_r_4o5\_ .labelBkg{background-color:rgba(48, 48, 48, 0.5);}#chatgpt-mermaid-\_r_4o5\_ .edgeLabel{background-color:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4o5\_ .edgeLabel .label rect{fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4o5\_ .edgeLabel .label text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4o5\_ .edgeLabel .label{fill:rgb(31, 78, 148);font-size:14px;}#chatgpt-mermaid-\_r_4o5\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4o5\_ .edge-pattern-dashed{stroke-dasharray:8,8;}#chatgpt-mermaid-\_r_4o5\_ .node rect,#chatgpt-mermaid-\_r_4o5\_ .node circle,#chatgpt-mermaid-\_r_4o5\_ .node ellipse,#chatgpt-mermaid-\_r_4o5\_ .node polygon{fill:rgb(9, 23, 44);stroke:rgb(31, 78, 148);stroke-width:1px;}#chatgpt-mermaid-\_r_4o5\_ .relationshipLine{stroke:rgb(175, 175, 175);stroke-width:1px;fill:none;}#chatgpt-mermaid-\_r_4o5\_ .marker{fill:none!important;stroke:rgb(175, 175, 175)!important;stroke-width:1;}#chatgpt-mermaid-\_r_4o5\_ [data-look=neo].labelBkg{background-color:rgba(48, 48, 48, 0.5);}#chatgpt-mermaid-\_r_4o5\_ .node .neo-node{stroke:rgb(31, 78, 148);}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_4o5\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_4o5\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].node .neo-line path{stroke:rgb(31, 78, 148);filter:none;}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_4o5\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_4o5\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4o5\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_4o5\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4o5\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}USERWORKSPACE_MEMBERWORKSPACECOMPANYCOMPANY_IDENTIFIERDOCUMENTEXTRACTION_RUNEXTRACTION_SCHEMASCHEMA_VERSIONSCHEMA_FIELDEXTRACTED_FACTFACT_PROVENANCEFINANCIAL_FACTFINANCIAL_CONCEPTFINANCIAL_PERIODCONVERSATIONAGENT_RUNAGENT_TASKAGENT_APPROVALRESEARCH_SESSIONRESEARCH_FINDINGEVIDENCEANALYTICS_RESULTGENERATED_REPORTjoinscontainsaccesseshasownsprocessed_byversionsdefinesextracted_fromusesproducessupported_byhasdefinesdescribesownscontainsdispatchesrequestsinvestigated_incontainssupported_byproducesproduces

This is a logical ER model. Some entities may map onto existing tables, particularly `DOCUMENT`, `EXTRACTION_RUN` and financial facts.

## **7.3 New and extended tables**

### **A. Company and workspace**

| Table                 | Important columns                             |
| --------------------- | --------------------------------------------- |
| `workspaces`          | id, name, owner_user_id                       |
| `workspace_members`   | workspace_id, user_id, role                   |
| `companies`           | id, canonical_name, country, status           |
| `company_identifiers` | company_id, identifier_type, identifier_value |
| `workspace_companies` | workspace_id, company_id, access_level        |

Why `workspace_companies`?

A company can be shared with multiple authorized workspaces without duplicating its document processing artifacts.

But workspace access must still be enforced. The company ID alone is not authorization.

Your existing project is not yet multi-tenant, so this needs to be an explicit security migration, not just a UI feature.&#x20;

Pasted text(20261121-141115).txt



### **B. Document metadata**

Extend the existing document model with fields such as:

```
company_id
workspace_id
financial_year
period_start
period_end
reporting_basis
report_type
currency
publication_date
restatement_status
document_version
```

Do not create a second documents table if the current document registry already handles document identity and lifecycle.

Add `document_relationships` if you need to track:

- Restated versions.
- Amended filings.
- Replacement documents.
- Related reports.

### **C. Dynamic extraction schemas**

Introduce:

| Table                | Purpose                       |
| -------------------- | ----------------------------- |
| `extraction_schemas` | Logical schema identity       |
| `schema_versions`    | Immutable schema revisions    |
| `schema_fields`      | Field definitions per version |
| `extraction_runs`    | Each extraction execution     |
| `extracted_facts`    | Results for requested fields  |
| `fact_provenance`    | Source location and evidence  |

Example:

```
CREATE TABLE extraction_schemas (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE schema_versions (
    id UUID PRIMARY KEY,
    schema_id UUID NOT NULL
        REFERENCES extraction_schemas(id),
    version INTEGER NOT NULL,
    definition_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(schema_id, version)
);

CREATE TABLE schema_fields (
    id UUID PRIMARY KEY,
    schema_version_id UUID NOT NULL
        REFERENCES schema_versions(id),
    field_key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    data_type TEXT NOT NULL,
    unit TEXT,
    instructions TEXT,
    validation_rules JSONB,
    required BOOLEAN NOT NULL DEFAULT false,
    position INTEGER NOT NULL,
    UNIQUE(schema_version_id, field_key)
);
```

A schema edit creates a new version rather than mutating an existing version.

For a deleted field, remove it from the new version but preserve the historical version and results.

### **D. Financial knowledge**

Reuse the existing FOG models wherever possible.

The logical entities should be:

| Table                     | Purpose                                      |
| ------------------------- | -------------------------------------------- |
| `financial_concepts`      | Canonical financial taxonomy                 |
| `financial_periods`       | Reporting period definitions                 |
| `financial_facts`         | Normalized financial observations            |
| `fact_provenance`         | Evidence references                          |
| `fact_relationships`      | Derivation and cross-reference relationships |
| `fact_validation_results` | Validation outcomes                          |

One important distinction: `extracted_facts` and `financial_facts` need not be the same entity.

- `extracted_facts` represents the output of a particular user schema and extraction run.
- `financial_facts` represents a canonical normalized financial observation in the company's knowledge base.

One canonical financial fact may be referenced by several extraction results, and one extracted field may require additional concept resolution before becoming a canonical financial fact.

### **Financial fact structure**

```
{
  "id": "fact_123",
  "company_id": "company_123",
  "concept_id": "revenue_from_operations",
  "period_id": "FY2025",
  "value": 120000.00,
  "currency": "INR",
  "scale": "crore",
  "reporting_basis": "consolidated",
  "document_id": "doc_2025",
  "status": "verified",
  "source_refs": [
    "evidence_123"
  ]
}
```

For financial facts, I recommend storing decimal values with appropriate precision, not floating-point values.

Also preserve the original reported value, original unit, normalized value and normalization metadata. Never lose the original representation during normalization.

### **E. Agent execution**

Introduce:

| Table                     | Purpose                     |
| ------------------------- | --------------------------- |
| `conversations`           | User chat sessions          |
| `conversation_messages`   | User and assistant messages |
| `agent_runs`              | One orchestration execution |
| `agent_tasks`             | Specialist tasks            |
| `agent_task_dependencies` | Task dependency graph       |
| `agent_events`            | Execution timeline          |
| `agent_approvals`         | Human-in-the-loop requests  |
| `agent_tool_invocations`  | Tool execution audit        |

Suggested `agent_runs`:

```
CREATE TABLE agent_runs (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    company_id UUID,
    conversation_id UUID,
    user_id UUID NOT NULL,

    request TEXT NOT NULL,
    intent TEXT,

    status TEXT NOT NULL,
    execution_plan JSONB,

    thread_id TEXT NOT NULL,
    celery_task_id TEXT,

    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    error JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The graph checkpoint itself should be managed by the LangGraph checkpointer. Don't duplicate the full graph state into `agent_runs`.

`agent_runs` is your application-level execution record.

### **F. Research and analytics**

| Table                | Purpose                       |
| -------------------- | ----------------------------- |
| `research_sessions`  | A research investigation      |
| `research_findings`  | Individual findings           |
| `evidence`           | Retrieved supporting evidence |
| `analytics_results`  | Calculated metrics            |
| `comparison_results` | Historical comparison outputs |
| `generated_reports`  | Saved research artifacts      |

A research finding should include:

- Finding title.
- Description.
- Finding type.
- Supporting evidence IDs.
- Confidence/verification status.
- Related financial concepts.
- Related financial periods.

Avoid storing a whole report only as an LLM-generated text blob. Preserve the structured findings and their references separately.

## **7.4 Database indexing and constraints**

A few indexes are important from the beginning:

```
CREATE INDEX idx_company_identifiers_lookup
ON company_identifiers(identifier_type, identifier_value);

CREATE INDEX idx_documents_company_period
ON documents(company_id, financial_year);

CREATE INDEX idx_financial_facts_lookup
ON financial_facts(company_id, concept_id, period_id);

CREATE INDEX idx_agent_runs_workspace_status
ON agent_runs(workspace_id, status, created_at DESC);

CREATE INDEX idx_agent_tasks_run_status
ON agent_tasks(agent_run_id, status);

CREATE INDEX idx_research_sessions_company
ON research_sessions(company_id, created_at DESC);
```

Additional constraints:

- Workspace membership must be unique per user/workspace.
- Company identifiers should have appropriate uniqueness rules.
- Schema versions must be immutable.
- Extraction runs must reference an exact schema version.
- Facts must reference valid documents and financial periods.
- All workspace-owned queries must enforce authorization.

For historical financial facts, do not enforce uniqueness on just `(company_id, concept_id, period_id)`. Different documents, reporting bases, restatements and source observations may legitimately produce multiple observations for the same period.

Use a proper observation identity and explicit conflict-resolution rules.

# **8. API architecture**

I would introduce a versioned API under:

```
/api/v1/
```

The API should be organized around business resources rather than agents.

Users should not need to know which agent is responsible for an operation.

For example, the frontend requests a financial analysis. The backend and LangGraph decide which agents to invoke.

## **8.1 Proposed API groups**

| API group          | Responsibility                  |
| ------------------ | ------------------------------- |
| `/workspaces`      | Workspace management            |
| `/companies`       | Company identity and reports    |
| `/documents`       | Upload and document lifecycle   |
| `/schemas`         | Dynamic extraction schemas      |
| `/extractions`     | Structured extraction           |
| `/conversations`   | Chat sessions                   |
| `/agent-runs`      | Agent execution                 |
| `/research`        | Saved investigations            |
| `/financial-facts` | Canonical financial information |
| `/analytics`       | Calculations and comparisons    |
| `/reports`         | Generated research reports      |

## **8.2 Company APIs**

```
POST   /api/v1/companies
GET    /api/v1/companies
GET    /api/v1/companies/{company_id}
PATCH  /api/v1/companies/{company_id}

GET    /api/v1/companies/{company_id}/documents
GET    /api/v1/companies/{company_id}/financial-facts
GET    /api/v1/companies/{company_id}/financial-history
GET    /api/v1/companies/{company_id}/research
```

The company workspace should provide a unified entry point to all its annual reports.

## **8.3 Dynamic schema APIs**

```
POST   /api/v1/schemas
GET    /api/v1/schemas
GET    /api/v1/schemas/{schema_id}

GET    /api/v1/schemas/{schema_id}/versions
GET    /api/v1/schemas/{schema_id}/versions/{version_id}

POST   /api/v1/schemas/{schema_id}/versions
POST   /api/v1/schemas/{schema_id}/fields
PATCH  /api/v1/schemas/{schema_id}/fields/{field_id}
DELETE /api/v1/schemas/{schema_id}/fields/{field_id}
```

The field operations should create a new immutable schema version.

For a larger schema edit, also support an atomic bulk update:

```
PUT /api/v1/schemas/{schema_id}/draft
```

This is preferable to creating a new schema version for every keystroke in the UI.

### **Extraction APIs**

```
POST /api/v1/extractions

GET  /api/v1/extractions/{run_id}

GET  /api/v1/documents/{document_id}/extractions
GET  /api/v1/documents/{document_id}/extractions/{run_id}

POST /api/v1/extractions/{run_id}/retry
POST /api/v1/extractions/{run_id}/review
```

Example request:

```
{
  "company_id": "company_123",
  "document_ids": ["doc_2024", "doc_2025"],
  "schema_id": "schema_123",
  "schema_version_id": "version_3",
  "field_ids": ["revenue", "ebitda"],
  "mode": "incremental"
}
```

The backend creates an extraction job and returns an execution identifier.

## **8.4 Chat APIs**

```
POST   /api/v1/conversations
GET    /api/v1/conversations
GET    /api/v1/conversations/{conversation_id}
DELETE /api/v1/conversations/{conversation_id}

POST   /api/v1/conversations/{conversation_id}/messages
GET    /api/v1/conversations/{conversation_id}/messages

GET    /api/v1/conversations/{conversation_id}/events
```

Example chat request:

```
{
  "message": "Compare the company's revenue and profitability over the last three years.",
  "company_id": "company_123",
  "context": {
    "financial_years": [
      "FY2023",
      "FY2024",
      "FY2025"
    ],
    "basis": "consolidated"
  }
}
```

The response should include:

```
{
  "conversation_id": "conv_123",
  "run_id": "run_123",
  "status": "queued"
}
```

For execution updates, use Server-Sent Events (SSE).

An event stream could contain:

```
event: run.started
data: {"run_id":"run_123"}

event: agent.started
data: {"agent":"comparison"}

event: task.completed
data: {"task_id":"task_123"}

event: evidence.found
data: {"evidence_count":12}

event: run.completed
data: {"run_id":"run_123"}
```

The frontend should receive meaningful progress events rather than raw internal chain-of-thought.

We should expose execution stages, tool names, progress, results and citations, not private model reasoning.

## **8.5 Agent execution APIs**

```
GET  /api/v1/agent-runs/{run_id}
GET  /api/v1/agent-runs/{run_id}/tasks
GET  /api/v1/agent-runs/{run_id}/events

POST /api/v1/agent-runs/{run_id}/cancel
POST /api/v1/agent-runs/{run_id}/resume

POST /api/v1/agent-runs/{run_id}/approvals/{approval_id}
```

These APIs are essential for debugging and frontend execution timelines.

## **8.6 Analytics APIs**

```
POST /api/v1/analytics/calculate
POST /api/v1/analytics/compare-periods
POST /api/v1/analytics/compare-companies
POST /api/v1/analytics/generate-report

GET  /api/v1/analytics/results/{result_id}
```

These can also be exposed as tools to the Analytics and Comparison Agents.

# **9. End-to-end execution architecture**

Now we need to connect FastAPI, LangGraph, Celery and RabbitMQ properly.

I recommend a hybrid execution architecture.

## **Agent run lifecycle**

**Next.js**

User submits a request

**FastAPI**

Authenticate · Authorize · Validate · Create run

**PostgreSQL**

Persist agent_run and initial status

**RabbitMQ**

Dispatch agent execution task

**Celery Worker**

Load execution context and invoke LangGraph

**LangGraph Runtime**

Plan · Delegate · Execute · Verify · Synthesize

**PostgreSQL + MinIO**

Persist results, events, evidence and artifacts

**SSE → Next.js**

Display progress and final response

## **9.1 Why this execution model?**

I would not execute every agentic request directly inside a FastAPI request handler.

A complex research task can take several minutes.

It needs:

- Durable execution.
- Retry support.
- Worker failure recovery.
- Cancellation.
- Checkpointing.
- Human approval.
- Progress reporting.

The Celery worker provides the durable execution boundary.

LangGraph provides the reasoning workflow inside that boundary.

### **Important implementation detail: transactional dispatch**

Creating an `agent_run` in PostgreSQL and publishing a RabbitMQ task are two separate operations.

There is a potential failure:

1. Database transaction commits.
2. Application crashes before publishing to RabbitMQ.
3. Agent run remains queued forever.

I recommend a transactional outbox for reliable dispatch.

```
Database Transaction
      |
      +-- INSERT agent_run
      |
      +-- INSERT outbox_event
      |
      v
COMMIT
      |
      v
Outbox Publisher
      |
      v
RabbitMQ
      |
      v
Celery Worker
```

The outbox publisher can retry safely. The worker must also be idempotent because Celery delivery is at-least-once.

Use a unique dispatch key such as `agent_run_id + execution_version` to prevent duplicate execution.

## **9.2 Worker execution**

Conceptually:

```
@celery_app.task(    bind=True,    acks_late=True,    reject_on_worker_lost=True,)def execute_agent_run(self, run_id: str):    return run_agent_workflow(run_id)
```

The actual task wrapper should follow your existing Celery conventions, including the project's fork-safe async database lifecycle.

The execution service:

```
async def run_agent_workflow(run_id: str):    run = await agent_repository.get(run_id)    context = await build_execution_context(run)    graph = build_agent_graph()    config = {        "configurable": {            "thread_id": str(run.thread_id)        }    }    result = await graph.ainvoke(        context,        config=config    )    await persist_execution_result(run_id, result)    return result
```

This is illustrative pseudocode; the production implementation should integrate with the current worker event-loop and database lifecycle rather than nesting `asyncio.run()` inside an already running event loop.

# **10. Three end-to-end workflows**

These are the three main workflows I would implement first.

## **Workflow A — Dynamic field extraction**

Business workflow 1

Scenario:

The user adds a new field called `Capital Expenditure` to the existing financial schema and wants it extracted from three annual reports.

\#chatgpt-mermaid-\_r_4rj\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(237, 237, 237);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_4rj\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4rj\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4rj\_ .error-icon{fill:rgb(48, 48, 48);}#chatgpt-mermaid-\_r_4rj\_ .error-text{fill:rgb(237, 237, 237);stroke:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4rj\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_4rj\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_4rj\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_4rj\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_4rj\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_4rj\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_4rj\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4rj\_ .marker.cross{stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4rj\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_4rj\_ p{margin:0;}#chatgpt-mermaid-\_r_4rj\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4rj\_ .cluster-label text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4rj\_ .cluster-label span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4rj\_ .cluster-label span p{background-color:transparent;}#chatgpt-mermaid-\_r_4rj\_ .label text,#chatgpt-mermaid-\_r_4rj\_ span{fill:rgb(237, 237, 237);color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4rj\_ .node rect,#chatgpt-mermaid-\_r_4rj\_ .node circle,#chatgpt-mermaid-\_r_4rj\_ .node ellipse,#chatgpt-mermaid-\_r_4rj\_ .node polygon,#chatgpt-mermaid-\_r_4rj\_ .node path{fill:rgb(9, 23, 44);stroke:rgb(31, 78, 148);stroke-width:1px;}#chatgpt-mermaid-\_r_4rj\_ .rough-node .label text,#chatgpt-mermaid-\_r_4rj\_ .node .label text,#chatgpt-mermaid-\_r_4rj\_ .image-shape .label,#chatgpt-mermaid-\_r_4rj\_ .icon-shape .label{text-anchor:middle;}#chatgpt-mermaid-\_r_4rj\_ .node .katex path{fill:#000;stroke:#000;stroke-width:1px;}#chatgpt-mermaid-\_r_4rj\_ .rough-node .label,#chatgpt-mermaid-\_r_4rj\_ .node .label,#chatgpt-mermaid-\_r_4rj\_ .image-shape .label,#chatgpt-mermaid-\_r_4rj\_ .icon-shape .label{text-align:center;}#chatgpt-mermaid-\_r_4rj\_ .node.clickable{cursor:pointer;}#chatgpt-mermaid-\_r_4rj\_ .root .anchor path{fill:rgb(175, 175, 175)!important;stroke-width:0;stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4rj\_ .arrowheadPath{fill:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4rj\_ .edgePath .path{stroke:rgb(175, 175, 175);stroke-width:1px;}#chatgpt-mermaid-\_r_4rj\_ .flowchart-link{stroke:rgb(175, 175, 175);fill:none;}#chatgpt-mermaid-\_r_4rj\_ .edgeLabel{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_4rj\_ .edgeLabel p{background-color:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4rj\_ .edgeLabel rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4rj\_ .labelBkg{background-color:rgba(0, 0, 0, 0.5);}#chatgpt-mermaid-\_r_4rj\_ .cluster rect{fill:rgb(48, 48, 48);stroke:rgba(255, 255, 255, 0.15);stroke-width:1px;}#chatgpt-mermaid-\_r_4rj\_ .cluster text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4rj\_ .cluster span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4rj\_ div.mermaidTooltip{position:absolute;text-align:center;max-width:200px;padding:2px;font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:12px;background:rgb(48, 48, 48);border:1px solid rgba(255, 255, 255, 0.15);border-radius:2px;pointer-events:none;z-index:100;}#chatgpt-mermaid-\_r_4rj\_ .flowchartTitleText{text-anchor:middle;font-size:18px;fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4rj\_ rect.text{fill:none;stroke-width:0;}#chatgpt-mermaid-\_r_4rj\_ .icon-shape,#chatgpt-mermaid-\_r_4rj\_ .image-shape{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_4rj\_ .icon-shape p,#chatgpt-mermaid-\_r_4rj\_ .image-shape p{background-color:rgb(0, 0, 0);padding:2px;}#chatgpt-mermaid-\_r_4rj\_ .icon-shape .label rect,#chatgpt-mermaid-\_r_4rj\_ .image-shape .label rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4rj\_ .label-icon{display:inline-block;height:1em;overflow:visible;vertical-align:-0.125em;}#chatgpt-mermaid-\_r_4rj\_ .node .label-icon path{fill:currentColor;stroke:revert;stroke-width:revert;}#chatgpt-mermaid-\_r_4rj\_ .node .neo-node{stroke:rgb(31, 78, 148);}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_4rj\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_4rj\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].node .neo-line path{stroke:rgb(31, 78, 148);filter:none;}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_4rj\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_4rj\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4rj\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_4rj\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4rj\_ .node text{font-size:14px;font-weight:600;letter-spacing:normal;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_4rj\_ .edgeLabels text{font-size:13px;font-weight:600;letter-spacing:-0.08px;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_4rj\_ .node tspan[font-weight="normal"],#chatgpt-mermaid-\_r_4rj\_ .edgeLabels tspan[font-weight="normal"]{font-weight:600;}#chatgpt-mermaid-\_r_4rj\_ .edgeLabel .label rect{opacity:1;rx:13px;ry:13px;fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-width:1px;}#chatgpt-mermaid-\_r_4rj\_ .node rect,#chatgpt-mermaid-\_r_4rj\_ .node circle,#chatgpt-mermaid-\_r_4rj\_ .node ellipse,#chatgpt-mermaid-\_r_4rj\_ .node polygon,#chatgpt-mermaid-\_r_4rj\_ .node path{fill:rgb(0, 40, 77);stroke:rgba(255, 255, 255, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_4rj\_ .node rect{rx:16px;ry:16px;}#chatgpt-mermaid-\_r_4rj\_ .mermaid-decision .label-container{fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-dasharray:2,2;}#chatgpt-mermaid-\_r_4rj\_ .edgePaths .flowchart-link{stroke:rgb(175, 175, 175);stroke-width:1px;stroke-linecap:round;stroke-linejoin:round;}#chatgpt-mermaid-\_r_4rj\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4rj\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}User edits schemaSchema APICreate immutable schemaversionCreate extraction runCelery dispatchExtraction SupervisorResolve documents and fieldsCheck extraction cacheCached valid result?Reuse resultExtraction AgentHybrid RetrievalInspect tables and notesStructured LLM ExtractionDeterministic NormalizationVerification AgentVerified?Persist extracted factReview or retryUpdate resultsYesNoYesNo

The key optimization is incremental processing.

Only the newly added or changed fields should be processed.

OCR and document normalization should not run again unless their underlying artifacts have changed or become invalid.

### **Extraction cache**

The cache key should incorporate:

```
document_id
schema_field_id
field_definition_hash
retrieval_config_version
prompt_version
model_version
```

A changed field definition invalidates the corresponding extraction result, not every result in the schema.

## **Workflow B — Agentic document research**

Business workflow 2

Scenario:

"Why did the company's operating margin decline in FY2025?"

The supervisor should identify that this is a multi-step investigation.

\#chatgpt-mermaid-\_r_4ru\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(237, 237, 237);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_4ru\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4ru\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4ru\_ .error-icon{fill:rgb(48, 48, 48);}#chatgpt-mermaid-\_r_4ru\_ .error-text{fill:rgb(237, 237, 237);stroke:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4ru\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_4ru\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_4ru\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_4ru\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_4ru\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_4ru\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_4ru\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4ru\_ .marker.cross{stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4ru\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_4ru\_ p{margin:0;}#chatgpt-mermaid-\_r_4ru\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4ru\_ .cluster-label text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4ru\_ .cluster-label span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4ru\_ .cluster-label span p{background-color:transparent;}#chatgpt-mermaid-\_r_4ru\_ .label text,#chatgpt-mermaid-\_r_4ru\_ span{fill:rgb(237, 237, 237);color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4ru\_ .node rect,#chatgpt-mermaid-\_r_4ru\_ .node circle,#chatgpt-mermaid-\_r_4ru\_ .node ellipse,#chatgpt-mermaid-\_r_4ru\_ .node polygon,#chatgpt-mermaid-\_r_4ru\_ .node path{fill:rgb(9, 23, 44);stroke:rgb(31, 78, 148);stroke-width:1px;}#chatgpt-mermaid-\_r_4ru\_ .rough-node .label text,#chatgpt-mermaid-\_r_4ru\_ .node .label text,#chatgpt-mermaid-\_r_4ru\_ .image-shape .label,#chatgpt-mermaid-\_r_4ru\_ .icon-shape .label{text-anchor:middle;}#chatgpt-mermaid-\_r_4ru\_ .node .katex path{fill:#000;stroke:#000;stroke-width:1px;}#chatgpt-mermaid-\_r_4ru\_ .rough-node .label,#chatgpt-mermaid-\_r_4ru\_ .node .label,#chatgpt-mermaid-\_r_4ru\_ .image-shape .label,#chatgpt-mermaid-\_r_4ru\_ .icon-shape .label{text-align:center;}#chatgpt-mermaid-\_r_4ru\_ .node.clickable{cursor:pointer;}#chatgpt-mermaid-\_r_4ru\_ .root .anchor path{fill:rgb(175, 175, 175)!important;stroke-width:0;stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4ru\_ .arrowheadPath{fill:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4ru\_ .edgePath .path{stroke:rgb(175, 175, 175);stroke-width:1px;}#chatgpt-mermaid-\_r_4ru\_ .flowchart-link{stroke:rgb(175, 175, 175);fill:none;}#chatgpt-mermaid-\_r_4ru\_ .edgeLabel{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_4ru\_ .edgeLabel p{background-color:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4ru\_ .edgeLabel rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4ru\_ .labelBkg{background-color:rgba(0, 0, 0, 0.5);}#chatgpt-mermaid-\_r_4ru\_ .cluster rect{fill:rgb(48, 48, 48);stroke:rgba(255, 255, 255, 0.15);stroke-width:1px;}#chatgpt-mermaid-\_r_4ru\_ .cluster text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4ru\_ .cluster span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4ru\_ div.mermaidTooltip{position:absolute;text-align:center;max-width:200px;padding:2px;font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:12px;background:rgb(48, 48, 48);border:1px solid rgba(255, 255, 255, 0.15);border-radius:2px;pointer-events:none;z-index:100;}#chatgpt-mermaid-\_r_4ru\_ .flowchartTitleText{text-anchor:middle;font-size:18px;fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4ru\_ rect.text{fill:none;stroke-width:0;}#chatgpt-mermaid-\_r_4ru\_ .icon-shape,#chatgpt-mermaid-\_r_4ru\_ .image-shape{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_4ru\_ .icon-shape p,#chatgpt-mermaid-\_r_4ru\_ .image-shape p{background-color:rgb(0, 0, 0);padding:2px;}#chatgpt-mermaid-\_r_4ru\_ .icon-shape .label rect,#chatgpt-mermaid-\_r_4ru\_ .image-shape .label rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4ru\_ .label-icon{display:inline-block;height:1em;overflow:visible;vertical-align:-0.125em;}#chatgpt-mermaid-\_r_4ru\_ .node .label-icon path{fill:currentColor;stroke:revert;stroke-width:revert;}#chatgpt-mermaid-\_r_4ru\_ .node .neo-node{stroke:rgb(31, 78, 148);}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_4ru\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_4ru\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].node .neo-line path{stroke:rgb(31, 78, 148);filter:none;}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_4ru\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_4ru\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4ru\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_4ru\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4ru\_ .node text{font-size:14px;font-weight:600;letter-spacing:normal;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_4ru\_ .edgeLabels text{font-size:13px;font-weight:600;letter-spacing:-0.08px;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_4ru\_ .node tspan[font-weight="normal"],#chatgpt-mermaid-\_r_4ru\_ .edgeLabels tspan[font-weight="normal"]{font-weight:600;}#chatgpt-mermaid-\_r_4ru\_ .edgeLabel .label rect{opacity:1;rx:13px;ry:13px;fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-width:1px;}#chatgpt-mermaid-\_r_4ru\_ .node rect,#chatgpt-mermaid-\_r_4ru\_ .node circle,#chatgpt-mermaid-\_r_4ru\_ .node ellipse,#chatgpt-mermaid-\_r_4ru\_ .node polygon,#chatgpt-mermaid-\_r_4ru\_ .node path{fill:rgb(0, 40, 77);stroke:rgba(255, 255, 255, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_4ru\_ .node rect{rx:16px;ry:16px;}#chatgpt-mermaid-\_r_4ru\_ .mermaid-decision .label-container{fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-dasharray:2,2;}#chatgpt-mermaid-\_r_4ru\_ .edgePaths .flowchart-link{stroke:rgb(175, 175, 175);stroke-width:1px;stroke-linecap:round;stroke-linejoin:round;}#chatgpt-mermaid-\_r_4ru\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4ru\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}User QuestionSupervisorIdentify Company and PeriodCreate Research PlanAnalytics AgentResearch AgentCalculate Margin TrendSearch ManagementDiscussionSearch Expense NotesCollect FindingsResearch Agent: IdentifyCausesVerification AgentEvidence Sufficient?Additional ResearchReporting AgentFinal Answer with CitationsNoYes

This is where genuine multi-agent coordination becomes valuable.

The Analytics Agent determines whether margins actually declined and by how much.

The Research Agent investigates potential explanations.

The Verification Agent checks whether the explanations are supported by the reports.

The Reporting Agent combines the results.

## **Workflow C — Multi-year company comparison**

Business workflow 3

Scenario:

"Compare revenue, EBITDA, PAT and debt from FY2023 to FY2025."

\#chatgpt-mermaid-\_r_4s5\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(237, 237, 237);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_4s5\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4s5\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4s5\_ .error-icon{fill:rgb(48, 48, 48);}#chatgpt-mermaid-\_r_4s5\_ .error-text{fill:rgb(237, 237, 237);stroke:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4s5\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_4s5\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_4s5\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_4s5\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_4s5\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_4s5\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_4s5\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4s5\_ .marker.cross{stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4s5\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_4s5\_ p{margin:0;}#chatgpt-mermaid-\_r_4s5\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4s5\_ .cluster-label text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4s5\_ .cluster-label span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4s5\_ .cluster-label span p{background-color:transparent;}#chatgpt-mermaid-\_r_4s5\_ .label text,#chatgpt-mermaid-\_r_4s5\_ span{fill:rgb(237, 237, 237);color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4s5\_ .node rect,#chatgpt-mermaid-\_r_4s5\_ .node circle,#chatgpt-mermaid-\_r_4s5\_ .node ellipse,#chatgpt-mermaid-\_r_4s5\_ .node polygon,#chatgpt-mermaid-\_r_4s5\_ .node path{fill:rgb(9, 23, 44);stroke:rgb(31, 78, 148);stroke-width:1px;}#chatgpt-mermaid-\_r_4s5\_ .rough-node .label text,#chatgpt-mermaid-\_r_4s5\_ .node .label text,#chatgpt-mermaid-\_r_4s5\_ .image-shape .label,#chatgpt-mermaid-\_r_4s5\_ .icon-shape .label{text-anchor:middle;}#chatgpt-mermaid-\_r_4s5\_ .node .katex path{fill:#000;stroke:#000;stroke-width:1px;}#chatgpt-mermaid-\_r_4s5\_ .rough-node .label,#chatgpt-mermaid-\_r_4s5\_ .node .label,#chatgpt-mermaid-\_r_4s5\_ .image-shape .label,#chatgpt-mermaid-\_r_4s5\_ .icon-shape .label{text-align:center;}#chatgpt-mermaid-\_r_4s5\_ .node.clickable{cursor:pointer;}#chatgpt-mermaid-\_r_4s5\_ .root .anchor path{fill:rgb(175, 175, 175)!important;stroke-width:0;stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4s5\_ .arrowheadPath{fill:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4s5\_ .edgePath .path{stroke:rgb(175, 175, 175);stroke-width:1px;}#chatgpt-mermaid-\_r_4s5\_ .flowchart-link{stroke:rgb(175, 175, 175);fill:none;}#chatgpt-mermaid-\_r_4s5\_ .edgeLabel{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_4s5\_ .edgeLabel p{background-color:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4s5\_ .edgeLabel rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4s5\_ .labelBkg{background-color:rgba(0, 0, 0, 0.5);}#chatgpt-mermaid-\_r_4s5\_ .cluster rect{fill:rgb(48, 48, 48);stroke:rgba(255, 255, 255, 0.15);stroke-width:1px;}#chatgpt-mermaid-\_r_4s5\_ .cluster text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4s5\_ .cluster span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4s5\_ div.mermaidTooltip{position:absolute;text-align:center;max-width:200px;padding:2px;font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:12px;background:rgb(48, 48, 48);border:1px solid rgba(255, 255, 255, 0.15);border-radius:2px;pointer-events:none;z-index:100;}#chatgpt-mermaid-\_r_4s5\_ .flowchartTitleText{text-anchor:middle;font-size:18px;fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4s5\_ rect.text{fill:none;stroke-width:0;}#chatgpt-mermaid-\_r_4s5\_ .icon-shape,#chatgpt-mermaid-\_r_4s5\_ .image-shape{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_4s5\_ .icon-shape p,#chatgpt-mermaid-\_r_4s5\_ .image-shape p{background-color:rgb(0, 0, 0);padding:2px;}#chatgpt-mermaid-\_r_4s5\_ .icon-shape .label rect,#chatgpt-mermaid-\_r_4s5\_ .image-shape .label rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4s5\_ .label-icon{display:inline-block;height:1em;overflow:visible;vertical-align:-0.125em;}#chatgpt-mermaid-\_r_4s5\_ .node .label-icon path{fill:currentColor;stroke:revert;stroke-width:revert;}#chatgpt-mermaid-\_r_4s5\_ .node .neo-node{stroke:rgb(31, 78, 148);}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_4s5\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_4s5\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].node .neo-line path{stroke:rgb(31, 78, 148);filter:none;}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_4s5\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_4s5\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4s5\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_4s5\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4s5\_ .node text{font-size:14px;font-weight:600;letter-spacing:normal;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_4s5\_ .edgeLabels text{font-size:13px;font-weight:600;letter-spacing:-0.08px;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_4s5\_ .node tspan[font-weight="normal"],#chatgpt-mermaid-\_r_4s5\_ .edgeLabels tspan[font-weight="normal"]{font-weight:600;}#chatgpt-mermaid-\_r_4s5\_ .edgeLabel .label rect{opacity:1;rx:13px;ry:13px;fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-width:1px;}#chatgpt-mermaid-\_r_4s5\_ .node rect,#chatgpt-mermaid-\_r_4s5\_ .node circle,#chatgpt-mermaid-\_r_4s5\_ .node ellipse,#chatgpt-mermaid-\_r_4s5\_ .node polygon,#chatgpt-mermaid-\_r_4s5\_ .node path{fill:rgb(0, 40, 77);stroke:rgba(255, 255, 255, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_4s5\_ .node rect{rx:16px;ry:16px;}#chatgpt-mermaid-\_r_4s5\_ .mermaid-decision .label-container{fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-dasharray:2,2;}#chatgpt-mermaid-\_r_4s5\_ .edgePaths .flowchart-link{stroke:rgb(175, 175, 175);stroke-width:1px;stroke-linecap:round;stroke-linejoin:round;}#chatgpt-mermaid-\_r_4s5\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4s5\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}User RequestSupervisorResolve CompanyFind Annual ReportsComparison AgentRetrieve FY2023 FactsRetrieve FY2024 FactsRetrieve FY2025 FactsNormalize Financial ConceptsValidate Basis, Units andPeriodsComparable?Flag DifferencesAnalytics AgentCalculate Growth and TrendsVerification AgentReporting AgentTables, Charts and AnalysisNoYes

The Comparison Agent should be able to retrieve historical facts from PostgreSQL first, and retrieve original document evidence from Milvus only when needed.

This keeps historical comparisons efficient.

# **11. Retrieval architecture: PostgreSQL + Milvus + FOG**

I want to go deeper into this because retrieval will be the foundation of every agent.

We should have one shared Retrieval Service, not separate retrieval implementations for each agent.

## **11.1 Retrieval hierarchy**

The retrieval service should support four retrieval paths.

## **Unified financial retrieval**

**Agent Retrieval Request**

**Query Understanding and Scope Resolution**

Company · Document · Period · Concept · Reporting basis

**Structured Lookup**

PostgreSQL / FOG

**Semantic Retrieval**

Milvus dense + BM25

**Context Expansion**

Tables · Sections · Notes · Cross-document references

**Ranked Evidence Package**

### **Path 1: Structured retrieval**

Use this when the question requests a known financial fact.

Example:

"What was the revenue in FY2025?"

Query:

```
SELECT *
FROM financial_facts
WHERE company_id = :company_id
  AND concept_id = :concept_id
  AND period_id = :period_id
  AND reporting_basis = :basis;
```

The real query should resolve canonical observations, validation status and applicable document version.

### **Path 2: Hybrid retrieval**

Use this for semantic questions.

Example:

"Why did employee expenses increase?"

Use:

- Dense vector search.
- BM25 sparse retrieval.
- RRF fusion.
- Existing retrieval ranking and filtering.

Your current hybrid retrieval should remain intact.

### **Path 3: Structural retrieval**

Use this when the question requires a full table or section.

Examples:

- "Show me the balance sheet."
- "What are the components of other expenses?"
- "Explain the cash flow statement."

The agent should call table or section inspection tools rather than relying on isolated chunks.

### **Path 4: Cross-document retrieval**

Use this when a question involves multiple years or companies.

Example:

"How did the company's employee expenses change over three years?"

The system should retrieve the relevant reports, resolve equivalent concepts and aggregate evidence.

## **11.2 Milvus collection design**

I would initially retain the existing collection and introduce the required metadata filters.

Suggested metadata:

```
{
  "workspace_id": "workspace_123",
  "company_id": "company_123",
  "document_id": "doc_2025",
  "financial_year": "FY2025",
  "reporting_basis": "consolidated",
  "section": "management_discussion",
  "chunk_type": "table",
  "artifact_id": "artifact_123",
  "document_version": 1
}
```

We need to make sure that workspace authorization is applied before retrieval and that Milvus filters cannot be bypassed by LLM-generated tool arguments.

A shared company corpus may be reused across authorized workspaces, but access to the underlying documents and results must still be controlled.

I would not create a separate Milvus collection for every company. Use metadata filtering unless measured scale or isolation requirements justify partitioning.

## **11.3 Retrieval output contract**

Every retrieval path should return a common evidence representation:

```
class EvidenceItem(BaseModel):    evidence_id: str    workspace_id: str    company_id: str    document_id: str    page_number: int    section: str | None    chunk_type: str    content: str    artifact_ref: str    table_id: str | None = None    cell_refs: list[str] = []    retrieval_method: str    relevance_score: float | None = None
```

This enables all agents to consume the same evidence format.

One important implementation detail: use a stable evidence ID referencing the original artifact and location. Do not make the retrieval rank or score part of evidence identity.

# **12. Frontend architecture and user experience**

The frontend should feel like a dedicated financial research workspace, not a conventional document management application with a chatbot added to it.

I would introduce a company-centric navigation structure.

[Itochu Corporation × NeuralSeek — Customer Highlight](https://images.openai.com/static-rsc-4/Zvqm5HI_CRUXAqjOl8gx9o3dMxMdWqM93u7Nzv3CJAcq7WuvnSVWrp6bGpNiKrc_rxHmaxZpFDssIUF0wnApqvO4tO53jGHAjm0-ExY4o_Ao60amKO4ADALyr4IRcYDG3U-WWYdftbt8TP0cZ17f8ox0NJ2XoSHG-oN--DEpyJ0?purpose=inline)

[neuralseek.ai](https://neuralseek.ai/highlights/itochu?utm_source=chatgpt.com)

## **12.1 Application navigation**

```
Financial Intelligence
│
├── Workspaces
│    └── Workspace A
│
├── Companies
│    └── Reliance Industries
│         │
│         ├── Overview
│         ├── Documents
│         ├── Financial Data
│         ├── Extraction
│         ├── AI Research
│         ├── Analytics
│         └── Reports
│
├── Schema Builder
│
└── Agent Activity
```

### **Company overview**

[Facta | Financial Data Management Software & Accounting Services](https://images.openai.com/static-rsc-4/phhQA_cXhxKdDTSTaPzVJWHF_Dd87r0--FOMq3ASysZ64bOG1CPwHV9FnqBIXw-KpwDxwjDvItlsvcXBR8oxJrOtnkWTtjoZaBrnJk034azTowuZc932KysKGotve0-oL_fB9KQ5TuTLqoeqiQ1SylsMiNw1wf4PMUHfCVtp_xQ?purpose=inline)

[facta.io](https://www.facta.io/?utm_source=chatgpt.com)

Show:

- Company identity.
- Available annual reports.
- Financial periods covered.
- Latest financial metrics.
- Extraction completeness.
- Recent research sessions.
- Agent activity.

### **Document explorer**

[Display by Fadoju David on Dribbble](https://images.openai.com/static-rsc-4/V2D0EGheChbdrukjOUYExl_gPGDkBTfzWBrFpkIuxsEKmYfQZzk1ZAl7QYlKuH_HiejJu4tFsWYzYnaZl2bBx5giwaQAXqG7eH_BOOsp-T1zUFdGqL-gglAhE4NtIrFJmdFOQCijWn6AhvRUcN4Qg4R6VVd46rGes3MQn3m3q10?purpose=inline)

[dribbble.com](https://dribbble.com/shots/24952837-Display?utm_source=chatgpt.com)

Show all reports associated with the company, with:

- Reporting year.
- Reporting basis.
- Processing status.
- Page count.
- Extraction status.
- Available schemas.

### **Extraction workspace**

[Lumenai](https://images.openai.com/static-rsc-4/xAwFPPDC5OwJeAuAWQ_AYhT9Bj7vgwIEdTu53cZmNW0aN-DRYFJdphUVHuMBqsovdalLqJ_8hnMTTfvXOKN7ZS-ZMDtbR4LMb2LutaLrQGUD4mQCFazQsxXU73Z75Zik4nxUtOu4hDDlzDwUDWi00fMn26KaJ7nFwrKW6l-Emtw?purpose=inline)

[getlumenai.com](https://getlumenai.com/?utm_source=chatgpt.com)

I would use a three-panel design:

1. Left: schema and field definitions.
2. Center: extraction results.
3. Right: PDF viewer and evidence.

The user can click an extracted value and immediately see its source.

### **AI research workspace**

[WatchWise | AI Investment Research Agent for Stock Analysis](https://images.openai.com/static-rsc-4/JCH8Acb0pV6dcwyb_8zqKZ2Uc2dCWI5k8Rg8aOKOetc5cUWdrRkl14wKWNaO6x7bGpJWLSTxFL5mfRQsFkGPjvyLh26f4FuQouOFTIwA0SHhe6KuQvUEiI7H3y4Hu7oabn1VX8d7YWmwby3qZtjcd13S7KFTCgw0UU0I5eoSPbs?purpose=inline)

[watchwise.ai](https://watchwise.ai/?utm_source=chatgpt.com)

This is the most important interface.

I recommend:

- Left sidebar: conversations and saved research.
- Main panel: conversational interaction.
- Right panel: sources, tables and document viewer.
- Execution timeline: agent tasks and progress.

The user should be able to inspect the execution process without seeing hidden internal reasoning.

### **Agent activity**

Show:

- Active runs.
- Completed runs.
- Failed runs.
- Current specialist agent.
- Tool execution events.
- Execution duration.
- Token usage.
- Cost.
- Retry history.

This will make the system much more demonstrable and easier to debug.

# **13. Security, reliability and failure handling**

This is where we turn the design into an actual production-oriented architecture.

## **13.1 Authorization model**

Introduce workspace-level roles:

| Role    | Permissions                                         |
| ------- | --------------------------------------------------- |
| Owner   | Full workspace access                               |
| Admin   | Manage members, companies and schemas               |
| Editor  | Upload documents, create schemas and run extraction |
| Analyst | Research, analytics and report generation           |
| Viewer  | Read-only access                                    |

Every operation should enforce authorization at the service boundary.

Agents must never be trusted to enforce permissions themselves.

For example, an LLM cannot decide whether it is allowed to retrieve a document. The retrieval service must enforce workspace access independently.

## **13.2 Agent tool security**

Classify tools into three categories.

| Tool type            | Examples                           | Policy                  |
| -------------------- | ---------------------------------- | ----------------------- |
| Read-only            | Search documents, get facts        | Normal authorization    |
| Controlled writes    | Create schema, save research       | Permission + validation |
| Consequential writes | Delete document, overwrite results | Explicit confirmation   |

Tool arguments must be validated through Pydantic.

Do not expose arbitrary SQL execution, arbitrary Python execution, shell access or unrestricted file-system access to agents.

For research tasks, read-only tools should be the default.

## **13.3 Idempotency**

Agent and extraction jobs may be delivered more than once.

Use idempotency keys for:

- Extraction runs.
- Agent runs.
- Financial fact persistence.
- Report generation.
- Tool operations that mutate data.

A retry should resume from a checkpoint or safely replay an operation, not create duplicate financial facts or duplicate reports.

## **13.4 Failure handling**

| Failure                 | Expected behavior                                                  |
| ----------------------- | ------------------------------------------------------------------ |
| LLM timeout             | Bounded retry with backoff                                         |
| Retrieval failure       | Retry, then mark task failed                                       |
| Agent failure           | Save failure state and checkpoint                                  |
| Celery worker crash     | Redelivery and recovery                                            |
| Duplicate task delivery | Idempotency protection                                             |
| Milvus unavailable      | Fail retrieval explicitly or use an authorized structured fallback |
| PostgreSQL unavailable  | Stop safely; don't publish unpersisted results                     |
| Insufficient evidence   | Ask for clarification or return inconclusive                       |
| Agent loop              | Terminate at execution budget                                      |
| Human approval pending  | Pause graph and resume after approval                              |
| User cancels run        | Signal cancellation and prevent further writes                     |

A critical point: if verification fails, the system should not silently publish an answer as verified.

It should either retry research, ask for clarification or clearly mark the claim as unsupported.

## **13.5 Observability**

I would introduce OpenTelemetry-compatible tracing across:

```
API Request
    |
    v
Agent Run
    |
    v
LangGraph Node
    |
    v
Specialist Agent
    |
    v
Tool Invocation
    |
    v
Domain Service
    |
    v
Database / Milvus / LLM
```

Every event should carry:

- `correlation_id`
- `agent_run_id`
- `task_id`
- `workspace_id`
- `company_id`
- `user_id`

Avoid logging sensitive document contents and full prompts by default. Store only the data needed for debugging and audit, with appropriate access controls and retention.

# **14. Proposed backend project structure**

I would evolve the existing backend without disrupting its current architecture.

A proposed target structure:

```
backend/
└── src/
    └── fdi/
        │
        ├── api/
        │   └── v1/
        │       ├── companies.py
        │       ├── documents.py
        │       ├── schemas.py
        │       ├── extractions.py
        │       ├── conversations.py
        │       ├── agent_runs.py
        │       ├── research.py
        │       ├── analytics.py
        │       └── reports.py
        │
        ├── application/
        │   ├── companies/
        │   ├── schemas/
        │   ├── extractions/
        │   ├── research/
        │   ├── analytics/
        │   └── agent_runs/
        │
        ├── agents/
        │   ├── runtime.py
        │   ├── registry.py
        │   ├── state.py
        │   ├── context.py
        │   ├── policies.py
        │   │
        │   ├── supervisor/
        │   │   ├── graph.py
        │   │   ├── nodes.py
        │   │   ├── planner.py
        │   │   └── routing.py
        │   │
        │   ├── research/
        │   │   ├── graph.py
        │   │   ├── state.py
        │   │   └── nodes.py
        │   │
        │   ├── extraction/
        │   ├── analytics/
        │   ├── comparison/
        │   ├── verification/
        │   └── reporting/
        │
        ├── tools/
        │   ├── registry.py
        │   ├── documents.py
        │   ├── retrieval.py
        │   ├── extraction.py
        │   ├── financial_facts.py
        │   ├── analytics.py
        │   ├── comparison.py
        │   └── reporting.py
        │
        ├── domain/
        │   ├── companies/
        │   ├── financial_facts/
        │   ├── concepts/
        │   ├── schemas/
        │   ├── evidence/
        │   └── research/
        │
        ├── services/
        │   ├── retrieval/
        │   ├── extraction/
        │   ├── fog/
        │   ├── calculations/
        │   ├── verification/
        │   ├── entity_resolution/
        │   └── reports/
        │
        ├── infrastructure/
        │   ├── persistence/
        │   │   ├── repositories/
        │   │   └── models/
        │   ├── vectorstore/
        │   ├── storage/
        │   ├── llm/
        │   ├── checkpointer/
        │   └── messaging/
        │
        ├── workers/
        │   ├── agent_tasks.py
        │   ├── extraction_tasks.py
        │   └── report_tasks.py
        │
        └── pipeline/
            └── existing 22-stage DAG
```

This is a target organization, not a mandate to move every existing module immediately.

Your current `core`, `platform`, `pipeline` and `workers` modules should continue working during migration. Move responsibilities incrementally, with compatibility tests.

The most important boundary is:

- `agents/` owns orchestration.
- `tools/` owns the agent-facing interfaces.
- `services/` owns reusable business operations.
- `domain/` owns financial and business rules.
- `infrastructure/` owns external systems and persistence.
- `pipeline/` owns document processing.

This separation will help you maintain the system as it grows.

# **15. Deployment and worker architecture**

I would retain your current Docker Compose infrastructure and extend it.

Do not introduce Kubernetes just because we are introducing agents. First establish reliable workload separation and resource limits.

## **15.1 Proposed deployment topology**

\#chatgpt-mermaid-\_r_4u0\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(237, 237, 237);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_4u0\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4u0\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_4u0\_ .error-icon{fill:rgb(48, 48, 48);}#chatgpt-mermaid-\_r_4u0\_ .error-text{fill:rgb(237, 237, 237);stroke:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4u0\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_4u0\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_4u0\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_4u0\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_4u0\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_4u0\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_4u0\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4u0\_ .marker.cross{stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4u0\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_4u0\_ p{margin:0;}#chatgpt-mermaid-\_r_4u0\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4u0\_ .cluster-label text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4u0\_ .cluster-label span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4u0\_ .cluster-label span p{background-color:transparent;}#chatgpt-mermaid-\_r_4u0\_ .label text,#chatgpt-mermaid-\_r_4u0\_ span{fill:rgb(237, 237, 237);color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4u0\_ .node rect,#chatgpt-mermaid-\_r_4u0\_ .node circle,#chatgpt-mermaid-\_r_4u0\_ .node ellipse,#chatgpt-mermaid-\_r_4u0\_ .node polygon,#chatgpt-mermaid-\_r_4u0\_ .node path{fill:rgb(9, 23, 44);stroke:rgb(31, 78, 148);stroke-width:1px;}#chatgpt-mermaid-\_r_4u0\_ .rough-node .label text,#chatgpt-mermaid-\_r_4u0\_ .node .label text,#chatgpt-mermaid-\_r_4u0\_ .image-shape .label,#chatgpt-mermaid-\_r_4u0\_ .icon-shape .label{text-anchor:middle;}#chatgpt-mermaid-\_r_4u0\_ .node .katex path{fill:#000;stroke:#000;stroke-width:1px;}#chatgpt-mermaid-\_r_4u0\_ .rough-node .label,#chatgpt-mermaid-\_r_4u0\_ .node .label,#chatgpt-mermaid-\_r_4u0\_ .image-shape .label,#chatgpt-mermaid-\_r_4u0\_ .icon-shape .label{text-align:center;}#chatgpt-mermaid-\_r_4u0\_ .node.clickable{cursor:pointer;}#chatgpt-mermaid-\_r_4u0\_ .root .anchor path{fill:rgb(175, 175, 175)!important;stroke-width:0;stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4u0\_ .arrowheadPath{fill:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4u0\_ .edgePath .path{stroke:rgb(175, 175, 175);stroke-width:1px;}#chatgpt-mermaid-\_r_4u0\_ .flowchart-link{stroke:rgb(175, 175, 175);fill:none;}#chatgpt-mermaid-\_r_4u0\_ .edgeLabel{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_4u0\_ .edgeLabel p{background-color:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4u0\_ .edgeLabel rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4u0\_ .labelBkg{background-color:rgba(0, 0, 0, 0.5);}#chatgpt-mermaid-\_r_4u0\_ .cluster rect{fill:rgb(48, 48, 48);stroke:rgba(255, 255, 255, 0.15);stroke-width:1px;}#chatgpt-mermaid-\_r_4u0\_ .cluster text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4u0\_ .cluster span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4u0\_ div.mermaidTooltip{position:absolute;text-align:center;max-width:200px;padding:2px;font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:12px;background:rgb(48, 48, 48);border:1px solid rgba(255, 255, 255, 0.15);border-radius:2px;pointer-events:none;z-index:100;}#chatgpt-mermaid-\_r_4u0\_ .flowchartTitleText{text-anchor:middle;font-size:18px;fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_4u0\_ rect.text{fill:none;stroke-width:0;}#chatgpt-mermaid-\_r_4u0\_ .icon-shape,#chatgpt-mermaid-\_r_4u0\_ .image-shape{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_4u0\_ .icon-shape p,#chatgpt-mermaid-\_r_4u0\_ .image-shape p{background-color:rgb(0, 0, 0);padding:2px;}#chatgpt-mermaid-\_r_4u0\_ .icon-shape .label rect,#chatgpt-mermaid-\_r_4u0\_ .image-shape .label rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_4u0\_ .label-icon{display:inline-block;height:1em;overflow:visible;vertical-align:-0.125em;}#chatgpt-mermaid-\_r_4u0\_ .node .label-icon path{fill:currentColor;stroke:revert;stroke-width:revert;}#chatgpt-mermaid-\_r_4u0\_ .node .neo-node{stroke:rgb(31, 78, 148);}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_4u0\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_4u0\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].node .neo-line path{stroke:rgb(31, 78, 148);filter:none;}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_4u0\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_4u0\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4u0\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_4u0\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_4u0\_ .node text{font-size:14px;font-weight:600;letter-spacing:normal;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_4u0\_ .edgeLabels text{font-size:13px;font-weight:600;letter-spacing:-0.08px;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_4u0\_ .node tspan[font-weight="normal"],#chatgpt-mermaid-\_r_4u0\_ .edgeLabels tspan[font-weight="normal"]{font-weight:600;}#chatgpt-mermaid-\_r_4u0\_ .edgeLabel .label rect{opacity:1;rx:13px;ry:13px;fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-width:1px;}#chatgpt-mermaid-\_r_4u0\_ .node rect,#chatgpt-mermaid-\_r_4u0\_ .node circle,#chatgpt-mermaid-\_r_4u0\_ .node ellipse,#chatgpt-mermaid-\_r_4u0\_ .node polygon,#chatgpt-mermaid-\_r_4u0\_ .node path{fill:rgb(0, 40, 77);stroke:rgba(255, 255, 255, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_4u0\_ .node rect{rx:16px;ry:16px;}#chatgpt-mermaid-\_r_4u0\_ .mermaid-decision .label-container{fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-dasharray:2,2;}#chatgpt-mermaid-\_r_4u0\_ .edgePaths .flowchart-link{stroke:rgb(175, 175, 175);stroke-width:1px;stroke-linecap:round;stroke-linejoin:round;}#chatgpt-mermaid-\_r_4u0\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_4u0\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}BrowserCaddyNext.jsFastAPIPostgreSQLRedisRabbitMQAgent WorkersExtraction WorkersReport WorkersExisting Pipeline WorkersLangGraph RuntimeAzure OpenAIMilvusFOG ServicesAzure Document IntelligenceMinIO

### **Worker separation**

I recommend three logical worker pools initially.

| Worker pool               | Responsibility      | Workload                             |
| ------------------------- | ------------------- | ------------------------------------ |
| Existing pipeline workers | 22-stage ingestion  | OCR, structure, extraction, indexing |
| Agent workers             | LangGraph execution | Research, comparison, analytics      |
| Reporting workers         | Artifact generation | Large reports, exports               |

You can initially run these as separate Celery queues with separate worker deployments.

For example:

```
RabbitMQ
  |
  +-- fdi.pipeline
  |      └── Existing document workers
  |
  +-- fdi.agent
  |      └── LangGraph workers
  |
  +-- fdi.extraction
  |      └── Extraction workers
  |
  +-- fdi.reporting
         └── Report generation workers
```

Extraction can initially use the existing pipeline execution infrastructure. Introduce a separate extraction queue only when its workload and resource requirements justify it.

### **Why separate agent workers?**

A complex agentic research task can make multiple LLM calls and execute many retrieval operations.

You do not want a long-running research task consuming the worker capacity needed to process newly uploaded annual reports.

Worker isolation also gives you independent concurrency limits, timeouts and scaling policies.

Your current system has one worker with concurrency two according to the audit, so this is a useful area to measure as you extend the system.&#x20;

Pasted text(20261121-141115).txt



# **16. Evaluation and acceptance criteria**

I would make evaluation part of the architecture rather than something we add after implementation.

We need to establish that the multi-agent system actually improves task completion and research quality.

## **16.1 Evaluation benchmark**

Create a test corpus containing annual reports from several companies and financial years.

Build a ground-truth dataset with:

| Benchmark                           | Suggested initial size |
| ----------------------------------- | ---------------------- |
| Structured extraction fields        | 100                    |
| Document research questions         | 50                     |
| Multi-year comparison tasks         | 30                     |
| Financial calculation tasks         | 20                     |
| Citation verification cases         | 50                     |
| Ambiguous or unanswerable questions | 20                     |

These are proposed benchmark targets, not existing project measurements.

## **16.2 Metrics**

| Area         | Metric                  | What it tells us                           |
| ------------ | ----------------------- | ------------------------------------------ |
| Retrieval    | Recall\@K               | Whether relevant evidence was retrieved    |
| Retrieval    | MRR / nDCG              | Whether useful evidence ranks highly       |
| Extraction   | Exact match             | Whether extracted values are correct       |
| Extraction   | Precision / Recall      | Whether fields are correctly identified    |
| Verification | Evidence support rate   | Whether claims are grounded                |
| Agents       | Task completion rate    | Whether workflows accomplish the request   |
| Agents       | Tool selection accuracy | Whether appropriate tools are selected     |
| Analytics    | Calculation accuracy    | Whether metrics are mathematically correct |
| Performance  | P50/P95 latency         | How responsive the system is               |
| Cost         | Cost per completed task | Economic efficiency                        |

One particularly important metric:

**Agent task completion rate should be measured against a predefined expected result, not merely whether the LLM returned a response.**

A research task that returns a fluent but unsupported explanation is a failed task.

## **16.3 Testing strategy**

I would create separate test suites:

```
tests/
├── unit/
│   ├── domain/
│   ├── calculations/
│   ├── schemas/
│   └── verification/
│
├── integration/
│   ├── retrieval/
│   ├── financial_facts/
│   ├── agent_tools/
│   └── persistence/
│
├── agents/
│   ├── supervisor/
│   ├── research/
│   ├── extraction/
│   ├── analytics/
│   ├── comparison/
│   └── verification/
│
├── evaluations/
│   ├── retrieval/
│   ├── extraction/
│   ├── citations/
│   ├── task_completion/
│   └── cost/
│
└── e2e/
    ├── schema_extraction/
    ├── document_chat/
    └── multi_year_analysis/
```

For agent evaluation, use fixed datasets and deterministic mock tools wherever possible. LLM-based evaluations should supplement, not replace, deterministic assertions.

# **17. Implementation roadmap and engineering priorities**

Here is the order I would follow.

## **Engineering implementation plan**

1. **Foundation — Company knowledge model**

   Priority 1
   - Canonical company entities.
   - Company-to-document associations.
   - Reporting period metadata.
   - Financial concept normalization.
   - Workspace authorization.
   - Historical report discovery.

   Acceptance: Multiple annual reports correctly belong to one company, with reporting periods and bases identified.
2. **Dynamic extraction engine**
   - Immutable schema versions.
   - Field CRUD.
   - Incremental extraction.
   - Extraction provenance.
   - Extraction result versioning.
   - Reuse existing extraction engine.

   Acceptance: A user can add a field and extract it without reprocessing the complete document.
3. **Unified retrieval and evidence services**
   - Structured fact retrieval.
   - Hybrid Milvus retrieval.
   - Table and note inspection.
   - Cross-document retrieval.
   - Unified evidence contracts.

   Acceptance: Research and extraction agents use the same retrieval infrastructure.
4. **LangGraph runtime**
   - Shared state model.
   - PostgreSQL checkpointing.
   - Supervisor graph.
   - Specialist subgraphs.
   - Tool registry.
   - Execution budgets.
   - Human approval.

   Acceptance: A supervisor can delegate a multi-step task, collect specialist results and resume from checkpoints.
5. **Chat and research workspace**
   - Conversation APIs.
   - Research Agent.
   - SSE execution events.
   - Source citations.
   - PDF viewer integration.
   - Saved research sessions.

   Acceptance: A user can conduct an evidence-backed investigation across company reports.
6. **Financial analytics and comparison**
   - Historical fact retrieval.
   - Deterministic calculations.
   - Comparability validation.
   - Comparison Agent.
   - Analytics Agent.
   - Research reports.

   Acceptance: A user can compare three reporting years and inspect the underlying values and sources.
7. **Production hardening**
   - Worker isolation.
   - Outbox dispatch.
   - Idempotency.
   - Retry and cancellation.
   - Agent evaluation.
   - Cost observability.
   - Security testing.

   Acceptance: The system can recover from worker failures without duplicating results or publishing unverified findings.

# **18. Architectural decisions to document as ADRs**

I would explicitly record the following decisions in your repository.

| ADR     | Decision                                                            |
| ------- | ------------------------------------------------------------------- |
| ADR-001 | LangGraph as agent orchestration runtime                            |
| ADR-002 | Hierarchical multi-agent architecture                               |
| ADR-003 | Deterministic services remain responsible for financial computation |
| ADR-004 | Celery handles durable background execution                         |
| ADR-005 | PostgreSQL-backed LangGraph checkpointing                           |
| ADR-006 | PostgreSQL/FOG as canonical financial knowledge                     |
| ADR-007 | Milvus as semantic and sparse retrieval infrastructure              |
| ADR-008 | Company-centric document association                                |
| ADR-009 | Immutable extraction schema versioning                              |
| ADR-010 | Evidence-first financial responses                                  |
| ADR-011 | Workspace-level authorization and data isolation                    |
| ADR-012 | Agent execution observability and evaluation                        |

These ADRs will make the architectural reasoning clear to anyone reviewing the project.

# **19. Final system design summary**

Here is the architecture I would consider the baseline for implementation.

# **Financial Document Intelligence Agent**

Company-centric, multi-agent financial research and analytics platform

[How I Built an AI Revenue Intelligence Engine That Predicts Profit Before It Happens | AhteVerse](https://images.openai.com/static-rsc-4/Dw0UZwXuU15fMghzZRqJ_YTHJetLRK6YgUcoGfnIaxLm5bvhNK7O7gWhAeULaSQm8Nv0mZi6o6RnkTh7WfmxrTeCKhR6hF5yW9m_TIknjC4U8ewK2AWlhQtaDf7seb-y9G3PRcJPQuJsD00HYXdqa9cxqzGO9BKD5j7e7u4XJKs?purpose=inline)

[ahteverse.net](https://ahteverse.net/2026/06/01/how-i-built-an-ai-revenue-intelligence-engine-that-predicts-profit-before-it-happens?utm_source=chatgpt.com)

**Application**

- Company workspaces.
- Annual report explorer.
- Dynamic schema builder.
- AI research chat.
- Financial analytics.
- Agent execution timeline.

**Agentic orchestration**

- LangGraph supervisor.
- Research Agent.
- Extraction Agent.
- Analytics Agent.
- Comparison Agent.
- Verification Agent.
- Reporting Agent.

**Intelligence services**

- Unified hybrid retrieval.
- Schema-driven extraction.
- Financial calculations.
- Company and concept resolution.
- Evidence verification.
- Financial report generation.

**Knowledge infrastructure**

- PostgreSQL.
- Milvus.
- Financial Object Graph.
- Redis.
- MinIO.
- LangGraph checkpoints.

**Execution infrastructure**

- FastAPI.
- Celery.
- RabbitMQ.
- Existing 22-stage DAG.
- Azure Document Intelligence.
- Azure OpenAI.

## **20. What I would implement first**

My recommendation is to avoid starting with the supervisor or the multi-agent system.

Start by establishing the company-level knowledge model and unified tool contracts.

Why?

Because all six agents will depend on them.

If company resolution, document association, financial fact retrieval and evidence representation are poorly designed, every agent will inherit those problems.

The dependency order should be:

```
Company Knowledge Model
          |
          v
Unified Retrieval & Evidence
          |
          v
Dynamic Extraction
          |
          v
Financial Fact Services
          |
          v
LangGraph Runtime
          |
          v
Specialized Agents
          |
          v
Company Research Workspace
```

The extraction engine can be developed alongside the knowledge model because it already has a functioning foundation.

**My suggested first engineering milestone:**

Build a company-level workspace that can hold three annual reports, resolve their reporting periods, expose their financial facts through a unified service, and allow users to define and execute versioned extraction schemas.

Once this foundation is established, the LangGraph agents become consumers of a well-defined financial intelligence platform rather than autonomous systems trying to make sense of loosely connected PDFs.

That is the architecture I would be comfortable defending in a Staff AI Engineer or AI Systems Engineer design interview.

**Next step:** I recommend turning this architecture into three concrete engineering artifacts before writing implementation code:

1. `ARCHITECTURE.md` — component architecture, boundaries, infrastructure and data flows.
2. `DATABASE_DESIGN.md` — ER diagram, table definitions, relationships, indexes and migrations.
3. `AGENT_DESIGN.md` — LangGraph state, specialist subgraphs, tools, execution policies and workflows.

These should become the source of truth for implementation, with ADRs recording subsequent architectural changes.