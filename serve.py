#!/usr/bin/env python3
"""Small read-only HTTP service for generated RomM/ES-DE bridge files."""

from __future__ import annotations

import argparse
import email.utils
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class BridgeHandler(SimpleHTTPRequestHandler):
    server_version = "romm-esde-bridge/3"
    lease_guard = threading.Lock()
    state_leases: dict[tuple[int, int], dict] = {}
    lock_path = re.compile(r"^/api/state-locks/(\d+)(?:/([0-9a-f-]+))?$")

    def _json_response(self, status: int, value: dict) -> None:
        data = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _romm_user_id(self) -> int | None:
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            self._json_response(401, {"error": "Bearer token required"})
            return None
        request = urllib.request.Request(
            os.getenv("ROMM_API_URL", "http://127.0.0.1:8080") + "/api/users/me",
            headers={"Authorization": authorization, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return int(json.load(response)["id"])
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, KeyError):
            self._json_response(401, {"error": "Token rejected by RomM"})
            return None

    def do_POST(self) -> None:  # noqa: N802
        match = self.lock_path.match(self.path)
        if not match or match.group(2):
            self._json_response(404, {"error": "Not found"})
            return
        user_id = self._romm_user_id()
        if user_id is None:
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            payload = json.loads(self.rfile.read(length) or b"{}")
            owner = str(payload["device_id"])
        except (ValueError, KeyError, TypeError):
            self._json_response(400, {"error": "device_id required"})
            return
        rom_id = int(match.group(1))
        now = time.monotonic()
        ttl = max(10, min(int(payload.get("ttl", 45)), 60))
        key = (user_id, rom_id)
        with self.lease_guard:
            current = self.state_leases.get(key)
            if current and current["expires"] > now and current["owner"] != owner:
                self._json_response(
                    409,
                    {"error": "locked", "retry_after": max(1, int(current["expires"] - now))},
                )
                return
            lease_id = str(uuid.uuid4())
            self.state_leases[key] = {
                "owner": owner, "lease_id": lease_id, "expires": now + ttl,
            }
        self._json_response(201, {"lease_id": lease_id, "ttl": ttl})

    def do_DELETE(self) -> None:  # noqa: N802
        match = self.lock_path.match(self.path)
        if not match or not match.group(2):
            self._json_response(404, {"error": "Not found"})
            return
        user_id = self._romm_user_id()
        if user_id is None:
            return
        key = (user_id, int(match.group(1)))
        with self.lease_guard:
            current = self.state_leases.get(key)
            if current and current["lease_id"] == match.group(2):
                self.state_leases.pop(key, None)
        self._json_response(200, {"released": True})

    def send_head(self):  # type: ignore[no-untyped-def]
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            path = path / "index.html"
        if not path.is_file():
            self.send_error(404, "File not found")
            return None
        try:
            stream = path.open("rb")
            metadata = os.fstat(stream.fileno())
        except OSError:
            self.send_error(404, "File not found")
            return None

        etag = f'"{metadata.st_mtime_ns:x}-{metadata.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            stream.close()
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return None

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(metadata.st_size))
        self.send_header("Last-Modified", email.utils.formatdate(metadata.st_mtime, usegmt=True))
        self.send_header("ETag", etag)
        self.end_headers()
        return stream

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def list_directory(self, path: str):  # type: ignore[no-untyped-def]
        self.send_error(404, "Directory listing disabled")
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default=os.getenv("BRIDGE_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("BRIDGE_PORT", "8090")))
    parser.add_argument(
        "--directory",
        default=os.getenv("BRIDGE_OUTPUT_DIR", "/var/lib/romm-esde-bridge"),
    )
    args = parser.parse_args()
    directory = Path(args.directory)
    if not (directory / "health.json").is_file():
        raise SystemExit(f"Bridge export is missing from {directory}")
    handler = lambda *values, **kwargs: BridgeHandler(  # noqa: E731
        *values, directory=str(directory), **kwargs
    )
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"Serving {directory} on {args.bind}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
