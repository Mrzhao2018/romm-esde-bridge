#!/usr/bin/env python3
"""Export a RomM-native catalogue for an ES-DE on-demand launcher.

The output contains no RomM credentials. A Deck pairs with RomM separately and
uses its own device-bound/client token when following the API endpoint templates.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


BRIDGE_VERSION = 4
DEFAULT_API_URL = "http://127.0.0.1:8080"
DEFAULT_PUBLIC_URL = "http://127.0.0.1:8080"
DEFAULT_BRIDGE_PUBLIC_URL = "http://127.0.0.1:8090"
DEFAULT_TOKEN_FILE = "/etc/romm-esde-bridge/server-token"
DEFAULT_OUTPUT_DIR = "/var/lib/romm-esde-bridge"
DEFAULT_HOST_ROOT = "/srv/romm"
EXTERNAL_URL_KEY = re.compile(r"(^url_|_url$|^url$)", re.IGNORECASE)
DISK_SLOT_PATTERN = re.compile(r"(?i)(?:disk|disc)\s*([a-z]|\d+)")


class RomMAPI:
    def __init__(self, api_url: str, token: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token.strip()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_url}{path}"
        if params:
            encoded = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{encoded}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "romm-esde-bridge/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            raise RuntimeError(f"RomM API {exc.code} for {path}: {detail}") from exc


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode()
    atomic_write(path, data)


def write_gzip(path: Path, data: bytes) -> None:
    atomic_write(path, gzip.compress(data, compresslevel=9, mtime=0))


def canonical_json_hash(value: Any) -> str:
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def local_asset_url(public_url: str, path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("/assets/"):
        encoded_path = urllib.parse.quote(path, safe="/:?=&")
        return f"{public_url.rstrip('/')}{encoded_path}"
    return None


def local_asset_metadata(
    public_url: str,
    asset_path: str | None,
    host_root: Path,
) -> dict[str, Any] | None:
    url = local_asset_url(public_url, asset_path)
    if not url or not asset_path:
        return None
    clean_path = asset_path.split("?", 1)[0]
    prefix = "/assets/romm/"
    result: dict[str, Any] = {"url": url, "asset_path": clean_path}
    if clean_path.startswith(prefix):
        host_path = host_root / clean_path.removeprefix(prefix)
        try:
            metadata = host_path.stat()
        except OSError:
            pass
        else:
            result["size_bytes"] = metadata.st_size
            result["mtime_ns"] = metadata.st_mtime_ns
    return result


def sanitize(value: Any) -> Any:
    """Remove upstream provider URLs which can embed provider credentials."""
    if isinstance(value, dict):
        return {
            key: sanitize(item)
            for key, item in value.items()
            if not EXTERNAL_URL_KEY.search(key)
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def quoted_path_segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def public_endpoint(public_url: str, path: str) -> str:
    return f"{public_url.rstrip('/')}{path}"


def disk_slot_index(file_name: str) -> int:
    """Return a zero-based disk slot inferred from a PC-98 file name."""
    match = DISK_SLOT_PATTERN.search(file_name)
    if not match:
        return 0
    value = match.group(1)
    if value.isdigit():
        return max(0, int(value) - 1)
    return ord(value.upper()) - ord("A")


def disk_descriptor(item: dict[str, Any], slot: int, role: str) -> dict[str, Any]:
    return {
        "slot": slot,
        "role": role,
        "file_id": item["id"],
        "file_name": item["file_name"],
        "file_size_bytes": item.get("file_size_bytes"),
        "crc_hash": item.get("crc_hash"),
        "md5_hash": item.get("md5_hash"),
        "sha1_hash": item.get("sha1_hash"),
        "hash_scope": item.get("hash_scope", "file"),
    }


def build_disk_options(
    canonical_files: list[dict[str, Any]], alternate_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group canonical disks and alternate dumps into selectable slots."""
    options = [
        {
            "slot": index,
            "canonical": disk_descriptor(item, index, "canonical"),
            "alternates": [],
        }
        for index, item in enumerate(canonical_files)
    ]
    for item in alternate_files:
        slot = disk_slot_index(str(item.get("file_name") or ""))
        if slot >= len(options):
            slot = 0
        options[slot]["alternates"].append(
            disk_descriptor(item, slot, "alternate")
        )
    return options


