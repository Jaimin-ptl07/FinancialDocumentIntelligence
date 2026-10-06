"""T3: the app-level JWT gate (ADR-0002), observed only at the HTTP seam.

Per the productionization spec's testing decisions there are no dedicated tests
for the auth dependency or token internals: everything assertable here is
asserted through requests — status codes, bodies, cookies. The backing store is
the real testcontainer Postgres with migrations applied, exactly like the other
route files.

Unlike those files' `make_client`s, this one never authenticates its client:
most of these tests are ABOUT the gate, so the unauthenticated view is the
default and logged-in clients are built explicitly per test.
"""

import re
import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"

# 32+ bytes: pyjwt warns on short HS256 keys (RFC 7518 s3.2), and the warning
# is correct -- production secrets are generated at least this long, so the
# test environment should not normalize anything shorter.
SIGNING_SECRET = "test-signing-secret-0123456789abcdef0123456789abcdef"


@pytest.fixture
def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    """Same wiring as test_api.py's factory, minus any automatic login."""

    @asynccontextmanager
    async def _make(**overrides: str):
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
            "FDI_AUTH_SECRET": SIGNING_SECRET,
        }
        env.update(overrides)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=True)
            # https on purpose: the refresh cookie is Secure, and the stdlib
            # cookie jar (httpx uses it) refuses to SEND a Secure cookie over
            # an insecure origin -- exactly like a browser. ASGITransport does
            # not open a socket, so the scheme is free; declaring https makes
            # the jar's semantics the real ones instead of working around them
            # with hand-built Cookie headers.
            async with AsyncClient(transport=transport, base_url="https://test") as client:
                yield client

    return _make


@pytest.fixture
async def client(make_client):
    """A ready unauthenticated client, for the many tests that need no setup."""
    async with make_client() as c:
        yield c


@pytest.fixture
def session_factory(migrated_engine):
    from fdi.platform.db.engine import make_session_factory

    return make_session_factory(migrated_engine)


@pytest.fixture
def cli_env(postgres_url, _alembic_applied, monkeypatch):
    """The env the admin CLI's OWN Settings build needs: the real container
    Postgres plus the inert stubs, exactly as stub_fdi_env does for import
    time -- but pointing at the database the app under test also reads."""
    monkeypatch.setenv("FDI_DATABASE_URL", postgres_url)
    for key, value in {
        "FDI_BROKER_URL": "amqp://guest:guest@localhost:5672/%2F",
        "FDI_RESULT_BACKEND": "redis://localhost:6379/0",
        "FDI_S3_ENDPOINT": "localhost:9000",
        "FDI_S3_ACCESS_KEY": "minioadmin",
        "FDI_S3_SECRET_KEY": "minioadmin",
        "FDI_AUTH_SECRET": "test-signing-secret-0123456789abcdef0123456789abcdef",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_invitee(
    session_factory, email: str, password: str = PASSWORD, *, active: bool = True
) -> None:
    """Seed one Invitee the way the admin CLI will: hash, then insert.

    The CLI (a later slice) calls this same repository path, so seeding here is
    not reaching around the app -- it is the one writer that exists by design,
    used before the CLI's argparse surface is built.
    """
    from fdi.platform.db.repository import UserRepository
    from fdi.platform.security import hash_password

    async with session_factory() as s:
        await UserRepository(s).create(
            email=email, password_hash=hash_password(password), active=active
        )
        await s.commit()


# --------------------------------------------------------------------------
# POST /auth/login
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# The gate: everything except /health, /ready and /auth/* requires a token.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("why", "headers"),
    [
        ("no Authorization header at all", {}),
        ("a garbage bearer token", {"Authorization": "Bearer not-a-jwt"}),
        ("the wrong scheme", {"Authorization": "Basic dXNlcjpwYXNz"}),
    ],
)
async def test_protected_route_refuses_every_tokenless_variant(
    make_client, _alembic_applied, why: str, headers: dict[str, str]
) -> None:
    """The 401 contract: problem+json (like every error here) + the RFC 9110
    challenge header naming the scheme that WOULD be accepted."""
    async with make_client() as client:
        r = await client.post("/ping-job", headers=headers)
    assert r.status_code == 401, why
    assert r.headers["content-type"].startswith("application/problem+json"), why
    assert r.headers["www-authenticate"] == "Bearer", why


