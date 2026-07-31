from __future__ import annotations

import threading
import unittest
from pathlib import Path
import tempfile
import zipfile
import hashlib
import io

from pc98_sessions import BrowserSession, PC98SessionManager, SessionError, safe_file_name
from serve import BridgeHandler


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))}
        self.stream = io.BytesIO(payload)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        self.stream.close()

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


def make_session(*, ticket: str = "ticket") -> BrowserSession:
    process = FakeProcess()
    return BrowserSession(
        session_id="a" * 32,
        user_id=7,
        username="tester",
        token="romm-token",
        device_id=None,
        rom={"id": 42, "name": "Test"},
        workdir=Path("/tmp/romm-test-session"),
        display=90,
        command_port=10001,
        vnc_port=10002,
        websocket_port=10003,
        ticket=ticket,
        started_at="2026-01-01T00:00:00+00:00",
        selected_disks=[{"file_id": 1, "file_name": "disk-a.hdm"}],
        content_path=Path("/tmp/romm-test-session/game.hdm"),
        xvfb=process,
        retroarch=process,
        x11vnc=process,
        websockify=process,
    )


class PC98SessionTests(unittest.TestCase):
    def test_command_file_names_reject_quotes_and_control_characters(self) -> None:
        self.assertEqual(safe_file_name("Disk A.hdm"), "Disk A.hdm")
        for value in ('bad"name.hdm', "bad\nname.hdm", "bad\x00name.hdm"):
            with self.assertRaises(SessionError):
                safe_file_name(value)

    def test_session_is_not_alive_when_vnc_process_has_exited(self) -> None:
        session = make_session()
        session.x11vnc = FakeProcess(1)
        self.assertFalse(session.process_alive())

    def test_ticket_authorizes_session_without_user_token(self) -> None:
        session = make_session()
        manager = PC98SessionManager.__new__(PC98SessionManager)
        manager._lock = threading.RLock()
        manager._sessions = {session.session_id: session}

        self.assertIs(manager.get_by_ticket(session.session_id, "ticket"), session)
        with self.assertRaises(SessionError):
            manager.get_by_ticket(session.session_id, "wrong")

    def test_nested_archive_extracts_all_disks_in_boot_order(self) -> None:
        manager = PC98SessionManager.__new__(PC98SessionManager)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "game.zip"
            target = root / "disks"
            target.mkdir()
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("set/Game (Disk A).hdm", b"A")
                handle.writestr("set/Game (Disk B).hdm", b"B")
                handle.writestr("set/Game (User disk).hdm", b"U")
            paths, descriptors = manager._expand_nested_archive(
                archive,
                target,
                {"file_id": 10, "file_name": "game.zip", "role": "canonical"},
            )
            self.assertEqual([path.name for path in paths], [
                "Game (Disk A).hdm", "Game (User disk).hdm", "Game (Disk B).hdm",
            ])
            self.assertEqual([item["slot"] for item in descriptors], [0, 1, 2])

    def test_nested_archive_download_validates_inner_hash_before_replace(self) -> None:
        manager = PC98SessionManager.__new__(PC98SessionManager)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_io = io.BytesIO()
            with zipfile.ZipFile(archive_io, "w") as handle:
                handle.writestr("game.hdm", b"disk data")
            payload = archive_io.getvalue()
            manager._request = lambda *_args, **_kwargs: FakeResponse(payload)
            target = root / "game.zip"
            manager._download_file(
                "token",
                {
                    "file_id": 1,
                    "file_name": "game.zip",
                    "file_size_bytes": len(payload),
                    "sha1_hash": hashlib.sha1(b"disk data").hexdigest(),
                    "md5_hash": hashlib.md5(b"disk data", usedforsecurity=False).hexdigest(),
                    "hash_scope": "archive_single_member",
                },
                target,
            )
            self.assertEqual(target.read_bytes(), payload)

    def test_audio_socket_parser_handles_masked_fragmented_close(self) -> None:
        payload = b"bye"
        mask = b"abcd"
        encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        frame = bytes((0x88, 0x80 | len(payload))) + mask + encoded
        buffer = bytearray()
        self.assertEqual(BridgeHandler._consume_websocket_frames(buffer, frame[:3]), [])
        self.assertEqual(
            BridgeHandler._consume_websocket_frames(buffer, frame[3:]),
            [(0x8, payload)],
        )

    def test_save_state_does_not_upload_stale_file(self) -> None:
        manager = PC98SessionManager.__new__(PC98SessionManager)
        manager.ffmpeg_path = "/nonexistent/ffmpeg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = make_session()
            session.workdir = root
            state_path, _ = manager._state_paths(root, session.rom_id)
            state_path.parent.mkdir(parents=True)
            state_path.write_bytes(b"old-state")

            def save_command(_session: BrowserSession, command: str) -> None:
                self.assertEqual(command, "SAVE_STATE")
                state_path.write_bytes(b"new-state")

            manager._send_command = save_command
            manager._upload_state = lambda _session, path, _screenshot, slot=None: {
                "uploaded": path.read_bytes(), "slot": slot,
            }

            result = manager.save_state(session)
            self.assertEqual(result["uploaded"], b"new-state")

    def test_request_log_redacts_session_ticket(self) -> None:
        line = "GET /pc98/player.html?session=abc&ticket=private-value HTTP/1.1"
        self.assertEqual(
            BridgeHandler._redact_request_line(line),
            "GET /pc98/player.html?session=abc&ticket=<redacted> HTTP/1.1",
        )


if __name__ == "__main__":
    unittest.main()
