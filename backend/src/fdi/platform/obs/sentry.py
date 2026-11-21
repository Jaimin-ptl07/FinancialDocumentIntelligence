"""Sentry error monitoring: an optional DSN, and a correlation id on every event.

ADR-0002's demo posture needs failures diagnosable after the fact, without
Prometheus or tracing (deliberately out of scope per the productionization
spec). Sentry is the chosen sink, and this module is the only place that
INITIALISES it: callers hand `init_sentry` a DSN plus the integrations
appropriate to their process (api -> FastAPI/Starlette, workers -> Celery),
and no other module calls into the SDK beyond choosing those integration
classes.

The no-op contract is load-bearing: an unset `FDI_SENTRY_DSN` must change
nothing at boot. That is why an empty/None DSN returns BEFORE sentry_sdk.init
ever runs -- no client, no patched integrations, no background threads; the
SDK's no-init state and this module's "off" state are the same state.

Correlation ids reach events through the SCOPE, not the structlog contextvar.
The contextvar is provably empty by capture time on both paths this module
serves: the API middleware clears it in a `finally` while the exception is
still unwinding past it, and run_stage_task clears in its own `finally` before
Celery's tracer handles the failure. Sentry keeps a per-request/per-task scope
object alive across that unwind (asgi.py's `isolation_scope`, celery's
`_wrap_tracer`), and a tag written to that object survives. `sentry_sdk`
calls in here are safe when the SDK is off: scopes exist without a client,
tags just sit on them unseen, and nothing is captured to send them anywhere.
"""

from typing import Any

import sentry_sdk
import structlog
from sentry_sdk.transport import Transport

log = structlog.get_logger(__name__)

# Same name as the log key and the problem+json `instance` id it carries, so
# "search Sentry for what this caller quoted" is one lookup.
CORRELATION_TAG = "correlation_id"


def init_sentry(
    dsn: str | None, *integrations: Any, transport: Transport | None = None
) -> bool:
    """Start Sentry when a DSN is configured; do nothing at all otherwise.

    Returns whether the SDK is active, so a caller with path-specific extras
    (the worker's prerun tagging) can skip wiring them when monitoring is off.

    Idempotent per process, FIRST CALLER WINS: sentry_sdk.init replaces the
    client wholesale, and the API process runs BOTH entry points -- create_app
    installs the FastAPI/Starlette integrations at import, then its lifespan
    imports fdi.workers.tasks, whose module body runs create_celery_app. An
    unguarded second init would strip the first's integrations and transport
    (sentry's auto-detection often restores the former, which is exactly why
    the bug would be invisible until it mattered). Whichever entry point boots
    first owns the process; each one runs alone in production (uvicorn boots
    create_app; `celery -A fdi.workers.app` boots create_celery_app), so this
    guard costs nothing there and exists for the one process that runs both.

    A DSN that fails to parse degrades to "off" with a log line rather than
    an exception: this knob is an optional add-on, and a typo converting it
    into a boot outage (create_app runs at import -- uvicorn would never
    start) would make enabling monitoring the most dangerous thing an
    operator can do. repr(exc), not exc_info: init can run before logging is
    configured, and structlog's fallback renderer draws tracebacks through
    rich, whose glyphs are unencodable on cp1252 stdout (see api/app.py's
    lifespan note).

    `transport` exists for tests: it is the one seam sentry_sdk offers for
    observing what would be sent without sending it. Production never passes
    one.
    """
    if not dsn:
        return False
    if sentry_sdk.get_client().dsn is not None:
        return True
    try:
        sentry_sdk.init(
            dsn=dsn,
            integrations=list(integrations),
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001 -- degrade, never block boot
        log.warning("sentry.init_failed", error=repr(exc))
        return False
    return True


def tag_correlation_id(value: str) -> None:
    """Stamp the correlation id on the current Sentry scope.

    Called where the id is BOUND (the API correlation middleware), not where
    the event is captured: by capture time the structlog contextvar has been
    cleared by the binders' own `finally` blocks, but the scope object holds
    the tag. An empty value is skipped, mirroring `errors._instance`: an
    absent id must stay absent, never become a tag of "".
    """
    if value:
        sentry_sdk.set_tag(CORRELATION_TAG, value)


def tag_celery_correlation_ids() -> None:
    """Tag worker events from the task's `correlation_id` kwarg.

    The id crosses the broker as a kwarg (dispatch.py: "nothing carries a
    contextvar across the broker"), and the task body binds it to structlog
    only for the logs -- then clears it in a `finally` before Celery's tracer
    reports the failure, so capture-time readers see nothing. `task_prerun`
    is the one hook that runs with the kwargs in hand while still inside
    Sentry's per-task scope, which is where the tag has to land.

    Connect only when Sentry is active; an unconnected signal handler on the
    "off" path would be wiring for wiring's sake.
    """
    from celery import signals  # local: keeps a no-Sentry boot from importing celery here

    def _on_prerun(
        sender: object = None, kwargs: dict[str, Any] | None = None, **_: Any
    ) -> None:
        tag_correlation_id(str((kwargs or {}).get("correlation_id", "")))

    # weak=False is load-bearing: connect() holds RECEIVERS by weak reference,
    # and this closure is local -- without a strong ref it is collected the
    # moment tag_celery_correlation_ids returns, and the tag silently never
    # fires. (Measured: events captured, tag absent, with weak's default True.)
    signals.task_prerun.connect(_on_prerun, weak=False)
