"""Correlation ID: accept the caller's, or mint one. Bind it for the whole request.

Why a middleware and not a dependency: the id must be bound before *anything*
logs and must still be bound when `api.errors._unhandled` renders a 500 -- and
that handler runs in `ServerErrorMiddleware`, outside the router entirely. A
`Depends` binds too late for the first and is gone by the second.

Why pure ASGI and not `BaseHTTPMiddleware`: BaseHTTPMiddleware runs the
downstream app in a child anyio task, so the endpoint gets a *copy* of the
contextvars Context (Global Constraint 4's copy-in/no-copy-out, one layer up).
Binding before `call_next` would still work, but anything bound further in --
which is what `tests/unit/test_api_errors.py` pins for the endpoint -> handler
hop -- would silently stop propagating. A plain ASGI middleware adds no task
boundary, so there is one Context for the whole request and no copy semantics to
reason about.
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fdi.api.errors import problem
from fdi.platform.obs.logging import (
    CORRELATION_ID_MAX_LENGTH,
    bind_correlation_id,
    clear_correlation_id,
    is_valid_correlation_id,
    new_correlation_id,
)
from fdi.platform.obs.sentry import tag_correlation_id

HEADER = "x-request-id"


class CorrelationIdMiddleware:
    """Bind a correlation id for every HTTP request; reject an unusable one.

    The inbound header is honoured verbatim -- propagating a caller's request id
    is the entire point, and a value we quietly rewrite is worse than useless:
    the caller's logs and ours would then disagree about the same request while
    both looking correct.

    That is also why an over-long or otherwise unusable id is a **400 and not a
    silent repair**. The three options were:

      * truncate -- the caller's id and ours silently differ, which breaks the
        one property the header exists to provide;
      * ignore and mint -- same divergence, and it deletes the feature;
      * reject -- the caller learns immediately, and it is *their* input.

    Rejecting is also the only option that stops a client header from choosing
    the response code: `correlation_id` is varchar(36) and PostgreSQL raises
    rather than truncates, so a 37-character header currently reaches the DB and
    comes back as a **500** (measured). A 400 names the actual mistake.

    The rejection itself still gets a freshly minted id, bound before the
    response is built, so even a refusal is traceable and carries an `instance`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # `lifespan` and `websocket` scopes traverse the middleware stack too --
        # Starlette's own middlewares all guard on this. Reading headers off a
        # lifespan scope would raise at startup, before anything can report it.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # `.get` returns the FIRST of a duplicated header, which is the value the
        # nearest proxy stamped. Both get validated the same way regardless.
        inbound = Headers(scope=scope).get(HEADER)
        # An empty header is "absent", not "invalid": `X-Request-ID:` with no
        # value is a proxy artefact, not a caller asserting an id. Minting here
        # is also what keeps `""` -- the third representation of "absent", the
        # one no type checker objects to -- out of the database.
        rejected = inbound is not None and inbound != "" and not is_valid_correlation_id(inbound)
        correlation_id = inbound if (inbound and not rejected) else new_correlation_id()

        bind_correlation_id(correlation_id)
        # The id must also reach Sentry's per-request SCOPE, here at bind time,
        # not the contextvar at capture time: the `finally` below clears the
        # contextvar while the exception is still unwinding past this frame, but
        # a tag on Sentry's scope object survives to the capture that happens
        # further out. A no-op with no DSN configured -- see obs/sentry.py.
        tag_correlation_id(correlation_id)
        # The echo wraps the REJECTION too, not just the app. A refusal is still
        # a response, and the id on it is the minted one -- which is the only
        # way the caller can quote the request whose id we would not accept.
        send = self._echo(send, correlation_id)
        try:
            if rejected:
                response = problem(
                    400,
                    f"The {HEADER} header must be 1-{CORRELATION_ID_MAX_LENGTH} "
                    f"characters from [A-Za-z0-9._~-]. A fresh correlation id was "
                    f"assigned to this response; see the `instance` member.",
                    title="Invalid request id",
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
        finally:
            # Not optional. uvicorn serves many requests per worker task context
            # and nothing resets contextvars between them -- the same mechanism
            # that leaked ids between Celery jobs in Task 14. Without this, a
            # request whose middleware somehow did not bind would log under its
            # predecessor's id.
            clear_correlation_id()

    def _echo(self, send: Send, correlation_id: str) -> Send:
        """Reflect the id on the response so a caller who sent none learns it.

        Safe only because the value is validated: an unvalidated header echoed
        into a response header is response splitting. This is the concrete reason
        the charset rule is not merely defensive.
        """

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault(HEADER, correlation_id)
            await send(message)

        return _send