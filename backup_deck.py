#!/usr/bin/env python3
"""Create a versioned, hash-indexed backup of selected Steam Deck config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


DEFAULT_DESTINATION = Path("/var/lib/romm-esde-bridge/device-backups/steamdeck")
DECK_HOME = Path("/home/deck")
SOURCES = (
    "ES-DE/settings",
    "ES-DE/custom_systems",
    "ES-DE/gamelists",
    "ES-DE/collections",
    ".var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg",
    ".var/app/org.libretro.RetroArch/config/retroarch/config",
    "Emulation/bios/np2kai/np2kai.cfg",
    ".config/romm-esde",
    ".local/share/romm-esde/backup/index.sqlite3",
    ".local/share/romm-esde/client.py",
)


def remote_exists(host: str, relative: str) -> bool:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "test", "-e", str(DECK_HOME / relative)],
        check=False,
    )
    return result.returncode == 0


def copy_source(host: str, relative: str, destination: Path) -> None:
    source = f"{host}:{DECK_HOME}/./{relative}"
    subprocess.run(
        [
            "rsync",
            "-a",
            "--relative",
            "--protect-args",
            "-e",
            "ssh",
            source,
            f"{destination}/",
        ],
        check=True,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.name == "manifest.json":
            continue
        metadata = path.lstat()
        item: dict[str, object] = {
            "path": str(path.relative_to(root)),
            "mode": stat.filemode(metadata.st_mode),
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
        }
        if path.is_symlink():
            item["type"] = "symlink"
            item["target"] = os.readlink(path)
        elif path.is_file():
            item["type"] = "file"
            item["sha256"] = sha256_file(path)
        elif path.is_dir():
            item["type"] = "directory"
        else:
            item["type"] = "other"
        entries.append(item)
    return entries


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="steamdeck")
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--layout", choices=("flat", "history"), default="flat")
    args = parser.parse_args()

    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    parent = args.destination / "history" if args.layout == "history" else args.destination
    snapshot = parent / f"{args.label}-{timestamp}"
    snapshot.mkdir(mode=0o700, parents=True, exist_ok=False)

    if args.layout == "history":
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", args.host, "/home/deck/.local/bin/romm-esde-client", "backup-prep"],
            check=True,
        )

    copied: list[str] = []
    missing: list[str] = []
    for relative in SOURCES:
        if not remote_exists(args.host, relative):
            missing.append(relative)
            continue
        copy_source(args.host, relative, snapshot)
        copied.append(relative)

    entries = inventory(snapshot)
    manifest = {
        "schema": "romm-esde-deck-backup-v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "host": args.host,
        "deck_home": str(DECK_HOME),
        "copied_sources": copied,
        "missing_optional_sources": missing,
        "entry_count": len(entries),
        "entries": entries,
    }
    atomic_json(snapshot / "manifest.json", manifest)
    if args.layout == "history":
        current = args.destination / "current"
        temporary = args.destination / f".current-{os.getpid()}"
        temporary.symlink_to(snapshot.relative_to(args.destination), target_is_directory=True)
        os.replace(temporary, current)
    print(snapshot)
    print(f"entries={len(entries)} copied={len(copied)} missing_optional={len(missing)}")


if __name__ == "__main__":
    main()
