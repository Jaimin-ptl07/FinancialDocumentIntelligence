"""The publishing seam.

`api` and `workers` are independent siblings, so the API cannot import the task
object to publish it. `JobDispatcher` is the seam that keeps them apart; these
tests pin what it sends.

**The MagicMock test below is weak by construction, and that is worth saying out
loud.** `sender.apply_async.assert_called_once_with(...)` asserts that the
dispatcher passed a particular shape to a mock that has no relationship to
`ping_job` whatsoever. Rename `correlation_id` to `cid` in tasks.py and that test
still passes, because a MagicMock accepts every call. The bug then surfaces as a
worker-side TypeError on a message that was already published and already acked
by the broker -- i.e. in production, not here.

`test_dispatch_payload_is_accepted_by_the_real_ping_job` closes that: it takes
what the dispatcher ACTUALLY sent and checks it against the real task's real
signature, offline. Celery does exactly this check itself inside `apply_async`
(`if self.typing: self.__header__(*args, **kwargs)` -- app/task.py), before it
touches the broker, so this is not an invented rule: it is the same gate the
production call passes through, run without a broker.
"""

import inspect
import os
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from fdi.pipeline.dispatch import JobDispatcher, StageDispatcher

REQUIRED_ENV = {
    "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
    "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
    "FDI_RESULT_BACKEND": "redis://h:6379/0",
    "FDI_S3_ENDPOINT": "h:9000",
    "FDI_S3_ACCESS_KEY": "ak",
    "FDI_S3_SECRET_KEY": "sk",
}


@pytest.fixture
def _worker_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make `fdi.workers.tasks` importable.

    `fdi.workers.app` builds Settings in its module body, so importing the task
    reads the environment. Same shape as tests/unit/test_celery_app.py's fixture,
    and for the same two reasons: an unrelated exported FDI_ var trips Task 6's
    validator, and only clearing the whole prefix can fix that.

    The import stays inside the test bodies rather than at module scope -- at
    module scope it would run at COLLECTION time, before any fixture, and the
    file would fail to collect on a machine with no FDI_ env at all.
    """
    for key in [k for k in os.environ if k.upper().startswith("FDI_")]:
        monkeypatch.delenv(key)
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    from fdi.platform.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_dispatch_publishes_with_correlation_id() -> None:
    sender = MagicMock()
    JobDispatcher(sender).dispatch_ping(job_id="j-1", correlation_id="c-1")
    sender.apply_async.assert_called_once_with(args=["j-1"], kwargs={"correlation_id": "c-1"})


def test_dispatch_payload_is_accepted_by_the_real_ping_job(_worker_env: None) -> None:
    """What the dispatcher sends must be callable against the task that runs it.

    This is the assertion the MagicMock cannot make. The mock proves the
    dispatcher is self-consistent; this proves it agrees with `ping_job`.

    Note it reads the payload back off the mock rather than restating
    `args=["j-1"]` inline. Restating it would test this file against itself: the
    literal would have to be edited in lockstep with the dispatcher, so it could
    never disagree with it, and the disagreement is the entire thing being
    tested.
    """
    from fdi.workers.tasks import ping_job

    sender = MagicMock()
    JobDispatcher(sender).dispatch_ping(job_id="j-1", correlation_id="c-1")
    sent = sender.apply_async.call_args.kwargs

    # `.run` is the undecorated function the worker actually executes.
    inspect.signature(ping_job.run).bind(*sent["args"], **sent["kwargs"])


def test_dispatch_kwargs_are_real_apply_async_parameters(_worker_env: None) -> None:
    """`apply_async(..., **options)` swallows a typo'd keyword in SILENCE.

    Its real signature is `(self, args=None, kwargs=None, task_id=None, ...,
    **options)`. A dispatcher that wrote `arguments=[job_id]` would raise no
    TypeError and fail no mock assertion -- the value would land in `**options`,
    `args` would stay `None`, and the task would be published with no arguments
    at all. The worker would then reject the message it was correctly handed.
    So binding is not enough; the kwargs must reach DECLARED parameters.

    Signature is read off the INSTANCE attribute. `ping_job` is a Celery
    `PromiseProxy`, not a Task -- the real task is built lazily on first attribute
    access -- so `type(ping_job).apply_async` raises AttributeError. Going through
    the proxy is what resolves it, and is also what production does.
    """
    from fdi.workers.tasks import ping_job

    sender = MagicMock()
    JobDispatcher(sender).dispatch_ping(job_id="j-1", correlation_id="c-1")
    sent = sender.apply_async.call_args.kwargs

    bound = inspect.signature(ping_job.apply_async).bind(**sent)
    assert "options" not in bound.arguments, (
        f"dispatch_ping passed {sorted(set(sent) - set(bound.arguments))} into "
        f"apply_async's **options catch-all instead of a declared parameter. "
        f"Celery accepts it silently and publishes the task without arguments."
    )


def test_stage_dispatcher_publishes_args_and_correlation_kwarg() -> None:
    """`StageDispatcher` reuses the same `TaskSender` protocol as `JobDispatcher`:
    args=[doc_id, stage_name], correlation_id as a kwarg -- nothing carries a
    contextvar across the broker."""
    calls = []

    class _Rec:
        def apply_async(self, args, kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            return object()

    StageDispatcher(_Rec()).dispatch("doc1", "ping", "cid-9")
    assert calls == [(["doc1", "ping"], {"correlation_id": "cid-9"})]
