#!/usr/bin/env python3
"""Interactive, user-level installer for the RomM ES-DE Linux client."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent
DEFAULTS = json.loads((ROOT / "defaults.json").read_text())
SERVER_URL = os.getenv("ROMM_ESDE_SERVER_URL", DEFAULTS["server_url"]).rstrip("/")
BRIDGE_URL = os.getenv("ROMM_ESDE_BRIDGE_URL", DEFAULTS["bridge_url"]).rstrip("/")
VERSION = DEFAULTS["client_version"]
SCOPES = [
    "me.read", "roms.read", "roms.user.read", "roms.user.write",
    "platforms.read", "assets.read", "assets.write", "devices.read",
    "devices.write", "firmware.read", "collections.read", "collections.write",
]


def tty_input(prompt: str) -> str:
    try:
        with open("/dev/tty", "r", encoding="utf-8") as source:
            print(prompt, end="", flush=True)
            return source.readline().strip()
    except OSError:
        return input(prompt).strip()


def confirm(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = tty_input(prompt + suffix).lower()
    return default if not answer else answer in {"y", "yes", "是", "好"}


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, text=True)


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def is_steam_deck() -> bool:
    candidates = [
        Path("/sys/devices/virtual/dmi/id/product_name"),
        Path("/sys/class/dmi/id/product_name"),
    ]
    return any(path.is_file() and "jupiter" in path.read_text(errors="ignore").lower()
               for path in candidates)


def emudeck_markers(home: Path) -> list[Path]:
    return [
        home / ".config/EmuDeck",
        home / "Applications/EmuDeck.AppImage",
        home / "Emulation/tools/launchers/es-de/es-de.sh",
        home / "Desktop/EmuDeck.desktop",
    ]


def emudeck_installed(home: Path) -> bool:
    return any(path.exists() for path in emudeck_markers(home))


def install_emudeck() -> None:
    print("\n即将运行 EmuDeck 官方 Linux 安装器。EmuDeck 的 Easy/Custom、存储位置等仍由其 GUI 确认。")
    script = Path("/tmp/romm-esde-emudeck-install.sh")
    run("curl", "-fL", "https://raw.githubusercontent.com/dragoonDorise/EmuDeck/main/install.sh", "-o", str(script))
    run("bash", str(script))
    script.unlink(missing_ok=True)
    tty_input("完成 EmuDeck GUI 配置后按 Enter 继续；如果 GUI 仍在运行，请先完成它：")


def find_core(home: Path) -> Path | None:
    candidates = [
        home / ".var/app/org.libretro.RetroArch/config/retroarch/cores/np2kai_libretro.so",
        home / ".config/retroarch/cores/np2kai_libretro.so",
        home / "Emulation/tools/retroarch/cores/np2kai_libretro.so",
    ]
    return next((path for path in candidates if path.is_file()), None)


def flatpak_retroarch() -> bool:
    if not shutil.which("flatpak"):
        return False
    result = subprocess.run(
        ["flatpak", "info", "org.libretro.RetroArch"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def pair_device(home: Path, device_name: str, runtime: str) -> tuple[str, str, list[str]]:
    identity_path = home / ".local/share/romm-esde/client-instance-id"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    if identity_path.is_file():
        identity = identity_path.read_text().strip()
    else:
        identity = str(uuid.uuid4())
        identity_path.write_text(identity + "\n")
        identity_path.chmod(0o600)
    init = post_json(
        SERVER_URL + "/api/auth/device/init",
        {
            "client_device_identifier": identity,
            "name": device_name,
            "client": "romm-esde",
            "platform": runtime,
            "client_version": VERSION,
            "requested_scopes": SCOPES,
        },
    )
    url = SERVER_URL + init["verification_path_complete"]
    print(f"\n请在 RomM 中登录要绑定的账号并批准设备：\n  配对码：{init['user_code']}\n  {url}\n")
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    interval = max(2, int(init["interval"]))
    deadline = time.monotonic() + int(init["expires_in"])
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            result = post_json(
                SERVER_URL + "/api/auth/device/token",
                {"device_code": init["device_code"]},
            )
            return result["access_token"], result["device_id"], result["scopes"]
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail")
            except Exception:
                detail = None
            if detail == "authorization_pending":
                continue
            if detail == "slow_down":
                interval += 2
                continue
            raise RuntimeError(f"RomM 配对失败：{detail or exc}") from exc
    raise RuntimeError("RomM 配对码已过期，请重新运行安装命令")


def q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_config(home: Path, device_name: str, runtime: str, core: Path, device_id: str) -> Path:
    config_dir = home / ".config/romm-esde"
    config_dir.mkdir(parents=True, exist_ok=True)
    ra_root = home / ".var/app/org.libretro.RetroArch/config/retroarch"
    launcher = home / "Emulation/tools/launchers/es-de/es-de.sh"
    values = {
        "server_url": SERVER_URL,
        "bridge_url": BRIDGE_URL,
        "token_file": str(config_dir / "token"),
        "platform_slug": "pc-9800-series",
        "data_dir": str(home / ".local/share/romm-esde"),
        "stub_dir": str(home / "Emulation/roms/romm-pc98"),
        "gamelist_path": str(home / "ES-DE/gamelists/romm-pc98/gamelist.xml"),
        "media_dir": str(home / "Emulation/tools/downloaded_media/romm-pc98"),
        "thumbnail_dir": str(home / ".local/share/romm-esde/media/thumbnails"),
        "cache_dir": str(home / ".local/share/romm-esde/cache"),
        "systems_xml": str(home / "ES-DE/custom_systems/es_systems.xml"),
        "retroarch_core": str(core),
        "state_dir": str(home / "Emulation/saves/retroarch/states"),
        "retroarch_autoconfig": str(ra_root / "autoconfig/sdl2/Steam Deck Controller.cfg"),
        "np2kai_options": str(ra_root / "config/Neko Project II kai/Neko Project II kai.opt"),
        "np2kai_override": str(ra_root / "config/Neko Project II kai/Neko Project II kai.cfg"),
        "firmware_dir": str(home / "Emulation/bios"),
        "runtime_platform": runtime,
        "launcher_command": str(home / ".local/bin/romm-esde-launch"),
        "device_name": device_name,
        "paired_device_id": device_id,
        "esde_launcher": str(launcher),
    }
    lines = [f"{key} = {q(value)}" for key, value in values.items()]
    lines += [
        'retroarch_command = ["flatpak", "run", "org.libretro.RetroArch"]',
        f"steam_deck_tuning = {'true' if is_steam_deck() else 'false'}",
        "cache_max_bytes = 53687091200",
        "min_free_percent = 20.0",
        "user_flags_ttl_seconds = 900",
    ]
    path = config_dir / "config.toml"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path


def install_program(home: Path) -> None:
    data = home / ".local/share/romm-esde"
    bindir = home / ".local/bin"
    data.mkdir(parents=True, exist_ok=True)
    bindir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "deck_client.py", data / "client.py")
    wrappers = {
        "romm-esde-client": f'#!/bin/sh\nexec /usr/bin/python3 "{data}/client.py" "$@"\n',
        "romm-esde-sync": f'#!/bin/sh\nexec "{bindir}/romm-esde-client" sync\n',
        "romm-esde-cache": f'#!/bin/sh\nexec "{bindir}/romm-esde-client" prune "$@"\n',
        "romm-esde-doctor": f'#!/bin/sh\nexec "{bindir}/romm-esde-client" doctor "$@"\n',
        "romm-esde-launch": (
            '#!/bin/sh\nunset LD_PRELOAD\n'
            'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"\n'
            'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"\n'
            f'exec "{bindir}/romm-esde-client" launch "$@"\n'
        ),
    }
    for name, body in wrappers.items():
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)


def install_systemd(home: Path) -> bool:
    if not shutil.which("systemctl"):
        return False
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    client = "%h/.local/bin/romm-esde-client"
    units = {
        "romm-esde-sync.service": f"[Unit]\nDescription=Synchronize RomM into ES-DE\nAfter=network-online.target\n[Service]\nType=oneshot\nExecStart={client} sync\nNice=10\n",
        "romm-esde-sync.timer": "[Unit]\nDescription=Refresh RomM ES-DE catalog\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=15min\nRandomizedDelaySec=2min\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
        "romm-esde-media.service": f"[Unit]\nDescription=Download RomM artwork\nAfter=network-online.target romm-esde-sync.service\n[Service]\nType=oneshot\nExecStart={client} media --workers 4\nNice=15\nIOSchedulingClass=idle\n",
        "romm-esde-media.timer": "[Unit]\nDescription=Refresh RomM artwork\n[Timer]\nOnBootSec=10min\nOnUnitActiveSec=1h\nRandomizedDelaySec=10min\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
        "romm-esde-user-sync.service": f"[Unit]\nDescription=Push ES-DE user metadata to RomM\nAfter=network-online.target\n[Service]\nType=oneshot\nExecStart={client} user-sync\nNice=10\n",
        "romm-esde-user-sync.path": "[Unit]\nDescription=Watch ES-DE RomM user metadata\n[Path]\nPathChanged=%h/ES-DE/gamelists/romm-pc98/gamelist.xml\nUnit=romm-esde-user-sync.service\n[Install]\nWantedBy=default.target\n",
        "romm-esde-config.service": f"[Unit]\nDescription=Restore RomM custom ES-DE system\n[Service]\nType=oneshot\nExecStart={client} repair-system\n",
        "romm-esde-config.path": "[Unit]\nDescription=Watch ES-DE custom systems file\n[Path]\nPathChanged=%h/ES-DE/custom_systems/es_systems.xml\nUnit=romm-esde-config.service\n[Install]\nWantedBy=default.target\n",
        "romm-esde-launcher.path": "[Unit]\nDescription=Watch the EmuDeck ES-DE launcher\n[Path]\nPathChanged=%h/Emulation/tools/launchers/es-de/es-de.sh\nUnit=romm-esde-config.service\n[Install]\nWantedBy=default.target\n",
    }
    for name, body in units.items():
        (unit_dir / name).write_text(body)
    result = subprocess.run(["systemctl", "--user", "daemon-reload"])
    if result.returncode:
        return False
    run("systemctl", "--user", "disable", "--now", "romm-esde-sync.timer", "romm-esde-media.timer", "romm-esde-user-sync.path", check=False)
    run("systemctl", "--user", "enable", "--now", "romm-esde-config.path", "romm-esde-launcher.path")
    return True


def main() -> int:
    if sys.platform != "linux":
        raise SystemExit("当前一键安装包支持 SteamOS/Linux；Windows、macOS、Android 需要各自的系统适配器。")
    if sys.version_info < (3, 11):
        raise SystemExit("需要 Python 3.11 或更高版本（用于读取 TOML 配置）")
    home = Path.home()
    runtime = "SteamOS" if is_steam_deck() or "steamos" in platform.platform().lower() else "Linux"
    print(f"RomM ES-DE 客户端 {VERSION} · {runtime} · 用户 {getpass.getuser()}")

    present = emudeck_installed(home)
    if present:
        if not confirm("检测到 EmuDeck，跳过 EmuDeck 并只安装 RomM 集成，继续吗？"):
            return 1
    elif confirm("没有检测到 EmuDeck。现在运行 EmuDeck 官方安装流程吗？"):
        install_emudeck()
    else:
        raise SystemExit("已取消；当前客户端依赖 EmuDeck 提供 ES-DE、RetroArch 和 NP2Kai")

    core = find_core(home)
    if not flatpak_retroarch() or core is None:
        raise SystemExit("尚未检测到 RetroArch Flatpak 或 NP2Kai 核心。请在 EmuDeck 中完成 RetroArch/ES-DE 配置后重跑本命令。")

    default_name = f"{runtime} · {socket.gethostname()}"
    device_name = tty_input(f"设备名称 [{default_name}]：") or default_name
    token_path = home / ".config/romm-esde/token"
    config_path = home / ".config/romm-esde/config.toml"
    if token_path.is_file() and config_path.is_file() and confirm("发现现有 RomM 绑定，保留原账号和设备身份吗？"):
        token = token_path.read_text().strip()
        old = config_path.read_text()
        marker = 'paired_device_id = '
        device_id = next((line.split("=", 1)[1].strip().strip('"') for line in old.splitlines() if line.startswith(marker)), "")
        scopes = SCOPES
    else:
        token, device_id, scopes = pair_device(home, device_name, runtime)

    install_program(home)
    config_path = write_config(home, device_name, runtime, core, device_id)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token + "\n")
    token_path.chmod(0o600)
    (home / "Emulation/saves/retroarch/states").mkdir(parents=True, exist_ok=True)
    installed_services = install_systemd(home)

    client = str(home / ".local/bin/romm-esde-client")
    run(client, "sync")
    run(client, "firmware")
    run(client, "repair-system")
    doctor = run(client, "doctor", check=False).returncode
    if installed_services:
        run("systemctl", "--user", "start", "--no-block", "romm-esde-media.service", check=False)
    missing_write = sorted(
        {"roms.user.write", "collections.write", "assets.write", "devices.write"}
        - set(scopes)
    )
    if missing_write:
        print("警告：账号未授予以下写权限，收藏/隐藏或存档同步会只读：" + ", ".join(missing_write))
    print(f"\n安装完成。配置：{config_path}\n打开 ES-DE 后选择 ‘RomM · PC-98’ 即可；ROM 会按需缓存。")
    return doctor


if __name__ == "__main__":
    raise SystemExit(main())