def normalize_rom(
    rom: dict[str, Any],
    public_url: str,
    host_root: Path,
) -> dict[str, Any]:
    safe_rom = sanitize(rom)
    # The generated manifest is shared by every client. Never embed the
    # service token owner's per-user view; clients overlay their own user data
    # directly from RomM using their device-bound token.
    safe_rom.pop("rom_user", None)
    safe_rom.pop("is_favorite", None)
    rom_id = rom["id"]
    files = rom.get("files") or []
    canonical_files = [item for item in files if item.get("category") == "game"]
    if not canonical_files and len(files) == 1:
        canonical_files = list(files)
    canonical_ids = {int(item["id"]) for item in canonical_files}
    alternate_files = [item for item in files if int(item["id"]) not in canonical_ids]
    output_name = rom.get("fs_name") or rom.get("name") or f"rom-{rom_id}"
    download_path = (
        f"/api/roms/{rom_id}/content/{quoted_path_segment(output_name)}"
    )
    file_downloads = [
        {
            "file_id": item["id"],
            "file_name": item["file_name"],
            "url": public_endpoint(
                public_url,
                f"/api/roms/{item['id']}/files/content/"
                f"{quoted_path_segment(item['file_name'])}",
            ),
        }
        for item in files
    ]
    disk_options = build_disk_options(canonical_files, alternate_files)
    if rom.get("has_nested_single_file"):
        for option in disk_options:
            for descriptor in [option["canonical"], *(option.get("alternates") or [])]:
                if str(descriptor.get("file_name") or "").casefold().endswith(".zip"):
                    descriptor["hash_scope"] = "archive_single_member"
    screenshots = [
        metadata
        for path in (rom.get("merged_screenshots") or [])
        if (metadata := local_asset_metadata(public_url, path, host_root))
    ]
    canonical_query = urllib.parse.urlencode(
        {"file_ids": ",".join(str(item["id"]) for item in canonical_files)}
    )
    canonical_path = download_path
    if canonical_query:
        canonical_path = f"{download_path}?{canonical_query}"
    if len(canonical_files) == 1:
        launch_strategy = "direct_file"
    elif len(canonical_files) > 1:
        launch_strategy = "canonical_multidisk"
    else:
        launch_strategy = "manual_selection_required"
    safe_rom["bridge"] = {
        "stub_file": f"romm-{rom_id}.romm",
        "download_url": public_endpoint(public_url, download_path),
        "download_api_path": download_path,
        "download_kind": "file" if len(files) == 1 else "streamed_zip",
        "canonical_download_kind": (
            "file" if len(canonical_files) == 1 else "streamed_zip"
        ),
        "launch_strategy": launch_strategy,
        "canonical_download_url": public_endpoint(public_url, canonical_path),
        "canonical_download_api_path": canonical_path,
        "canonical_file_ids": [item["id"] for item in canonical_files],
        "canonical_file_names": [item["file_name"] for item in canonical_files],
        "canonical_size_bytes": sum(
            int(item.get("file_size_bytes") or 0) for item in canonical_files
        ),
        "alternate_file_ids": [item["id"] for item in alternate_files],
        "alternate_file_names": [item["file_name"] for item in alternate_files],
        "disk_options": disk_options,
        "file_downloads": file_downloads,
        "cover_small": local_asset_metadata(
            public_url, rom.get("path_cover_small"), host_root
        ),
        "cover_large": local_asset_metadata(
            public_url, rom.get("path_cover_large"), host_root
        ),
        "screenshots": screenshots,
    }
    # Compatibility aliases retained for Bridge v1 clients.
    safe_rom["bridge"]["cover_small_url"] = (
        safe_rom["bridge"]["cover_small"] or {}
    ).get("url")
    safe_rom["bridge"]["cover_large_url"] = (
        safe_rom["bridge"]["cover_large"] or {}
    ).get("url")
    safe_rom["bridge"]["screenshot_urls"] = [item["url"] for item in screenshots]
    return safe_rom


def normalize_firmware(item: dict[str, Any], public_url: str) -> dict[str, Any]:
    result = sanitize(item)
    result["download_api_path"] = (
        f"/api/firmware/{item['id']}/content/"
        f"{quoted_path_segment(item['file_name'])}"
    )
    result["download_url"] = public_endpoint(public_url, result["download_api_path"])
    return result


def millis_or_seconds_to_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def iso_to_es_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.strftime("%Y%m%dT%H%M%S")


def append_xml(parent: ET.Element, tag: str, value: Any) -> None:
    if value is None or value == "" or value == []:
        return
    child = ET.SubElement(parent, tag)
    child.text = str(value)


