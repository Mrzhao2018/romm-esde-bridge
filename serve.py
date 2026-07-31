#!/usr/bin/env python3
"""Small read-only HTTP service for generated RomM/ES-DE bridge files."""

from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import json
import mimetypes
import os
import re
import select
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pc98_sessions import PC98SessionManager, SessionError, UnsupportedFeatureError, terminate_process
from vision_translate import (
    ALLOWED_MIME_TYPES,
    MAX_IMAGE_BYTES,
    TranslationError,
    TranslationProviderError,
    TranslationUnavailableError,
    VisionTranslator,
)


class BridgeHandler(SimpleHTTPRequestHandler):
    server_version = "romm-esde-bridge/4"
    lease_guard = threading.Lock()
    state_leases: dict[tuple[int, int], dict] = {}
    lock_path = re.compile(r"^/api/state-locks/(\d+)(?:/([0-9a-f-]+))?$")
    browser_sessions_path = re.compile(r"^/api/browser/sessions/([0-9a-f]{32})$")
    browser_control_path = re.compile(
        r"^/api/browser/sessions/([0-9a-f]{32})/(pause|resume|reset|eject|insert|save-state|load-state)$"
    )
    browser_socket_path = re.compile(r"^/api/browser/sessions/([0-9a-f]{32})/socket$")
    browser_audio_path = re.compile(r"^/api/browser/sessions/([0-9a-f]{32})/audio$")
    browser_translate_path = re.compile(r"^/api/browser/sessions/([0-9a-f]{32})/translate$")
    browser_scopes = [
        "me.read", "roms.read", "firmware.read", "devices.read", "devices.write",
        "assets.read", "assets.write", "roms.user.read", "roms.user.write",
    ]

    @staticmethod
    def _redact_request_line(value: str) -> str:
        return re.sub(
            r"([?&](?:ticket|access_token)=)[^&\s]*",
            r"\1<redacted>",
            value,
            flags=re.IGNORECASE,
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Player URLs and websocket URLs carry a session capability in the
        # query string. Keep it out of journald and reverse-proxy access logs.
        if args and isinstance(args[0], str):
            args = (self._redact_request_line(args[0]), *args[1:])
        super().log_message(format, *args)

    @property
    def session_manager(self) -> PC98SessionManager:
        manager = getattr(self.server, "session_manager", None)
        if manager is None:
            raise RuntimeError("PC-98 session manager is not configured")
        return manager

    @property
    def vision_translator(self) -> VisionTranslator:
        translator = getattr(self.server, "vision_translator", None)
        if translator is None:
            raise RuntimeError("Vision translator is not configured")
        return translator

    @property
    def romm_api_url(self) -> str:
        return str(getattr(self.server, "romm_api_url", os.getenv("ROMM_API_URL", "http://127.0.0.1:8080"))).rstrip("/")

    @property
    def romm_public_url(self) -> str:
        return str(getattr(self.server, "romm_public_url", os.getenv("ROMM_PUBLIC_URL", self.romm_api_url))).rstrip("/")

    @property
    def bridge_public_url(self) -> str:
        return str(getattr(self.server, "bridge_public_url", os.getenv("BRIDGE_PUBLIC_URL", "http://127.0.0.1:8090"))).rstrip("/")

    def _json_response(self, status: int, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self, limit: int = 32 * 1024) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise SessionError("Invalid Content-Length") from exc
        if length < 0 or length > limit:
            raise SessionError("Request body is too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError) as exc:
            raise SessionError("JSON request body required") from exc
        if not isinstance(value, dict):
            raise SessionError("JSON object required")
        return value

    def _translation_request(self) -> tuple[bytes, str, str]:
        payload = self._body(3 * 1024 * 1024)
        image_data = str(payload.get("image") or "")
        match = re.fullmatch(
            r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)",
            image_data,
            flags=re.IGNORECASE,
        )
        if not match:
            raise SessionError("translation image must be a base64 PNG, JPEG or WebP data URL")
        try:
            image = base64.b64decode(match.group(2), validate=True)
        except (ValueError, TypeError) as exc:
            raise SessionError("translation image is not valid base64") from exc
        mime_type = match.group(1).lower()
        if mime_type not in ALLOWED_MIME_TYPES or not image or len(image) > MAX_IMAGE_BYTES:
            raise SessionError("translation image is too large or uses an unsupported format")
        target_language = str(payload.get("target_language") or "zh-CN")[:40]
        return image, mime_type, target_language

    def _token(self, *, respond: bool = True) -> str | None:
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            if respond:
                self._json_response(401, {"error": "Bearer token required"})
            return None
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            if respond:
                self._json_response(401, {"error": "Bearer token required"})
            return None
        return token

    def _romm_user(self) -> tuple[str, dict[str, Any]] | None:
        token = self._token()
        if token is None:
            return None
        request = urllib.request.Request(
            self.romm_api_url + "/api/users/me",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                user = json.load(response)
                return token, user
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, KeyError, TypeError):
            self._json_response(401, {"error": "Token rejected by RomM"})
            return None

    def _romm_json_proxy(self, path: str, method: str, payload: dict[str, Any] | None = None) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        request = urllib.request.Request(
            self.romm_api_url + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "romm-esde-bridge-browser/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                value = json.load(response)
                self._json_response(response.status, value)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            self._json_response(exc.code, {"error": detail[:500]})
        except (urllib.error.URLError, ValueError) as exc:
            self._json_response(502, {"error": f"RomM device pairing unavailable: {exc}"})

    def _pair_init(self) -> None:
        payload = self._body(16 * 1024)
        identifier = str(payload.get("client_device_identifier") or uuid.uuid4())[:255]
        name = str(payload.get("name") or "RomM PC-98 Browser")[:255]
        request = {
            "client_device_identifier": identifier,
            "name": name,
            "client": "romm-esde-browser",
            "platform": "browser",
            "client_version": "1",
            "requested_scopes": self.browser_scopes,
        }
        self._romm_json_proxy("/api/auth/device/init", "POST", request)

    def _pair_token(self) -> None:
        payload = self._body(4096)
        device_code = str(payload.get("device_code") or "")
        if not device_code or len(device_code) > 128:
            self._json_response(400, {"error": "device_code required"})
            return
        self._romm_json_proxy("/api/auth/device/token", "POST", {"device_code": device_code})

    def _session_error(self, exc: Exception) -> None:
        message = str(exc)
        lowered = message.casefold()
        if isinstance(exc, UnsupportedFeatureError):
            status = 501
        elif isinstance(exc, TranslationUnavailableError):
            status = 503
        elif isinstance(exc, TranslationProviderError):
            status = 502
        elif "ticket" in lowered or "token" in lowered or "bearer" in lowered:
            status = 401
        elif "not found" in lowered:
            status = 404
        elif "ended" in lowered:
            status = 410
        else:
            status = 409 if "最多同时" in message or "locked" in message.lower() else 400
        self._json_response(status, {"error": message})

    def _browser_session_response(self, session: Any, status: int = 200) -> None:
        self._json_response(status, session.public_dict(self.bridge_public_url))

    def _authorized_session(
        self,
        session_id: str,
        *,
        touch: bool = True,
        require_alive: bool = True,
    ) -> Any | None:
        # The ticket is already the capability embedded in player_url. This
        # lets a player opened on another browser operate an existing session
        # without copying that browser's RomM device token.
        ticket = self.headers.get("X-Session-Ticket", "").strip()
        if ticket:
            return self.session_manager.get_by_ticket(
                session_id, ticket, touch=touch, require_alive=require_alive
            )
        identity = self._romm_user()
        if identity is None:
            return None
        _, user = identity
        return self.session_manager.get(
            session_id, int(user["id"]), touch=touch, require_alive=require_alive
        )

    def _proxy_websocket(self, session_id: str, ticket: str) -> None:
        connected = False
        local: socket.socket | None = None
        try:
            key = self.headers.get("Sec-WebSocket-Key", "")
            if not key:
                self._json_response(400, {"error": "Websocket key is missing"})
                return
            target_port = self.session_manager.websocket_target(session_id, ticket)
            local = socket.create_connection(("127.0.0.1", target_port), timeout=10)
            self.session_manager.websocket_connected(session_id, ticket)
            connected = True
        except (OSError, SessionError) as exc:
            if local is not None:
                local.close()
            self._json_response(401 if isinstance(exc, SessionError) else 502, {"error": str(exc)})
            return
        try:
            version = self.headers.get("Sec-WebSocket-Version", "13")
            origin = self.headers.get("Origin")
            request = [
                "GET / HTTP/1.1",
                "Host: 127.0.0.1",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                f"Sec-WebSocket-Version: {version}",
            ]
            if origin:
                request.append(f"Origin: {origin}")
            local.sendall(("\r\n".join(request) + "\r\n\r\n").encode())
            handshake = b""
            while b"\r\n\r\n" not in handshake and len(handshake) < 64 * 1024:
                chunk = local.recv(4096)
                if not chunk:
                    break
                handshake += chunk
            if not handshake.startswith(b"HTTP/1.1 101"):
                self._json_response(502, {"error": "Websocket backend handshake failed"})
                return
            header_end = handshake.find(b"\r\n\r\n")
            if header_end < 0:
                self._json_response(502, {"error": "Websocket backend handshake was incomplete"})
                return
            header_end += 4
            self.connection.sendall(handshake[:header_end])
            remainder = handshake[header_end:]
            if remainder:
                self.connection.sendall(remainder)
            self.close_connection = True
            self.connection.settimeout(None)
            local.settimeout(None)
            while True:
                readable, _, _ = select.select([self.connection, local], [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(64 * 1024)
                    if not data:
                        return
                    (local if source is self.connection else self.connection).sendall(data)
                    self.session_manager.websocket_target(session_id, ticket)
        except (OSError, SessionError):
            return
        finally:
            if connected:
                self.session_manager.websocket_disconnected(session_id, ticket)
            if local is not None:
                local.close()

    @staticmethod
    def _websocket_frame(opcode: int, data: bytes) -> bytes:
        size = len(data)
        if size < 126:
            return bytes((0x80 | (opcode & 0x0F), size)) + data
        if size <= 0xFFFF:
            return bytes((0x80 | (opcode & 0x0F), 126)) + size.to_bytes(2, "big") + data
        return bytes((0x80 | (opcode & 0x0F), 127)) + size.to_bytes(8, "big") + data

    @staticmethod
    def _websocket_binary_frame(data: bytes) -> bytes:
        return BridgeHandler._websocket_frame(2, data)

    @staticmethod
    def _consume_websocket_frames(buffer: bytearray, data: bytes) -> list[tuple[int, bytes]]:
        """Parse browser-to-server frames used by the one-way audio socket."""
        buffer.extend(data)
        frames: list[tuple[int, bytes]] = []
        while len(buffer) >= 2:
            first, second = buffer[0], buffer[1]
            size = second & 0x7F
            offset = 2
            if size == 126:
                if len(buffer) < 4:
                    break
                size = int.from_bytes(buffer[2:4], "big")
                offset = 4
            elif size == 127:
                if len(buffer) < 10:
                    break
                size = int.from_bytes(buffer[2:10], "big")
                offset = 10
            if size > 1024 * 1024:
                raise ValueError("Websocket control frame is too large")
            masked = bool(second & 0x80)
            if masked:
                if len(buffer) < offset + 4:
                    break
                mask = bytes(buffer[offset:offset + 4])
                offset += 4
            else:
                mask = b""
            end = offset + size
            if len(buffer) < end:
                break
            payload = bytes(buffer[offset:end])
            del buffer[:end]
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            frames.append((first & 0x0F, payload))
        return frames

    def _proxy_audio_websocket(self, session_id: str, ticket: str) -> None:
        capture = None
        connected = False
        upgraded = False
        client_buffer = bytearray()
        try:
            key = self.headers.get("Sec-WebSocket-Key", "")
            if not key:
                raise SessionError("Websocket key is missing")
            capture = self.session_manager.audio_capture(session_id, ticket)
            if capture.stdout is None:
                raise SessionError("Audio capture stream is unavailable")
            self.session_manager.websocket_connected(session_id, ticket)
            connected = True
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            self.connection.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            upgraded = True
            self.close_connection = True
            self.connection.settimeout(None)
            while True:
                readable, _, _ = select.select([self.connection, capture.stdout], [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    if source is self.connection:
                        data = self.connection.recv(64 * 1024)
                        if not data:
                            return
                        for opcode, payload in self._consume_websocket_frames(client_buffer, data):
                            if opcode == 0x8:
                                return
                            if opcode == 0x9:
                                self.connection.sendall(self._websocket_frame(0xA, payload))
                        continue
                    data = os.read(capture.stdout.fileno(), 32 * 1024)
                    if not data:
                        return
                    self.connection.sendall(self._websocket_binary_frame(data))
                    self.session_manager.websocket_target(session_id, ticket)
        except (OSError, SessionError, ValueError) as exc:
            if not upgraded:
                if isinstance(exc, SessionError):
                    self._session_error(exc)
                else:
                    self._json_response(502, {"error": str(exc)})
        finally:
            if connected:
                self.session_manager.websocket_disconnected(session_id, ticket)
            if capture is not None:
                terminate_process(capture)

    def _request_path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def do_POST(self) -> None:  # noqa: N802
        path = self._request_path()
        if path == "/api/browser/pair/init":
            try:
                self._pair_init()
            except SessionError as exc:
                self._session_error(exc)
            return
        if path == "/api/browser/pair/token":
            try:
                self._pair_token()
            except SessionError as exc:
                self._session_error(exc)
            return
        if path == "/api/browser/sessions":
            identity = self._romm_user()
            if identity is None:
                return
            token, user = identity
            try:
                payload = self._body()
                session = self.session_manager.create(
                    user_id=int(user["id"]),
                    username=str(user.get("username") or user["id"]),
                    token=token,
                    rom_id=int(payload["rom_id"]),
                    device_id=(str(payload["device_id"]) if payload.get("device_id") else None),
                    disk_file_ids=payload.get("disk_file_ids"),
                )
                self._browser_session_response(session, 201)
            except (KeyError, TypeError, ValueError, SessionError) as exc:
                self._session_error(exc)
            return
        translation_match = self.browser_translate_path.match(path)
        if translation_match:
            try:
                session = self._authorized_session(translation_match.group(1))
                if session is None:
                    return
                image, mime_type, target_language = self._translation_request()
                with session.operation_lock:
                    result = self.vision_translator.translate(image, mime_type, target_language)
                    session.touch()
                self._json_response(200, result)
            except (SessionError, TranslationError, TypeError, ValueError) as exc:
                self._session_error(exc)
            return
        control = self.browser_control_path.match(path)
        if control:
            session_id, action = control.groups()
            try:
                session = self._authorized_session(session_id)
                if session is None:
                    return
                payload = self._body() if self.headers.get("Content-Length") else {}
                if action == "pause":
                    result = self.session_manager.pause(session, True)
                elif action == "resume":
                    result = self.session_manager.pause(session, False)
                elif action == "reset":
                    result = self.session_manager.reset(session)
                elif action == "eject":
                    result = self.session_manager.eject(session)
                elif action == "insert":
                    result = self.session_manager.insert(session, payload.get("slot"))
                elif action == "save-state":
                    result = self.session_manager.save_state(session, payload.get("slot"))
                else:
                    result = self.session_manager.load_state(session, payload.get("slot"))
                self._json_response(200, result)
            except (TypeError, ValueError, SessionError) as exc:
                self._session_error(exc)
            return
        match = self.lock_path.match(path)
        if not match or match.group(2):
            self._json_response(404, {"error": "Not found"})
            return
        identity = self._romm_user()
        if identity is None:
            return
        _, user = identity
        try:
            payload = self._body(4096)
            owner = str(payload["device_id"])
        except (ValueError, KeyError, TypeError):
            self._json_response(400, {"error": "device_id required"})
            return
        rom_id = int(match.group(1))
        now = time.monotonic()
        ttl = max(10, min(int(payload.get("ttl", 45)), 60))
        key = (int(user["id"]), rom_id)
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
        path = self._request_path()
        session_match = self.browser_sessions_path.match(path)
        if session_match:
            try:
                session = self._authorized_session(
                    session_match.group(1), touch=False, require_alive=False
                )
                if session is None:
                    return
                self._json_response(200, self.session_manager.stop_session(session))
            except SessionError as exc:
                self._session_error(exc)
            return
        match = self.lock_path.match(path)
        if not match or not match.group(2):
            self._json_response(404, {"error": "Not found"})
            return
        identity = self._romm_user()
        if identity is None:
            return
        _, user = identity
        key = (int(user["id"]), int(match.group(1)))
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
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-Session-Ticket",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def list_directory(self, path: str):  # type: ignore[no-untyped-def]
        self.send_error(404, "Directory listing disabled")
        return None

    def translate_path(self, path: str) -> str:
        parsed = urllib.parse.urlsplit(path)
        if parsed.path == "/novnc" or parsed.path.startswith("/novnc/"):
            relative = urllib.parse.unquote(parsed.path.removeprefix("/novnc/")).lstrip("/")
            root = Path(os.getenv("BRIDGE_NOVNC_DIR", "/usr/share/novnc")).resolve()
            candidate = (root / relative).resolve()
            if candidate != root and root not in candidate.parents:
                return str(root / "__missing__")
            return str(candidate)
        return super().translate_path(path)

    def do_GET(self) -> None:  # noqa: N802
        path = self._request_path()
        audio_match = self.browser_audio_path.match(path)
        if audio_match and self.headers.get("Upgrade", "").casefold() == "websocket":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            ticket = str((query.get("ticket") or [""])[0])
            self._proxy_audio_websocket(audio_match.group(1), ticket)
            return
        socket_match = self.browser_socket_path.match(path)
        if socket_match and self.headers.get("Upgrade", "").casefold() == "websocket":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            ticket = str((query.get("ticket") or [""])[0])
            self._proxy_websocket(socket_match.group(1), ticket)
            return
        if path == "/api/browser/me":
            identity = self._romm_user()
            if identity is None:
                return
            _, user = identity
            self._json_response(200, user)
            return
        if path == "/api/browser/sessions":
            identity = self._romm_user()
            if identity is None:
                return
            _, user = identity
            self._json_response(200, {"sessions": self.session_manager.list_user(int(user["id"]))})
            return
        session_match = self.browser_sessions_path.match(path)
        if session_match:
            try:
                session = self._authorized_session(session_match.group(1))
                if session is None:
                    return
                self._browser_session_response(session)
            except SessionError as exc:
                self._session_error(exc)
            return
        super().do_GET()


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
    server.romm_api_url = os.getenv("ROMM_API_URL", "http://127.0.0.1:8080")
    server.romm_public_url = os.getenv("ROMM_PUBLIC_URL", server.romm_api_url)
    server.bridge_public_url = os.getenv("BRIDGE_PUBLIC_URL", f"http://127.0.0.1:{args.port}")
    server.vision_translator = VisionTranslator.from_env()
    server.session_manager = PC98SessionManager(
        directory,
        server.romm_api_url,
        server.bridge_public_url,
        session_dir=Path(
            os.getenv(
                "BRIDGE_SESSION_DIR",
                "/tmp/romm-esde-bridge-sessions",
            )
        ),
        max_sessions=int(os.getenv("BRIDGE_PC98_MAX_SESSIONS", "2")),
        idle_timeout=int(os.getenv("BRIDGE_PC98_IDLE_TIMEOUT", str(45 * 60))),
    )
    print(f"Serving {directory} on {args.bind}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.session_manager.shutdown()


if __name__ == "__main__":
    main()
