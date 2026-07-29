#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import bridge


class BridgeTests(unittest.TestCase):
    def test_sanitize_removes_provider_urls_recursively(self) -> None:
        value = {
            "name": "Game",
            "url_cover": "https://provider.invalid/secret",
            "metadata": {
                "box2d_url": "https://provider.invalid/secret",
                "first_release_date": 123,
            },
        }
        self.assertEqual(
            bridge.sanitize(value),
            {"name": "Game", "metadata": {"first_release_date": 123}},
        )

    def test_normalize_rom_separates_canonical_and_alternate_disks(self) -> None:
        rom = {
            "id": 8,
            "platform_id": 283,
            "platform_slug": "pc-9800-series",
            "fs_name": "Dragon Knight",
            "name": "Dragon Knight",
            "files": [
                {
                    "id": 1,
                    "file_name": "Dragon Knight (Disk A).hdm",
                    "file_size_bytes": 10,
                    "category": "game",
                },
                {
                    "id": 2,
                    "file_name": "Dragon Knight (Disk B).hdm",
                    "file_size_bytes": 10,
                    "category": "game",
                },
                {
                    "id": 3,
                    "file_name": "Dragon Knight (Disk A) [Alt 1].hdm",
                    "file_size_bytes": 10,
                    "category": None,
                },
            ],
        }
        normalized = bridge.normalize_rom(
            rom,
            "http://romm.invalid",
            Path("/nonexistent"),
        )
        data = normalized["bridge"]
        self.assertEqual(data["launch_strategy"], "canonical_multidisk")
        self.assertEqual(data["canonical_file_ids"], [1, 2])
        self.assertEqual(data["alternate_file_ids"], [3])
        self.assertEqual(data["canonical_size_bytes"], 20)
        self.assertIn("file_ids=1%2C2", data["canonical_download_url"])

    def test_single_uncategorized_file_becomes_canonical(self) -> None:
        rom = {
            "id": 31,
            "platform_id": 283,
            "platform_slug": "pc-9800-series",
            "fs_name": "Example",
            "name": "Example",
            "files": [
                {
                    "id": 99,
                    "file_name": "Example.zip",
                    "file_size_bytes": 123,
                    "category": None,
                }
            ],
        }
        data = bridge.normalize_rom(
            rom,
            "http://romm.invalid",
            Path("/nonexistent"),
        )["bridge"]
        self.assertEqual(data["launch_strategy"], "direct_file")
        self.assertEqual(data["canonical_file_ids"], [99])

    def test_asset_metadata_reports_local_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "resources/roms/283/1/cover/small.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"image")
            metadata = bridge.local_asset_metadata(
                "http://romm.invalid",
                "/assets/romm/resources/roms/283/1/cover/small.png?ts=1",
                root,
            )
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(metadata["size_bytes"], 5)
            self.assertEqual(
                metadata["url"],
                "http://romm.invalid/assets/romm/resources/roms/283/1/cover/small.png?ts=1",
            )


if __name__ == "__main__":
    unittest.main()