async def test_health_and_ready_stay_open(make_client, _alembic_applied) -> None:
    """Infra probes must never need a token -- that is user story 6, and the
    whole reason /health and /ready are a separate router in app.py."""
    async with make_client() as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
    assert health.status_code == 200
    assert ready.status_code == 200


async def test_a_valid_token_unlocks_a_protected_route(
    client: AsyncClient, session_factory, _alembic_applied
) -> None:
    """The full first hop of the lifecycle: 401 -> login -> 200/202."""
    email = f"invitee-{uuid.uuid4().hex}@example.test"
    await _create_invitee(session_factory, email)
    r = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    token = r.json()["access_token"]
    ok = await client.post("/ping-job", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 202, ok.text


async def test_an_expired_token_is_refused(
    make_client, session_factory, _alembic_applied
) -> None:
    """TTLs are settings, so expiry is observable WITHOUT sleeping: a negative
    access TTL mints a token that was born expired. If the verifier trusted
    only the signature and not the exp claim, this is the test that says so."""
    await _create_invitee(session_factory, "expired-token@example.test")
    async with make_client(FDI_AUTH_ACCESS_TTL_SECONDS="-1") as client:
        login = await client.post(
            "/auth/login",
            json={"email": "expired-token@example.test", "password": PASSWORD},
        )
        assert login.status_code == 200
        r = await client.post(
            "/ping-job",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
    assert r.status_code == 401


async def test_login_returns_access_token_and_sets_refresh_cookie(
    client: AsyncClient, session_factory, _alembic_applied
) -> None:
    email = f"invitee-{uuid.uuid4().hex}@example.test"
    await _create_invitee(session_factory, email)
    r = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] == 900  # the configured ~15-minute access TTL

    cookie = r.headers["set-cookie"]
    assert "fdi_refresh=" in cookie
    # ADR-0002's cookie contract, attribute by attribute: httpOnly keeps the
    # token away from page JavaScript, Secure keeps it off plain HTTP,
    # SameSite=Lax lets the silent-refresh POST ride along on top-level
    # navigation without opening the door to cross-site POSTs.
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    # Path=/auth: the refresh cookie is only ever needed by the auth routes,
    # so it is not even sent on any other request.
    assert "Path=/auth" in cookie


async def test_login_rejects_a_wrong_password(
    client: AsyncClient, session_factory, _alembic_applied
) -> None:
    email = f"invitee-{uuid.uuid4().hex}@example.test"
    await _create_invitee(session_factory, email)
    r = await client.post("/auth/login", json={"email": email, "password": "wrong"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_login_rejects_an_unknown_email_without_revealing_it(
    client: AsyncClient, _alembic_applied
) -> None:
    known = f"invitee-{uuid.uuid4().hex}@example.test"
    r = await client.post(
        "/auth/login", json={"email": f"nobody-{uuid.uuid4().hex}@example.test",
                             "password": "wrong"}
    )
    assert r.status_code == 401
    # No account enumeration: the wrong-email and wrong-password answers must
    # be indistinguishable, so both carry the same detail string and neither
    # hints at which half of the pair was wrong.
    assert "email" not in r.json()["detail"].lower()
    _ = known


async def test_login_rejects_a_deactivated_invitee(
    client: AsyncClient, session_factory, _alembic_applied
) -> None:
    email = f"invitee-{uuid.uuid4().hex}@example.test"
    await _create_invitee(session_factory, email, active=False)
    r = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# POST /auth/refresh + POST /auth/logout
# --------------------------------------------------------------------------


async def _login(client: AsyncClient, email: str, password: str = PASSWORD):
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r


async def test_refresh_rotates_and_invalidates_its_predecessor(
    client: AsyncClient, session_factory, _alembic_applied
) -> None:
    """The rotation contract (ADR-0002): each use of a refresh token mints a
    new one and kills the presented one. The replay assertions are the point --
    a rotation that left the old cookie working would be a token *duplication*
    scheme wearing rotation's name."""
    email = f"invitee-{uuid.uuid4().hex}@example.test"
    await _create_invitee(session_factory, email)
    await _login(client, email)
    first = client.cookies.get("fdi_refresh")
    assert first

    refreshed = await client.post("/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["token_type"] == "bearer"
    second = client.cookies.get("fdi_refresh")
    assert second and second != first

    # The rotated-in access token is real.
    ok = await client.post(
        "/ping-job", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    )
    assert ok.status_code == 202

    # Replay of the rotated-OUT cookie: refused, and the live session is
    # untouched by that refusal. Sent as an explicit Cookie header -- the jar
    # cannot be trusted to REPLACE an httpOnly cookie's domain-spec variant
    # (set() adds a sibling entry rather than replacing, and the request then
    # carries both values), and a replay test must present exactly one value.
    replay = await client.post(
        "/auth/refresh", headers={"Cookie": f"fdi_refresh={first}"}
    )
    assert replay.status_code == 401
    still_live = await client.post(
        "/auth/refresh", headers={"Cookie": f"fdi_refresh={second}"}
    )
    assert still_live.status_code == 200


async def test_logout_revokes_the_refresh_token(
    client: AsyncClient, session_factory, _alembic_applied
) -> None:
    """User story 9: a stolen cookie has bounded value -- logout kills it."""
    email = f"invitee-{uuid.uuid4().hex}@example.test"
    await _create_invitee(session_factory, email)
    await _login(client, email)
    cookie = client.cookies.get("fdi_refresh")

    out = await client.post("/auth/logout")
    assert out.status_code == 204
    # The cookie is cleared on the way out, so even a browser that ignores
    # state stops presenting it.
    cleared = out.headers["set-cookie"]
    assert "fdi_refresh=" in cleared and ("Max-Age=0" in cleared or "max-age=0" in cleared)

    replay = await client.post("/auth/refresh", headers={"Cookie": f"fdi_refresh={cookie}"})
    assert replay.status_code == 401


async def test_logout_without_a_cookie_is_still_204(client: AsyncClient, _alembic_applied) -> None:
    """Idempotent by design: a second logout (or a logged-out tab's) is not
    an error condition worth a 401."""
    assert (await client.post("/auth/logout")).status_code == 204


async def test_an_expired_refresh_token_is_refused(
    make_client, session_factory, _alembic_applied
) -> None:
    """Same no-sleep trick as the access-token expiry test: a negative refresh
    TTL stores a refresh row that was born expired."""
    email = "expired-refresh@example.test"
    await _create_invitee(session_factory, email)
    async with make_client(FDI_AUTH_REFRESH_TTL_SECONDS="-1") as client:
        await _login(client, email)
        assert (await client.post("/auth/refresh")).status_code == 401


async def test_a_deactivated_invitee_cannot_refresh(
    client: AsyncClient, session_factory, _alembic_applied
) -> None:
    """The documented deactivation semantics, pinned at the seam where they
    bite: live access tokens run out their clock (<= one TTL), but the NEXT
    refresh refuses the account -- so a session cannot outlive deactivation
    by more than one access-token window."""
    from fdi.platform.db.repository import UserRepository

    email = f"invitee-{uuid.uuid4().hex}@example.test"
    await _create_invitee(session_factory, email)
    await _login(client, email)

    async with session_factory() as s:
        user = await UserRepository(s).get_by_email(email)
        assert user is not None
        await UserRepository(s).set_active(user.id, active=False)
        await s.commit()

    assert (await client.post("/auth/refresh")).status_code == 401


# --------------------------------------------------------------------------
# The admin CLI -- the only door accounts enter through. Driven through its
# real entry point (main + argv), then proven at the HTTP seam by logging in.
# --------------------------------------------------------------------------


async def test_admin_cli_creates_an_invitee_who_can_log_in(
    client: AsyncClient, session_factory, _alembic_applied
) -> None:
    """create-user -> an argon2 hash in the row (never the password), and a
    working login. The whole invitation flow in one test, because the parts
    are worthless separately: a hash nobody can log in with proves nothing,
    and a login proves nothing about what was stored."""
    from fdi.platform.admin_cli import amain
    from fdi.platform.db.repository import UserRepository

    email = f"cli-{uuid.uuid4().hex}@example.test"
    await amain(["create-user", "--email", email, "--password", "chosen-by-the-operator"])

    async with session_factory() as s:
        user = await UserRepository(s).get_by_email(email)
        assert user is not None
        assert user.active is True
        assert user.password_hash.startswith("$argon2")
        assert "chosen-by-the-operator" not in user.password_hash

    r = await client.post(
        "/auth/login", json={"email": email, "password": "chosen-by-the-operator"}
    )
    assert r.status_code == 200


async def test_admin_cli_generates_a_password_when_none_is_given(
    client: AsyncClient, capsys, _alembic_applied
) -> None:
    """The operator path that will actually be used: invite someone without
    thinking up a password. The generated value is printed exactly once, to
    be pasted into the invite -- so it must be recoverable from stdout and
    sufficient for a login."""
    from fdi.platform.admin_cli import amain

    email = f"genpw-{uuid.uuid4().hex}@example.test"
    await amain(["create-user", "--email", email])
    out = capsys.readouterr().out
    # The password is on the "generated password: <value>" line; the lines
    # after it are instructions. Parsed, not positionally split, so adding a
    # sentence to the output cannot change what this reads.
    match = re.search(r"generated password: (\S+)", out)
    assert match is not None, out
    password = match.group(1)
    assert len(password) >= 16

    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200


async def test_admin_cli_refuses_a_duplicate_email(cli_env, _alembic_applied) -> None:
    from fdi.platform.admin_cli import amain

    email = f"dup-{uuid.uuid4().hex}@example.test"
    await amain(["create-user", "--email", email, "--password", "first"])
    with pytest.raises(SystemExit) as dup:
        await amain(["create-user", "--email", email, "--password", "second"])
    assert dup.value.code == 1


async def test_admin_cli_deactivates_an_invitee(
    client: AsyncClient, _alembic_applied
) -> None:
    """User story 8: access is revocable. Deactivation + the login refusal,
    one flow."""
    from fdi.platform.admin_cli import amain

    email = f"deact-{uuid.uuid4().hex}@example.test"
    await amain(["create-user", "--email", email, "--password", PASSWORD])
    assert (
        await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    ).status_code == 200

    await amain(["deactivate-user", "--email", email])
    assert (
        await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    ).status_code == 401


async def test_admin_cli_deactivate_of_an_unknown_email_fails_loudly(
    cli_env, _alembic_applied,
) -> None:
    from fdi.platform.admin_cli import amain

    with pytest.raises(SystemExit) as missing:
        await amain(["deactivate-user", "--email", f"nobody-{uuid.uuid4().hex}@example.test"])
    assert missing.value.code == 1


# --------------------------------------------------------------------------
# No signup exists anywhere.
# --------------------------------------------------------------------------


async def test_no_signup_endpoint_exists(client: AsyncClient, _alembic_applied) -> None:
    """Accounts enter through the admin CLI only (ADR-0002). This is a
    negative-space test: it pins the ABSENCE of the routes a future PR might
    absent-mindedly add, on the paths a caller would actually try."""
    for path in ("/auth/signup", "/auth/register", "/auth/users", "/users", "/signup"):
        r = await client.post(path, json={"email": "someone@example.test", "password": "x"})
        assert r.status_code == 404, path
