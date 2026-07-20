"""The browser contract done-criterion 6 depends on and httpx cannot see.

The Next.js UI (Task 22) fetches this API from http://localhost:3000, a different
origin. A browser sends such a request but then withholds the RESPONSE from the
page's JavaScript unless the response carries `Access-Control-Allow-Origin` -- so
without CORS the fetch "succeeds" at the network layer and `response.json()`
throws, the job view never fills, and the walking skeleton looks broken with no
server error to point at. curl and httpx enforce no origin policy, so every other
test in this suite is blind to it; this is the one that stands in for the browser.

`create_app()` is driven directly over ASGITransport. Lifespan is NOT entered
(httpx does not run it here), so no engine, bucket, or broker is touched -- which
is why `/health` is the probe: it is the only route that needs none of them, and
CORS headers are applied by the middleware regardless of what the route does.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.api.app import create_app
from fdi.platform.config import get_settings

DEV_ORIGIN = "http://localhost:3000"


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_preflight_from_the_ui_origin_is_allowed(client: AsyncClient) -> None:
    """The OPTIONS the browser sends before a POST /ping-job.

    CORSMiddleware answers it itself and never reaches the endpoint, so this holds
    without any backend wired -- it proves the preflight is permitted for the
    method the UI actually uses.
    """
    r = await client.options(
        "/ping-job",
        headers={
            "Origin": DEV_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == DEV_ORIGIN
    assert "POST" in r.headers["access-control-allow-methods"]


async def test_simple_request_response_is_readable_by_the_ui(client: AsyncClient) -> None:
    """A plain GET carries ACAO, so the page's JS may read the body.

    This is the failure mode CORS actually fixes for a bodyless fetch: no
    preflight is sent, but the browser still blocks the READ without this header.
    """
    r = await client.get("/health", headers={"Origin": DEV_ORIGIN})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == DEV_ORIGIN


async def test_an_unlisted_origin_gets_no_grant(client: AsyncClient) -> None:
    """CORS is an allowlist, not decoration: a foreign origin is not echoed.

    `allow_origins=["*"]` would pass the two tests above while granting every site
    on the internet read access to this API from a victim's browser. Pinning a
    disallowed origin to NO ACAO is what makes those two assertions mean the
    allowlist, rather than a wildcard, is in force.
    """
    r = await client.get("/health", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


async def test_a_configured_origin_is_granted_and_the_default_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FDI_CORS_ORIGINS REPLACES the allowlist, it does not extend it.

    A deployment that serves the UI from https://fdi.example must not keep
    granting localhost:3000 -- an allowlist that only ever grows is not an
    allowlist. Built through create_app() with the env set, which is the only
    way the middleware's origins are decided.
    """
    for key, value in {
        "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
        "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
        "FDI_RESULT_BACKEND": "redis://h:6379/0",
        "FDI_S3_ENDPOINT": "h:9000",
        "FDI_S3_ACCESS_KEY": "ak",
        "FDI_S3_SECRET_KEY": "sk",
        "FDI_CORS_ORIGINS": '["https://fdi.example"]',
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        granted = await c.get("/health", headers={"Origin": "https://fdi.example"})
        revoked = await c.get("/health", headers={"Origin": DEV_ORIGIN})

    assert granted.headers["access-control-allow-origin"] == "https://fdi.example"
    assert "access-control-allow-origin" not in revoked.headers


async def test_an_unconfigured_environment_still_grants_the_dev_origin() -> None:
    """create_app() runs at IMPORT time (`app = create_app()` at the bottom of
    fdi/api/app.py) and in every test whose fixtures clear FDI_* -- so an
    unbuildable Settings must fall back to the historical dev default, not
    raise and fail collection of the whole suite."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health", headers={"Origin": DEV_ORIGIN})
    assert r.headers["access-control-allow-origin"] == DEV_ORIGIN
