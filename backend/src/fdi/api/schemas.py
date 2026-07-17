from pydantic import BaseModel


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