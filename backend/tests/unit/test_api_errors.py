"""Every error this API emits must be RFC 9457 application/problem+json.

The module's entire value is that the media type is *unconditional*, so most of
these tests attack the paths that never reach a hand-written `raise
HTTPException`: the 404 and 405 Starlette's router raises on its own, and the 422
FastAPI raises from validation. Each of those already has a default handler that
answers in a different media type, and a default handler is invisible until
something asserts on it -- the client is the one who discovers that an API has
two error formats.

Handlers are matched by walking `type(exc).__mro__` (starlette/_exception_handler.py
`_lookup_exception_handler`), which is why the registration *key* is load-bearing
rather than a stylistic choice: see test_unmatched_route_is_problem_json.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from fdi.api.errors import install_error_handlers
from fdi.platform.obs.logging import bind_correlation_id, configure_logging


def _build_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=404, detail="job not found")

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("unexpected")

    @app.get("/typed")
    async def typed(n: int) -> dict[str, int]:
        return {"n": n}

    @app.get("/guarded")
    async def guarded() -> None:
        raise HTTPException(
            status_code=401, detail="nope", headers={"WWW-Authenticate": "Bearer"}
        )

    @app.get("/not-modified")
    async def not_modified() -> None:
        raise HTTPException(status_code=304)

    @app.get("/crash-after-binding")
    async def crash_after_binding() -> None:
        # Models Task 16's routes, which bind the id inside the endpoint.
        bind_correlation_id("cid-from-endpoint")
        raise RuntimeError("unexpected")

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """raise_app_exceptions=False is required here, not stylistic.

    ServerErrorMiddleware sends the 500 and then re-raises unconditionally, so
    with the httpx default this client raises instead of returning a response.
    What that flag costs us is pinned in
    test_unhandled_exception_reaches_the_client_before_propagating, which drives
    the app the way uvicorn does and asserts on both halves.

    `yield` inside `async with`, not `return`: a returned client is never closed.
    """
    transport = ASGITransport(app=_build_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------
# The invariant itself, stated once over every way out of the app.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "status", "why"),
    [
        ("GET", "/boom", 404, "endpoint raised fastapi.HTTPException"),
        ("GET", "/crash", 500, "endpoint raised a bare Exception"),
        ("GET", "/nope", 404, "router raised starlette.HTTPException: no such route"),
        ("POST", "/boom", 405, "router raised starlette.HTTPException: wrong method"),
        ("GET", "/typed?n=notanint", 422, "FastAPI raised RequestValidationError"),
    ],
)
async def test_every_error_path_is_problem_json(
    client: AsyncClient, method: str, path: str, status: int, why: str
) -> None:
    """One media type, whoever raised. The last three never touch our code."""
    r = await client.request(method, path)
    assert r.status_code == status, why
    assert r.headers["content-type"].startswith("application/problem+json"), (
        f"{why} -> escaped as {r.headers['content-type']!r}"
    )
    body = r.json()
    # RFC 9457 s3.1: type is the only member with a default ("about:blank").
    # We commit to emitting all four so clients can rely on their presence.
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert isinstance(body["title"], str) and body["title"]
    assert isinstance(body["detail"], str) and body["detail"]


# --------------------------------------------------------------------------
# Per-path specifics.
# --------------------------------------------------------------------------


async def test_http_exception_is_problem_json(client: AsyncClient) -> None:
    r = await client.get("/boom")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 404
    assert body["detail"] == "job not found"
    assert body["title"] == "Not Found"


async def test_unhandled_exception_is_problem_json_without_leaking(
    client: AsyncClient,
) -> None:
    r = await client.get("/crash")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 500
    assert "unexpected" not in body["detail"], "internals must not leak to clients"


async def test_unmatched_route_is_problem_json(client: AsyncClient) -> None:
    """Starlette's router raises *starlette* HTTPException, not FastAPI's.

    Registering the handler against fastapi.HTTPException cannot catch it: the
    lookup walks type(exc).__mro__, and the parent's MRO does not contain the
    subclass. FastAPI pre-registers its own handler for the parent, so this path
    silently answers in application/json -- two error formats from one API,
    chosen by how the client got it wrong.
    """
    r = await client.get("/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["title"] == "Not Found"


async def test_wrong_method_is_problem_json(client: AsyncClient) -> None:
    r = await client.post("/boom")
    assert r.status_code == 405
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["title"] == "Method Not Allowed"


async def test_method_not_allowed_keeps_its_allow_header(client: AsyncClient) -> None:
    """RFC 9110 s15.5.6: a 405 response MUST generate an Allow header.

    Starlette's router builds it and attaches it to the exception. A handler that
    renders only status+detail drops it -- trading one spec violation for
    another. `Allow` is joined from a set upstream, so compare as a set.

    {"GET"} and not {"GET", "HEAD"}: FastAPI's APIRoute takes `methods` verbatim,
    where Starlette's Route implies HEAD for a GET. That is FastAPI's call to
    make; this test's business is only that whatever the router decided survives
    us intact.
    """
    r = await client.post("/boom")
    assert r.status_code == 405
    assert set(r.headers["allow"].replace(" ", "").split(",")) == {"GET"}


async def test_http_exception_keeps_its_headers(client: AsyncClient) -> None:
    """RFC 9110 s11.6.1: a 401 MUST carry WWW-Authenticate. Same failure mode."""
    r = await client.get("/guarded")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_validation_error_is_problem_json_with_the_errors(
    client: AsyncClient,
) -> None:
    """RequestValidationError is not an HTTPException -- it derives from
    ValidationException -- so no HTTPException handler can reach it, and
    FastAPI's default answers 422 in application/json.

    The structured errors survive as an RFC 9457 extension member (s3.2); they
    describe the caller's own input, not our internals, and dropping them would
    make the fix a downgrade against FastAPI's default.
    """
    r = await client.get("/typed?n=notanint")
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["title"] == "Unprocessable Content"
    assert body["errors"][0]["loc"] == ["query", "n"]
    assert body["errors"][0]["type"] == "int_parsing"


async def test_status_forbidding_a_body_gets_no_body(client: AsyncClient) -> None:
    """RFC 9110 s15.4.5: a 304 has no content. Both handlers this module
    displaces special-case that; a problem+json body here is malformed HTTP."""
    r = await client.get("/not-modified")
    assert r.status_code == 304
    assert r.content == b""
    assert "content-type" not in r.headers


# --------------------------------------------------------------------------
# The 500 path: what leaves the process, and what only reaches the log.
# --------------------------------------------------------------------------


async def test_500_leaks_nothing_through_headers(client: AsyncClient) -> None:
    """The body is checked elsewhere; headers are the other way out."""
    r = await client.get("/crash")
    assert {k.lower() for k in r.headers} <= {"content-type", "content-length"}
    blob = json.dumps(dict(r.headers)).lower()
    assert "unexpected" not in blob
    assert "runtimeerror" not in blob


async def test_500_body_carries_no_trace_of_the_cause(client: AsyncClient) -> None:
    r = await client.get("/crash")
    blob = json.dumps(r.json()).lower()
    for internal in ("unexpected", "runtimeerror", "traceback", "test_api_errors"):
        assert internal not in blob, f"{internal!r} leaked to the client"


async def test_unhandled_exception_log_carries_correlation_id_and_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cause must reach the operator even though it never reaches the client.

    Also pins that structlog's context survives the endpoint -> handler hop: the
    id is bound *inside* the endpoint (as Task 16's routes do) and read back in
    the exception handler. Starlette's request_response is a plain await chain
    with no task group, so the contextvars Context is shared rather than copied --
    but Global Constraint 4 exists because that is exactly the kind of thing that
    is assumed rather than checked.
    """
    configure_logging("INFO")
    transport = ASGITransport(app=_build_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/crash-after-binding")
    assert r.status_code == 500

    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if ln.startswith("{")]
    entry = next(
        p for p in (json.loads(ln) for ln in lines) if p.get("event") == "api.unhandled_exception"
    )
    assert entry["correlation_id"] == "cid-from-endpoint"
    assert "RuntimeError" in entry["exception"], "the traceback must survive in the log"


