"""Sentry error monitoring (T8): an optional DSN, and a correlation id on
every captured event.

The whole feature is one contract: an unset `FDI_SENTRY_DSN` changes nothing
at boot, and a set one turns both the API and the worker into reporters whose
events carry the SAME correlation id the logs and the problem+json `instance`
member already use -- so a Sentry issue, a log line, and a caller's quote of
`instance` are one lookup, not three.

Why the tests below assert on tags rather than the log/`instance` plumbing:
the id reaches Sentry through the SCOPE, not the structlog contextvar. The
contextvar is provably empty by capture time on both paths -- the API
middleware's `finally: clear_correlation_id()` runs while the exception is
still unwinding past it, and `run_stage_task` clears in its own `finally`
before Celery's tracer sees the failure. Sentry, uniquely, keeps a per-request
/per-task scope OBJECT alive across that unwind (asgi.py's `isolation_scope`
and celery's `_wrap_tracer`), and a tag written to that object survives. The
task id travels as a kwarg (dispatch.py: "nothing carries a contextvar across
the broker"), so the worker path tags from `task_prerun`.

Transport: a real DSN is never contacted here. `init_sentry` accepts a
transport purely so these tests can observe what would have been sent -- the
same seam sentry_sdk's own test suite uses.
"""

from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport

from fdi.api.errors import install_error_handlers
from fdi.api.middleware import CorrelationIdMiddleware
from fdi.platform.config import Settings
from fdi.platform.obs.logging import is_valid_correlation_id
from fdi.platform.obs.sentry import (
    CORRELATION_TAG,
    init_sentry,
    tag_celery_correlation_ids,
    tag_correlation_id,
)
from tests.conftest import TEST_SIGNING_SECRET

# RFC 2606: the .invalid TLD can never resolve, so a wiring test that gets a
# real transport can at worst attempt (and swallow) a DNS failure. No test
# here ever talks to a real Sentry.
_DUMMY_DSN = "https://deadbeef@o0.ingest.sentry.invalid/0"

# The required minimum for Settings() -- same shape as test_config.REQUIRED,
# restated here rather than imported so this file does not depend on another
# test module's fixtures evolving underneath it.
_REQUIRED = {
    "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
    "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
    "FDI_RESULT_BACKEND": "redis://h:6379/0",
    "FDI_S3_ENDPOINT": "h:9000",
    "FDI_S3_ACCESS_KEY": "ak",
    "FDI_S3_SECRET_KEY": "sk",
}


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _REQUIRED.items():
        monkeypatch.setenv(key, value)
    # T3's auth gate made FDI_AUTH_SECRET required (it is in this tree --
    # the conditional shim that guarded against a pre-auth tree is gone).
    # The shared test constant, not a local literal.
    monkeypatch.setenv("FDI_AUTH_SECRET", TEST_SIGNING_SECRET)


