from pydantic import BaseModel, Field


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