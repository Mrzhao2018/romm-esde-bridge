#!/usr/bin/env python3
"""Bridge-owned PC-98 browser sessions.

The browser never receives a ROM path or a VNC port.  A session downloads the
selected files with the requesting user's RomM token, starts an isolated
NP2Kai/RetroArch process in Xvfb, and exposes its localhost VNC connection
through the Bridge's authenticated websocket route.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Any, BinaryIO


SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_PLATFORM = "pc-9800-series"
DEFAULT_CORE = "/opt/romm-esde-bridge/pc98/cores/np2kai_libretro.so"
DEFAULT_RETROARCH = "/usr/bin/retroarch"
DEFAULT_XVFB = "/usr/bin/Xvfb"
DEFAULT_X11VNC = "/usr/bin/x11vnc"
DEFAULT_WEBSOCKIFY = "/usr/bin/websockify"
DEFAULT_FFMPEG = "/usr/bin/ffmpeg"
DEFAULT_PW_DUMP = "/usr/bin/pw-dump"
DEFAULT_PW_RECORD = "/usr/bin/pw-record"
DEFAULT_NOVNC = "/usr/share/novnc"
MAX_JSON_BYTES = 32 * 1024
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
MAX_STATE_BYTES = 256 * 1024 * 1024
CORE_STATE_DIRECTORY = "Neko Project II kai"
NP2KAI_DISK_EXTENSIONS = {
    ".d88", ".88d", ".d98", ".98d", ".fdi", ".xdf", ".hdm", ".dup",
    ".2hd", ".tfd", ".nfd", ".hd4", ".hd5", ".hd9", ".fdd", ".h01",
    ".hdb", ".ddb", ".dd6", ".dcp", ".dcu", ".flp", ".img", ".ima",
    ".bin", ".fim", ".thd", ".nhd", ".hdi", ".hdd", ".vhd", ".slh", ".hdn",
}
FIRMWARE_ALIASES = {
    "font.rom": "FONT.ROM",
    "font.bmp": "font.bmp",
    "2608_bd.wav": "2608_BD.WAV",
    "2608_sd.wav": "2608_SD.WAV",
    "2608_top.wav": "2608_TOP.WAV",
    "2608_hh.wav": "2608_HH.WAV",
    "2608_tom.wav": "2608_TOM.WAV",
    "2608_rim.wav": "2608_RIM.WAV",
}
NP2KAI_CORE_OPTIONS = {
    "np2kai_model": "PC-9801VM",
    "np2kai_clk_base": "2.4576 MHz",
    "np2kai_clk_mult": "4",
    "np2kai_cpu_feature": "Intel 80386",
    "np2kai_ExMemory": "3",
    "np2kai_FastMC": "ON",
    "np2kai_gdc": "uPD7220",
    "np2kai_PEGC": "ON",
    "np2kai_skipline": "Full 255 lines",
    "np2kai_realpal": "OFF",
    "np2kai_vf1": "OFF",
    "np2kai_SNDboard": "PC9801-86",
    "np2kai_118ROM": "ON",
    "np2kai_usefmgen": "fmgen",
    "np2kai_volume_F": "64",
    "np2kai_volume_S": "64",
    "np2kai_volume_A": "64",
    "np2kai_volume_P": "64",
    "np2kai_volume_R": "64",
    "np2kai_volume_C": "128",
    "np2kai_Seek_Snd": "OFF",
    "np2kai_Seek_Vol": "0",
    "np2kai_inputmouse": "ON",
    "np2kai_stick2mouse": "R-stick",
    "np2kai_stick2mouse_shift": "R1",
    "np2kai_joymode": "Arrows 3button",
    "np2kai_joynp2menu": "Select",
    "np2kai_keyboard": "Ja",
    "np2kai_keyrepeat": "OFF",
    "np2kai_uselasthddmount": "OFF",
    "np2kai_xroll": "ON",
}


class SessionError(RuntimeError):
    """A user-facing session error which the HTTP layer maps to 4xx."""


class UnsupportedFeatureError(SessionError):
    """The installed emulator core cannot provide a requested feature."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def safe_file_name(value: str) -> str:
    if any(char in value for char in ('"', "\r", "\n", "\x00")):
        raise SessionError("RomM returned a file name unsupported by the PC-98 command format")
    name = Path(value).name
    if not name or name in {".", ".."} or name != value.replace("\\", "/").split("/")[-1]:
        raise SessionError("RomM returned an unsafe file name")
    return name


def terminate_process(process: subprocess.Popen[Any] | None, timeout: float = 4) -> None:
    if process is None or process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        if os.name == "posix" and process.pid:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix" and process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_tcp(port: int, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.connect(("127.0.0.1", port))
            except OSError:
                time.sleep(0.05)
            else:
                return
    raise SessionError(f"Local session service did not start on port {port}")


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def descriptor_from_file(item: dict[str, Any], slot: int, role: str) -> dict[str, Any]:
    return {
        "slot": slot,
        "role": role,
        "file_id": int(item["id"]),
        "file_name": str(item["file_name"]),
        "file_size_bytes": item.get("file_size_bytes"),
        "crc_hash": item.get("crc_hash"),
        "md5_hash": item.get("md5_hash"),
        "sha1_hash": item.get("sha1_hash"),
        "hash_scope": item.get("hash_scope", "file"),
    }


