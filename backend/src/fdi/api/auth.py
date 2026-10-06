"""The app-level JWT gate (ADR-0002): /auth/* routes + the access-token check.

Three routes own sessions -- login, refresh, logout -- and one dependency
(`require_access_token`) guards everything else; app.py attaches it to the
protected router wholesale, which is what makes "every route except /health,
/ready and /auth/*" a structural fact rather than a per-route memory test.

Token model (ADR-0002):

- ACCESS: HS256 JWT, ~15 min (FDI_AUTH_ACCESS_TTL_SECONDS), carried in
  `Authorization: Bearer`. Stateless on purpose -- a per-request DB read to
  re-check `active` would couple every route to the users table for a window
  (one access TTL) that revocation-by-deactivation is already bounded by.
- REFRESH: opaque 256-bit value in an httpOnly/Secure/SameSite=Lax cookie
  scoped to Path=/auth, remembered server-side as its SHA-256
  (fdi.platform.security). Rotation invalidates the predecessor; logout
  revokes; deactivation is noticed at the next refresh.

Deactivated-Invitee semantics (deliberate, documented): an access token keeps
working until it expires -- at most one access TTL (~15 min) after
deactivation -- and every subsequent login AND refresh refuses the account.
No silent path back in exists.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.api.deps import SessionDep
from fdi.api.schemas import LoginRequest, TokenView
from fdi.platform.config import get_settings
from fdi.platform.db.repository import UserRepository
from fdi.platform.security import (
    BEARER_SCHEME,
    InvalidTokenError,
    create_access_token,
    hash_password,
    hash_refresh_token,
    mint_refresh_token,
    verify_access_token,
    verify_password,
)

auth_router = APIRouter(prefix="/auth")

REFRESH_COOKIE = "fdi_refresh"

# A single argon2 hash of a throwaway value. Presented to verify_password when
# the email matches no account, so an unknown email and a wrong password cost
# the verifier the same ~100ms and take the same code path -- the timing side
# channel that would otherwise let a caller enumerate which emails exist.
# Computed at import: it is deterministic work, and doing it per call would
# measure the hash of the hash on every failed login.
_DUMMY_HASH = hash_password("not-a-real-account")


def _unauthorized(detail: str = "Invalid credentials.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=detail
    )


async def _issue_session(
    *, user_id: str, session: AsyncSession, response: Response
) -> TokenView:
    """Mint an access token + a fresh refresh cookie. Shared by login and
    refresh (rotation), which issue sessions identically."""
    settings = get_settings()
    token_view = TokenView(
        access_token=create_access_token(
            user_id, secret=settings.auth_secret, ttl_seconds=settings.auth_access_ttl_seconds
        ),
        expires_in=settings.auth_access_ttl_seconds,
    )
    raw, token_hash = mint_refresh_token()
    repo = UserRepository(session)
    await repo.store_refresh_token(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.auth_refresh_ttl_seconds),
    )
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=settings.auth_refresh_ttl_seconds,
        # The cookie contract, attribute by attribute (asserted at the HTTP
        # seam in test_auth.py): httpOnly keeps page JS from reading the
        # token; Secure keeps it off plain HTTP; SameSite=Lax survives
        # top-level navigations without opening cross-site POSTs; Path=/auth
        # means the cookie is not even SENT anywhere else.
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
    )
    return token_view


@auth_router.post("/login", response_model=TokenView)
async def login(
    payload: LoginRequest, session: SessionDep, response: Response
) -> TokenView:
    """Exchange email + password for an access token and a refresh cookie."""
    repo = UserRepository(session)
    user = await repo.get_by_email(payload.email)
    if user is None:
        # Equal-work path for unknown emails -- see _DUMMY_HASH.
        verify_password(_DUMMY_HASH, payload.password)
        raise _unauthorized()
    if not verify_password(user.password_hash, payload.password) or not user.active:
        # One message for both: which half failed is not the caller's business.
        raise _unauthorized()
    view = await _issue_session(user_id=user.id, session=session, response=response)
    await session.commit()
    return view


@auth_router.post("/refresh", response_model=TokenView)
async def refresh(request: Request, session: SessionDep, response: Response) -> TokenView:
    """Rotate the refresh cookie into a fresh access token + cookie.

    The presented token dies here, whatever happens next: it is revoked BEFORE
    the new session is minted, so even the failure paths (expired token,
    deactivated account) burn the cookie they were handed.
    """
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise _unauthorized("No refresh token was presented.")
    repo = UserRepository(session)
    row = await repo.find_refresh_token(hash_refresh_token(raw))
    now = datetime.now(UTC)
    if row is None:
        raise _unauthorized("The refresh token is not recognised.")
    if row.revoked_at is not None:
        raise _unauthorized("The refresh token has been revoked.")
    if row.expires_at <= now:
        raise _unauthorized("The refresh token has expired.")
    user = await repo.get(row.user_id)
    if user is None or not user.active:
        # Deactivation semantics (module docstring): the session dies at its
        # next refresh, not retroactively.
        raise _unauthorized()
    # Rotate. Read-then-write under READ COMMITTED, without FOR UPDATE: two
    # concurrent refreshes with one cookie can both see the unrevoked row and
    # both rotate. The window is one request, the attacker already holds the
    # cookie, and the demo has one operator -- locking the row here would buy
    # a guarantee nothing in the threat model asks for.
    await repo.revoke_refresh_token(row.id)
    view = await _issue_session(user_id=user.id, session=session, response=response)
    await session.commit()
    return view


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, session: SessionDep, response: Response) -> None:
    """Revoke the presented refresh token and clear the cookie.

    Idempotent: no cookie, or an unrecognised one, is still a 204 -- "log out
    again" is not an error the caller can act on.
    """
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        repo = UserRepository(session)
        row = await repo.find_refresh_token(hash_refresh_token(raw))
        if row is not None and row.revoked_at is None:
            await repo.revoke_refresh_token(row.id)
            await session.commit()
    response.delete_cookie(REFRESH_COOKIE, path="/auth")


def require_access_token(request: Request) -> str:
    """The gate dependency: the authenticated user id, or a 401.

    Returns the id so routes that need identity (job ownership, per-user
    quotas) can take `Annotated[str, Depends(require_access_token)]` and be
    served by the same decode -- one token, one verification, no second
    parse. WWW-Authenticate rides the 401 per RFC 9110 s11.6.1; the problem
    renderer preserves exception headers, so it survives to the wire.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != BEARER_SCHEME.lower() or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An access token is required.",
            headers={"WWW-Authenticate": BEARER_SCHEME},
        )
    try:
        return verify_access_token(token, secret=get_settings().auth_secret)
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The access token is invalid or has expired.",
            headers={"WWW-Authenticate": BEARER_SCHEME},
        ) from None
