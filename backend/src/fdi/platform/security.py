"""Password and token primitives for the app-level JWT gate (ADR-0002).

Lives in `platform`, not `core`: core is pure (no clock, no RNG, no I/O) by the
layered-purity contract, and every function here needs at least one of those --
argon2 salts every hash, the access token stamps `iat`/`exp`, and a refresh
token is nothing but RNG. The module still takes everything it needs as
arguments (no Settings, no DB), so it stays trivially testable and the routes
own the wiring.

Design notes:

- Passwords use argon2id via `argon2-cffi`'s high-level API. passlib was
  rejected: unmaintained since 2020, and its crypt-module probing breaks on
  modern Pythons.
- The access token is a plain HS256 JWT with `sub` = user id. No `email`
  claim: tokens outlive email changes and the API re-reads the row whenever
  it needs the account, so the claim would be a second, staler copy of the
  same fact.
- The refresh token is OPAQUE, not a JWT. It is stored server-side (hashed),
  so rotation and revocation are row operations -- the properties ADR-0002
  requires. Only its SHA-256 is persisted, so a database leak does not leak
  usable cookies.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# WWW-Authenticate/Authorization vocabulary for the gate. A constant, not a
# per-call string, so the challenge and the parser cannot drift apart.
BEARER_SCHEME = "Bearer"

ACCESS_TOKEN_TYPE = "access"

_hasher = PasswordHasher()


class InvalidTokenError(Exception):
    """The presented access token is not a token this app signed and trusts."""


def hash_password(password: str) -> str:
    """Argon2id hash. Salt and parameters are encoded in the string itself."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """False on any mismatch or malformed hash -- never raises to the caller.

    A malformed stored hash is an operational problem, but at the point this
    runs (inside credential verification) the safe answer is a rejection the
    logs can explain, not an exception the request has to translate.
    """
    try:
        _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False
    return True


def create_access_token(user_id: str, *, secret: str, ttl_seconds: int) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "typ": ACCESS_TOKEN_TYPE,
            "iat": now,
            "exp": now + timedelta(seconds=ttl_seconds),
        },
        secret,
        algorithm="HS256",
    )


def verify_access_token(token: str, *, secret: str) -> str:
    """The authenticated user id, or InvalidTokenError.

    `algorithms` is pinned on decode -- the locked-down counterpart of the
    algorithm chosen at encode time. Accepting the token's own `alg` header
    is the classic `alg=none` / algorithm-confusion opening.
    """
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        # jwt.MissingRequiredClaimError, ExpiredSignatureError, DecodeError...
        # are all InvalidTokenError subclasses; one except is the whole point.
        raise InvalidTokenError("token rejected") from exc
    if claims.get("typ") != ACCESS_TOKEN_TYPE:
        raise InvalidTokenError("not an access token")
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise InvalidTokenError("no subject")
    return user_id


def mint_refresh_token() -> tuple[str, str]:
    """(raw, hash): the cookie value, and what may be stored server-side.

    Split so no caller ever has to remember which half persists: `raw` goes to
    the cookie and is never stored; `hash` goes to the database and never
    leaves it. token_urlsafe(32) = 256 bits of entropy, ~43 URL-safe chars.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    """The SHA-256 that may be stored/looked up server-side for a raw token.

    Public (not _-prefixed) because the refresh and logout routes must hash a
    COOKIE-presented value with the exact function that minted the row.

    SHA-256, not argon2, on purpose: the raw token is 256 bits of RNG, not a
    human password, so there is no entropy to stretch -- and /auth/refresh
    hashes on every call, where argon2's ~100ms would be pure latency."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
