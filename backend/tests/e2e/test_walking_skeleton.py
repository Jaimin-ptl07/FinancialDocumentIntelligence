"""The SP-1 walking skeleton, end to end, against the live compose stack.

Unlike tests/integration -- which builds a per-test app wired to testcontainers
and drives it in-process over ASGITransport -- these drive the REAL, already
running HTTP surface at localhost:8000 and the real Celery worker behind it. They
prove the deployed wiring: API -> broker -> worker -> Postgres + S3 -> API, over
actual sockets, exactly as `docker compose --profile core up -d` assembles it.

Reachability is a SESSION-SCOPED FIXTURE, never a module-level guard. pytest
imports every test module during COLLECTION to discover its marks, and it does so
BEFORE `-m` deselection applies -- so a module-level `httpx.get(...)` would fire a
real socket to :8000 on every invocation, including the fast unit loop
(`pytest -m "not integration and not e2e"`), whose defining property is that it
touches no network at all. It would also be unsound: if some unrelated process
answered on :8000, the probe would pass and these tests would run against the
wrong server. Mark deselection happens before fixture setup, so a fixture probes
only when an e2e test is genuinely about to run; `-m "not e2e"` opens no socket.
"""

import time

import httpx
import pytest

# Module-level mark is cheap and is what `-m "not e2e"` deselects on. It carries
# no network cost -- the probe lives in the `stack` fixture below, not here.
pytestmark = pytest.mark.e2e

API = "http://localhost:8000"


@pytest.fixture(scope="session")
def stack() -> str:
    """Return the API base URL, or skip every e2e test if the stack is down.

    The probe is INSIDE this fixture body on purpose (see the module docstring):
    collection must not open a socket. Session scope means it runs once and the
    result -- including a `pytest.skip` -- is reused for all three tests, so a
    down stack yields three honest `skipped` lines rather than three probes.
    """
    try:
        reachable = httpx.get(f"{API}/health", timeout=2.0).status_code == 200
    except Exception:
        reachable = False
    if not reachable:
        pytest.skip("compose --profile core is not up (nothing serving localhost:8000)")
    return API


def test_ready_reports_all_backends(stack: str) -> None:
    """/ready sees every backend it needs to actually serve /ping-job."""
    body = httpx.get(f"{stack}/ready", timeout=5.0).json()
    assert body["postgres"] is True
    assert body["s3"] is True


def test_full_round_trip(stack: str) -> None:
    """POST a job, poll until terminal, then fetch and verify its artifact."""
    accepted = httpx.post(f"{stack}/ping-job", timeout=5.0)
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]

    deadline = time.monotonic() + 60.0
    view: dict[str, object] | None = None
    while time.monotonic() < deadline:
        view = httpx.get(f"{stack}/jobs/{job_id}", timeout=5.0).json()
        if view["status"] in {"done", "failed"}:
            break
        time.sleep(1.0)

    assert view is not None, "no /jobs response before the 60s deadline"
    assert view["status"] == "done", view
    assert view["artifact_key"] == f"jobs/{job_id}/ping.json"

    artifact = httpx.get(str(view["artifact_url"]), timeout=5.0)
    assert artifact.status_code == 200
    assert artifact.json() == {"kind": "ping", "schema_version": 1}


def test_correlation_id_is_echoed(stack: str) -> None:
    """A caller-supplied X-Request-ID comes back as the job's correlation_id."""
    accepted = httpx.post(
        f"{stack}/ping-job", headers={"X-Request-ID": "e2e-fixed-id"}, timeout=5.0
    )
    assert accepted.json()["correlation_id"] == "e2e-fixed-id"
