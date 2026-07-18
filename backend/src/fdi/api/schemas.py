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


class ExtractSourceView(BaseModel):
    page: int | None
    snippet: str | None
    evidence_ids: list[str]


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
