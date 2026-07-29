from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from publish_bootstrap import publish


class PublishBootstrapTests(unittest.TestCase):
    def test_public_bundle_contains_no_authorized_key_or_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with mock.patch.dict(os.environ, {}, clear=True):
                publish(output, "http://romm.example:8080", "http://bridge.example:8090")

            helper = (output / "bootstrap/enable-ssh.ps1").read_text(encoding="utf-8")
            page = (output / "project/index.html").read_text(encoding="utf-8")
            self.assertIn("[string]$PublicKey = ''", helper)
            self.assertNotIn("@@", helper)
            self.assertNotIn("@@", page)
            self.assertIn("http://romm.example:8080", page)

    def test_deployer_public_key_is_injected_from_configured_file(self) -> None:
        public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnly public-test"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key_file = root / "installer.pub"
            key_file.write_text(public_key + "\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"BOOTSTRAP_SSH_PUBLIC_KEY_FILE": str(key_file)},
                clear=True,
            ):
                publish(root / "output", "http://romm.example:8080", "http://bridge.example:8090")

            helper = (root / "output/bootstrap/enable-ssh.ps1").read_text(encoding="utf-8")
            self.assertIn(public_key, helper)
            self.assertNotIn("@@", helper)


if __name__ == "__main__":
    unittest.main()
