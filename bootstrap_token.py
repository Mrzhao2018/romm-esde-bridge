#!/usr/bin/env python3
"""Create a least-privilege RomM client token for the server-side indexer."""

from __future__ import annotations

import os
from pathlib import Path

from handler.auth import auth_handler
from handler.database import db_client_token_handler, db_user_handler
from models.client_token import ClientToken


TOKEN_NAME = "ES-DE Bridge (server indexer)"
TOKEN_PATH = Path("/romm/config/bridge/server-token")
SCOPES = (
    "me.read roms.read roms.user.read platforms.read assets.read "
    "collections.read firmware.read devices.read"
)


def atomic_private_write(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main() -> None:
    users = list(db_user_handler.get_users())
    admins = [user for user in users if str(user.role) == "admin" and user.enabled]
    if not admins:
        raise SystemExit("No enabled RomM admin user found")

    user = admins[0]
    # A raw client token is intentionally shown only once. If its private file is
    # gone, replace the old database row instead of accumulating stale tokens.
    for token in db_client_token_handler.get_tokens_by_user(user.id):
        if token.name == TOKEN_NAME:
            db_client_token_handler.delete_token(token.id, user.id)

    raw_token = auth_handler.generate_client_token()
    token = ClientToken(
        user_id=user.id,
        name=TOKEN_NAME,
        hashed_token=auth_handler.hash_client_token(raw_token),
        scopes=SCOPES,
        expires_at=None,
    )
    db_client_token_handler.add_token(token)
    atomic_private_write(TOKEN_PATH, raw_token)
    print(f"Created read-only bridge token for RomM user {user.username!r}")


if __name__ == "__main__":
    main()
