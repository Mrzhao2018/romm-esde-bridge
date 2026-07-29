#!/usr/bin/env python3
"""RomM-backed, on-demand ES-DE client for Steam Deck.

Only the catalog, launch stubs and artwork are kept locally by default. ROM
content is downloaded into a bounded cache when a stub is launched.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows adapter will use a native lock
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - only present on Windows
    msvcrt = None
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


USER_FIELDS = ("playcount", "playtime", "lastplayed", "favorite", "hidden")
CLIENT_VERSION = "5"
SYSTEM_NAME = "romm-pc98"
NP2KAI_DISK_EXTENSIONS = {
    ".d98", ".98d", ".fdi", ".fdd", ".2hd", ".tfd", ".d88", ".88d",
    ".hdm", ".xdf", ".dup", ".hdi", ".thd", ".nhd", ".hdd", ".hdn",
}
SYSTEM_XML = """  <system>
    <name>romm-pc98</name>
    <fullname>RomM · PC-98</fullname>
    <path>%ROMPATH%/romm-pc98</path>
    <extension>.romm .ROMM</extension>
    <command label="RomM On-demand">\"{launcher}\" \"%ROM%\"</command>
    <platform>pc98</platform>
    <theme>pc98</theme>
  </system>
"""
STEAM_DECK_AUTOCONFIG = """input_driver = "sdl2"
input_device = "Steam Deck Controller"
input_vendor_id = "10462"
input_product_id = "4613"
input_b_btn = "0"
input_y_btn = "2"
input_select_btn = "4"
input_start_btn = "6"
input_up_btn = "11"
input_down_btn = "12"
input_left_btn = "13"
input_right_btn = "14"
input_a_btn = "1"
input_x_btn = "3"
input_l_btn = "9"
input_r_btn = "10"
input_l2_axis = "+4"
input_r2_axis = "+5"
input_l3_btn = "7"
input_r3_btn = "8"
input_l_x_plus_axis = "+0"
input_l_x_minus_axis = "-0"
input_l_y_plus_axis = "+1"
input_l_y_minus_axis = "-1"
input_r_x_plus_axis = "+2"
input_r_x_minus_axis = "-2"
input_r_y_plus_axis = "+3"
input_r_y_minus_axis = "-3"
input_b_btn_label = "A"
input_y_btn_label = "X"
input_select_btn_label = "Select"
input_start_btn_label = "Start"
input_up_btn_label = "Dpad Up"
input_down_btn_label = "Dpad Down"
input_left_btn_label = "Dpad Left"
input_right_btn_label = "Dpad Right"
input_a_btn_label = "B"
input_x_btn_label = "Y"
input_l_btn_label = "L1"
input_r_btn_label = "R1"
input_l2_axis_label = "L2"
input_r2_axis_label = "R2"
input_l3_btn_label = "L3"
input_r3_btn_label = "R3"
"""
NP2KAI_RETROARCH_OVERRIDE = """# Managed by romm-esde: Steam Deck 1280x800 / NP2Kai 640x400
aspect_ratio_index = "0"
video_fullscreen = "true"
video_scale_integer = "true"
video_scale_integer_axis = "0"
video_scale_integer_overscale = "false"
video_smooth = "false"
video_shader_enable = "false"
video_threaded = "false"
video_vsync = "true"
video_max_swapchain_images = "2"
video_frame_delay = "0"
video_frame_delay_auto = "false"
video_hard_sync = "false"
audio_sync = "true"
audio_latency = "64"
run_ahead_enabled = "false"
preemptive_frames_enable = "false"
rewind_enable = "false"
input_overlay_enable = "false"
input_auto_game_focus = "1"
"""
NP2KAI_TUNING = {
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


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


class Client:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
        self.bridge_url = self.cfg["bridge_url"].rstrip("/")
        self.server_url = self.cfg["server_url"].rstrip("/")
        self.token_file = Path(os.path.expanduser(self.cfg["token_file"]))
        self.token = self.token_file.read_text(encoding="utf-8").strip()
        self.platform = self.cfg.get("platform_slug", "pc-9800-series")
        self.data = Path(os.path.expanduser(self.cfg["data_dir"]))
        self.rom_dir = Path(os.path.expanduser(self.cfg["stub_dir"]))
        self.gamelist = Path(os.path.expanduser(self.cfg["gamelist_path"]))
        self.media = Path(os.path.expanduser(self.cfg["media_dir"]))
        self.thumbnails = Path(os.path.expanduser(self.cfg["thumbnail_dir"]))
        self.cache = Path(os.path.expanduser(self.cfg["cache_dir"]))
        self.systems_xml = Path(os.path.expanduser(self.cfg["systems_xml"]))
        self.core = Path(os.path.expanduser(self.cfg["retroarch_core"]))
        self.state_dir = Path(os.path.expanduser(self.cfg["state_dir"]))
        self.retroarch_autoconfig = Path(os.path.expanduser(self.cfg["retroarch_autoconfig"]))
        self.np2kai_options = Path(os.path.expanduser(self.cfg["np2kai_options"]))
        self.np2kai_override = Path(os.path.expanduser(self.cfg["np2kai_override"]))
        self.firmware_dir = Path(os.path.expanduser(
            self.cfg.get("firmware_dir", "~/Emulation/bios")
        ))
        self.retroarch_command = list(
            self.cfg.get("retroarch_command", ["flatpak", "run", "org.libretro.RetroArch"])
        )
        self.runtime_platform = self.cfg.get("runtime_platform", "SteamOS")
        self.steam_deck_tuning = bool(self.cfg.get("steam_deck_tuning", True))
        self.launcher_command = os.path.expanduser(
            self.cfg.get("launcher_command", "~/.local/bin/romm-esde-launch")
        )
        self.esde_launcher = Path(os.path.expanduser(
            self.cfg.get("esde_launcher", "~/Emulation/tools/launchers/es-de/es-de.sh")
        ))
        self.db_path = self.data / "index.sqlite3"
        self.manifest_cache = self.data / f"manifest-{self.platform}.json"
        self.lock_path = self.data / "client.lock"
        self.data.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self._schema()
        paired_device_id = self.cfg.get("paired_device_id")
        if paired_device_id and not self.get_meta("device_id"):
            self.set_meta("device_id", str(paired_device_id))
            self.db.commit()
        self.user_id, self.username = self._bind_user_identity()
        self.user_flags_cache = self.data / f"user-flags-{self.user_id}.json"

    def _schema(self) -> None:
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS games (
              rom_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
              stub_file TEXT NOT NULL, revision TEXT,
              launch_strategy TEXT NOT NULL, canonical_url TEXT NOT NULL,
              canonical_names TEXT NOT NULL, canonical_size INTEGER NOT NULL,
              canonical_files TEXT NOT NULL DEFAULT '[]',
              cached_path TEXT, cache_size INTEGER DEFAULT 0,
              last_access INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS media (
              rom_id INTEGER NOT NULL, kind TEXT NOT NULL, source_url TEXT NOT NULL,
              source_size INTEGER, local_path TEXT NOT NULL,
              PRIMARY KEY (rom_id, kind)
            );
            CREATE TABLE IF NOT EXISTS outbox (
              id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
              created_at INTEGER NOT NULL, attempts INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS state_sync (
              rom_id INTEGER PRIMARY KEY, server_id INTEGER,
              local_hash TEXT, server_updated TEXT,
              screenshot_hash TEXT, server_screenshot_id INTEGER,
              server_screenshot_updated TEXT
            );
            CREATE TABLE IF NOT EXISTS user_sync (
              rom_id INTEGER PRIMARY KEY, favorite INTEGER NOT NULL DEFAULT 0,
              hidden INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(games)")}
        if "canonical_files" not in columns:
            self.db.execute("ALTER TABLE games ADD COLUMN canonical_files TEXT NOT NULL DEFAULT '[]'")
        state_columns = {row[1] for row in self.db.execute("PRAGMA table_info(state_sync)")}
        if "screenshot_hash" not in state_columns:
            self.db.execute("ALTER TABLE state_sync ADD COLUMN screenshot_hash TEXT")
        if "server_screenshot_id" not in state_columns:
            self.db.execute("ALTER TABLE state_sync ADD COLUMN server_screenshot_id INTEGER")
        if "server_screenshot_updated" not in state_columns:
            self.db.execute("ALTER TABLE state_sync ADD COLUMN server_screenshot_updated TEXT")
        self.db.commit()

    @contextlib.contextmanager
    def locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
                return
            if msvcrt is None:
                raise RuntimeError("No supported file locking implementation")
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def request(self, url: str, *, etag: str | None = None, start: int | None = None):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": f"romm-esde/{CLIENT_VERSION}"}
        if etag:
            headers["If-None-Match"] = etag
        if start:
            headers["Range"] = f"bytes={start}-"
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)

    def json_request(self, path: str, method: str = "GET", payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.server_url}{path}", data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": f"romm-esde/{CLIENT_VERSION}",
                "Accept-Encoding": "gzip",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw) if raw else None

    def bridge_json_request(
        self, path: str, method: str = "GET", payload: dict | None = None,
    ):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.bridge_url}{path}", data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/json",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    @contextlib.contextmanager
    def state_upload_lock(self, rom_id: int):
        device_id = self.register_device()
        deadline = time.monotonic() + 45
        lease_id: str | None = None
        while lease_id is None:
            try:
                result = self.bridge_json_request(
                    f"/api/state-locks/{rom_id}", "POST",
                    {"device_id": device_id, "ttl": 45},
                )
                lease_id = result["lease_id"]
            except urllib.error.HTTPError as exc:
                if exc.code != 409 or time.monotonic() >= deadline:
                    raise
                try:
                    retry = min(2, max(1, int(json.loads(exc.read())["retry_after"])))
                except Exception:
                    retry = 1
                time.sleep(retry)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                self.bridge_json_request(
                    f"/api/state-locks/{rom_id}/{lease_id}", "DELETE"
                )

    def _bind_user_identity(self) -> tuple[int, str]:
        cached_id = self.get_meta("romm_user_id")
        cached_name = self.get_meta("romm_username") or "unknown"
        try:
            user = self.json_request("/api/users/me")
        except Exception:
            if cached_id:
                return int(cached_id), cached_name
            raise
        user_id = int(user["id"])
        username = str(user["username"])
        if cached_id and int(cached_id) != user_id:
            raise RuntimeError(
                f"This client data belongs to RomM user {cached_id}; token belongs to {user_id}. "
                "Use a separate data_dir when switching users on one device."
            )
        self.set_meta("romm_user_id", str(user_id))
        self.set_meta("romm_username", username)
        if not self.get_meta("client_instance_id"):
            self.set_meta("client_instance_id", str(uuid.uuid4()))
        self.db.commit()
        return user_id, username

    def multipart_request(
        self, path: str, method: str, fields: dict[str, str],
        files: dict[str, Path] | None = None,
    ):
        boundary = f"romm-esde-{uuid.uuid4().hex}"
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )
        for file_field, file_path in (files or {}).items():
            content_type = "image/png" if file_path.suffix.lower() == ".png" else "application/octet-stream"
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                f"filename=\"{file_path.name}\"\r\nContent-Type: {content_type}\r\n\r\n".encode()
                + file_path.read_bytes() + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{self.server_url}{path}", data=b"".join(parts), method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def register_device(self) -> str:
        current = self.get_meta("device_id")
        instance_id = self.get_meta("client_instance_id") or str(uuid.uuid4())
        hostname = socket.gethostname()
        device_name = self.cfg.get(
            "device_name", f"{self.runtime_platform} · {hostname} · {instance_id[:8]}"
        )
        sync_config = {
            "client_instance_id": instance_id,
            "romm_user_id": self.user_id,
            "romm_username": self.username,
        }
        devices = self.json_request("/api/devices") or []
        if current and any(item["id"] == current for item in devices):
            if self.get_meta("device_identity_version") != "3":
                self.json_request(
                    f"/api/devices/{current}", "PUT",
                    {
                        "name": device_name, "platform": self.runtime_platform, "client": "romm-esde",
                        "client_version": CLIENT_VERSION, "hostname": hostname, "sync_mode": "api",
                        "sync_config": sync_config,
                    },
                )
                self.set_meta("device_identity_version", "3")
                self.db.commit()
            return current
        existing = next(
            (
                item for item in devices
                if (
                    item.get("client_device_identifier") == instance_id
                    or (item.get("sync_config") or {}).get("client_instance_id") == instance_id
                )
            ),
            None,
        )
        if existing:
            self.set_meta("device_id", existing["id"])
            self.db.commit()
            return existing["id"]
        result = self.json_request(
            "/api/devices", "POST",
            {
                "name": device_name, "platform": self.runtime_platform, "client": "romm-esde",
                "client_version": CLIENT_VERSION, "hostname": hostname, "sync_mode": "api",
                "sync_config": sync_config, "allow_existing": False, "allow_duplicate": True,
            },
        )
        self.set_meta("device_id", result["device_id"])
        self.set_meta("device_identity_version", "3")
        self.db.commit()
        return result["device_id"]

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def fetch_manifest(self) -> tuple[dict, bool]:
        url = f"{self.bridge_url}/platforms/{self.platform}/manifest.json.gz"
        etag = self.get_meta("manifest_etag")
        try:
            with self.request(url, etag=etag) as response:
                raw = response.read()
                manifest = json.loads(gzip.decompress(raw))
                if manifest.get("schema") != "romm-esde-platform-v2":
                    raise RuntimeError(f"Unsupported manifest schema: {manifest.get('schema')}")
                atomic_write(self.manifest_cache, json.dumps(manifest, ensure_ascii=False).encode())
                self.set_meta("manifest_etag", response.headers.get("ETag", ""))
                self.set_meta("content_revision", manifest["content_revision"])
                self.db.commit()
                return manifest, True
        except urllib.error.HTTPError as exc:
            if exc.code != 304 or not self.manifest_cache.is_file():
                raise
            return json.loads(self.manifest_cache.read_text(encoding="utf-8")), False

    def _current_user_flags(self, roms: list[dict], platform_id: int) -> dict[int, dict[str, bool]]:
        flags: dict[int, dict[str, bool]] = {}
        for rom in roms:
            rom_id = int(rom["id"])
            base = self.db.execute(
                "SELECT favorite,hidden FROM user_sync WHERE rom_id=?", (rom_id,)
            ).fetchone()
            flags[rom_id] = {
                "favorite": bool(base["favorite"]) if base else False,
                "hidden": bool(base["hidden"]) if base else False,
            }

        try:
            collections = self.json_request("/api/collections") or []
            favorite_ids = {
                int(rom_id)
                for collection in collections
                if collection.get("is_favorite")
                for rom_id in (collection.get("rom_ids") or [])
            }
            for rom_id in flags:
                flags[rom_id]["favorite"] = rom_id in favorite_ids
        except Exception as exc:
            print(f"Favorite refresh deferred: {exc}", file=sys.stderr)

        ttl = int(self.cfg.get("user_flags_ttl_seconds", 900))
        cached: dict = {}
        try:
            cached = json.loads(self.user_flags_cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        hidden_ids: set[int] | None = None
        if (
            cached.get("user_id") == self.user_id
            and cached.get("platform_id") == platform_id
            and time.time() - float(cached.get("fetched_at", 0)) < ttl
        ):
            hidden_ids = {int(value) for value in cached.get("hidden_ids", [])}
        else:
            query = urllib.parse.urlencode(
                {
                    "platform_ids": platform_id, "with_files": "false",
                    "with_char_index": "false", "with_filter_values": "false",
                    "order_by": "id", "order_dir": "asc", "limit": 10_000, "offset": 0,
                }
            )
            try:
                page = self.json_request(f"/api/roms?{query}")
                if int(page.get("total", 0)) > len(page.get("items") or []):
                    raise RuntimeError("User ROM overlay exceeded one page")
                visible_ids = {int(item["id"]) for item in (page.get("items") or [])}
                hidden_ids = (set(flags) - visible_ids) | {
                    int(item["id"])
                    for item in (page.get("items") or [])
                    if bool((item.get("rom_user") or {}).get("hidden"))
                }
                atomic_write(
                    self.user_flags_cache,
                    json.dumps(
                        {
                            "user_id": self.user_id, "platform_id": platform_id,
                            "fetched_at": time.time(), "hidden_ids": sorted(hidden_ids),
                        }, separators=(",", ":"),
                    ).encode(),
                    0o600,
                )
            except Exception as exc:
                print(f"Hidden-state refresh deferred: {exc}", file=sys.stderr)
                if cached.get("user_id") == self.user_id:
                    hidden_ids = {int(value) for value in cached.get("hidden_ids", [])}
        if hidden_ids is not None:
            for rom_id in flags:
                flags[rom_id]["hidden"] = rom_id in hidden_ids
        return flags

    def sync(self) -> tuple[int, bool]:
        with self.locked():
            self.ensure_input_config()
            manifest, changed = self.fetch_manifest()
            roms = manifest["roms"]
            user_flags = self._current_user_flags(roms, int(manifest["platform"]["id"]))
            for rom in roms:
                current = user_flags[int(rom["id"])]
                rom["is_favorite"] = current["favorite"]
                rom["rom_user"] = {"hidden": current["hidden"]}
            valid: set[str] = set()
            self.rom_dir.mkdir(parents=True, exist_ok=True)
            for rom in roms:
                bridge = rom["bridge"]
                stub_name = bridge["stub_file"]
                valid.add(stub_name)
                stub = {
                    "schema": "romm-esde-stub-v2",
                    "rom_id": rom["id"],
                    "platform_slug": self.platform,
                    "name": rom["name"],
                    "content_revision": manifest["content_revision"],
                }
                target = self.rom_dir / stub_name
                encoded = (json.dumps(stub, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                if not target.is_file() or target.read_bytes() != encoded:
                    atomic_write(target, encoded)
                self.db.execute(
                    """INSERT INTO games
                    (rom_id,name,stub_file,revision,launch_strategy,canonical_url,
                     canonical_names,canonical_size,canonical_files)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(rom_id) DO UPDATE SET
                    name=excluded.name,stub_file=excluded.stub_file,revision=excluded.revision,
                    launch_strategy=excluded.launch_strategy,canonical_url=excluded.canonical_url,
                    canonical_names=excluded.canonical_names,canonical_size=excluded.canonical_size,
                    canonical_files=excluded.canonical_files""",
                    (
                        rom["id"], rom["name"], stub_name, rom.get("updated_at"),
                        bridge["launch_strategy"], bridge["canonical_download_url"],
                        json.dumps(bridge["canonical_file_names"], ensure_ascii=False),
                        bridge["canonical_size_bytes"],
                        json.dumps(
                            [
                                {
                                    "name": item["file_name"], "size": item.get("file_size_bytes"),
                                    "sha1": item.get("sha1_hash"), "md5": item.get("md5_hash"),
                                    "hash_scope": (
                                        "archive_single_member"
                                        if item["file_name"].lower().endswith(".zip")
                                        and rom.get("has_nested_single_file")
                                        else "file"
                                    ),
                                }
                                for item in (rom.get("files") or [])
                                if int(item["id"]) in {int(value) for value in bridge["canonical_file_ids"]}
                            ], ensure_ascii=False,
                        ),
                    ),
                )
                self._record_media(rom)
            for old in self.rom_dir.glob("romm-*.romm"):
                if old.name not in valid:
                    old.unlink()
            ids = [int(r["id"]) for r in roms]
            if ids:
                marks = ",".join("?" for _ in ids)
                self.db.execute(f"DELETE FROM games WHERE rom_id NOT IN ({marks})", ids)
            # ES-DE keeps the gamelist in memory and may overwrite external
            # edits on exit. While it is running, only push its own saved user
            # changes; defer server-to-Deck edits until ES-DE has stopped.
            if self._esde_running():
                self._push_local_user_flags_unlocked()
            else:
                flags = self._reconcile_user_flags(roms)
                self._write_gamelist(roms, flags)
            self.set_meta("last_sync", dt.datetime.now(dt.timezone.utc).isoformat())
            self.db.commit()
            self.register_device()
            self.flush_outbox()
            return len(roms), changed

    def flush_outbox(self) -> int:
        rows = self.db.execute("SELECT id,kind,payload FROM outbox ORDER BY id LIMIT 100").fetchall()
        sent = 0
        for row in rows:
            try:
                if row["kind"] == "play_session":
                    self.json_request(
                        "/api/play-sessions", "POST",
                        {"device_id": self.register_device(), "sessions": [json.loads(row["payload"])]},
                    )
                else:
                    continue
            except Exception:
                self.db.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (row["id"],))
                continue
            self.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
            sent += 1
        self.db.commit()
        return sent

    def _record_media(self, rom: dict) -> None:
        bridge = rom["bridge"]
        candidates = [
            ("cover", bridge.get("cover_large"), self.media / "covers" / f"romm-{rom['id']}.png"),
            ("thumbnail", bridge.get("cover_small"), self.thumbnails / f"romm-{rom['id']}.png"),
        ]
        shots = bridge.get("screenshots") or []
        if shots:
            candidates.append(("screenshot", shots[0], self.media / "screenshots" / f"romm-{rom['id']}.jpg"))
        for kind, item, path in candidates:
            if not item:
                continue
            self.db.execute(
                """INSERT INTO media(rom_id,kind,source_url,source_size,local_path)
                VALUES(?,?,?,?,?) ON CONFLICT(rom_id,kind) DO UPDATE SET
                source_url=excluded.source_url,source_size=excluded.source_size,
                local_path=excluded.local_path""",
                (rom["id"], kind, item["url"], item.get("size_bytes"), str(path)),
            )

    def _existing_user_fields(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        if not self.gamelist.is_file():
            return result
        try:
            root = ET.parse(self.gamelist).getroot()
        except ET.ParseError:
            return result
        for game in root.findall("game"):
            path = game.findtext("path")
            if path:
                result[path] = {
                    key: value for key in USER_FIELDS if (value := game.findtext(key)) is not None
                }
        return result

    def _favorite_collection(self, create: bool) -> int | None:
        saved = self.get_meta("favorite_collection_id")
        collections = self.json_request("/api/collections") or []
        if saved and any(int(item["id"]) == int(saved) for item in collections):
            return int(saved)
        favorite = next((item for item in collections if item.get("is_favorite")), None)
        if not favorite and create:
            favorite = self.multipart_request(
                "/api/collections?is_favorite=true", "POST",
                {"name": "ES-DE Favorites", "description": "Synced from ES-DE by romm-esde"},
            )
        if favorite:
            self.set_meta("favorite_collection_id", str(favorite["id"]))
            self.db.commit()
            return int(favorite["id"])
        return None

    def _set_favorite(self, rom_id: int, value: bool) -> None:
        collection_id = self._favorite_collection(create=value)
        if collection_id is None:
            return
        self.json_request(
            f"/api/collections/{collection_id}/roms", "POST" if value else "DELETE",
            {"rom_ids": [rom_id]},
        )

    def _reconcile_user_flags(self, roms: list[dict]) -> dict[int, dict[str, bool]]:
        local_fields = self._existing_user_fields()
        effective: dict[int, dict[str, bool]] = {}
        for rom in roms:
            rom_id = int(rom["id"])
            path = f"./{rom['bridge']['stub_file']}"
            local = {
                "favorite": local_fields.get(path, {}).get("favorite", "false").lower() == "true",
                "hidden": local_fields.get(path, {}).get("hidden", "false").lower() == "true",
            }
            server = {
                "favorite": bool(rom.get("is_favorite")),
                "hidden": bool((rom.get("rom_user") or {}).get("hidden")),
            }
            base_row = self.db.execute("SELECT favorite,hidden FROM user_sync WHERE rom_id=?", (rom_id,)).fetchone()
            if base_row is None:
                chosen = {key: local[key] if key in local_fields.get(path, {}) else server[key] for key in local}
            else:
                base = {"favorite": bool(base_row["favorite"]), "hidden": bool(base_row["hidden"])}
                chosen = {}
                for key in local:
                    if local[key] != base[key]:
                        chosen[key] = local[key]
                        if local[key] != server[key]:
                            if key == "favorite":
                                self._set_favorite(rom_id, local[key])
                            else:
                                self.json_request(f"/api/roms/{rom_id}/props", "PUT", {"hidden": local[key]})
                    else:
                        chosen[key] = server[key]
            effective[rom_id] = chosen
            self.db.execute(
                """INSERT INTO user_sync(rom_id,favorite,hidden) VALUES(?,?,?)
                ON CONFLICT(rom_id) DO UPDATE SET favorite=excluded.favorite,hidden=excluded.hidden""",
                (rom_id, int(chosen["favorite"]), int(chosen["hidden"])),
            )
        return effective

    @staticmethod
    def _esde_running() -> bool:
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["tasklist", "/FO", "CSV", "/NH"], check=False,
                    capture_output=True, text=True, timeout=10,
                )
                names = result.stdout.casefold()
                return '"es-de.exe"' in names or '"es-de"' in names
            except (OSError, subprocess.SubprocessError):
                return False
        for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                args = cmdline.read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            names = {Path(os.fsdecode(arg)).name for arg in args if arg}
            if "es-de" in names or "ES-DE.AppImage" in names:
                return True
        return False

    def _push_local_user_flags_unlocked(self) -> int:
        """Push ES-DE favorite/hidden edits without waiting for a catalog sync.

        Only fields differing from the last synchronized baseline are sent. The
        cached manifest is deliberately ignored because it can be stale just
        after a successful push.
        """
        local_fields = self._existing_user_fields()
        rows = self.db.execute(
            """SELECT g.rom_id,g.stub_file,u.favorite,u.hidden
            FROM games AS g JOIN user_sync AS u ON u.rom_id=g.rom_id"""
        ).fetchall()
        changed = 0
        for row in rows:
            fields = local_fields.get(f"./{row['stub_file']}", {})
            desired = {
                "favorite": fields.get("favorite", "false").lower() == "true",
                "hidden": fields.get("hidden", "false").lower() == "true",
            }
            current = {
                "favorite": bool(row["favorite"]),
                "hidden": bool(row["hidden"]),
            }
            rom_id = int(row["rom_id"])
            for key in ("favorite", "hidden"):
                if desired[key] == current[key]:
                    continue
                if key == "favorite":
                    self._set_favorite(rom_id, desired[key])
                else:
                    self.json_request(
                        f"/api/roms/{rom_id}/props", "PUT", {"hidden": desired[key]}
                    )
                self.db.execute(
                    f"UPDATE user_sync SET {key}=? WHERE rom_id=?",
                    (int(desired[key]), rom_id),
                )
                changed += 1
        self.db.commit()
        return changed

    def push_local_user_flags(self) -> int:
        with self.locked():
            return self._push_local_user_flags_unlocked()

    def _write_gamelist(self, roms: list[dict], flags: dict[int, dict[str, bool]]) -> None:
        preserved = self._existing_user_fields()
        root = ET.Element("gameList")
        for rom in sorted(roms, key=lambda r: r.get("name_sort_key") or r["name"].casefold()):
            path = f"./{rom['bridge']['stub_file']}"
            game = ET.SubElement(root, "game")
            values = {
                "path": path, "name": rom["name"], "sortname": rom.get("name_sort_key"),
                "desc": rom.get("summary"),
                # ES-DE accepts forward slashes on every supported platform.
                # Native Windows backslashes in absolute metadata paths are
                # not consistently resolved by its resource loader.
                "image": (self.media / "covers" / f"romm-{rom['id']}.png").as_posix(),
                "thumbnail": (self.thumbnails / f"romm-{rom['id']}.png").as_posix(),
                "screenshot": (self.media / "screenshots" / f"romm-{rom['id']}.jpg").as_posix(),
            }
            metadata = rom.get("metadatum") or {}
            if metadata.get("genres"):
                values["genre"] = ", ".join(metadata["genres"])
            if metadata.get("companies"):
                values["developer"] = ", ".join(dict.fromkeys(metadata["companies"]))
            if metadata.get("player_count"):
                values["players"] = str(metadata["player_count"])
            for tag, value in values.items():
                if value:
                    ET.SubElement(game, tag).text = str(value)
            for tag, value in preserved.get(path, {}).items():
                if tag in ("favorite", "hidden"):
                    continue
                node = game.find(tag) or ET.SubElement(game, tag)
                node.text = value
            for tag in ("favorite", "hidden"):
                if flags[int(rom["id"])][tag]:
                    ET.SubElement(game, tag).text = "true"
        ET.indent(root, space="  ")
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
        if not self.gamelist.is_file() or self.gamelist.read_bytes() != data:
            atomic_write(self.gamelist, data)

    def sync_media(self, workers: int = 4, limit: int = 0) -> tuple[int, int]:
        rows = self.db.execute(
            "SELECT rom_id,kind,source_url,source_size,local_path FROM media ORDER BY rom_id,kind"
        ).fetchall()
        if limit:
            rows = rows[:limit]
        def one(row):
            path = Path(row["local_path"])
            expected = row["source_size"]
            if path.is_file() and (not expected or path.stat().st_size == expected):
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            with self.request(row["source_url"]) as response:
                atomic_write(path, response.read())
            if expected and path.stat().st_size != expected:
                path.unlink(missing_ok=True)
                raise RuntimeError(f"Size mismatch for {path}")
            return True
        done = failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, row): row for row in rows}
            for future in concurrent.futures.as_completed(futures):
                try:
                    done += int(future.result())
                except Exception as exc:
                    failed += 1
                    print(f"media error: {exc}", file=sys.stderr)
        return done, failed

    def repair_system(self) -> bool:
        input_changed = self.ensure_input_config()
        launcher_changed = self.ensure_esde_launcher()
        self.systems_xml.parent.mkdir(parents=True, exist_ok=True)
        if not self.systems_xml.exists():
            atomic_write(self.systems_xml, b'<?xml version="1.0"?>\n<systemList>\n</systemList>\n')
        text = self.systems_xml.read_text(encoding="utf-8")
        if "<name>romm-pc98</name>" in text:
            return input_changed or launcher_changed
        marker = "</systemList>"
        if marker not in text:
            raise RuntimeError(f"Invalid ES-DE systems XML: {self.systems_xml}")
        backup = self.systems_xml.with_suffix(f".xml.romm-{int(time.time())}.bak")
        shutil.copy2(self.systems_xml, backup)
        xml_launcher = (
            self.launcher_command.replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;")
        )
        system_xml = SYSTEM_XML.format(launcher=xml_launcher)
        atomic_write(self.systems_xml, text.replace(marker, system_xml + marker).encode())
        return True

    def ensure_esde_launcher(self) -> bool:
        """Wrap ES-DE in the on-demand catalog/media/user-sync session."""
        if not self.esde_launcher.is_file():
            return False
        text = self.esde_launcher.read_text(encoding="utf-8")
        marker = '"$ESDE_toolPath" "${@}"'
        client_command = str(Path(self.launcher_command).with_name("romm-esde-client"))
        managed = f'{client_command} session -- "$ESDE_toolPath" "${{@}}"'
        if managed in text:
            return False
        sync_command = str(Path(self.launcher_command).with_name("romm-esde-sync"))
        previous = f"{sync_command} || true\n{marker}\n{sync_command} || true"
        replaceable = previous if previous in text else marker
        if replaceable not in text:
            raise RuntimeError(f"Unrecognized ES-DE launcher: {self.esde_launcher}")
        backup = self.esde_launcher.with_suffix(f".sh.romm-{int(time.time())}.bak")
        shutil.copy2(self.esde_launcher, backup)
        atomic_write(self.esde_launcher, text.replace(replaceable, managed, 1).encode(), 0o755)
        return True

    def esde_session(self, command: list[str]) -> int:
        """Synchronize around ES-DE and watch user metadata only while it runs."""
        if not command:
            raise RuntimeError("ES-DE session command is empty")
        try:
            count, changed = self.sync()
            print(f"Pre-session catalog: {count} games (changed={changed})")
        except Exception as exc:
            print(f"Pre-session catalog deferred: {exc}", file=sys.stderr)
        try:
            done, failed = self.sync_media(workers=4)
            print(f"Pre-session media: downloaded={done}; failures={failed}")
        except Exception as exc:
            print(f"Pre-session media deferred: {exc}", file=sys.stderr)

        process = subprocess.Popen(command, env=os.environ.copy())
        previous: tuple[int, int] | None = None
        pending_since: float | None = None
        while process.poll() is None:
            try:
                stat = self.gamelist.stat()
                current = (stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                current = (0, 0)
            if previous is None:
                previous = current
            elif current != previous:
                previous = current
                pending_since = time.monotonic()
            if pending_since is not None and time.monotonic() - pending_since >= 1.0:
                try:
                    print(f"Mid-session user sync: {self.push_local_user_flags()} changes")
                except Exception as exc:
                    print(f"Mid-session user sync deferred: {exc}", file=sys.stderr)
                pending_since = None
            time.sleep(0.5)
        try:
            print(f"Post-session user sync: {self.push_local_user_flags()} changes")
        except Exception as exc:
            print(f"Post-session user sync deferred: {exc}", file=sys.stderr)
        return int(process.returncode or 0)

    def ensure_input_config(self) -> bool:
        changed = False
        if self.steam_deck_tuning:
            encoded = STEAM_DECK_AUTOCONFIG.encode()
            if not self.retroarch_autoconfig.is_file() or self.retroarch_autoconfig.read_bytes() != encoded:
                atomic_write(self.retroarch_autoconfig, encoded)
                changed = True
        if self.steam_deck_tuning:
            override = NP2KAI_RETROARCH_OVERRIDE.encode()
            if not self.np2kai_override.is_file() or self.np2kai_override.read_bytes() != override:
                atomic_write(self.np2kai_override, override)
                changed = True
        if self.np2kai_options.is_file():
            text = self.np2kai_options.read_text(encoding="utf-8")
            lines = text.splitlines()
            found: set[str] = set()
            tuned: list[str] = []
            for line in lines:
                match = re.match(r"^(np2kai_[A-Za-z0-9_]+)\s*=", line)
                if match and match.group(1) in NP2KAI_TUNING:
                    key = match.group(1)
                    tuned.append(f'{key} = "{NP2KAI_TUNING[key]}"')
                    found.add(key)
                else:
                    tuned.append(line)
            for key, value in NP2KAI_TUNING.items():
                if key not in found:
                    tuned.append(f'{key} = "{value}"')
            tuned_text = "\n".join(tuned) + "\n"
            if tuned_text != text:
                backup = self.np2kai_options.with_suffix(
                    f".opt.romm-{int(time.time())}.bak"
                )
                shutil.copy2(self.np2kai_options, backup)
                atomic_write(self.np2kai_options, tuned_text.encode())
                changed = True
        else:
            text = "\n".join(
                f'{key} = "{value}"' for key, value in NP2KAI_TUNING.items()
            ) + "\n"
            atomic_write(self.np2kai_options, text.encode())
            changed = True
        return changed

    def sync_firmware(self) -> tuple[int, int]:
        """Download this platform's RomM firmware into RetroArch's system dir."""
        manifest, _ = self.fetch_manifest()
        target_dir = (
            self.firmware_dir / "np2kai"
            if self.platform == "pc-9800-series"
            else self.firmware_dir
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        np2kai_names = {
            "font.rom": "FONT.ROM",
            **{f"2608_{part.lower()}.wav": f"2608_{part}.WAV" for part in (
                "BD", "SD", "TOP", "HH", "TOM", "RIM"
            )},
        }
        downloaded = skipped = 0
        for item in manifest.get("firmware", []):
            file_name = Path(item["file_name"]).name
            file_name = np2kai_names.get(file_name.lower(), file_name)
            target = target_dir / file_name
            expected = int(item.get("file_size_bytes") or 0)
            if target.is_file() and (not expected or target.stat().st_size == expected):
                skipped += 1
                continue
            with self.request(item["download_url"]) as response:
                atomic_write(target, response.read())
            if expected and target.stat().st_size != expected:
                target.unlink(missing_ok=True)
                raise RuntimeError(f"Firmware size mismatch: {target}")
            downloaded += 1
        return downloaded, skipped

    def _cache_target(self, row: sqlite3.Row) -> Path:
        names = json.loads(row["canonical_names"])
        suffix = Path(names[0]).suffix if len(names) == 1 else ".zip"
        return self.cache / f"romm-{row['rom_id']}{suffix.lower()}"

    def _multidisk_launch_path(
        self, row: sqlite3.Row, archive_path: Path, canonical_files: list[dict]
    ) -> Path:
        disk_dir = self.cache / f"romm-{row['rom_id']}-disks"
        disk_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            available = {Path(name).name: name for name in archive.namelist() if not name.endswith("/")}
            for item in canonical_files:
                name = item["name"]
                target = disk_dir / name
                needs_extract = not target.is_file() or (
                    item.get("size") and target.stat().st_size != int(item["size"])
                )
                if needs_extract:
                    member = available.get(name)
                    if member is None:
                        raise RuntimeError(f"Missing canonical disk in ZIP: {name}")
                    with archive.open(member) as source:
                        atomic_write(target, source.read())
                self._validate_direct(target, [{**item, "hash_scope": "file"}])
        first = disk_dir / canonical_files[0]["name"]
        stable = disk_dir / f"romm-{row['rom_id']}{first.suffix.lower()}"
        desired = first.name
        if os.name == "nt":
            if not stable.is_file() or stable.stat().st_size != first.stat().st_size:
                stable.unlink(missing_ok=True)
                try:
                    os.link(first, stable)
                except OSError:
                    shutil.copy2(first, stable)
            return stable
        if not stable.is_symlink() or os.readlink(stable) != desired:
            stable.unlink(missing_ok=True)
            stable.symlink_to(desired)
        return stable

    def _nested_archive_launch_path(self, row: sqlite3.Row, archive_path: Path) -> Path:
        """Extract nested RomM ZIPs and create NP2Kai's multi-drive command.

        RetroArch expands a ZIP before giving it to cores, but only passes one
        member.  PC-98 titles such as 4D Boxing/Driving need Disk A and Disk B
        mounted together during boot.  NP2Kai's .cmd format supplies all images
        to the core and keeps the remaining images available for disk control.
        """
        disk_dir = self.cache / f"romm-{row['rom_id']}-disks"
        disk_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[Path] = []
        seen: set[str] = set()
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or Path(entry.filename).suffix.lower() not in NP2KAI_DISK_EXTENSIONS:
                    continue
                name = Path(entry.filename).name
                if not name or name in seen or '"' in name:
                    raise RuntimeError(f"Unsafe or duplicate disk name in ZIP: {name!r}")
                seen.add(name)
                target = disk_dir / name
                if not target.is_file() or target.stat().st_size != entry.file_size:
                    with archive.open(entry) as source:
                        atomic_write(target, source.read())
                extracted.append(target)
        if not extracted:
            raise RuntimeError(f"No NP2Kai disk images inside {archive_path}")
        if len(extracted) == 1:
            return extracted[0]
        # NP2Kai mounts the first two floppy arguments into FDD1/FDD2. Games
        # shipped with a writable User/Save disk generally need that disk in
        # FDD2 during boot; later content disks remain available for swapping.
        primary = extracted[0]
        original_order = {path: index for index, path in enumerate(extracted)}

        def boot_order(item: Path) -> tuple[int, int]:
            lowered = item.stem.lower()
            writable = any(marker in lowered for marker in (
                "user", "save", "system", "ユーザー", "セーブ", "システム",
            ))
            return (0 if item == primary else 1 if writable else 2, original_order[item])

        extracted.sort(key=boot_order)
        command = disk_dir / f"romm-{row['rom_id']}.cmd"
        line = "np2kai " + " ".join(f'\"{path.name}\"' for path in extracted) + "\r\n"
        atomic_write(command, line.encode("utf-8"))
        return command

    def _launch_path(self, row: sqlite3.Row, target: Path, canonical_files: list[dict]) -> Path:
        if row["launch_strategy"] == "canonical_multidisk":
            return self._multidisk_launch_path(row, target, canonical_files)
        if (
            target.suffix.lower() == ".zip"
            and canonical_files
            and canonical_files[0].get("hash_scope") == "archive_single_member"
        ):
            return self._nested_archive_launch_path(row, target)
        return target

    @staticmethod
    def _validate_multidisk_zip(path: Path, canonical_files: list[dict]) -> None:
        """Accept the selected disks plus RomM's generated M3U helper only."""
        with zipfile.ZipFile(path) as archive:
            actual = {Path(name).name for name in archive.namelist() if not name.endswith("/")}
            wanted = {item["name"] for item in canonical_files}
            extras = actual - wanted
            missing = wanted - actual
            if missing or any(not name.lower().endswith(".m3u") for name in extras) or len(extras) > 1:
                raise RuntimeError(f"Canonical ZIP contents differ: missing={missing}, extras={extras}")
            if extras:
                playlist = archive.read(next(iter(extras))).decode(errors="replace").splitlines()
                if {Path(line.strip()).name for line in playlist if line.strip()} != wanted:
                    raise RuntimeError("RomM M3U does not reference exactly the canonical disks")
            for item in canonical_files:
                with archive.open(item["name"]) as handle:
                    sha1 = hashlib.sha1()
                    md5 = hashlib.md5(usedforsecurity=False)
                    size = 0
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        size += len(chunk); sha1.update(chunk); md5.update(chunk)
                if item.get("size") and size != int(item["size"]):
                    raise RuntimeError(f"Size mismatch inside ZIP for {item['name']}")
                if item.get("sha1") and sha1.hexdigest() != item["sha1"]:
                    raise RuntimeError(f"SHA-1 mismatch inside ZIP for {item['name']}")
                if item.get("md5") and md5.hexdigest() != item["md5"]:
                    raise RuntimeError(f"MD5 mismatch inside ZIP for {item['name']}")

    @staticmethod
    def _validate_direct(path: Path, canonical_files: list[dict]) -> None:
        item = canonical_files[0]
        if item.get("size") and path.stat().st_size != int(item["size"]):
            raise RuntimeError(f"Size mismatch for {path}")
        archive = None
        if item.get("hash_scope") == "archive_single_member":
            archive = zipfile.ZipFile(path)
            members = [entry for entry in archive.infolist() if not entry.is_dir()]
            if not members:
                archive.close()
                raise RuntimeError(f"Expected at least one file inside {path}")
            # RomM hashes the primary (first) member of nested archives.  The
            # archive may still contain additional disks; NP2Kai consumes the
            # complete ZIP and exposes those through its disk controls.
            source = archive.open(members[0])
        else:
            source = path.open("rb")
        checks = (("sha1", hashlib.sha1), ("md5", lambda: hashlib.md5(usedforsecurity=False)))
        try:
            for key, factory in checks:
                if not item.get(key):
                    continue
                digest = factory()
                source.seek(0)
                handle = source
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != item[key]:
                    raise RuntimeError(f"{key.upper()} mismatch for {path}")
        finally:
            source.close()
            if archive:
                archive.close()

    def ensure_cached(self, rom_id: int) -> Path:
        row = self.db.execute("SELECT * FROM games WHERE rom_id=?", (rom_id,)).fetchone()
        if not row:
            self.sync()
            row = self.db.execute("SELECT * FROM games WHERE rom_id=?", (rom_id,)).fetchone()
        if not row:
            raise RuntimeError(f"ROM {rom_id} is absent from the current catalog")
        if row["launch_strategy"] == "manual_selection_required":
            raise RuntimeError("RomM has no canonical game file for this title")
        target = self._cache_target(row)
        part = target.with_suffix(target.suffix + ".part")
        expected = row["canonical_size"] if row["launch_strategy"] == "direct_file" else 0
        canonical_files = json.loads(row["canonical_files"])
        if target.is_file() and (not expected or target.stat().st_size == expected):
            if row["launch_strategy"] == "canonical_multidisk":
                self._validate_multidisk_zip(target, canonical_files)
            elif canonical_files:
                self._validate_direct(target, canonical_files)
            self._touch_cache(row["rom_id"], target)
            return self._launch_path(row, target, canonical_files)
        if row["launch_strategy"] == "canonical_multidisk" and part.is_file():
            try:
                self._validate_multidisk_zip(part, canonical_files)
            except (OSError, zipfile.BadZipFile, RuntimeError):
                pass
            else:
                os.replace(part, target)
                self._touch_cache(row["rom_id"], target)
                return self._launch_path(row, target, canonical_files)
        if row["launch_strategy"] == "direct_file" and part.is_file() and (
            not expected or part.stat().st_size == expected
        ):
            self._validate_direct(part, canonical_files)
            os.replace(part, target)
            self._touch_cache(row["rom_id"], target)
            return self._launch_path(row, target, canonical_files)
        start = part.stat().st_size if part.is_file() else 0
        with self.request(row["canonical_url"], start=start) as response:
            append = start > 0 and response.status == 206
            if start and not append:
                start = 0
            with part.open("ab" if append else "wb") as handle:
                shutil.copyfileobj(response, handle, 1024 * 1024)
        if expected and part.stat().st_size != expected:
            raise RuntimeError(f"Downloaded {part.stat().st_size} bytes, expected {expected}")
        if row["launch_strategy"] == "canonical_multidisk":
            self._validate_multidisk_zip(part, canonical_files)
        elif canonical_files:
            self._validate_direct(part, canonical_files)
        os.replace(part, target)
        self._touch_cache(row["rom_id"], target)
        self.prune(exclude=target)
        return self._launch_path(row, target, canonical_files)

    def _touch_cache(self, rom_id: int, path: Path) -> None:
        now = int(time.time())
        os.utime(path, (now, now))
        self.db.execute(
            "UPDATE games SET cached_path=?,cache_size=?,last_access=? WHERE rom_id=?",
            (str(path), path.stat().st_size, now, rom_id),
        )
        self.db.commit()

    def prune(self, exclude: Path | None = None) -> int:
        max_bytes = int(self.cfg.get("cache_max_bytes", 50 * 1024**3))
        min_free = float(self.cfg.get("min_free_percent", 20.0))
        rows = self.db.execute(
            "SELECT rom_id,cached_path,cache_size FROM games WHERE cached_path IS NOT NULL AND pinned=0 ORDER BY last_access"
        ).fetchall()
        total = sum(int(r["cache_size"] or 0) for r in rows)
        removed = 0
        for row in rows:
            usage = shutil.disk_usage(self.cache)
            free_pct = usage.free * 100 / usage.total
            if total <= max_bytes and free_pct >= min_free:
                break
            path = Path(row["cached_path"])
            if exclude and path == exclude:
                continue
            size = path.stat().st_size if path.exists() else 0
            path.unlink(missing_ok=True)
            disk_dir = self.cache / f"romm-{row['rom_id']}-disks"
            if disk_dir.is_dir():
                shutil.rmtree(disk_dir)
            self.db.execute(
                "UPDATE games SET cached_path=NULL,cache_size=0 WHERE rom_id=?", (row["rom_id"],)
            )
            total -= size
            removed += 1
        self.db.commit()
        return removed

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _state_path(self, rom_id: int) -> Path:
        return self.state_dir / f"romm-{rom_id}.state.auto"

    def _state_screenshot_path(self, rom_id: int) -> Path:
        state = self._state_path(rom_id)
        return state.with_name(state.name + ".png")

    def _download_state(self, state_id: int, target: Path) -> None:
        with self.request(f"{self.server_url}/api/states/{state_id}/content") as response:
            atomic_write(target, response.read())

    def _download_state_screenshot(self, screenshot: dict, target: Path) -> None:
        # RomM 5's download_path timestamp contains an unescaped space. Use
        # the stable ID endpoint directly so urllib never receives an invalid URL.
        path = f"/api/screenshots/{screenshot['id']}/content"
        with self.request(f"{self.server_url}{path}") as response:
            data = response.read()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("RomM state screenshot is not a PNG")
        atomic_write(target, data)

    def _pull_state_screenshot(
        self, rom_id: int, remote: dict, state_action: str,
    ) -> tuple[str | None, int | None, str | None, str]:
        screenshot = remote.get("screenshot")
        local = self._state_screenshot_path(rom_id)
        if not screenshot or screenshot.get("missing_from_fs"):
            return None, None, None, "none"
        incoming = local.with_name(local.name + ".incoming")
        self._download_state_screenshot(screenshot, incoming)
        incoming_hash = self._sha256(incoming)
        screenshot_id = int(screenshot["id"])
        screenshot_updated = screenshot.get("updated_at")
        if local.is_file() and self._sha256(local) == incoming_hash:
            incoming.unlink()
            return incoming_hash, screenshot_id, screenshot_updated, "equal"
        if not local.is_file():
            os.replace(incoming, local)
            return incoming_hash, screenshot_id, screenshot_updated, "downloaded"

        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        remote_wins = state_action in ("downloaded", "server-won-local-preserved")
        if state_action == "equal":
            remote_time = dt.datetime.fromisoformat(
                (screenshot_updated or remote["updated_at"]).replace("Z", "+00:00")
            ).timestamp()
            remote_wins = remote_time > local.stat().st_mtime
        if remote_wins:
            shutil.copy2(local, local.with_name(local.name + f".conflict-deck-{stamp}"))
            os.replace(incoming, local)
            return incoming_hash, screenshot_id, screenshot_updated, "server-won-local-preserved"
        os.replace(incoming, local.with_name(local.name + f".conflict-server-{stamp}"))
        return None, screenshot_id, screenshot_updated, "local-won-server-preserved"

    @staticmethod
    def _canonical_remote_state(rom_id: int, states: list[dict]) -> dict | None:
        available = [item for item in states if not item.get("missing_from_fs")]
        exact_name = f"romm-{rom_id}.state.auto"
        canonical = [item for item in available if item.get("file_name") == exact_name]
        candidates = canonical or available
        return max(candidates, key=lambda item: item.get("updated_at") or "") if candidates else None

    @staticmethod
    def _remote_state_revision(remote: dict | None) -> tuple:
        if not remote:
            return (None, None, None, None)
        screenshot = remote.get("screenshot") or {}
        return (
            int(remote["id"]), remote.get("updated_at"),
            screenshot.get("id"), screenshot.get("updated_at"),
        )

    def _recorded_state_revision(self, record: sqlite3.Row | None) -> tuple:
        if not record:
            return (None, None, None, None)
        return (
            record["server_id"], record["server_updated"],
            record["server_screenshot_id"], record["server_screenshot_updated"],
        )

    def _archive_remote_state(self, rom_id: int, remote: dict) -> int:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        device = (self.get_meta("client_instance_id") or "device")[:8]
        with tempfile.TemporaryDirectory(prefix="romm-esde-conflict-") as directory:
            state = Path(directory) / f"romm-{rom_id}.conflict-server-before-{device}-{stamp}.state.auto"
            self._download_state(int(remote["id"]), state)
            screenshot_path = None
            if remote.get("screenshot") and not remote["screenshot"].get("missing_from_fs"):
                screenshot_path = state.with_name(state.name + ".png")
                self._download_state_screenshot(remote["screenshot"], screenshot_path)
            archived = self._upload_state(rom_id, state, screenshot_path, None, True)
            return int(archived["id"])

    def pull_state(self, rom_id: int) -> str:
        states = self.json_request(f"/api/states?rom_id={rom_id}") or []
        remote = self._canonical_remote_state(rom_id, states)
        if not remote:
            return "none"
        local = self._state_path(rom_id)
        local.parent.mkdir(parents=True, exist_ok=True)
        incoming = local.with_name(local.name + ".incoming")
        self._download_state(int(remote["id"]), incoming)
        incoming_hash = self._sha256(incoming)
        if local.is_file() and self._sha256(local) == incoming_hash:
            incoming.unlink()
            action = "equal"
        elif not local.is_file():
            os.replace(incoming, local)
            action = "downloaded"
        else:
            server_time = dt.datetime.fromisoformat(remote["updated_at"].replace("Z", "+00:00")).timestamp()
            stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            if server_time > local.stat().st_mtime:
                shutil.copy2(local, local.with_name(local.name + f".conflict-deck-{stamp}"))
                os.replace(incoming, local)
                action = "server-won-local-preserved"
            else:
                os.replace(incoming, local.with_name(local.name + f".conflict-server-{stamp}"))
                action = "local-won-server-preserved"
                incoming_hash = None
        screenshot_hash, screenshot_id, screenshot_updated, screenshot_action = (
            self._pull_state_screenshot(rom_id, remote, action)
        )
        self.db.execute(
            """INSERT INTO state_sync
            (rom_id,server_id,local_hash,server_updated,screenshot_hash,
             server_screenshot_id,server_screenshot_updated)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(rom_id) DO UPDATE SET
            server_id=excluded.server_id,
            local_hash=COALESCE(excluded.local_hash,state_sync.local_hash),
            server_updated=excluded.server_updated,
            screenshot_hash=excluded.screenshot_hash,
            server_screenshot_id=excluded.server_screenshot_id,
            server_screenshot_updated=excluded.server_screenshot_updated""",
            (
                rom_id, remote["id"], incoming_hash, remote["updated_at"],
                screenshot_hash, screenshot_id, screenshot_updated,
            ),
        )
        self.db.commit()
        return f"{action}; screenshot={screenshot_action}"

    def _upload_state(
        self, rom_id: int, path: Path, screenshot: Path | None,
        server_id: int | None, state_changed: bool,
    ):
        if server_id:
            api_path, method = f"/api/states/{server_id}", "PUT"
        else:
            query = urllib.parse.urlencode({"rom_id": rom_id, "emulator": "np2kai"})
            api_path, method = f"/api/states?{query}", "POST"
        files: dict[str, Path] = {}
        if state_changed or not server_id:
            files["stateFile"] = path
        if screenshot and screenshot.is_file():
            files["screenshotFile"] = screenshot
        return self.multipart_request(api_path, method, {}, files)

    def push_state(self, rom_id: int) -> str:
        local = self._state_path(rom_id)
        if not local.is_file():
            return "none"
        digest = self._sha256(local)
        screenshot = self._state_screenshot_path(rom_id)
        screenshot_digest = self._sha256(screenshot) if screenshot.is_file() else None
        record = self.db.execute("SELECT * FROM state_sync WHERE rom_id=?", (rom_id,)).fetchone()
        state_changed = not record or record["local_hash"] != digest
        screenshot_changed = bool(
            screenshot_digest and (not record or record["screenshot_hash"] != screenshot_digest)
        )
        if not state_changed and not screenshot_changed:
            return "unchanged"
        conflict_archive_id = None
        with self.state_upload_lock(rom_id):
            states = self.json_request(f"/api/states?rom_id={rom_id}") or []
            remote = self._canonical_remote_state(rom_id, states)
            if remote and self._remote_state_revision(remote) != self._recorded_state_revision(record):
                conflict_archive_id = self._archive_remote_state(rom_id, remote)
            result = self._upload_state(
                rom_id, local, screenshot if screenshot.is_file() else None,
                int(remote["id"]) if remote else None,
                state_changed or bool(conflict_archive_id),
            )
        remote_screenshot = result.get("screenshot") or {}
        self.db.execute(
            """INSERT INTO state_sync
            (rom_id,server_id,local_hash,server_updated,screenshot_hash,
             server_screenshot_id,server_screenshot_updated)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(rom_id) DO UPDATE SET
            server_id=excluded.server_id,local_hash=excluded.local_hash,
            server_updated=excluded.server_updated,
            screenshot_hash=excluded.screenshot_hash,
            server_screenshot_id=excluded.server_screenshot_id,
            server_screenshot_updated=excluded.server_screenshot_updated""",
            (
                rom_id, result["id"], digest, result.get("updated_at"), screenshot_digest,
                remote_screenshot.get("id"), remote_screenshot.get("updated_at"),
            ),
        )
        self.db.commit()
        action = "uploaded-with-screenshot" if screenshot_digest else "uploaded"
        if conflict_archive_id:
            action += f"; archived-server-conflict={conflict_archive_id}"
        return action

    def launch(self, stub_path: Path, dry_run: bool = False) -> int:
        stub = json.loads(stub_path.read_text(encoding="utf-8"))
        rom_id = int(stub["rom_id"])
        with self.locked():
            rom = self.ensure_cached(rom_id)
            try:
                state_action = self.pull_state(rom_id)
            except Exception as exc:
                state_action = f"deferred ({exc})"
            print("State sync before launch:", state_action)
        command = self.retroarch_command + ["--verbose", "-L", str(self.core), str(rom)]
        print("Launching:", " ".join(command))
        if dry_run:
            return 0
        started = dt.datetime.now(dt.timezone.utc)
        launch_env = os.environ.copy()
        if os.name != "nt":
            runtime_dir = launch_env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            launch_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
            launch_env["LANG"] = "en_US.UTF-8"
            launch_env["LC_ALL"] = "en_US.UTF-8"
        # ES-DE itself is launched through Steam's pressure-vessel. Passing its
        # private bus and mixed 32/64-bit overlay preload into a Flatpak child
        # causes portal failures and can crash the NP2 file dialog on exit.
        launch_env.pop("LD_PRELOAD", None)
        result = subprocess.run(command, check=False, env=launch_env)
        ended = dt.datetime.now(dt.timezone.utc)
        duration_ms = max(0, int((ended - started).total_seconds() * 1000))
        with self.locked():
            try:
                state_action = self.push_state(rom_id)
            except Exception as exc:
                state_action = f"deferred ({exc})"
            print("State sync after launch:", state_action)
        self.db.execute(
            "INSERT INTO outbox(kind,payload,created_at) VALUES('play_session',?,?)",
            (
                json.dumps({
                    "rom_id": rom_id, "start_time": started.isoformat(),
                    "end_time": ended.isoformat(), "duration_ms": duration_ms,
                }), int(started.timestamp()),
            ),
        )
        self.db.commit()
        self.flush_outbox()
        return result.returncode

    def doctor(self) -> int:
        if os.name == "nt":
            # POSIX mode bits are synthesized on Windows and therefore cannot
            # prove anything about the file ACL.  The installer removes ACL
            # inheritance from the token file; icacls marks inherited entries
            # with the stable, non-localized ``(I)`` flag.
            acl = subprocess.run(
                ["icacls.exe", str(self.token_file)],
                check=False,
                capture_output=True,
                text=True,
            )
            token_check_name = "token ACL inheritance disabled"
            token_permissions_ok = acl.returncode == 0 and "(I)" not in acl.stdout.upper()
        else:
            token_check_name = "token permissions 0600"
            token_permissions_ok = self.token_file.stat().st_mode & 0o777 == 0o600
        checks = {
            token_check_name: token_permissions_ok,
            "NP2Kai core": self.core.is_file(),
            "NP2Kai text font firmware": (
                (self.firmware_dir / "np2kai" / "font.bmp").is_file()
                or (self.firmware_dir / "np2kai" / "FONT.ROM").is_file()
            ),
            "ES-DE system": self.systems_xml.is_file() and "<name>romm-pc98</name>" in self.systems_xml.read_text(),
            "manifest cache": self.manifest_cache.is_file(),
            "stub directory": self.rom_dir.is_dir(),
            "state directory": self.state_dir.is_dir(),
            "controller mapping": (not self.steam_deck_tuning) or (
                self.retroarch_autoconfig.is_file()
                and self.retroarch_autoconfig.read_text() == STEAM_DECK_AUTOCONFIG
            ),
            "NP2Kai joypad mode": (
                self.np2kai_options.is_file()
                and 'np2kai_joymode = "OFF"' not in self.np2kai_options.read_text()
            ),
            "NP2Kai platform video/audio override": (not self.steam_deck_tuning) or (
                self.np2kai_override.is_file()
                and self.np2kai_override.read_text() == NP2KAI_RETROARCH_OVERRIDE
            ),
            f"RomM user binding ({self.username}:{self.user_id})": bool(
                self.get_meta("romm_user_id") and self.get_meta("client_instance_id")
            ),
            "unique RomM device": bool(self.get_meta("device_id")),
        }
        try:
            with urllib.request.urlopen(f"{self.bridge_url}/health.json", timeout=10) as response:
                checks["Bridge reachable"] = json.load(response).get("status") == "ok"
        except Exception:
            checks["Bridge reachable"] = False
        for name, ok in checks.items():
            print(f"{'OK' if ok else 'FAIL':4} {name}")
        return 0 if all(checks.values()) else 1

    def backup_prep(self) -> Path:
        target = self.data / "backup" / "index.sqlite3"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".sqlite3.tmp")
        temporary.unlink(missing_ok=True)
        destination = sqlite3.connect(temporary)
        try:
            self.db.backup(destination)
        finally:
            destination.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("~/.config/romm-esde/config.toml").expanduser())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync")
    sub.add_parser("user-sync")
    media = sub.add_parser("media")
    media.add_argument("--workers", type=int, default=4)
    media.add_argument("--limit", type=int, default=0)
    launch = sub.add_parser("launch")
    launch.add_argument("stub", type=Path)
    launch.add_argument("--dry-run", action="store_true")
    sub.add_parser("repair-system")
    sub.add_parser("firmware")
    sub.add_parser("prune")
    sub.add_parser("doctor")
    sub.add_parser("backup-prep")
    session = sub.add_parser("session")
    session.add_argument("session_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    client = Client(args.config)
    if args.command == "sync":
        count, changed = client.sync(); print(f"Synced {count} games (changed={changed})")
    elif args.command == "user-sync":
        print(f"Pushed {client.push_local_user_flags()} favorite/hidden changes")
    elif args.command == "media":
        done, failed = client.sync_media(args.workers, args.limit); print(f"Downloaded {done} media; failures={failed}")
        return int(failed > 0)
    elif args.command == "launch":
        return client.launch(args.stub, args.dry_run)
    elif args.command == "repair-system":
        print("ES-DE system repaired" if client.repair_system() else "ES-DE system already present")
    elif args.command == "firmware":
        done, skipped = client.sync_firmware(); print(f"Firmware downloaded={done}; already-present={skipped}")
    elif args.command == "prune":
        print(f"Pruned {client.prune()} cached games")
    elif args.command == "doctor":
        return client.doctor()
    elif args.command == "backup-prep":
        print(client.backup_prep())
    elif args.command == "session":
        command = args.session_command
        if command and command[0] == "--":
            command = command[1:]
        return client.esde_session(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