def build_gamelist(roms: list[dict[str, Any]]) -> bytes:
    root = ET.Element("gameList")
    for rom in sorted(roms, key=lambda item: item.get("name_sort_key") or item["name"]):
        bridge = rom["bridge"]
        metadata = rom.get("metadatum") or {}
        user = rom.get("rom_user") or {}
        game = ET.SubElement(root, "game")
        append_xml(game, "path", f"./{bridge['stub_file']}")
        append_xml(game, "name", rom.get("name") or rom.get("fs_name"))
        append_xml(game, "sortname", rom.get("name_sort_key"))
        append_xml(game, "desc", rom.get("summary"))
        if bridge.get("cover_large_url"):
            append_xml(game, "image", f"./media/covers/romm-{rom['id']}.png")
            append_xml(game, "thumbnail", f"./media/covers/romm-{rom['id']}.png")
        if bridge.get("screenshot_urls"):
            append_xml(game, "screenshot", f"./media/screenshots/romm-{rom['id']}.jpg")
        release = millis_or_seconds_to_datetime(metadata.get("first_release_date"))
        if release:
            append_xml(game, "releasedate", release.strftime("%Y%m%dT000000"))
        companies = list(dict.fromkeys(metadata.get("companies") or []))
        append_xml(game, "developer", ", ".join(companies))
        append_xml(game, "publisher", ", ".join(companies))
        append_xml(game, "genre", ", ".join(metadata.get("genres") or []))
        append_xml(game, "players", metadata.get("player_count"))
        rating = metadata.get("average_rating")
        if rating is not None:
            try:
                rating_float = float(rating)
                append_xml(game, "rating", min(1.0, rating_float / 100.0))
            except (TypeError, ValueError):
                pass
        append_xml(game, "favorite", "true" if rom.get("is_favorite") else None)
        append_xml(game, "lastplayed", iso_to_es_date(user.get("last_played")))
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def make_stub(rom: dict[str, Any]) -> dict[str, Any]:
    bridge = rom["bridge"]
    return {
        "schema": "romm-esde-stub-v1",
        "rom_id": rom["id"],
        "platform_id": rom["platform_id"],
        "platform_slug": rom["platform_slug"],
        "name": rom["name"],
        "revision": rom.get("revision"),
        "updated_at": rom.get("updated_at"),
        "download_api_path": bridge["download_api_path"],
        "canonical_download_api_path": bridge["canonical_download_api_path"],
        "download_kind": bridge["download_kind"],
        "canonical_download_kind": bridge["canonical_download_kind"],
        "launch_strategy": bridge["launch_strategy"],
        "canonical_file_ids": bridge["canonical_file_ids"],
        "alternate_file_ids": bridge["alternate_file_ids"],
        "file_ids": [item["id"] for item in rom.get("files") or []],
        "file_names": [item["file_name"] for item in rom.get("files") or []],
        "expected_size_bytes": rom.get("fs_size_bytes") or 0,
    }