def fallback_disk_options(rom: dict[str, Any]) -> list[dict[str, Any]]:
    bridge = rom.get("bridge") or {}
    canonical_ids = {int(value) for value in bridge.get("canonical_file_ids") or []}
    canonical = [
        item for item in rom.get("files") or [] if int(item["id"]) in canonical_ids
    ]
    alternate = [
        item for item in rom.get("files") or [] if int(item["id"]) not in canonical_ids
    ]
    options = [
        {"slot": index, "canonical": descriptor_from_file(item, index, "canonical"), "alternates": []}
        for index, item in enumerate(canonical)
    ]
    for item in alternate:
        name = str(item.get("file_name") or "").casefold()
        match = re.search(r"(?:disk|disc)\s*([a-z]|\d+)", name)
        if match and match.group(1).isdigit():
            slot = max(0, int(match.group(1)) - 1)
        elif match:
            slot = max(0, ord(match.group(1).upper()) - ord("A"))
        else:
            slot = 0
        if slot >= len(options):
            slot = 0
        options[slot]["alternates"].append(
            descriptor_from_file(item, slot, "alternate")
        )
    return options


@dataclass
class BrowserSession:
    session_id: str
    user_id: int
    username: str
    token: str
    device_id: str | None
    rom: dict[str, Any]
    workdir: Path
    display: int
    command_port: int
    vnc_port: int
    websocket_port: int
    ticket: str
    started_at: str
    selected_disks: list[dict[str, Any]]
    content_path: Path
    xvfb: subprocess.Popen[Any] | None = None
    retroarch: subprocess.Popen[Any] | None = None
    x11vnc: subprocess.Popen[Any] | None = None
    websockify: subprocess.Popen[Any] | None = None
    active_websockets: int = 0
    log_handle: BinaryIO | None = None
    tray_open: bool = False
    current_disk: int = 0
    paused: bool = False
    last_activity: float = field(default_factory=time.monotonic)
    stopped_at: str | None = None
    operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    stop_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    stopped: bool = field(default=False, repr=False)

    @property
    def rom_id(self) -> int:
        return int(self.rom["id"])

    @property
    def name(self) -> str:
        return str(self.rom.get("name") or self.rom.get("fs_name") or self.rom_id)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def process_alive(self) -> bool:
        return all(
            process is not None and process.poll() is None
            for process in (self.xvfb, self.retroarch, self.x11vnc, self.websockify)
        )

    def emulator_alive(self) -> bool:
        return all(
            process is not None and process.poll() is None
            for process in (self.xvfb, self.retroarch)
        )

    def status(self) -> str:
        if not self.process_alive():
            return "stopped"
        if self.paused:
            return "paused"
        return "running"

    def public_dict(self, bridge_public_url: str) -> dict[str, Any]:
        disks = [
            {
                "slot": index,
                "file_id": int(item["file_id"]),
                "file_name": item["file_name"],
                "role": item.get("role", "canonical"),
            }
            for index, item in enumerate(self.selected_disks)
        ]
        return {
            "id": self.session_id,
            "user_id": self.user_id,
            "rom_id": self.rom_id,
            "name": self.name,
            "status": self.status(),
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_activity": dt.datetime.fromtimestamp(
                time.time() - (time.monotonic() - self.last_activity),
                tz=dt.timezone.utc,
            ).isoformat(),
            "display": {"width": 640, "height": 400},
            "player_url": (
                f"{bridge_public_url.rstrip('/')}/pc98/player.html?"
                f"session={self.session_id}&ticket={urllib.parse.quote(self.ticket)}"
            ),
            "websocket_path": f"/api/browser/sessions/{self.session_id}/socket",
            "audio_websocket_path": f"/api/browser/sessions/{self.session_id}/audio",
            "ticket": self.ticket,
            "disks": disks,
            "current_disk": self.current_disk,
            "tray_open": self.tray_open,
            "paused": self.paused,
            "capabilities": {
                "disk_control": True,
                "save_states": True,
                "audio": True,
            },
        }


