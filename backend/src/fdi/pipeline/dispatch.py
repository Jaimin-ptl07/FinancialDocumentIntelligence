"""Publishing seam.

`api` and `workers` are independent siblings -- neither may import the other, so
the API cannot import the task object directly. `pipeline` sits below both and
owns dispatch. The API depends on this; the concrete task is injected at wiring
time, which also makes dispatch trivially testable with no broker.

The one import of `fdi.workers.tasks` from `api` lives in `api/app.py`'s
lifespan -- the composition root -- and is the single edge exempted in
`.importlinter`. That exemption names exactly `fdi.api.app -> fdi.workers.tasks`;
it does not make `api -> workers` legal in general, and the route module going
back to importing the task still breaks the contract. Verified by re-introducing
that import against the exemption in place, not assumed from the syntax.

On the Protocol below -- read this before trusting it. Celery ships no `py.typed`,
so under `ignore_missing_imports` the whole package resolves to `Any` and
`ping_job` is `Any` (`reveal_type` confirms it; same root cause as the
`type: ignore[untyped-decorator]` in workers/tasks.py). `Any` satisfies every
Protocol, so `JobDispatcher(ping_job)` -- THE ONE CONSTRUCTION THIS CODE PERFORMS
IN PRODUCTION -- is not type-checked against `TaskSender` at all, and will not be
while Celery is untyped.

What it does buy is real, but it is not that: mypy rejects a non-`Any` argument
lacking `apply_async` -- `JobDispatcher(object())`, `JobDispatcher("x")` and
`JobDispatcher(42)` are each measured `arg-type` errors. So the Protocol is not
vacuous; it is simply blind at the call site that matters. Keep it as the stated
requirement of the seam, not as evidence the injected object is a Celery task.
The test suite, not mypy, is what checks that.

One measured consequence, recorded here so it is not rediscovered as a mystery:
if `celery-types` is ever installed, `JobDispatcher(ping_job)` will FAIL mypy --
    Expected: def apply_async(self, args: list[str], ...)
    Got:      def apply_async(self, args: tuple[Any, ...] | None = ..., ...)
because celery-types follows Celery's docstring ("args (Tuple)") rather than its
runtime, which takes either. The fix at that point is to send a tuple and declare
`args: tuple[str, ...]`; it is left alone now because the break is a loud mypy
error, not a silent one, and today's annotation describes what is actually sent.

`args`/`kwargs` mirror `Task.apply_async`'s real parameter names, not a guess:
its signature is `(self, args=None, kwargs=None, task_id=None, ..., **options)`,
and that `**options` means a misspelled keyword is accepted in silence and the
task publishes with no arguments. `tests/unit/test_dispatch.py` pins the names
against the real signature for exactly that reason.

A `list` is correct despite the docstring saying `args (Tuple)`: `AMQP.as_task_v2`
checks `isinstance(args, (list, tuple))` and raises otherwise, so both are
explicitly supported, and kombu's JSON encoder renders them identically on the
wire.
"""

from typing import Protocol


class TaskSender(Protocol):
    """The slice of a Celery task this seam needs: publish, ignore the handle.

    The return is `object`, not Celery's `AsyncResult`, so that `pipeline` states
    no dependency on Celery's result API -- and so that a caller cannot start
    reaching through the dispatcher for one.
    """

    def apply_async(self, args: list[str], kwargs: dict[str, str]) -> object: ...


class JobDispatcher:
    def __init__(self, ping_task: TaskSender) -> None:
        self._ping_task = ping_task

    def dispatch_ping(self, job_id: str, correlation_id: str) -> None:
        """Publish, and deliberately drop the AsyncResult.

        The correlation id travels as a task KWARG, not as a contextvar: nothing
        propagates a contextvar across a broker. `ping_job` binds it on the far
        side, before its own `asyncio.run()` (Global Constraint 4).
        """
        self._ping_task.apply_async(args=[job_id], kwargs={"correlation_id": correlation_id})
