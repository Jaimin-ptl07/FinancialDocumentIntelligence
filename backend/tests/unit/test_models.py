from fdi.platform.db.models import Base, Job, JobStatus


def test_job_table_name() -> None:
    assert Job.__tablename__ == "jobs"


def test_job_is_registered_on_metadata() -> None:
    assert "jobs" in Base.metadata.tables


def test_job_columns() -> None:
    cols = Base.metadata.tables["jobs"].columns
    assert {"id", "status", "artifact_key", "error",
            "correlation_id", "created_at", "updated_at"} <= set(cols.keys())
    assert cols["id"].primary_key
    assert cols["status"].index
    assert cols["artifact_key"].nullable
    assert not cols["status"].nullable


def test_job_status_values() -> None:
    assert {s.value for s in JobStatus} == {"queued", "running", "done", "failed"}
