"""Agora CMS command-line entry point.

Currently exposes a single administrative command:

    python -m cms reset-password <email> [--password PW | --generate]

This is the **offline fallback** for the forgot-password flow (issue #231):
when SMTP isn't configured, or the *only* admin is locked out and can't reach
the web UI to invite/reset anyone, an operator with shell access to the
container/host can set a new password directly. It bypasses the email round
trip entirely — it writes the new ``password_hash`` straight to the row.

Run it from inside the CMS container (where ``DATABASE_URL`` is set), e.g.:

    docker exec -it agora-cms-cms-1 python -m cms reset-password admin@example.com

If neither ``--password`` nor ``--generate`` is given, you'll be prompted to
type the new password twice (hidden input).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys

from sqlalchemy import select

from cms.auth import get_settings, hash_password
from cms.database import init_db, get_session_factory, wait_for_db, dispose_db
from cms.models.user import User


def _resolve_new_password(args: argparse.Namespace) -> str:
    """Return the new plaintext password from args or an interactive prompt."""
    if args.password is not None:
        if len(args.password) < 6:
            print("error: password must be at least 6 characters", file=sys.stderr)
            raise SystemExit(2)
        return args.password
    if args.generate:
        # URL-safe, ~24 chars of entropy — printed once for the operator to relay.
        return secrets.token_urlsafe(18)
    # Interactive: prompt twice, hidden.
    first = getpass.getpass("New password: ")
    if len(first) < 6:
        print("error: password must be at least 6 characters", file=sys.stderr)
        raise SystemExit(2)
    second = getpass.getpass("Confirm new password: ")
    if first != second:
        print("error: passwords do not match", file=sys.stderr)
        raise SystemExit(2)
    return first


async def _reset_password(email: str, new_password: str) -> int:
    """Set ``email``'s password to ``new_password``. Returns a process exit code."""
    init_db(get_settings())
    await wait_for_db()
    try:
        factory = get_session_factory()
        async with factory() as db:
            user = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if user is None:
                print(f"error: no user found with email {email!r}", file=sys.stderr)
                return 1
            user.password_hash = hash_password(new_password)
            # Clear any pending one-time tokens / forced change so the account
            # is fully re-credentialed and immediately usable.
            user.must_change_password = False
            user.reset_token = None
            user.reset_token_created_at = None
            user.setup_token = None
            user.setup_token_created_at = None
            await db.commit()
            print(f"Password reset for {email} (active={user.is_active}).")
            return 0
    finally:
        await dispose_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cms")
    sub = parser.add_subparsers(dest="command", required=True)

    rp = sub.add_parser(
        "reset-password",
        help="Set a user's password directly (offline forgot-password fallback).",
    )
    rp.add_argument("email", help="Email address of the account to reset.")
    grp = rp.add_mutually_exclusive_group()
    grp.add_argument(
        "--password", help="New password (≥6 chars). Omit to be prompted interactively."
    )
    grp.add_argument(
        "--generate",
        action="store_true",
        help="Generate a random password and print it.",
    )

    args = parser.parse_args(argv)

    if args.command == "reset-password":
        new_password = _resolve_new_password(args)
        code = asyncio.run(_reset_password(args.email, new_password))
        if code == 0 and args.generate:
            print(f"Generated password: {new_password}")
        return code

    parser.error(f"unknown command {args.command!r}")
    return 2  # unreachable; parser.error raises SystemExit


if __name__ == "__main__":
    raise SystemExit(main())
