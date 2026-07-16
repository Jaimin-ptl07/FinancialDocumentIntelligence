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