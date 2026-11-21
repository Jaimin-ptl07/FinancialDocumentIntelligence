"""The Invitee admin CLI (ADR-0002) -- the ONLY door accounts enter through.

No signup route exists anywhere; this tool is the entire account-creation
surface, and deactivation is its other half. Run wherever the backend's FDI_
environment lives (in Compose: `docker compose exec api fdi-admin ...`; the
console script ships with the package, so the image has it too).

    fdi-admin create-user --email mentor@example.org [--password <pw>]
    fdi-admin deactivate-user --email mentor@example.org
    fdi-admin activate-user --email mentor@example.org
    fdi-admin list-users

Passwords: --password for scripts; without it a 24-character value is
GENERATED and printed once -- the operator pastes it into the invitation and
it is never stored anywhere in the clear (the row keeps only the argon2 hash).

Exit codes: 0 success, 1 an expected operational failure (duplicate email,
unknown account, bad input) with a message on stderr. Anything else is a bug
and exits via traceback.
"""

import argparse
import asyncio
import secrets
import string
import sys
from typing import NoReturn

from sqlalchemy.exc import IntegrityError

from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.db.repository import UserRepository
from fdi.platform.security import hash_password

# The generated-password alphabet: unambiguous characters only -- no 0/O or
# 1/l/I -- because the password's consumer is a human reading an invitation.
_ALPHABET = string.ascii_letters + string.digits
_GENERATED_LENGTH = 24


def generate_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_GENERATED_LENGTH))


async def _create_user(email: str, password: str) -> None:
    engine = make_engine(get_settings().database_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            await UserRepository(session).create(
                email=email, password_hash=hash_password(password)
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _set_active(email: str, *, active: bool) -> None:
    engine = make_engine(get_settings().database_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            repo = UserRepository(session)
            user = await repo.get_by_email(email)
            if user is None:
                _fail(f"No account with email {email!r}.")
            await repo.set_active(user.id, active=active)
            await session.commit()
    finally:
        await engine.dispose()


async def _list_users() -> None:
    engine = make_engine(get_settings().database_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            users = await UserRepository(session).list()
    finally:
        await engine.dispose()
    for u in users:
        # Tab-separated: one line per account, cut-paste friendly.
        print(f"{u.email}\t{'active' if u.active else 'deactivated'}\t{u.created_at:%Y-%m-%d}")


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


async def amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="fdi-admin",
        description="Manage Invitee accounts for the FDI Gated demo (ADR-0002).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="Create an Invitee account.")
    create.add_argument("--email", required=True)
    create.add_argument(
        "--password",
        help="Omit to generate one; it is printed once, never stored.",
    )

    deactivate = sub.add_parser("deactivate-user", help="Refuse an Invitee's logins.")
    deactivate.add_argument("--email", required=True)

    activate = sub.add_parser("activate-user", help="Restore a deactivated Invitee.")
    activate.add_argument("--email", required=True)

    sub.add_parser("list-users", help="List every account and its state.")

    args = parser.parse_args(argv)

    if args.command == "create-user":
        password = args.password or generate_password()
        try:
            await _create_user(args.email, password)
        except IntegrityError:
            _fail(f"An account with email {args.email!r} already exists.")
        if args.password is None:
            print(f"Created {args.email} with generated password: {password}")
            print("Store it now -- it is not recoverable (only its hash is kept).")
        else:
            print(f"Created {args.email}.")
    elif args.command == "deactivate-user":
        await _set_active(args.email, active=False)
        print(f"Deactivated {args.email}. Live access tokens run out within one "
              f"access TTL; every refresh and login is refused immediately.")
    elif args.command == "activate-user":
        await _set_active(args.email, active=True)
        print(f"Activated {args.email}.")
    elif args.command == "list-users":
        await _list_users()


def main(argv: list[str] | None = None) -> None:
    """Console-script shim. The loop-free body lives in `amain` so tests (and
    anything else already inside a loop) can drive it directly."""
    asyncio.run(amain(argv if argv is not None else sys.argv[1:]))
