import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from deck_client import Client


class DeckClientValidationTests(unittest.TestCase):
    def test_esde_session_syncs_before_during_and_after(self):
        with tempfile.TemporaryDirectory() as directory:
            gamelist = Path(directory) / "gamelist.xml"
            gamelist.write_text("initial")
            calls = []
            client = Client.__new__(Client)
            client.gamelist = gamelist
            client.sync = lambda: (12, True)
            client.sync_media = lambda workers=4: (3, 0)
            client.push_local_user_flags = lambda: calls.append("push") or 1
            script = (
                "import pathlib,time; time.sleep(.2); "
                f"pathlib.Path({str(gamelist)!r}).write_text('changed'); "
                "time.sleep(1.5)"
            )
            self.assertEqual(client.esde_session([sys.executable, "-c", script]), 0)
            self.assertGreaterEqual(len(calls), 2)

    def test_nested_zip_hashes_apply_to_inner_member(self):
        payload = b"PC-98 disk image" * 100
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "game.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("game.hdi", payload)
            descriptor = [{
                "name": "game.zip",
                "size": archive.stat().st_size,
                "sha1": hashlib.sha1(payload).hexdigest(),
                "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
                "hash_scope": "archive_single_member",
            }]
            Client._validate_direct(archive, descriptor)

    def test_nested_zip_rejects_wrong_inner_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "game.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("game.hdi", b"content")
            descriptor = [{
                "name": "game.zip", "size": archive.stat().st_size,
                "sha1": "0" * 40, "md5": None,
                "hash_scope": "archive_single_member",
            }]
            with self.assertRaisesRegex(RuntimeError, "SHA1 mismatch"):
                Client._validate_direct(archive, descriptor)

    def test_nested_multidisk_zip_hashes_primary_member(self):
        primary = b"disk A" * 100
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "game.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("Game (Disk A).hdm", primary)
                handle.writestr("Game (Disk B).hdm", b"disk B" * 100)
                handle.writestr("Game (User disk).hdm", b"user" * 100)
            descriptor = [{
                "name": "game.zip",
                "size": archive.stat().st_size,
                "sha1": hashlib.sha1(primary).hexdigest(),
                "md5": hashlib.md5(primary, usedforsecurity=False).hexdigest(),
                "hash_scope": "archive_single_member",
            }]
            Client._validate_direct(archive, descriptor)

    def test_nested_multidisk_zip_creates_np2kai_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "romm-44.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("set/4D Boxing (Disk A).hdm", b"A" * 32)
                handle.writestr("set/4D Boxing (Disk B).hdm", b"B" * 32)
                handle.writestr("set/4D Boxing (User disk).hdm", b"U" * 32)
                handle.writestr("readme.txt", b"ignored")
            client = Client.__new__(Client)
            client.cache = root
            launch = client._nested_archive_launch_path({"rom_id": 44}, archive)
            self.assertEqual(launch.name, "romm-44.cmd")
            self.assertEqual(
                launch.read_text(),
                'np2kai "4D Boxing (Disk A).hdm" "4D Boxing (User disk).hdm" '
                '"4D Boxing (Disk B).hdm"\n',
            )
            self.assertEqual(
                {item.name for item in launch.parent.iterdir()},
                {
                    "4D Boxing (Disk A).hdm", "4D Boxing (Disk B).hdm",
                    "4D Boxing (User disk).hdm", "romm-44.cmd",
                },
            )

    def test_multidisk_cache_extracts_only_canonical_disks_and_stable_link(self):
        disks = {"Game (Disk A).hdm": b"A" * 32, "Game (Disk B).hdm": b"B" * 32}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "romm-8.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                for name, payload in disks.items():
                    handle.writestr(name, payload)
                handle.writestr("Game.m3u", "Game (Disk A).hdm\nGame (Disk B).hdm\n")
            files = [
                {
                    "name": name, "size": len(payload),
                    "sha1": hashlib.sha1(payload).hexdigest(), "md5": None,
                    "hash_scope": "file",
                }
                for name, payload in disks.items()
            ]
            client = Client.__new__(Client)
            client.cache = root
            launch = client._multidisk_launch_path({"rom_id": 8}, archive, files)
            self.assertTrue(launch.is_symlink())
            self.assertEqual(launch.name, "romm-8.hdm")
            self.assertEqual(launch.read_bytes(), disks["Game (Disk A).hdm"])
            extracted = {item.name for item in (root / "romm-8-disks").iterdir()}
            self.assertEqual(extracted, {"Game (Disk A).hdm", "Game (Disk B).hdm", "romm-8.hdm"})


if __name__ == "__main__":
    unittest.main()
