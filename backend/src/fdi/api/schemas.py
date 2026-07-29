from datetime import datetime

from pydantic import BaseModel, Field

from fdi.core.ontology import CanonicalConcept


class JobAccepted(BaseModel):
    job_id: str
    correlation_id: str
    status: str


class JobView(BaseModel):
    job_id: str
    status: str
    artifact_key: str | None = None
    artifact_url: str | None = None
    error: str | None = None


class ReadyView(BaseModel):
    postgres: bool
    s3: bool
    broker: bool


class StaleView(BaseModel):
    stale: list[str]


class ReprocessView(BaseModel):
    dispatched: list[str]


class OcrTriggerView(BaseModel):
    doc_id: str
    stage: str


class SemanticTriggerView(BaseModel):
    doc_id: str
    stage: str


class EmbeddingTriggerView(BaseModel):
    doc_id: str
    stage: str


class GraphLoadTriggerView(BaseModel):
    doc_id: str
    stage: str


class EvalRequest(BaseModel):
    candidate: str
    dataset: str
    dataset_version: int
    metric: str | None = None


class EvalAccepted(BaseModel):
    run_id: str
    aggregate: float


class LeaderboardEntry(BaseModel):
    candidate: str
    version: int
    aggregate: float


class LeaderboardView(BaseModel):
    leaderboard: list[LeaderboardEntry]


class DocumentView(BaseModel):
    id: str
    checksum: str | None
    filename: str | None
    byte_size: int | None
    doc_key: str | None
    version: int | None
    content_type: str | None


class DocumentVersionsView(BaseModel):
    versions: list[DocumentView]


class DocumentSummaryView(BaseModel):
    """One corpus-list row: the latest version of a doc_key, plus a
    {status: count} summary over the latest run per stage name."""

    id: str
    checksum: str | None
    filename: str | None
    byte_size: int | None
    doc_key: str | None
    version: int | None
    content_type: str | None
    stage_counts: dict[str, int]


class DocumentListView(BaseModel):
    documents: list[DocumentSummaryView]
    total: int
    limit: int
    offset: int


class StageStatusView(BaseModel):
    name: str
    status: str
    error: str | None = None


class StagesView(BaseModel):
    stages: list[StageStatusView]


class SearchRequest(BaseModel):
    query: str
    k: int = 5


class SearchResultView(BaseModel):
    id: str
    score: float
    metadata: dict[str, object]


class SearchResultsView(BaseModel):
    results: list[SearchResultView]


class EntityMemberView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    section_count: int
    concept_count: int
    quality_ok: bool


class QualityTrendPoint(BaseModel):
    version: int | None
    ok: bool
    error_count: int
    warning_count: int


class EntityView(BaseModel):
    schema_version: int
    entity_id: str
    member_count: int
    members: list[EntityMemberView]
    concepts: list[str]
    # presence value is a member version (int) or, for a version-less member, its doc_id (str)
    concept_presence: dict[str, list[int | str]]
    quality_trend: list[QualityTrendPoint]


class EntityResolveRequest(BaseModel):
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class EntitiesView(BaseModel):
    schema_version: int
    threshold: float
    entity_count: int
    entities: list[EntityView]
    unresolved: list[str]


class TableGridBody(BaseModel):
    n_rows: int
    n_cols: int
    header_row_count: int
    column_headers: list[str]
    grid: list[list[str]]


class TableView(BaseModel):
    table_index: int
    row_count: int
    column_count: int
    stitched: bool = False
    grid: TableGridBody


class TablesView(BaseModel):
    doc_id: str
    tables: list[TableView]


class ConceptValueView(BaseModel):
    column_header: str
    raw: str
    value: float | None


class ConceptRowView(BaseModel):
    table_index: int
    row_label: str
    values: list[ConceptValueView]


class ConceptLookupView(BaseModel):
    doc_id: str
    concept: str
    rows: list[ConceptRowView]


