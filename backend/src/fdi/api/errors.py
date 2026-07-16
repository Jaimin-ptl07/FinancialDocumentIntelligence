"""RFC 9457 problem+json error responses.

The contract is that *every* error leaving this app is application/problem+json,
whoever raised it. Most of the work here is not formatting the body -- it is
reaching the errors the app never raises by hand.

Starlette dispatches by walking `type(exc).__mro__` and taking the first class
present in the handler table (starlette/_exception_handler.py). A handler keyed
on a *subclass* therefore never sees the *parent*, and the parent --
`starlette.exceptions.HTTPException` -- is what the router itself raises for an
unmatched path (404) and a wrong method (405). FastAPI pre-registers its own
plain-`application/json` handler against that parent, and against
`RequestValidationError` (422), which is not an HTTPException at all. Key the
handlers wrong and those three keep answering in the other media type: one API,
two error formats, chosen by how the caller got it wrong.
"""

import contextlib
import http
from collections.abc import Mapping
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

log = structlog.get_logger(__name__)

CONTENT_TYPE = "application/problem+json"

# An exception's headers are part of the protocol contract, not decoration:
# RFC 9110 requires Allow on a 405 and WWW-Authenticate on a 401, and Starlette's
# router attaches the former itself. But they must not be able to unset the one
# thing this module exists to guarantee, so the framing headers stay ours.
_RESERVED_HEADERS = frozenset({"content-type", "content-length"})


def _body_allowed(status: int) -> bool:
    """RFC 9110: 1xx, 204, 205 and 304 responses carry no content.

    Inlined rather than imported: Starlette's default handler hardcodes
    {204, 304} and FastAPI's reaches for a private helper in `fastapi.utils`.
    The rule belongs to the RFC, so depend on neither.
    """
    return status >= 200 and status not in (204, 205, 304)


def _safe_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    if not headers:
        return None
    return {k: v for k, v in headers.items() if k.lower() not in _RESERVED_HEADERS} or None


def _problem(
    status: int,
    detail: str,
    title: str | None = None,
    headers: Mapping[str, str] | None = None,
    **extra: Any,
) -> Response:
    safe = _safe_headers(headers)
    if not _body_allowed(status):
        return Response(status_code=status, headers=safe)
    if title is None:
        try:
            # RFC 9457 s4.2.1: with type "about:blank", title SHOULD be the
            # HTTP status phrase.
            title = http.HTTPStatus(status).phrase
        except ValueError:
            title = "Error"
    content: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
    }
    content.update(extra)  # RFC 9457 s3.2 extension members
    return JSONResponse(status_code=status, media_type=CONTENT_TYPE, content=content, headers=safe)


async def _http_exc(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    return _problem(exc.status_code, str(exc.detail), headers=exc.headers)


async def _validation_exc(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    # The structured errors describe the caller's own input, not our internals,
    # so they survive as an extension member. Dropping them would make uniform
    # framing a downgrade against FastAPI's default, which is how a "fix" gets
    # reverted later.
    return _problem(422, "Request validation failed.", errors=jsonable_encoder(exc.errors()))


async def _unhandled(_: Request, exc: Exception) -> Response:
    # Log the real cause; return an opaque body. Internals are not client business.
    #
    # The response must not depend on logging succeeding. Measured on Windows:
    # with structlog unconfigured, its default ConsoleRenderer draws the traceback
    # through rich, whose box glyphs are unencodable on a cp1252 stdout -- the
    # UnicodeEncodeError escapes this handler and the caller gets a bodyless 500
    # with no content-type, which is precisely what this module exists to prevent.
    # (Same failure class as the PYTHONUTF8 note on the Imports gate.) pytest hides
    # it: its capture replaces sys.stdout with a UTF-8 CaptureIO, so the encoding
    # that breaks production is never the one under test.
    #
    # Swallowing is safe rather than lossy: ServerErrorMiddleware re-raises `exc`
    # after this response is sent, so the server still logs the original cause.
    with contextlib.suppress(Exception):
        log.exception("api.unhandled_exception", error=repr(exc))
    return _problem(500, "An internal error occurred.")


def install_error_handlers(app: FastAPI) -> None:
    """Route every error class this app can emit through problem+json.

    Keyed on `starlette.exceptions.HTTPException`, which also covers FastAPI's
    subclass (the MRO walk finds the parent). Keying on FastAPI's would leave the
    router's own 404 and 405 to FastAPI's default handler -- see module docstring.
    """
    app.add_exception_handler(StarletteHTTPException, _http_exc)
    app.add_exception_handler(RequestValidationError, _validation_exc)
    app.add_exception_handler(Exception, _unhandled)
