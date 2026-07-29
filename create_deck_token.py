#!/usr/bin/env python3
"""Create a dedicated RomM client token for the Steam Deck ES-DE bridge."""

from __future__ import annotations

import os
from pathlib import Path

from handler.auth import auth_handler
from handler.database import db_client_token_handler, db_user_handler
from models.client_token import ClientToken


TOKEN_NAME = "Steam Deck ES-DE"
TOKEN_PATH = Path("/romm/config/bridge/deck-token")
SCOPES = (
    "me.read roms.read roms.user.read roms.user.write platforms.read "
    "assets.read assets.write devices.read devices.write firmware.read "
    "collections.read collections.write"
)


def private_write(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    admins = [
        user
        for user in db_user_handler.get_users()
        if str(user.role) == "admin" and user.enabled
    ]
    if not admins:
        raise SystemExit("No enabled RomM admin user found")
    user = admins[0]

    for token in db_client_token_handler.get_tokens_by_user(user.id):
        if token.name == TOKEN_NAME:
            db_client_token_handler.delete_token(
                token_id=token.id,
                user_id=user.id,
            )

    raw_token = auth_handler.generate_client_token()
    db_client_token_handler.add_token(
        ClientToken(
            user_id=user.id,
            name=TOKEN_NAME,
            hashed_token=auth_handler.hash_client_token(raw_token),
            scopes=SCOPES,
            expires_at=None,
        )
    )
    private_write(TOKEN_PATH, raw_token)
    print(f"Created {TOKEN_NAME!r} token for {user.username!r}")


if __name__ == "__main__":
    main()
