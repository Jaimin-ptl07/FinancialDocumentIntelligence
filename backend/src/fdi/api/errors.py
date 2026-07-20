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
import uuid
from collections.abc import Mapping
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from fdi.platform.obs.logging import get_correlation_id

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


def _instance(correlation_id: str) -> str | None:
    """RFC 9457 s3.1 `instance`: a URI identifying this *occurrence*.

    Without it a 500 body is the same four opaque fields for every caller and
    every cause -- there is nothing to quote at support. The correlation id is
    the handle we already have; this makes it a URI.

    Two shapes, because one honest field beats one uniform lie. `urn:uuid:` has
    a spec (RFC 9562) and a caller may reasonably parse it, so it is used only
    when the id really is a canonical UUID -- which is every id this app mints,
    i.e. the default path. A caller-supplied id is honoured verbatim and is
    often not a UUID (nginx's $request_id is 32 undashed hex; X-Ray's is not a
    UUID at all), and `urn:uuid:1-63441c4a-...` would be malformed. Those get an
    informal, unregistered NID instead: syntactically a valid URN (RFC 8141),
    dereferences to nothing, and claims nothing it cannot back.

    `str(uuid.UUID(v)) == v` rather than a bare `uuid.UUID(v)`: the constructor
    happily accepts 32 undashed hex and normalises it, so the naive check would
    emit an `instance` whose id does not match the `correlation_id` in the log
    line beside it -- the one thing this member exists to make possible.
    """
    if not correlation_id:
        # get_correlation_id() returns "" when unbound, never None. Absent is
        # absent: an `instance` of "urn:uuid:" is worse than no instance.
        return None
    try:
        canonical = str(uuid.UUID(correlation_id)) == correlation_id
    except ValueError:
        canonical = False
    return f"urn:uuid:{correlation_id}" if canonical else f"urn:fdi:request:{correlation_id}"


def problem(
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
    instance = _instance(get_correlation_id())
    if instance is not None:
        content["instance"] = instance
    content.update(extra)  # RFC 9457 s3.2 extension members
    return JSONResponse(status_code=status, media_type=CONTENT_TYPE, content=content, headers=safe)


async def _http_exc(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    detail = exc.detail
    if isinstance(detail, Mapping) and isinstance(detail.get("message"), str):
        # A structured detail: `message` becomes RFC 9457's string `detail` and
        # everything else is spread as extension members (s3.2), exactly the
        # shape `_validation_exc` already emits for its `errors` list. Without
        # this, `str(dict)` would ship a Python repr as the detail string --
        # unparseable by the caller that raised the dict precisely so the
        # caller COULD parse it (PUT /schema's per-path error list).
        extra = {k: v for k, v in detail.items() if k != "message"}
        return problem(
            exc.status_code,
            detail["message"],
            headers=exc.headers,
            **jsonable_encoder(extra),
        )
    return problem(exc.status_code, str(detail), headers=exc.headers)


async def _validation_exc(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    # The structured errors describe the caller's own input, not our internals,
    # so they survive as an extension member. Dropping them would make uniform
    # framing a downgrade against FastAPI's default, which is how a "fix" gets
    # reverted later.
    return problem(422, "Request validation failed.", errors=jsonable_encoder(exc.errors()))


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
    return problem(500, "An internal error occurred.")


def install_error_handlers(app: FastAPI) -> None:
    """Route every error class this app can emit through problem+json.

    Keyed on `starlette.exceptions.HTTPException`, which also covers FastAPI's
    subclass (the MRO walk finds the parent). Keying on FastAPI's would leave the
    router's own 404 and 405 to FastAPI's default handler -- see module docstring.
    """
    app.add_exception_handler(StarletteHTTPException, _http_exc)
    app.add_exception_handler(RequestValidationError, _validation_exc)
    app.add_exception_handler(Exception, _unhandled)
