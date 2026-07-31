#!/usr/bin/env python3
"""Publish a deterministic Linux client bootstrap bundle into bridge output."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(data)
        temporary = Path(stream.name)
    temporary.chmod(mode)
    temporary.replace(path)


def publish(output: Path, server_url: str, bridge_url: str) -> None:
    target = output / "bootstrap"
    defaults = json.dumps({
        "server_url": server_url.rstrip("/"),
        "bridge_url": bridge_url.rstrip("/"),
        "client_version": "5",
    }, ensure_ascii=False, indent=2).encode() + b"\n"
    files = {
        "romm-esde-linux/installer.py": (ROOT / "bootstrap/installer.py").read_bytes(),
        "romm-esde-linux/deck_client.py": (ROOT / "deck_client.py").read_bytes(),
        "romm-esde-linux/defaults.json": defaults,
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".py") else 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    payload = archive.getvalue()
    bundle = target / "romm-esde-linux.tar.gz"
    atomic_write(bundle, payload)
    digest = hashlib.sha256(payload).hexdigest()
    atomic_write(bundle.with_suffix(bundle.suffix + ".sha256"), f"{digest}  {bundle.name}\n".encode())
    launcher = (ROOT / "bootstrap/install.sh").read_text().replace("@@BRIDGE_URL@@", bridge_url.rstrip("/"))
    atomic_write(target / "install.sh", launcher.encode(), 0o755)
    windows_archive = io.BytesIO()
    with zipfile.ZipFile(windows_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive_zip:
        windows_files = {
            # Windows PowerShell 5.1 interprets BOM-less scripts using the
            # active ANSI code page. Include a UTF-8 BOM so both 5.1 and 7.x
            # parse the Chinese interactive text identically.
            "romm-esde-windows/installer.ps1": (
                b"\xef\xbb\xbf" + (ROOT / "bootstrap/installer-windows.ps1").read_bytes()
            ),
            "romm-esde-windows/deck_client.py": (ROOT / "deck_client.py").read_bytes(),
            "romm-esde-windows/defaults.json": defaults,
        }
        for name, data in sorted(windows_files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive_zip.writestr(info, data)
    windows_payload = windows_archive.getvalue()
    windows_bundle = target / "romm-esde-windows.zip"
    atomic_write(windows_bundle, windows_payload)
    windows_digest = hashlib.sha256(windows_payload).hexdigest()
    atomic_write(
        windows_bundle.with_suffix(windows_bundle.suffix + ".sha256"),
        f"{windows_digest}  {windows_bundle.name}\n".encode(),
    )
    powershell = (
        (ROOT / "bootstrap/install.ps1").read_text()
        .replace("@@BRIDGE_URL@@", bridge_url.rstrip("/"))
    )
    atomic_write(target / "install.ps1", powershell.encode())
    ssh_public_key = ""
    ssh_public_key_file = os.getenv("BOOTSTRAP_SSH_PUBLIC_KEY_FILE", "")
    if ssh_public_key_file:
        ssh_public_key = Path(ssh_public_key_file).read_text(encoding="utf-8").strip()
    ssh_helper = (
        (ROOT / "bootstrap/enable-ssh.ps1").read_text()
        .replace("@@BRIDGE_URL@@", bridge_url.rstrip("/"))
        .replace("@@SSH_PUBLIC_KEY@@", ssh_public_key.replace("'", "''"))
    )
    atomic_write(target / "enable-ssh.ps1", ssh_helper.encode())
    atomic_write(target / "release.json", json.dumps({
        "version": "5",
        "packages": {
            "linux": {"sha256": digest},
            "windows-x64": {"sha256": windows_digest},
        },
        "install_commands": {
            "linux": f"curl -fsSL {bridge_url.rstrip('/')}/bootstrap/install.sh | bash",
            "windows": f"irm {bridge_url.rstrip('/')}/bootstrap/install.ps1 | iex",
        },
    }, ensure_ascii=False, indent=2).encode() + b"\n")

    project_page = (
        (ROOT / "web/project.html").read_text(encoding="utf-8")
        .replace("@@ROMM_URL@@", server_url.rstrip("/"))
        .replace("@@BRIDGE_URL@@", bridge_url.rstrip("/"))
    )
    atomic_write(output / "project/index.html", project_page.encode("utf-8"))
    pc98_page = (
        (ROOT / "web/pc98.html").read_text(encoding="utf-8")
        .replace("@@ROMM_URL@@", server_url.rstrip("/"))
        .replace("@@BRIDGE_URL@@", bridge_url.rstrip("/"))
    )
    atomic_write(output / "pc98/index.html", pc98_page.encode("utf-8"))
    player_page = (
        (ROOT / "web/pc98-player.html").read_text(encoding="utf-8")
        .replace("@@ROMM_URL@@", server_url.rstrip("/"))
        .replace("@@BRIDGE_URL@@", bridge_url.rstrip("/"))
    )
    atomic_write(output / "pc98/player.html", player_page.encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="/var/lib/romm-esde-bridge")
    parser.add_argument("--server-url", default="http://127.0.0.1:8080")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8090")
    args = parser.parse_args()
    publish(Path(args.output_dir), args.server_url, args.bridge_url)


if __name__ == "__main__":
    main()
