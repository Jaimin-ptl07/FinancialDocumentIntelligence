"""What a REAL uvicorn does with a 500 — the ASGI transport cannot answer this.

`ServerErrorMiddleware` sends the problem+json response and then re-raises,
unconditionally ("We always continue to raise the exception. This allows servers
to log the error."). `tests/unit/test_api_errors.py` pins that the client gets
its body, and drives raw ASGI to prove it. What neither it nor `ASGITransport`
can show is what happens to the exception AFTER it escapes — because in those
tests nothing is on the other side of the re-raise. uvicorn is.

The concern (from Task 7, and carried into Task 16 as an inference from
uvicorn's source, unverified): uvicorn logs its own traceback for that re-raise,
alongside the one `api.errors._unhandled` already logged — which would partly
undo the no-double-printing work `configure_logging`'s ProcessorFormatter exists
to do.

So: a real uvicorn, on a real socket, over real HTTP. No Docker — this needs no
backend, only a route that raises.
"""

import json
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from fdi.api.errors import install_error_handlers
from fdi.platform.obs.logging import configure_logging


def _build_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("uvicorn-visible-boom")

    return app


def _serve(app: FastAPI, sock: socket.socket) -> tuple[uvicorn.Server, threading.Thread]:
    """Run uvicorn on an already-bound socket, in this process.

    In-process, not a subprocess, so `configure_logging`'s StreamHandler and
    pytest's capture see the same stdout.

    Note what is NOT passed: `log_config`. Leaving it at uvicorn's default is
    the entire point -- that default is what `CMD ["uvicorn", "fdi.api.app:app",
    ...]` gets, and it is the thing that used to break us. Passing
    `log_config=None` here would make the test agree with itself: it would
    disable the hijacking that `_HIJACKED_LOGGERS` exists to undo, and pass
    whether or not that code was there at all.
    """
    config = uvicorn.Config(app, lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "uvicorn did not start"
    return server, thread


@pytest.fixture
def crashing_server():
    """Note what is deliberately NOT here: `configure_logging`.

    pytest uses a different CaptureIO for the setup phase than for the call
    phase, and `configure_logging` binds a StreamHandler to `sys.stdout` as it
    finds it. Configure from a fixture -- i.e. during setup -- and the handler
    holds a stream that is CLOSED by the time the test body runs: every record
    is dropped with `--- Logging error --- ValueError: I/O operation on closed
    file`, and a test asserting on log output reads an empty string and passes.
    Measured; it is how this file's first draft proved nothing. So the tests
    that read log output configure logging in their own body.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))  # port 0: let the OS pick, so tests never collide
    port = sock.getsockname()[1]
    server, thread = _serve(_build_app(), sock)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_real_uvicorn_still_returns_the_problem_json_500(crashing_server: str) -> None:
    """The response survives a real server, not just the ASGI transport.

    httpx FABRICATES `status_code = 500` with empty headers over
    ASGITransport(raise_app_exceptions=False) when the app sends nothing at all
    -- so every 500 assertion in this project made over that transport is, on its
    own, not evidence the app produced anything. Over a real socket there is
    nothing to fabricate: these bytes crossed a TCP connection.
    """
    r = httpx.get(f"{crashing_server}/crash")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 500
    assert "uvicorn-visible-boom" not in json.dumps(body), "internals must not leak"


def test_traceback_is_logged_exactly_once_by_a_real_uvicorn(
    capsys, crashing_server: str
) -> None:
    """Task 16 carry-forward 3, and the answer is: uvicorn DOES log its own.

    Two records carry the traceback for one 500:
      * ours    -- `api.unhandled_exception`, JSON, with the correlation id;
      * uvicorn -- `uvicorn.error`: "Exception in ASGI application", from the
        re-raise, via `logging.Logger.error(exc_info=True)` in
        uvicorn/protocols/http/httptools_impl.py.

    Both are single, well-formed JSON lines -- `configure_logging`'s
    ProcessorFormatter renders uvicorn's record through the same chain via
    `foreign_pre_chain`, so the traceback lands in an `exception` FIELD rather
    than being re-appended as raw text. That is what Task 7's work bought and it
    is intact: the count is 2 records, not 2 records plus 2 loose tracebacks.

    So the duplication is real but benign, and this test pins the shape rather
    than pretending it away. Deleting uvicorn's copy would mean silencing
    `uvicorn.error`, which also silences the failures our handler never sees.
    """
    configure_logging("INFO")  # in the body -- see the crashing_server docstring
    httpx.get(f"{crashing_server}/crash")
    # uvicorn logs from the server thread; give it a moment to flush.
    time.sleep(0.5)

    captured = capsys.readouterr()
    out = captured.out.strip()
    assert out, "captured nothing at all -- this test would otherwise prove nothing"

    # Every line must be a complete JSON record. This is the invariant Task 7
    # bought: a raw traceback printed beside the JSON shows up here as
    # unparseable lines, so a log shipper sees N events and not N events plus
    # loose stack frames. Checked over ALL lines before any filtering -- a
    # `startswith("{")` filter would discard exactly what this is looking for.
    records: list[dict[str, Any]] = []
    for line in out.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pytest.fail(f"non-JSON line on stdout -- something printed raw:\n{line!r}")

    with_traceback = [r for r in records if "uvicorn-visible-boom" in r.get("exception", "")]
    events = [r.get("event") for r in with_traceback]
    assert len(with_traceback) == 2, f"expected our record + uvicorn's, got {events}"
    assert "api.unhandled_exception" in events
    assert any("ASGI" in str(e) for e in events), f"uvicorn's own record missing: {events}"

    # The regression this file exists for. Before _HIJACKED_LOGGERS, uvicorn's
    # dictConfig sent its copy to a StreamHandler on STDERR as raw text -- a
    # bare "Traceback (most recent call last):" and loose frames, beside our
    # JSON. Both halves of the assert matter: a traceback on stderr is the bug,
    # and stderr going quiet only because uvicorn stopped logging would be a
    # different bug, which the count above catches.
    assert "Traceback (most recent call last)" not in captured.err, (
        "uvicorn printed a raw traceback outside the JSON renderer:\n" + captured.err[:2000]
    )