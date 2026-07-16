"""Broker reachability, for readiness only.

`kombu` rather than `fdi.workers`: `api` and `workers` are independent siblings
in the layers contract, so the API cannot import the Celery app to ask it
anything. kombu is Celery's own transport library and is what `apply_async`
dials underneath, so this probes the same thing the publish path uses without
crossing the boundary. It is declared explicitly in pyproject rather than
leaned on as a transitive dependency of celery.

The connection is opened and closed per probe. A pooled/kept-open connection
would be the obvious optimisation and is wrong here: a readiness probe that
reuses a socket reports on the socket's last known state, which is exactly the
question it is not being asked.
"""

import asyncio

import kombu


async def check_broker(url: str, timeout: float = 2.0) -> bool:  # noqa: ASYNC109
    """True iff a fresh AMQP connection completes inside `timeout`.

    `max_retries=0` is load-bearing: kombu's default retry policy retries
    forever, so without it a dead broker makes /ready hang rather than answer
    False -- turning a readiness probe into the outage it is meant to report.

    ASYNC109 says to drop this parameter and let the caller wrap the call in
    `asyncio.timeout`. That advice is wrong *here* specifically, and taking it
    would be a real bug: kombu is sync, so the connect blocks inside
    `asyncio.to_thread`, and `asyncio.timeout` cannot cancel a thread. It would
    return control on schedule while leaving the socket attempt running in the
    threadpool -- a probe that reports a timeout and leaks a thread per poll.
    The timeout has to be pushed down to the blocking call that can honour it.
    """

    def _probe() -> bool:
        connection = kombu.Connection(url, connect_timeout=timeout)
        try:
            connection.ensure_connection(max_retries=0, timeout=timeout)
            return True
        finally:
            connection.release()

    return await asyncio.to_thread(_probe)