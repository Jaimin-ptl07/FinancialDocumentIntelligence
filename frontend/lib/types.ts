/** Mirrors backend/src/fdi/api/schemas.py. Field names stay identical to the
 *  Pydantic views — there is no camelCase translation layer, so a drift
 *  between the two files is visible rather than laundered. */

export type StageStatus = "queued" | "running" | "done" | "failed";

export interface DocumentView {
  id: string;
  checksum: string | null;
  filename: string | null;
  byte_size: number | null;
  doc_key: string | null;
  version: number | null;
  content_type: string | null;
}

/** `GET /documents` list-all: the latest version of each doc_key. `stage_counts`
 *  is a {status: count} summary over the latest run per stage name, so the
 *  filings index does not have to fire one request per row. */
export interface DocumentSummaryView extends DocumentView {
  stage_counts: Record<string, number>;
}

export interface DocumentListView {
  documents: DocumentSummaryView[];
  total: number;
  limit: number;
  offset: number;
}

export interface DocumentVersionsView {
  versions: DocumentView[];
}

export interface StageView {
  name: string;
  status: StageStatus;
  error: string | null;
}

export interface StagesView {
  stages: StageView[];
}

/** Azure Document Intelligence geometry: `polygon` is eight numbers — four
 *  (x, y) corners in INCHES, not pixels. */
export interface BoundingRegion {
  page_number: number | null;
  polygon: number[];
}

export interface BoundingRegionsView {
  current: BoundingRegion[];
  prior: BoundingRegion[];
}

export interface ExtractSourceView {
  page: number | null;
  snippet: string | null;
  evidence_ids: string[];
  /** Split by period — `current` is always a list (possibly a whole-table/
   *  whole-section fallback box); `prior` is `[]` for a non-dual-period
   *  field or when no prior-column cell was found. */
  bounding_regions: BoundingRegionsView;
}

export interface ExtractFieldView {
  field_name: string;
  value: unknown;
  source: ExtractSourceView;
  confidence: number;
}

export interface ExtractDocumentView {
  doc_key: string;
  doc_id: string;
  module: string | null;
  field_count: number;
  fields: ExtractFieldView[];
}

export type DataType = "decimal" | "integer" | "string" | "date" | "list";

export interface SchemaField {
  field_name: string;
  field_path: string;
  canonical_label: string;
  aliases: string[];
  data_type: DataType;
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

/** `GET|PUT /schema`. The document is nested under `schema_document` because
 *  `schema` shadows a Pydantic BaseModel attribute. `source` is "store" once an
 *  edit has been saved, "seed" while the on-disk file is still authoritative. */
export interface SchemaView {
  version: string;
  source: "store" | "seed";
  module_count: number;
  unit_count: number;
  field_count: number;
  schema_document: ExtractionSchema;
}

export interface SchemaValidationView {
  valid: boolean;
  errors: string[];
  module_count: number;
  unit_count: number;
  field_count: number;
}

/** `GET /analytics` — corpus and pipeline figures aggregated over stage_run.
 *  Extraction quality is NOT in here: extraction is an on-demand synchronous
 *  call and nothing persists its results, so the analytics screen derives
 *  quality from the filings it has actually read, and says so. */
export interface StageThroughputView {
  name: string;
  done: number;
  running: number;
  queued: number;
  failed: number;
  median_seconds: number | null;
  cost_inr: number;
}

/** One usage source's totals for a document, priced into INR at read time. */
export interface CostBySourceView {
  source: string;
  call_count: number;
  page_count: number;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
  cost_inr: number;
}

export interface DocumentCostView {
  doc_id: string;
  total_cost_inr: number;
  by_source: CostBySourceView[];
}

/** One individual OCR/LLM call for a document (costing drill-down) — the
 *  raw record CostBySourceView aggregates over. `kind` is "ocr" or "llm";
 *  `page_count` is 0 for an "llm" event, `input_tokens`/`output_tokens`
 *  are 0 for an "ocr" event. */
export interface UsageEventView {
  id: string;
  kind: "ocr" | "llm";
  source: string;
  model: string;
  page_count: number;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
  stage_run_id: string | null;
  correlation_id: string | null;
  created_at: string;
  cost_inr: number;
}

export interface DocumentUsageEventsView {
  doc_id: string;
  events: UsageEventView[];
}

/** One document's total cost, as one row of the corpus-wide "which
 *  filings cost the most" listing. */
export interface DocumentCostSummaryView {
  doc_id: string;
  doc_key: string | null;
  filename: string | null;
  total_cost_inr: number;
}

/** `GET /costing` — the dedicated costing dashboard, distinct from
 *  AnalyticsView (which mixes pipeline throughput with a lighter
 *  cost_inr column): a corpus-wide total, a breakdown by usage source,
 *  and a per-document listing sorted by spend descending. */
export interface CorpusCostView {
  total_cost_inr: number;
  by_source: CostBySourceView[];
  by_document: DocumentCostSummaryView[];
}

export interface AnalyticsView {
  corpus: {
    document_count: number;
    doc_key_count: number;
    total_bytes: number;
    latest_ingested_at: string | null;
  };
  stages: StageThroughputView[];
  documents_complete: number;
  documents_failed: number;
  chunk_count: number;
  total_cost_inr: number;
}