async def test_500_gives_the_caller_a_handle_without_leaking(client: AsyncClient) -> None:
    """RFC 9457 s3.1 `instance`. The opaque 500 body is the point AND the problem.

    Four identical fields for every caller and every cause means a user reporting
    "it 500s" hands support nothing to search on, while the log line that knows
    everything is keyed by a correlation id the caller was never told. `instance`
    is the join between them, and it leaks nothing: it is the id we minted, not
    anything about the failure.

    urn:fdi:request:, not urn:uuid: -- "cid-from-endpoint" is not a UUID, and
    urn:uuid: has a syntax (RFC 9562) a client may reasonably parse.
    """
    r = await client.get("/crash-after-binding")
    assert r.status_code == 500
    body = r.json()
    assert body["instance"] == "urn:fdi:request:cid-from-endpoint"
    assert "unexpected" not in json.dumps(body), "the handle must not become a leak"


async def test_problem_without_a_bound_correlation_id_omits_instance(
    client: AsyncClient,
) -> None:
    """`get_correlation_id()` returns "" when unbound, never None.

    So the naive `content["instance"] = f"urn:uuid:{get_correlation_id()}"`
    emits `"urn:uuid:"` -- a syntactically invalid URI that a client would take
    for a real handle and quote at support, where it identifies nothing. Absent
    must stay absent. (This app's middleware always binds; this pins the
    behaviour for every other caller of `problem()`, including a 500 raised
    before the middleware ran.)
    """
    r = await client.get("/crash")
    assert r.status_code == 500
    assert "instance" not in r.json()