class PC98SessionManager:
    def __init__(
        self,
        output_dir: Path,
        api_url: str,
        bridge_public_url: str,
        *,
        session_dir: Path | None = None,
        core_path: str | None = None,
        retroarch_path: str | None = None,
        xvfb_path: str | None = None,
        x11vnc_path: str | None = None,
        websockify_path: str | None = None,
        ffmpeg_path: str | None = None,
        pw_dump_path: str | None = None,
        pw_record_path: str | None = None,
        max_sessions: int = 2,
        idle_timeout: int = 45 * 60,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.api_url = api_url.rstrip("/")
        self.bridge_public_url = bridge_public_url.rstrip("/")
        self.manifest_path = self.output_dir / "platforms" / DEFAULT_PLATFORM / "manifest.json"
        self.session_dir = session_dir or Path(
            os.getenv(
                "BRIDGE_SESSION_DIR",
                str(Path(tempfile.gettempdir()) / "romm-esde-bridge-sessions"),
            )
        )
        self.core_path = core_path or os.getenv("BRIDGE_PC98_CORE", DEFAULT_CORE)
        self.retroarch_path = retroarch_path or os.getenv("BRIDGE_RETROARCH", DEFAULT_RETROARCH)
        self.xvfb_path = xvfb_path or os.getenv("BRIDGE_XVFB", DEFAULT_XVFB)
        self.x11vnc_path = x11vnc_path or os.getenv("BRIDGE_X11VNC", DEFAULT_X11VNC)
        self.websockify_path = websockify_path or os.getenv("BRIDGE_WEBSOCKIFY", DEFAULT_WEBSOCKIFY)
        self.ffmpeg_path = ffmpeg_path or os.getenv("BRIDGE_FFMPEG", DEFAULT_FFMPEG)
        self.pw_dump_path = pw_dump_path or os.getenv("BRIDGE_PW_DUMP", DEFAULT_PW_DUMP)
        self.pw_record_path = pw_record_path or os.getenv("BRIDGE_PW_RECORD", DEFAULT_PW_RECORD)
        self.max_sessions = max(1, max_sessions)
        self.idle_timeout = max(60, idle_timeout)
        self.disconnected_grace = max(
            5, int(os.getenv("BRIDGE_PC98_DISCONNECTED_GRACE", "90"))
        )
        self._lock = threading.RLock()
        self._display_lock = threading.Lock()
        self._reserved_displays: set[int] = set()
        self._sessions: dict[str, BrowserSession] = {}
        self._creating = 0
        self._stop_event = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="pc98-session-reaper", daemon=True)
        self._reaper.start()

    def _reserve_display(self) -> int:
        """Reserve an X display before starting Xvfb to avoid concurrent races."""
        with self._display_lock:
            for display in range(90, 191):
                if display in self._reserved_displays:
                    continue
                if Path(f"/tmp/.X11-unix/X{display}").exists() or Path(f"/tmp/.X{display}-lock").exists():
                    continue
                self._reserved_displays.add(display)
                return display
        raise SessionError("No free Xvfb display is available")

    def _release_display(self, display: int) -> None:
        with self._display_lock:
            self._reserved_displays.discard(display)

    def _load_manifest(self) -> dict[str, Any]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SessionError("PC-98 manifest is unavailable") from exc
        if manifest.get("schema") != "romm-esde-platform-v2":
            raise SessionError("Unsupported PC-98 manifest")
        return manifest

    def _find_rom(self, rom_id: int) -> dict[str, Any]:
        manifest = self._load_manifest()
        try:
            rom_id = int(rom_id)
        except (TypeError, ValueError) as exc:
            raise SessionError("rom_id must be an integer") from exc
        rom = next((item for item in manifest.get("roms", []) if int(item["id"]) == rom_id), None)
        if not rom:
            raise SessionError("PC-98 game was not found in the Bridge manifest")
        return rom

    def _request(
        self,
        token: str,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
    ) -> urllib.response.addinfourl:
        url = f"{self.api_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "User-Agent": "romm-esde-bridge-browser/1",
        }
        if data is not None:
            headers["Content-Type"] = content_type or "application/json"
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, data=data, method=method, headers=headers),
                timeout=120,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read(1024).decode("utf-8", "replace")
            raise SessionError(f"RomM request failed ({exc.code}): {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise SessionError("RomM is unreachable") from exc

    def _json_request(self, token: str, path: str) -> Any:
        with self._request(token, path) as response:
            try:
                return json.load(response)
            except ValueError as exc:
                raise SessionError("RomM returned invalid JSON") from exc

    def _download_file(
        self,
        token: str,
        file_item: dict[str, Any],
        target: Path,
        *,
        path: str | None = None,
    ) -> None:
        file_id = int(file_item["file_id"])
        file_name = safe_file_name(str(file_item["file_name"]))
        api_path = path or (
            f"/api/roms/{file_id}/files/content/{urllib.parse.quote(file_name, safe='')}"
        )
        expected_size = int(file_item.get("file_size_bytes") or 0)
        expected_sha1 = str(file_item.get("sha1_hash") or "").lower()
        expected_md5 = str(file_item.get("md5_hash") or "").lower()
        temporary = target.with_name(f".{target.name}.part")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        size = 0
        sha1 = hashlib.sha1()
        md5 = hashlib.md5(usedforsecurity=False)
        try:
            with self._request(token, api_path, accept="application/octet-stream") as response:
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length and content_length > MAX_DOWNLOAD_BYTES:
                    raise SessionError("ROM file is too large for a browser session")
                with temporary.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise SessionError("ROM file is too large for a browser session")
                        handle.write(chunk)
                        sha1.update(chunk)
                        md5.update(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            if expected_size and size != expected_size:
                raise SessionError(f"ROM file size mismatch: {file_name}")
            hash_scope = str(file_item.get("hash_scope") or "file")
            if hash_scope == "archive_single_member":
                try:
                    with zipfile.ZipFile(temporary) as archive:
                        members = [
                            item for item in archive.infolist()
                            if not item.is_dir()
                            and Path(item.filename).suffix.casefold() in NP2KAI_DISK_EXTENSIONS
                        ]
                        if not members:
                            members = [item for item in archive.infolist() if not item.is_dir()]
                        if not members:
                            raise SessionError(f"Nested PC-98 archive is empty: {file_name}")
                        matched = False
                        for member in members:
                            inner_sha1 = hashlib.sha1()
                            inner_md5 = hashlib.md5(usedforsecurity=False)
                            with archive.open(member) as inner:
                                for chunk in iter(lambda: inner.read(1024 * 1024), b""):
                                    inner_sha1.update(chunk)
                                    inner_md5.update(chunk)
                            if (
                                (not expected_sha1 or inner_sha1.hexdigest().casefold() == expected_sha1)
                                and (not expected_md5 or inner_md5.hexdigest().casefold() == expected_md5)
                            ):
                                matched = True
                                break
                except (OSError, zipfile.BadZipFile, KeyError) as exc:
                    raise SessionError(f"Invalid nested PC-98 archive: {file_name}") from exc
                if (expected_sha1 or expected_md5) and not matched:
                    raise SessionError(f"ROM file hash mismatch: {file_name}")
            else:
                if expected_sha1 and sha1.hexdigest().lower() != expected_sha1:
                    raise SessionError(f"ROM file SHA-1 mismatch: {file_name}")
                if expected_md5 and md5.hexdigest().lower() != expected_md5:
                    raise SessionError(f"ROM file MD5 mismatch: {file_name}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _download_firmware(self, token: str, manifest: dict[str, Any], target: Path) -> None:
        for firmware in manifest.get("firmware") or []:
            source_name = safe_file_name(str(firmware.get("file_name") or ""))
            output_name = FIRMWARE_ALIASES.get(source_name.casefold(), source_name)
            item = {
                "file_id": int(firmware["id"]),
                "file_name": source_name,
                "file_size_bytes": firmware.get("file_size_bytes"),
                "sha1_hash": firmware.get("sha1_hash"),
                "md5_hash": firmware.get("md5_hash"),
            }
            path = firmware.get("download_api_path")
            self._download_file(
                token,
                item,
                target / output_name,
                path=path,
            )

    def _expand_nested_archive(
        self,
        archive_path: Path,
        target_dir: Path,
        source: dict[str, Any],
    ) -> tuple[list[Path], list[dict[str, Any]]]:
        """Extract disk members from a RomM nested ZIP for NP2Kai."""
        extracted: list[Path] = []
        seen: set[str] = set()
        expanded_size = 0
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or Path(entry.filename).suffix.casefold() not in NP2KAI_DISK_EXTENSIONS:
                    continue
                name = Path(entry.filename).name
                if not name or name in seen:
                    raise SessionError(f"Nested PC-98 archive contains duplicate disk: {name}")
                safe_file_name(name)
                expanded_size += int(entry.file_size)
                if expanded_size > MAX_DOWNLOAD_BYTES:
                    raise SessionError("Nested PC-98 archive expands beyond the session limit")
                seen.add(name)
                target = target_dir / name
                if target.exists():
                    raise SessionError(f"Nested PC-98 archive has a colliding disk name: {name}")
                temporary = target.with_name(f".{target.name}.part")
                try:
                    with archive.open(entry) as source_stream, temporary.open("wb") as target_stream:
                        shutil.copyfileobj(source_stream, target_stream, 1024 * 1024)
                        target_stream.flush()
                        os.fsync(target_stream.fileno())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                extracted.append(target)
        if not extracted:
            raise SessionError(f"Nested PC-98 archive has no disk image: {archive_path.name}")

        original_order = {path: index for index, path in enumerate(extracted)}
        primary = extracted[0]

        def boot_order(item: Path) -> tuple[int, int]:
            lowered = item.stem.casefold()
            writable = any(marker in lowered for marker in (
                "user", "save", "system", "ユーザー", "セーブ", "システム",
            ))
            return (0 if item == primary else 1 if writable else 2, original_order[item])

        extracted.sort(key=boot_order)
        descriptors = [
            {
                **source,
                "slot": index,
                "file_name": path.name,
                "role": "archive",
            }
            for index, path in enumerate(extracted)
        ]
        return extracted, descriptors

    @staticmethod
    def _state_paths(workdir: Path, rom_id: int) -> tuple[Path, Path]:
        state_dir = workdir / "states" / CORE_STATE_DIRECTORY
        base = state_dir / f"romm-{int(rom_id)}.state"
        return base, base.with_name(base.name + ".auto")

    @staticmethod
    def _canonical_remote_state(rom_id: int, states: list[dict[str, Any]]) -> dict[str, Any] | None:
        available = [item for item in states if not item.get("missing_from_fs")]
        exact_name = f"romm-{int(rom_id)}.state.auto"
        exact = [item for item in available if item.get("file_name") == exact_name]
        candidates = exact or available
        return max(candidates, key=lambda item: item.get("updated_at") or "") if candidates else None

    def _download_state_content(self, token: str, state_id: int, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        chunks: list[bytes] = []
        size = 0
        with self._request(
            token,
            f"/api/states/{int(state_id)}/content",
            accept="application/octet-stream",
        ) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_STATE_BYTES:
                    raise SessionError("RomM state is too large for a browser session")
                chunks.append(chunk)
        atomic_write(target, b"".join(chunks))

    def _pull_remote_state(self, token: str, rom_id: int, workdir: Path) -> None:
        states = self._json_request(token, f"/api/states?rom_id={int(rom_id)}") or []
        remote = self._canonical_remote_state(int(rom_id), states)
        if not remote:
            return
        _, auto_path = self._state_paths(workdir, int(rom_id))
        self._download_state_content(token, int(remote["id"]), auto_path)

    @staticmethod
    def _disk_options(rom: dict[str, Any]) -> list[dict[str, Any]]:
        options = (rom.get("bridge") or {}).get("disk_options")
        options = options or fallback_disk_options(rom)
        if rom.get("has_nested_single_file"):
            for option in options:
                for descriptor in [option["canonical"], *(option.get("alternates") or [])]:
                    if str(descriptor.get("file_name") or "").casefold().endswith(".zip"):
                        descriptor.setdefault("hash_scope", "archive_single_member")
        return options

    def _select_disks(self, rom: dict[str, Any], requested: Any) -> list[dict[str, Any]]:
        options = self._disk_options(rom)
        if not options:
            raise SessionError("PC-98 game has no playable disk file")
        allowed = {
            int(item["file_id"]): item
            for option in options
            for item in [option["canonical"], *(option.get("alternates") or [])]
        }
        selected = [option["canonical"] for option in options]
        if requested is None:
            return selected
        if not isinstance(requested, list) or len(requested) > len(options):
            raise SessionError("disk_file_ids must contain valid PC-98 disk choices")
        replacements: dict[int, int] = {}
        for value in requested:
            try:
                file_id = int(value)
            except (TypeError, ValueError) as exc:
                raise SessionError("disk_file_ids must contain integers") from exc
            item = allowed.get(file_id)
            if not item:
                raise SessionError("Selected disk is not part of this RomM game")
            slot = int(item["slot"])
            if slot in replacements:
                raise SessionError("Only one disk choice may be selected for each slot")
            if not 0 <= slot < len(selected):
                raise SessionError("Selected disk slot is not available for this game")
            replacements[slot] = file_id
        for slot, file_id in replacements.items():
            selected[slot] = allowed[file_id]
        return selected

    @staticmethod
    def _pipewire_env() -> dict[str, str]:
        env = os.environ.copy()
        runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
        return env

    def _pipewire_audio_target(self, session: BrowserSession) -> int | None:
        if shutil.which(self.pw_dump_path) is None and not Path(self.pw_dump_path).is_file():
            return None
        retroarch_pid = session.retroarch.pid if session.retroarch else None
        if not retroarch_pid:
            return None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    [self.pw_dump_path],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=self._pipewire_env(),
                    timeout=2,
                )
                objects = json.loads(result.stdout)
            except (OSError, subprocess.SubprocessError, ValueError, TypeError):
                objects = []
            if isinstance(objects, list):
                client_ids = set()
                for item in objects:
                    if not isinstance(item, dict):
                        continue
                    props = ((item.get("info") or {}).get("props") or {})
                    if str(props.get("application.process.id")) != str(retroarch_pid):
                        continue
                    try:
                        client_ids.add(int(item["id"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                client_ids_text = {str(value) for value in client_ids}
                for item in objects:
                    if not isinstance(item, dict):
                        continue
                    props = ((item.get("info") or {}).get("props") or {})
                    if str(props.get("media.class")) != "Stream/Output/Audio":
                        continue
                    if (
                        str(props.get("application.process.id")) != str(retroarch_pid)
                        and str(props.get("client.id")) not in client_ids_text
                    ):
                        continue
                    try:
                        return int(item["id"])
                    except (KeyError, TypeError, ValueError):
                        continue
            time.sleep(0.1)
        return None

    def audio_capture(self, session_id: str, ticket: str) -> subprocess.Popen[bytes]:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session or not secrets.compare_digest(session.ticket, ticket):
            raise SessionError("Invalid browser session ticket")
        if not session.process_alive():
            raise SessionError("Browser session has ended")
        if shutil.which(self.pw_record_path) is None and not Path(self.pw_record_path).is_file():
            raise UnsupportedFeatureError("PipeWire 音频捕获不可用")
        target = self._pipewire_audio_target(session)
        if target is None:
            raise UnsupportedFeatureError("RetroArch 音频流尚未就绪")
        capture = subprocess.Popen(
            [self.pw_record_path, "--raw", "--rate", "48000", "--channels", "2", "--format", "s16",
             "--target", str(target), "-"],
            stdout=subprocess.PIPE,
            stderr=session.log_handle or subprocess.DEVNULL,
            env=self._pipewire_env(),
            start_new_session=True,
            bufsize=0,
        )
        time.sleep(0.15)
        if capture.poll() is not None or capture.stdout is None:
            terminate_process(capture)
            raise UnsupportedFeatureError("PipeWire 音频流无法读取")
        return capture

    def _start_processes(
        self,
        session_id: str,
        workdir: Path,
        content_path: Path,
        rom: dict[str, Any],
        token: str,
    ) -> tuple[
        int, int, int, int,
        subprocess.Popen[Any], subprocess.Popen[Any], subprocess.Popen[Any], subprocess.Popen[Any], BinaryIO,
    ]:
        if not Path(self.core_path).is_file():
            raise SessionError(f"NP2Kai core is unavailable: {self.core_path}")
        for executable in (self.retroarch_path, self.xvfb_path, self.x11vnc_path, self.websockify_path):
            if not Path(executable).is_file() and shutil.which(executable) is None:
                raise SessionError(f"Session dependency is unavailable: {executable}")

        display = self._reserve_display()
        try:
            command_port = free_local_port()
            vnc_port = free_local_port()
            websocket_port = free_local_port()
            bios_dir = workdir / "bios"
            save_dir = workdir / "saves"
            state_dir = workdir / "states"
            capture_dir = workdir / "captures"
            config_dir = workdir / "config"
            for path in (bios_dir, save_dir, state_dir, capture_dir, config_dir):
                path.mkdir(parents=True, exist_ok=True)
            config = (
                f'video_driver = "gl"\n'
                f'video_fullscreen = "true"\n'
                f'video_windowed_fullscreen = "false"\n'
                f'video_width = "640"\nvideo_height = "400"\n'
                f'video_vsync = "true"\nvideo_threaded = "false"\n'
                f'audio_driver = "pipewire"\naudio_out_rate = "48000"\naudio_latency = "64"\n'
                f'input_driver = "x"\n'
                f'system_directory = "{bios_dir}"\n'
                f'savefile_directory = "{save_dir}"\n'
                f'savestate_directory = "{state_dir}"\n'
                f'screenshot_directory = "{capture_dir}"\n'
                f'network_cmd_enable = "true"\nnetwork_cmd_port = "{command_port}"\n'
                f'config_save_on_exit = "false"\nsave_on_exit = "false"\n'
                f'savestate_auto_load = "true"\nsavestate_auto_save = "true"\n'
                f'savestate_file_compression = "true"\nsavestate_thumbnail_enable = "true"\n'
                f'quit_on_close_content = "true"\n'
            ).encode()
            atomic_write(config_dir / "retroarch.cfg", config)
            core_options_path = (
                config_dir / "retroarch" / "config" / CORE_STATE_DIRECTORY
                / f"{CORE_STATE_DIRECTORY}.opt"
            )
            core_options = "\n".join(
                f'{key} = "{value}"' for key, value in NP2KAI_CORE_OPTIONS.items()
            ) + "\n"
            atomic_write(core_options_path, core_options.encode())
            log_handle = (workdir / "session.log").open("ab")
        except Exception:
            self._release_display(display)
            raise
        env = os.environ.copy()
        env.update({
            "DISPLAY": f":{display}",
            "HOME": str(workdir / "home"),
            "XDG_CONFIG_HOME": str(config_dir),
            "XDG_DATA_HOME": str(workdir / "data"),
            "XDG_CACHE_HOME": str(workdir / "cache"),
            "XDG_RUNTIME_DIR": os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SDL_VIDEODRIVER": "x11",
        })
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        xvfb = retroarch = x11vnc = websockify = None
        try:
            xvfb = subprocess.Popen(
                [self.xvfb_path, f":{display}", "-screen", "0", "640x400x24", "-nolisten", "tcp", "-ac"],
                stdout=log_handle, stderr=subprocess.STDOUT, env=env, start_new_session=True,
            )
            deadline = time.monotonic() + 8
            while not Path(f"/tmp/.X11-unix/X{display}").exists():
                if xvfb.poll() is not None:
                    raise SessionError("Xvfb exited before creating its display")
                if time.monotonic() > deadline:
                    raise SessionError("Xvfb did not become ready")
                time.sleep(0.05)
            retroarch = subprocess.Popen(
                [self.retroarch_path, "--config", str(config_dir / "retroarch.cfg"), "--verbose",
                 "-L", self.core_path, str(content_path)],
                stdout=log_handle, stderr=subprocess.STDOUT, env=env, start_new_session=True,
            )
            time.sleep(0.5)
            if retroarch.poll() is not None:
                raise SessionError("RetroArch exited while loading the PC-98 game")
            x11vnc = subprocess.Popen(
                [self.x11vnc_path, "-display", f":{display}", "-localhost", "-forever", "-shared",
                 "-nopw", "-noxdamage", "-quiet", "-rfbport", str(vnc_port)],
                stdout=log_handle, stderr=subprocess.STDOUT, env=env, start_new_session=True,
            )
            wait_for_tcp(vnc_port)
            websockify = subprocess.Popen(
                [self.websockify_path, "127.0.0.1:" + str(websocket_port), "127.0.0.1:" + str(vnc_port),
                 "--heartbeat=30"],
                stdout=log_handle, stderr=subprocess.STDOUT, env=env, start_new_session=True,
            )
            wait_for_tcp(websocket_port)
            return display, command_port, vnc_port, websocket_port, xvfb, retroarch, x11vnc, websockify, log_handle
        except Exception:
            terminate_process(websockify)
            terminate_process(x11vnc)
            terminate_process(retroarch)
            terminate_process(xvfb)
            self._release_display(display)
            log_handle.close()
            raise

    def create(
        self,
        *,
        user_id: int,
        username: str,
        token: str,
        rom_id: int,
        device_id: str | None = None,
        disk_file_ids: Any = None,
    ) -> BrowserSession:
        with self._lock:
            if len(self._sessions) + self._creating >= self.max_sessions:
                raise SessionError(
                    f"Bridge 当前最多同时运行 {self.max_sessions} 个 PC-98 会话"
                )
            self._creating += 1
        session_id = secrets.token_hex(16)
        workdir = self.session_dir / session_id
        started: tuple[Any, ...] | None = None
        try:
            rom = self._find_rom(rom_id)
            selected_disks = self._select_disks(rom, disk_file_ids)
            workdir.mkdir(parents=True, mode=0o700)
            manifest = self._load_manifest()
            # NP2Kai resolves its firmware relative to system_directory/np2kai.
            self._download_firmware(token, manifest, workdir / "bios" / "np2kai")
            disks_dir = workdir / "disks"
            disks_dir.mkdir(parents=True, exist_ok=True)
            local_disks: list[Path] = []
            playable_disks: list[dict[str, Any]] = []
            for index, descriptor in enumerate(selected_disks, start=1):
                target = disks_dir / f"disk-{index:02d}{Path(descriptor['file_name']).suffix.lower()}"
                self._download_file(token, descriptor, target)
                if (
                    descriptor.get("hash_scope") == "archive_single_member"
                    and target.suffix.casefold() == ".zip"
                ):
                    nested_paths, nested_descriptors = self._expand_nested_archive(
                        target, disks_dir, descriptor
                    )
                    local_disks.extend(nested_paths)
                    playable_disks.extend(nested_descriptors)
                else:
                    local_disks.append(target)
                    playable_disks.append(dict(descriptor))
            for slot, descriptor in enumerate(playable_disks):
                descriptor["slot"] = slot
            content_stem = f"romm-{int(rom['id'])}"
            if len(local_disks) == 1:
                content_path = workdir / f"{content_stem}{local_disks[0].suffix}"
                shutil.copy2(local_disks[0], content_path)
            else:
                # Deck launches canonical multi-disk games from disk A and
                # lets NP2Kai manage the other images.  An M3U makes RetroArch
                # expose disk control, but this core disables serialization
                # for that content type, which prevents RomM state sync.
                content_path = disks_dir / f"{content_stem}.cmd"
                command = "np2kai " + " ".join(
                    f'"{path.name}"' for path in local_disks
                ) + "\r\n"
                atomic_write(content_path, command.encode("utf-8"))
            # Match the Deck client: RetroArch auto-loads this before the
            # first frame, while a missing or unreadable remote state must not
            # prevent the game itself from starting.
            with contextlib.suppress(SessionError, KeyError, TypeError, ValueError):
                self._pull_remote_state(token, int(rom["id"]), workdir)
            result = self._start_processes(session_id, workdir, content_path, rom, token)
            started = result
            display, command_port, vnc_port, websocket_port, xvfb, retroarch, x11vnc, websockify, log_handle = result
            session = BrowserSession(
                session_id=session_id, user_id=int(user_id), username=username, token=token,
                device_id=device_id, rom=rom, workdir=workdir, display=display,
                command_port=command_port, vnc_port=vnc_port, websocket_port=websocket_port,
                ticket=secrets.token_urlsafe(32), started_at=iso_now(), selected_disks=playable_disks,
                content_path=content_path, xvfb=xvfb, retroarch=retroarch, x11vnc=x11vnc,
                websockify=websockify, log_handle=log_handle,
            )
            # x11vnc is the process between the X display and websockify. It is
            # looked up by its port because _start_processes only returns the
            # public process handles needed by the lifecycle manager.
            with self._lock:
                self._sessions[session_id] = session
            started = None
            return session
        except Exception:
            if started is not None:
                for process in reversed(started[4:8]):
                    terminate_process(process)
                self._release_display(int(started[0]))
                log_handle = started[8]
                if log_handle:
                    with contextlib.suppress(Exception):
                        log_handle.close()
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        finally:
            with self._lock:
                self._creating -= 1

    def _send_command(self, session: BrowserSession, command: str) -> None:
        session.touch()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            try:
                sock.sendto((command + "\n").encode(), ("127.0.0.1", session.command_port))
            except OSError as exc:
                raise SessionError("RetroArch command channel is unavailable") from exc

    def _find_state_path(self, session: BrowserSession) -> Path | None:
        state_path, _ = self._state_paths(session.workdir, session.rom_id)
        return state_path if state_path.is_file() else None

    @staticmethod
    def _state_signature(path: Path) -> tuple[int, int, int] | None:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return None
        return metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_size

    @classmethod
    def _wait_for_new_state(
        cls,
        path: Path,
        previous: tuple[int, int, int] | None,
        timeout: float = 12,
    ) -> Path | None:
        """Wait until RetroArch replaces the state instead of reusing an old one."""
        deadline = time.monotonic() + timeout
        stable_signature: tuple[int, int, int] | None = None
        stable_since = 0.0
        while time.monotonic() < deadline:
            signature = cls._state_signature(path)
            if signature is not None and signature != previous:
                if signature != stable_signature:
                    stable_signature = signature
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 0.1:
                    return path
            time.sleep(0.1)
        return None

    def _capture_screenshot(self, session: BrowserSession, target: Path) -> None:
        if shutil.which(self.ffmpeg_path) is None and not Path(self.ffmpeg_path).is_file():
            return
        result = subprocess.run(
            [self.ffmpeg_path, "-loglevel", "error", "-y", "-f", "x11grab", "-video_size", "640x400",
             "-i", f":{session.display}", "-frames:v", "1", str(target)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15,
        )
        if result.returncode != 0:
            target.unlink(missing_ok=True)

    @staticmethod
    def _multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
        boundary = f"romm-esde-browser-{secrets.token_hex(16)}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )
        for name, path in files.items():
            content_type = "image/png" if path.suffix.casefold() == ".png" else "application/octet-stream"
            data = path.read_bytes()
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n"
            )
        chunks.append(f"--{boundary}--\r\n".encode())
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _materialize_auto_state(self, session: BrowserSession, state_path: Path) -> tuple[Path, Path | None]:
        _, auto_path = self._state_paths(session.workdir, session.rom_id)
        shutil.copy2(state_path, auto_path)
        screenshot = state_path.with_name(state_path.name + ".png")
        auto_screenshot = auto_path.with_name(auto_path.name + ".png")
        if screenshot.is_file():
            shutil.copy2(screenshot, auto_screenshot)
        elif auto_screenshot.is_file():
            auto_screenshot.unlink()
        elif session.process_alive():
            self._capture_screenshot(session, auto_screenshot)
        return auto_path, auto_screenshot if auto_screenshot.is_file() else None

    def _upload_state(
        self,
        session: BrowserSession,
        state_path: Path,
        screenshot: Path | None,
        slot: int | None = None,
    ) -> dict[str, Any]:
        states = self._json_request(session.token, f"/api/states?rom_id={session.rom_id}") or []
        expected_name = state_path.name
        remote = next((item for item in states if item.get("file_name") == expected_name), None)
        files = {"stateFile": state_path}
        if screenshot and screenshot.is_file():
            files["screenshotFile"] = screenshot
        body, content_type = self._multipart({}, files)
        if remote:
            path = f"/api/states/{int(remote['id'])}"
            method = "PUT"
        else:
            query = urllib.parse.urlencode({"rom_id": session.rom_id, "emulator": "np2kai"})
            path = f"/api/states?{query}"
            method = "POST"
        with self._request(session.token, path, method=method, data=body, content_type=content_type) as response:
            try:
                result = json.load(response)
            except ValueError as exc:
                raise SessionError("RomM returned an invalid state response") from exc
        return {"slot": slot, "state": result}

    def save_state(self, session: BrowserSession, slot: Any = None) -> dict[str, Any]:
        with session.operation_lock:
            if not session.emulator_alive():
                raise SessionError("Browser session has ended")
            state_path, _ = self._state_paths(session.workdir, session.rom_id)
            previous = self._state_signature(state_path)
            self._send_command(session, "SAVE_STATE")
            state_path = self._wait_for_new_state(state_path, previous)
            if not state_path:
                raise SessionError("RetroArch did not create the PC-98 state")
            auto_path, screenshot = self._materialize_auto_state(session, state_path)
            result = self._upload_state(session, auto_path, screenshot)
            session.touch()
            return result

    def load_state(self, session: BrowserSession, slot: Any = None) -> dict[str, Any]:
        with session.operation_lock:
            states = self._json_request(session.token, f"/api/states?rom_id={session.rom_id}") or []
            remote = self._canonical_remote_state(session.rom_id, states)
            if not remote:
                raise SessionError("RomM 中没有这个 PC-98 存档")
            state_path, auto_path = self._state_paths(session.workdir, session.rom_id)
            self._download_state_content(session.token, int(remote["id"]), state_path)
            shutil.copy2(state_path, auto_path)
            self._send_command(session, "LOAD_STATE")
            session.touch()
            return {"state": remote}

    def pause(self, session: BrowserSession, paused: bool | None = None) -> dict[str, Any]:
        with session.operation_lock:
            desired = not session.paused if paused is None else bool(paused)
            if desired != session.paused:
                self._send_command(session, "PAUSE_TOGGLE")
                session.paused = desired
            return session.public_dict(self.bridge_public_url)

    def reset(self, session: BrowserSession) -> dict[str, Any]:
        with session.operation_lock:
            self._send_command(session, "RESET")
            session.tray_open = False
            session.current_disk = 0
            session.touch()
            return session.public_dict(self.bridge_public_url)

    def eject(self, session: BrowserSession) -> dict[str, Any]:
        with session.operation_lock:
            self._send_command(session, "DISK_EJECT_TOGGLE")
            session.tray_open = not session.tray_open
            session.touch()
            return session.public_dict(self.bridge_public_url)

    def insert(self, session: BrowserSession, slot: Any) -> dict[str, Any]:
        with session.operation_lock:
            try:
                slot = int(slot)
            except (TypeError, ValueError) as exc:
                raise SessionError("disk slot must be an integer") from exc
            if not 0 <= slot < len(session.selected_disks):
                raise SessionError("disk slot is not available for this game")
            if not session.tray_open:
                self._send_command(session, "DISK_EJECT_TOGGLE")
                session.tray_open = True
            while session.current_disk != slot:
                direction = "DISK_NEXT" if slot > session.current_disk else "DISK_PREV"
                self._send_command(session, direction)
                session.current_disk = (session.current_disk + (1 if direction == "DISK_NEXT" else -1)) % len(session.selected_disks)
            self._send_command(session, "DISK_EJECT_TOGGLE")
            session.tray_open = False
            session.touch()
            return session.public_dict(self.bridge_public_url)

    def get(
        self,
        session_id: str,
        user_id: int,
        *,
        touch: bool = True,
        require_alive: bool = True,
    ) -> BrowserSession:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise SessionError("Invalid browser session id")
        with self._lock:
            session = self._sessions.get(session_id)
        if not session or session.user_id != int(user_id):
            raise SessionError("Browser session not found")
        if require_alive and not session.process_alive():
            raise SessionError("Browser session has ended")
        if touch:
            session.touch()
        return session

    def get_by_ticket(
        self,
        session_id: str,
        ticket: str,
        *,
        touch: bool = True,
        require_alive: bool = True,
    ) -> BrowserSession:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise SessionError("Invalid browser session id")
        if not ticket:
            raise SessionError("Invalid browser session ticket")
        with self._lock:
            session = self._sessions.get(session_id)
        if not session or not secrets.compare_digest(session.ticket, ticket):
            raise SessionError("Invalid browser session ticket")
        if require_alive and not session.process_alive():
            raise SessionError("Browser session has ended")
        if touch:
            session.touch()
        return session

    def stop_session(self, session: BrowserSession) -> dict[str, Any]:
        with self._lock:
            current = self._sessions.get(session.session_id)
            if current is session:
                self._sessions.pop(session.session_id, None)
        self._stop(session)
        return {"stopped": True, "id": session.session_id}

    def list_user(self, user_id: int) -> list[dict[str, Any]]:
        self.reap()
        with self._lock:
            sessions = [item for item in self._sessions.values() if item.user_id == int(user_id)]
        return [item.public_dict(self.bridge_public_url) for item in sessions]

    def websocket_target(self, session_id: str, ticket: str) -> int:
        session = self.get_by_ticket(session_id, ticket)
        return session.websocket_port

    def websocket_connected(self, session_id: str, ticket: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or not secrets.compare_digest(session.ticket, ticket):
                raise SessionError("Invalid browser session ticket")
            if not session.process_alive():
                raise SessionError("Browser session has ended")
            session.active_websockets += 1
            session.touch()

    def websocket_disconnected(self, session_id: str, ticket: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or not secrets.compare_digest(session.ticket, ticket):
                return
            session.active_websockets = max(0, session.active_websockets - 1)

    def _record_play_session(self, session: BrowserSession, ended_at: str) -> None:
        started = dt.datetime.fromisoformat(session.started_at)
        ended = dt.datetime.fromisoformat(ended_at)
        duration_ms = max(0, int((ended - started).total_seconds() * 1000))
        payload = {
            "device_id": session.device_id,
            "sessions": [{
                "rom_id": session.rom_id,
                "start_time": session.started_at,
                "end_time": ended_at,
                "duration_ms": duration_ms,
            }],
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        with contextlib.suppress(SessionError):
            with self._request(session.token, "/api/play-sessions", method="POST", data=body) as response:
                response.read()

    def _stop(self, session: BrowserSession) -> None:
        with session.stop_lock:
            if session.stopped:
                return
            session.stopped = True
            with session.operation_lock:
                ended_at = iso_now()
                # Persist the RetroArch state before tearing down the emulator.
                # A crashed emulator must not upload an old state left in its
                # workdir and overwrite a newer RomM state.
                if session.emulator_alive():
                    with contextlib.suppress(Exception):
                        self.save_state(session)
                    with contextlib.suppress(Exception):
                        self._send_command(session, "SAVE_FILES")
                with contextlib.suppress(Exception):
                    self._record_play_session(session, ended_at)
                terminate_process(session.websockify)
                terminate_process(session.x11vnc)
                terminate_process(session.retroarch)
                terminate_process(session.xvfb)
                self._release_display(session.display)
                if session.log_handle:
                    with contextlib.suppress(Exception):
                        session.log_handle.close()
                session.stopped_at = ended_at
                shutil.rmtree(session.workdir, ignore_errors=True)

    def stop(self, session_id: str, user_id: int) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session.user_id != int(user_id):
                raise SessionError("Browser session not found")
            self._sessions.pop(session_id, None)
        return self.stop_session(session)

    def reap(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [
                session for session in self._sessions.values()
                if (
                    not session.process_alive()
                    or (
                        session.active_websockets == 0
                        and now - session.last_activity > self.disconnected_grace
                    )
                    or (
                        session.active_websockets > 0
                        and now - session.last_activity > self.idle_timeout
                    )
                )
            ]
            for session in expired:
                self._sessions.pop(session.session_id, None)
        for session in expired:
            self._stop(session)

    def _reap_loop(self) -> None:
        while not self._stop_event.wait(30):
            with contextlib.suppress(Exception):
                self.reap()

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._stop(session)