class VectorSearchFilters(BaseModel):
    section_type: str | None = None
    doc_key: str | None = None
    version: int | None = None


class VectorSearchRequest(BaseModel):
    query: str
    k: int = 5
    filters: VectorSearchFilters | None = None


class VectorSearchResultView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    chunk_id: str
    heading: str | None
    page: int | None
    section_type: str | None
    text: str
    score: float


class VectorSearchResultsView(BaseModel):
    results: list[VectorSearchResultView]


class IndexFtsView(BaseModel):
    doc_id: str
    chunks_indexed: int


class KeywordSearchRequest(BaseModel):
    query: str
    k: int = 5
    section_type: str | None = None
    doc_key: str | None = None
    version: int | None = None


class KeywordHitView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    chunk_id: str
    heading: str | None
    page: int | None
    section_type: str | None
    text: str
    score: float


class KeywordSearchResultsView(BaseModel):
    results: list[KeywordHitView]


class HybridSearchFilters(BaseModel):
    section_type: str | None = None
    doc_key: str | None = None
    version: int | None = None


class HybridSearchRequest(BaseModel):
    query: str
    k: int = 5
    filters: HybridSearchFilters | None = None


class HybridSearchResultView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    chunk_id: str
    heading: str | None
    page: int | None
    section_type: str | None
    text: str
    score: float


class HybridSearchResultsView(BaseModel):
    results: list[HybridSearchResultView]


class GraphCitationView(BaseModel):
    doc_id: str | None
    doc_key: str | None
    version: int | None
    heading: str | None
    page: int | None
    bounding_regions: list[dict[str, object]] | None


class GraphNodeView(BaseModel):
    id: str
    type: str
    label: str
    doc_id: str | None = None
    citation: GraphCitationView


class GraphParagraphView(BaseModel):
    id: str
    type: str
    label: str
    citation: GraphCitationView


class GraphSectionView(BaseModel):
    id: str
    type: str
    label: str
    citation: GraphCitationView
    paragraphs: list[GraphParagraphView]


class GraphNeighborsRequest(BaseModel):
    node_id: str


class GraphNeighborView(BaseModel):
    node: GraphNodeView
    relationship: str
    direction: str


class GraphNeighborsView(BaseModel):
    schema_version: int
    node_id: str
    neighbors: list[GraphNeighborView]


class DocumentStructureView(BaseModel):
    schema_version: int
    doc_id: str
    document: GraphNodeView | None
    sections: list[GraphSectionView]


class ReadingOrderView(BaseModel):
    schema_version: int
    doc_id: str
    sections: list[GraphNodeView]


class EntityTimelineQualityView(BaseModel):
    ok: bool
    error_count: int
    warning_count: int


class EntityTimelineEntryView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    section_count: int
    concept_count: int
    quality_ok: bool
    concepts: list[str]
    quality: EntityTimelineQualityView


class EntityTimelineView(BaseModel):
    schema_version: int
    doc_key: str
    member_count: int
    timeline: list[EntityTimelineEntryView]
    concepts: list[str]
    concept_presence: dict[str, list[int | str]]


class ConceptComparisonValueView(BaseModel):
    column_header: str
    raw: str
    value: float | None


class ConceptComparisonRowView(BaseModel):
    table_index: int
    row_label: str
    values: list[ConceptComparisonValueView]


class ConceptComparisonVersionView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    status: str
    rows: list[ConceptComparisonRowView]


class ConceptCompareRequest(BaseModel):
    doc_key: str
    concept: CanonicalConcept


class ConceptComparisonView(BaseModel):
    schema_version: int
    doc_key: str
    concept: str
    versions: list[ConceptComparisonVersionView]


class AskRequest(BaseModel):
    question: str
    max_rounds: int | None = None