async def test_500_survives_a_logger_that_raises(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """The client's response must not depend on logging succeeding.

    Not hypothetical, and not about exotic loggers. Measured on this box: with
    structlog unconfigured, `log.exception` renders the traceback through rich,
    whose box glyphs are unencodable on a cp1252 stdout. The UnicodeEncodeError
    escapes the handler and the caller gets a bodyless 500 with no content-type --
    the exact outcome this module exists to prevent.

    pytest cannot see that on its own: its capture swaps sys.stdout for a UTF-8
    CaptureIO, so the encoding that breaks production is never the one under test.
    Hence a logger that raises on purpose, which pins the invariant without
    depending on the console encoding of whoever runs the suite.
    """

    class _Boom:
        def exception(self, *_a: object, **_k: object) -> None:
            raise UnicodeEncodeError("charmap", "x", 0, 1, "unencodable")

    monkeypatch.setattr("fdi.api.errors.log", _Boom())

    r = await client.get("/crash")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["status"] == 500


async def test_unhandled_exception_reaches_the_client_before_propagating() -> None:
    """What raise_app_exceptions=False hides, asserted directly.

    ServerErrorMiddleware sends the response and *then* re-raises, unconditionally
    -- starlette/middleware/errors.py: "We always continue to raise the
    exception. This allows servers to log the error." httpx swallows that only
    because the fixture asks it to; a real server (uvicorn) sees the exception and
    logs its own traceback.

    So drive the ASGI app the way uvicorn does and assert both halves at once:
    the client still gets the full problem+json 500, AND the exception still
    escapes. Worth its own test because with raise_app_exceptions=False httpx
    *fabricates* `status_code = 500` when the app sends nothing at all -- so
    `assert r.status_code == 500` over that transport is not by itself evidence
    that this module produced anything.
    """
    app = _build_app()
    messages: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/crash",
        "raw_path": b"/crash",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "root_path": "",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    with pytest.raises(RuntimeError, match="unexpected"):
        await app(scope, receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 500
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert headers["content-type"].startswith("application/problem+json")

    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    body = json.loads(raw)
    assert body["status"] == 500
    assert "unexpected" not in json.dumps(body)