class _CaptureTransport(Transport):
    """Keeps events in-process. Capture is synchronous at this seam: the
    client calls the transport inline; only the real HTTP transport queues."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Any) -> None:
        event = envelope.get_event()
        if event is not None:  # transactions etc. are not this ticket's business
            self.events.append(event)


@pytest.fixture
def transport() -> _CaptureTransport:
    return _CaptureTransport()


def _reset_sdk() -> None:
    """No-DSN client + cleared ambient scopes.

    init() alone is NOT enough between tests: it replaces the client but not
    the scope OBJECTS, so a tag written to the shared current scope in one
    test (set_tag with no per-request/per-task fork around it) is still on it
    in the next. Clearing matches production, where every request and task
    starts from a fresh fork (isolation_scope forks the current scope too --
    sentry_sdk/scope.py), and the global scope is left alone: that is where
    integrations hang their own state.
    """
    sentry_sdk.init()
    sentry_sdk.get_current_scope().clear()
    sentry_sdk.get_isolation_scope().clear()


@pytest.fixture
def sentry_off() -> Iterator[None]:
    """A deterministic start AND end for every test that touches the SDK.

    There is no public de-init; `init()` with no DSN installs an inactive
    client, which is exactly the state an unset FDI_SENTRY_DSN boots into.
    Without the teardown half, a test that activates Sentry leaks an active
    client into every later test file in the same pytest process.
    """
    _reset_sdk()
    yield
    _reset_sdk()


# ---------------------------------------------------------------------------
# init_sentry: the no-op contract
# ---------------------------------------------------------------------------


def test_unset_dsn_leaves_the_sdk_inactive(sentry_off: None) -> None:
    assert init_sentry(None) is False
    # `.dsn`, not `.is_active()`: since sentry-sdk 2.x a client is ALWAYS
    # "active"; the honest "sends nothing" signal is a DSN of None.
    assert sentry_sdk.get_client().dsn is None


def test_empty_dsn_is_a_clean_noop(sentry_off: None) -> None:
    """Compose interpolates `${FDI_SENTRY_DSN}` unconditionally, so an unset
    variable reaches the process as "" (the _EmptyIsUnsetEnvSource story).
    An empty string must behave exactly like None, not raise or half-init."""
    assert init_sentry("") is False
    assert sentry_sdk.get_client().dsn is None


def test_dsn_activates_and_captures(sentry_off: None, transport: _CaptureTransport) -> None:
    assert init_sentry(_DUMMY_DSN, transport=transport) is True
    assert sentry_sdk.get_client().dsn == _DUMMY_DSN


def test_a_typoed_dsn_degrades_to_off_not_an_outage(sentry_off: None) -> None:
    """A malformed DSN must not take the service down. The knob is optional:
    a one-character typo in FDI_SENTRY_DSN raises BadDsn out of sentry_sdk.init,
    which without this guard escapes create_app at import time (uvicorn never
    boots) and crashes the worker's module import -- an optional monitor
    converting itself into a full outage. The contract is: log it, boot
    unmonitored."""
    assert init_sentry("sentry.io/0") is False
    assert sentry_sdk.get_client().dsn is None


def test_reinit_keeps_the_first_callers_integrations(
    sentry_off: None, transport: _CaptureTransport
) -> None:
    """One process, two entry points: the API boots create_app (FastAPI +
    Starlette integrations), then its lifespan imports fdi.workers.tasks,
    whose module body runs create_celery_app (CeleryIntegration).
    sentry_sdk.init REPLACES the client, so the second call would silently
    strip StarletteIntegration -- starlette.py's patched __call__ then skips
    SentryAsgiMiddleware entirely and the API half of this feature is dead in
    every monitored deployment, with the correlation tag landing on the
    process-shared scope where concurrent requests race. The FIRST caller's
    init must therefore win; later calls are no-ops that still report "on"."""
    assert init_sentry(
        _DUMMY_DSN, FastApiIntegration(), StarletteIntegration(), transport=transport
    ) is True
    # The second init (what the workers import does) must not replace it.
    assert init_sentry(_DUMMY_DSN, CeleryIntegration()) is True
    client = sentry_sdk.get_client()
    assert client.get_integration(StarletteIntegration) is not None
    assert client.dsn == _DUMMY_DSN

    sentry_sdk.capture_exception(ValueError("boom"))
    assert len(transport.events) == 1
    assert transport.events[0]["exception"]["values"][0]["type"] == "ValueError"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_default_sentry_dsn_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    assert Settings().sentry_dsn is None


def test_settings_read_sentry_dsn_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("FDI_SENTRY_DSN", _DUMMY_DSN)
    assert Settings().sentry_dsn == _DUMMY_DSN


def test_settings_empty_sentry_dsn_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compose passthrough: absent from .env -> "" in the container ->
    None in Settings, i.e. monitoring off rather than a boot failure."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("FDI_SENTRY_DSN", "")
    assert Settings().sentry_dsn is None


# ---------------------------------------------------------------------------
# The correlation tag
# ---------------------------------------------------------------------------


def test_tag_correlation_id_stamps_the_event(
    sentry_off: None, transport: _CaptureTransport
) -> None:
    init_sentry(_DUMMY_DSN, transport=transport)
    tag_correlation_id("cid-1")
    sentry_sdk.capture_exception(ValueError("boom"))
    assert transport.events[0]["tags"][CORRELATION_TAG] == "cid-1"


def test_empty_correlation_id_is_never_a_tag(
    sentry_off: None, transport: _CaptureTransport
) -> None:
    """Same rule as `instance`: an absent id must stay absent. A tag of ""
    would join this event to every other unattributed one in a search."""
    init_sentry(_DUMMY_DSN, transport=transport)
    tag_correlation_id("")
    sentry_sdk.capture_exception(ValueError("boom"))
    assert CORRELATION_TAG not in transport.events[0].get("tags", {})


# ---------------------------------------------------------------------------
# API seam: an unhandled request error, captured, tagged, contract intact
# ---------------------------------------------------------------------------


def _build_stack() -> FastAPI:
    """create_app's error-bearing shape: error handlers + correlation binding
    + a route that raises. The router itself is irrelevant to what is pinned
    here, exactly as in test_api_errors."""
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("unexpected")

    return app


async def test_unhandled_api_error_is_captured_with_correlation_id(
    sentry_off: None, transport: _CaptureTransport
) -> None:
    init_sentry(
        _DUMMY_DSN,
        FastApiIntegration(),
        StarletteIntegration(),
        transport=transport,
    )
    http = ASGITransport(app=_build_stack(), raise_app_exceptions=False)
    async with AsyncClient(transport=http, base_url="http://test") as client:
        r = await client.get("/crash", headers={"x-request-id": "cid-asgi-1"})

    # Sentry observes by wrapping Starlette's __call__ and re-raising; it must
    # not change what the caller sees doing so.
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    assert "unexpected" not in r.json()["detail"]

    assert len(transport.events) == 1
    event = transport.events[0]
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    assert event["tags"][CORRELATION_TAG] == "cid-asgi-1"


async def test_a_minted_id_reaches_the_event_too(
    sentry_off: None, transport: _CaptureTransport
) -> None:
    """No inbound header: the middleware mints an id, and THAT id -- not a
    fresh one -- must be what the event carries. The value itself cannot be
    observed from the response: ServerErrorMiddleware renders a 500 through
    its own send path, outside the correlation middleware's echo (a
    pre-existing gap this ticket does not widen), so presence + validity of
    the minted id is the strongest external assertion available."""
    init_sentry(
        _DUMMY_DSN,
        FastApiIntegration(),
        StarletteIntegration(),
        transport=transport,
    )
    http = ASGITransport(app=_build_stack(), raise_app_exceptions=False)
    async with AsyncClient(transport=http, base_url="http://test") as client:
        r = await client.get("/crash")

    assert r.status_code == 500
    minted = transport.events[0]["tags"][CORRELATION_TAG]
    assert is_valid_correlation_id(minted)





# ---------------------------------------------------------------------------
# Worker seam: a failed task, captured, tagged from its kwarg
# ---------------------------------------------------------------------------


def test_failed_task_is_captured_with_correlation_id(
    sentry_off: None, transport: _CaptureTransport
) -> None:
    from celery import Celery

    init_sentry(_DUMMY_DSN, CeleryIntegration(), transport=transport)
    tag_celery_correlation_ids()

    eager_app = Celery("sentrytest")
    eager_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
        broker_url="memory://",
    )

    @eager_app.task(name="sentrytest.boom")  # type: ignore[untyped-decorator]
    def boom(**kwargs: object) -> None:
        raise RuntimeError("task kaboom")

    with pytest.raises(RuntimeError, match="task kaboom"):
        boom.apply(kwargs={"correlation_id": "cid-task-1"})

    assert len(transport.events) == 1
    event = transport.events[0]
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    # celery_task_id is the integration's own per-task tag; asserting it proves
    # the event flowed through CeleryIntegration, not some generic capture.
    assert "celery_task_id" in event["tags"]
    assert event["tags"][CORRELATION_TAG] == "cid-task-1"


def test_task_without_correlation_id_is_captured_without_the_tag(
    sentry_off: None, transport: _CaptureTransport
) -> None:
    """A task dispatched with no id mints one INSIDE its body, after task_prerun
    -- so the prerun tag cannot exist. The event must still be captured; the
    tag must simply stay absent (never ""), mirroring the API-side rule."""
    from celery import Celery

    init_sentry(_DUMMY_DSN, CeleryIntegration(), transport=transport)
    tag_celery_correlation_ids()

    eager_app = Celery("sentrytest2")
    eager_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
        broker_url="memory://",
    )

    @eager_app.task(name="sentrytest2.boom")  # type: ignore[untyped-decorator]
    def boom(**kwargs: object) -> None:
        raise RuntimeError("task kaboom")

    with pytest.raises(RuntimeError, match="task kaboom"):
        boom.apply()

    assert len(transport.events) == 1
    assert CORRELATION_TAG not in transport.events[0].get("tags", {})


# ---------------------------------------------------------------------------
# Wiring: the factories actually start Sentry when a DSN is configured
# ---------------------------------------------------------------------------


def test_create_app_activates_sentry_when_dsn_set(
    monkeypatch: pytest.MonkeyPatch, sentry_off: None
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("FDI_SENTRY_DSN", _DUMMY_DSN)
    from fdi.api.app import create_app

    create_app()
    client = sentry_sdk.get_client()
    assert client.dsn == _DUMMY_DSN
    assert client.get_integration(StarletteIntegration) is not None


def test_create_app_without_dsn_stays_inactive(
    monkeypatch: pytest.MonkeyPatch, sentry_off: None
) -> None:
    """Unset DSN = nothing happens at boot. Also covers the guard: create_app
    runs at import time under pytest collection, where Settings cannot be
    built -- it must fall back to "off", never raise."""
    _set_required_env(monkeypatch)
    from fdi.api.app import create_app

    create_app()
    assert sentry_sdk.get_client().dsn is None


def test_create_celery_app_activates_sentry_when_dsn_set(
    monkeypatch: pytest.MonkeyPatch, sentry_off: None
) -> None:
    # Env BEFORE the import: fdi.workers.app's module body builds a Celery app
    # -- and therefore Settings -- at import time, so a lazily-imported module
    # must find the environment already in shape.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("FDI_SENTRY_DSN", _DUMMY_DSN)
    from fdi.workers.app import create_celery_app

    create_celery_app()
    client = sentry_sdk.get_client()
    assert client.dsn == _DUMMY_DSN
    assert client.get_integration(CeleryIntegration) is not None


def test_the_api_process_keeps_its_own_instrumentation(
    monkeypatch: pytest.MonkeyPatch, sentry_off: None
) -> None:
    """The full composition, as it runs in a monitored deployment: create_app
    at import, then the lifespan's `from fdi.workers.tasks import ...` firing
    fdi.workers.app's module body (create_celery_app). The module body runs
    once per process, so the test calls create_celery_app directly -- what
    that body executes. The API's Starlette instrumentation must survive the
    encounter (see test_reinit_keeps_the_first_callers_integrations for the
    failure mode this pins out at the composition level)."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("FDI_SENTRY_DSN", _DUMMY_DSN)
    from fdi.api.app import create_app
    from fdi.workers.app import create_celery_app

    create_app()
    create_celery_app()
    assert sentry_sdk.get_client().get_integration(StarletteIntegration) is not None