class AskCitationView(BaseModel):
    doc_id: str | None = None
    doc_key: str | None = None
    version: int | None = None
    heading: str | None = None
    page: int | None = None
    chunk_id: str | None = None
    table_index: int | None = None
    row_label: str | None = None
    column_header: str | None = None
    node_id: str | None = None


class AskToolTraceEntryView(BaseModel):
    round: int
    tool: str
    arguments: dict[str, object]
    result: object
    error: str | None


class AskResponse(BaseModel):
    answer: str
    citations: list[AskCitationView]
    tool_trace: list[AskToolTraceEntryView]


class ExtractRequest(BaseModel):
    doc_key: str
    module: str | None = None
    # When False (the default), a persisted record for this exact
    # (doc_id, schema) pair is served as-is -- no retrieval, no LLM calls.
    # Set True to discard it and re-run extraction from scratch.
    force: bool = False


class BoundingRegionView(BaseModel):
    """One page-scoped bounding region, in the shape `ocr`/`provenance`
    already store: `page_number` plus Azure's flat coordinate list."""

    page_number: int | None = None
    polygon: list[object] = []


class BoundingRegionsView(BaseModel):
    """A field's bounding geometry, split by period. `current` is always a
    list (possibly the whole-table/whole-section fallback box); `prior` is
    [] for a non-dual-period field or when no prior-column cell was found."""

    current: list[BoundingRegionView] = []
    prior: list[BoundingRegionView] = []


class ExtractSourceView(BaseModel):
    page: int | None
    snippet: str | None
    evidence_ids: list[str]
    # {current, prior}, not a flat list -- see 2026-07-25 design. A record
    # persisted before this change (schema_version 1) will not validate here;
    # EXTRACTION_RECORD_VERSION was bumped alongside this so a v1 record is a
    # cache miss (re-extracted fresh), never fed to this validator.
    bounding_regions: BoundingRegionsView = BoundingRegionsView()


class ExtractFieldView(BaseModel):
    field_name: str
    value: object
    source: ExtractSourceView
    confidence: float


class ExtractDocumentView(BaseModel):
    doc_key: str
    doc_id: str
    module: str | None
    field_count: int
    fields: list[ExtractFieldView]


class SchemaView(BaseModel):
    """The current field-extraction schema plus the counts a UI header
    shows. `source` is "store" (an edited copy in the artifact bucket) or
    "seed" (the immutable on-disk file, when nothing has been saved yet).
    The document field is named `schema_document`, not `schema`, because
    `schema` shadows a BaseModel attribute."""

    version: str
    source: str
    module_count: int
    unit_count: int
    field_count: int
    schema_document: dict[str, object]


class SchemaWriteRequest(BaseModel):
    base_version: str
    schema_document: dict[str, object]


class SchemaValidateRequest(BaseModel):
    schema_document: dict[str, object]


class SchemaValidationView(BaseModel):
    valid: bool
    errors: list[str]
    module_count: int
    unit_count: int
    field_count: int


class CorpusTotalsView(BaseModel):
    """Whole-corpus intake figures. `document_count` counts rows,
    `doc_key_count` counts filings (rows grouped on `coalesce(doc_key, id)`),
    so the two differ by exactly the number of re-uploaded versions."""

    document_count: int
    doc_key_count: int
    total_bytes: int
    latest_ingested_at: datetime | None


class StageThroughputView(BaseModel):
    """One pipeline stage, counted over the LATEST run per (document, stage).
    `median_seconds` covers the `done` runs only, and is null when there are
    none -- a stage that has never succeeded has no throughput, which is not
    the same as a throughput of zero."""

    name: str
    done: int
    running: int
    queued: int
    failed: int
    median_seconds: float | None


class AnalyticsView(BaseModel):
    """`GET /analytics`. Corpus-wide and never paginated: the screen that
    reads it shows totals, and a page of totals is not a total."""

    corpus: CorpusTotalsView
    stages: list[StageThroughputView]
    documents_complete: int
    documents_failed: int
    chunk_count: int