def build_stub_bundle(path: Path, platform_slug: str, roms: list[dict[str, Any]], gamelist: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("gamelist.xml", gamelist)
            for rom in roms:
                stub = json.dumps(make_stub(rom), ensure_ascii=False, separators=(",", ":"))
                archive.writestr(rom["bridge"]["stub_file"], stub + "\n")
            archive.writestr(
                "README.txt",
                f"Generated RomM ES-DE stubs for {platform_slug}.\n"
                "These files contain identifiers only; ROM content is downloaded on launch.\n",
            )
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def fetch_all_roms(api: RomMAPI, platform_id: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # ID ordering is stable while metadata/title updates are happening. Name
    # ordering is not: a concurrent scraper can move records across page
    # boundaries and yield duplicate/missing launch stubs.
    for attempt in range(3):
        limit = 10_000
        offset = 0
        by_id: dict[int, dict[str, Any]] = {}
        filter_values: dict[str, Any] = {}
        expected_total = 0
        while True:
            page = api.get(
                "/api/roms",
                {
                    "platform_ids": platform_id,
                    "with_files": "true",
                    "with_char_index": "false",
                    "with_filter_values": "true" if offset == 0 else "false",
                    "order_by": "id",
                    "order_dir": "asc",
                    "limit": limit,
                    "offset": offset,
                },
            )
            batch = page.get("items") or []
            by_id.update((int(item["id"]), item) for item in batch)
            if offset == 0:
                filter_values = sanitize(page.get("filter_values") or {})
                expected_total = int(page.get("total", len(batch)))
            offset += len(batch)
            if not batch or offset >= expected_total:
                break
        if len(by_id) == expected_total:
            return list(by_id.values()), filter_values
        if attempt == 2:
            raise RuntimeError(
                f"RomM changed during export: expected {expected_total} unique ROMs, "
                f"received {len(by_id)}"
            )
    raise AssertionError("unreachable")


def recover_user_hidden_roms(
    api: RomMAPI, platform_id: int, visible: list[dict[str, Any]], seed_paths: list[Path],
) -> list[dict[str, Any]]:
    """Recover service-user-hidden ROMs from prior shared catalogue IDs.

    RomM omits `rom_user.hidden` entries from list/identifier endpoints, while
    the direct detail endpoint remains readable to their owner. A previously
    exported user-neutral catalogue therefore acts as a monotonic identifier
    seed; deleted IDs simply return 404 and are ignored.
    """
    by_id = {int(item["id"]): item for item in visible}
    candidate_ids: set[int] = set()
    for path in seed_paths:
        if path.suffix == ".sqlite3":
            try:
                database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    candidate_ids.update(
                        int(row[0]) for row in database.execute("SELECT rom_id FROM games")
                    )
                finally:
                    database.close()
            except (OSError, sqlite3.Error):
                pass
            continue
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if int((previous.get("platform") or {}).get("id", -1)) != platform_id:
            continue
        candidate_ids.update(int(item["id"]) for item in previous.get("roms", []))
    for rom_id in sorted(candidate_ids - set(by_id)):
        try:
            item = api.get(f"/api/roms/{rom_id}")
        except RuntimeError as exc:
            if " 404 " in f" {exc} ":
                continue
            raise
        if int(item.get("platform_id", -1)) == platform_id:
            by_id[rom_id] = item
    return list(by_id.values())


def refresh(args: argparse.Namespace) -> None:
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    api = RomMAPI(args.api_url, token)
    output = Path(args.output_dir)
    host_root = Path(args.host_root)
    generated_at = datetime.now(timezone.utc).isoformat()

    heartbeat = sanitize(api.get("/api/heartbeat"))
    platforms = sanitize(api.get("/api/platforms"))
    # Only public collections belong in the shared catalogue. Favorites and
    # private collections are fetched by each client as its authenticated user.
    collections = [
        item for item in sanitize(api.get("/api/collections")) if item.get("is_public")
    ]
    smart_collections: list[dict[str, Any]] = []
    requested_slugs = {slug.strip() for slug in args.platforms.split(",") if slug.strip()}
    selected = [
        platform
        for platform in platforms
        if int(platform.get("rom_count") or 0) > 0
        and (not requested_slugs or platform.get("slug") in requested_slugs)
    ]
    if requested_slugs:
        found = {platform["slug"] for platform in selected}
        missing = requested_slugs - found
        if missing:
            raise RuntimeError(f"Requested RomM platforms not found/non-empty: {sorted(missing)}")

    catalog_platforms: list[dict[str, Any]] = []
    for platform in selected:
        platform_id = int(platform["id"])
        slug = platform["slug"]
        raw_roms, filter_values = fetch_all_roms(api, platform_id)
        platform_dir = output / "platforms" / slug
        seed_paths = [platform_dir / "manifest.json"] + [
            Path(value) for value in args.seed_manifests.split(",") if value.strip()
        ]
        raw_roms = recover_user_hidden_roms(api, platform_id, raw_roms, seed_paths)
        expected_count = int(platform.get("rom_count") or 0)
        if len(raw_roms) != expected_count:
            raise RuntimeError(
                f"Shared catalogue incomplete for {slug}: RomM reports {expected_count}, "
                f"export recovered {len(raw_roms)}. Supply a prior complete manifest via "
                "BRIDGE_SEED_MANIFESTS."
            )
        roms = [normalize_rom(rom, args.public_url, host_root) for rom in raw_roms]
        firmware = [
            normalize_firmware(item, args.public_url)
            for item in api.get("/api/firmware", {"platform_id": platform_id})
        ]
        content = {
            "platform": platform,
            "filter_values": filter_values,
            "firmware": firmware,
            "roms": roms,
        }
        content_revision = canonical_json_hash(content)
        manifest = {
            "schema": "romm-esde-platform-v2",
            "bridge_version": BRIDGE_VERSION,
            "generated_at": generated_at,
            "content_revision": content_revision,
            "romm_public_url": args.public_url.rstrip("/"),
            **content,
        }
        manifest_path = platform_dir / "manifest.json"
        previous_revision = None
        previous_bridge_version = None
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            previous_revision = previous_manifest.get("content_revision")
            previous_bridge_version = previous_manifest.get("bridge_version")
        except (OSError, ValueError, AttributeError):
            pass
        manifest_data = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        ).encode("utf-8")
        content_changed = (
            previous_revision != content_revision
            or previous_bridge_version != BRIDGE_VERSION
        )
        gzip_path = platform_dir / "manifest.json.gz"
        gamelist_path = platform_dir / "gamelist.xml"
        stubs_path = platform_dir / "esde-stubs.zip"
        if content_changed or not manifest_path.is_file():
            atomic_write(manifest_path, manifest_data)
        if content_changed or not gzip_path.is_file():
            write_gzip(gzip_path, manifest_path.read_bytes())
        if content_changed or not gamelist_path.is_file() or not stubs_path.is_file():
            gamelist = build_gamelist(roms)
            atomic_write(gamelist_path, gamelist)
            build_stub_bundle(stubs_path, slug, roms, gamelist)
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        catalog_platforms.append(
            {
                **platform,
                "exported_rom_count": len(roms),
                "exported_firmware_count": len(firmware),
                "manifest_sha256": manifest_hash,
                "content_revision": content_revision,
                "manifest_path": f"/platforms/{slug}/manifest.json",
                "manifest_gzip_path": f"/platforms/{slug}/manifest.json.gz",
                "gamelist_path": f"/platforms/{slug}/gamelist.xml",
                "stubs_path": f"/platforms/{slug}/esde-stubs.zip",
            }
        )

    catalog = {
        "schema": "romm-esde-catalog-v2",
        "bridge_version": BRIDGE_VERSION,
        "generated_at": generated_at,
        "romm_public_url": args.public_url.rstrip("/"),
        "romm": heartbeat,
        "capabilities": {
            "on_demand_download": True,
            "streamed_multifile_zip": True,
            "esde_gamelist": True,
            "firmware_manifest": True,
            "public_romm_collections": True,
            "client_user_overlay": True,
            "device_pairing": True,
            "browser_pc98": True,
            "browser_multidisk_control": True,
            "browser_save_states": True,
            "save_state_sync": True,
            "play_sessions": True,
            "media_from_romm_assets": True,
            "client_bootstrap": True,
            "client_platforms": ["linux", "windows-x64"],
        },
        "api_paths": {
            "device_pair_init": "/api/auth/device/init",
            "device_pair_token": "/api/auth/device/token",
            "devices": "/api/devices",
            "sync_negotiate": "/api/sync/negotiate",
            "sync_sessions": "/api/sync/sessions",
            "saves": "/api/saves",
            "states": "/api/states",
            "play_sessions": "/api/play-sessions",
            "linux_install": (
                getattr(args, "bridge_public_url", DEFAULT_BRIDGE_PUBLIC_URL).rstrip("/")
                + "/bootstrap/install.sh"
            ),
            "windows_install": (
                getattr(args, "bridge_public_url", DEFAULT_BRIDGE_PUBLIC_URL).rstrip("/")
                + "/bootstrap/install.ps1"
            ),
        },
        "platforms": catalog_platforms,
        "collections": collections,
        "smart_collections": smart_collections,
    }
    write_json(output / "catalog.json", catalog)
    write_json(
        output / "health.json",
        {
            "status": "ok",
            "generated_at": generated_at,
            "bridge_version": BRIDGE_VERSION,
            "platform_count": len(catalog_platforms),
            "rom_count": sum(item["exported_rom_count"] for item in catalog_platforms),
        },
    )
    # Publish the credential-free installer beside every fresh catalog. Device
    # credentials are obtained later through RomM's browser approval flow.
    from publish_bootstrap import publish
    publish(output, args.public_url, getattr(args, "bridge_public_url", DEFAULT_BRIDGE_PUBLIC_URL))
    print(
        f"Exported {sum(item['exported_rom_count'] for item in catalog_platforms)} "
        f"ROMs across {len(catalog_platforms)} platform(s)"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=os.getenv("ROMM_API_URL", DEFAULT_API_URL))
    parser.add_argument("--public-url", default=os.getenv("ROMM_PUBLIC_URL", DEFAULT_PUBLIC_URL))
    parser.add_argument(
        "--bridge-public-url",
        default=os.getenv("BRIDGE_PUBLIC_URL", DEFAULT_BRIDGE_PUBLIC_URL),
    )
    parser.add_argument("--token-file", default=os.getenv("ROMM_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    parser.add_argument("--output-dir", default=os.getenv("BRIDGE_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
    parser.add_argument("--host-root", default=os.getenv("ROMM_HOST_ROOT", DEFAULT_HOST_ROOT))
    parser.add_argument(
        "--seed-manifests", default=os.getenv("BRIDGE_SEED_MANIFESTS", ""),
        help="Comma-separated prior manifests or client SQLite indexes used to recover hidden IDs",
    )
    parser.add_argument(
        "--platforms",
        default=os.getenv("BRIDGE_PLATFORMS", ""),
        help="Comma-separated RomM platform slugs; empty exports every non-empty platform",
    )
    return parser.parse_args()


if __name__ == "__main__":
    refresh(parse_args())
