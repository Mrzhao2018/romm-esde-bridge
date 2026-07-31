from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vision_translate import TranslationUnavailableError, VisionTranslator


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self.payload


class VisionTranslateTests(unittest.TestCase):
    def test_unconfigured_translator_fails_without_network(self) -> None:
        translator = VisionTranslator(
            base_url="", model="gemini-3-flash", api_key_file="",
        )
        with self.assertRaises(TranslationUnavailableError):
            translator.translate(b"image", "image/png")

    def test_openai_compatible_request_parses_regions_and_clamps_coordinates(self) -> None:
        captured: dict[str, object] = {}
        model_result = {
            "choices": [{"message": {"content": """```json
{
  "summary": "测试翻译",
  "regions": [{
    "x": -10, "y": 390, "width": 900, "height": 80,
    "original": "今日はいい天気ですね。",
    "translation": "今天天气真好啊。"
  }]
}
```"""}}],
        }

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(json.dumps(model_result).encode("utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "translation.key"
            key_path.write_text("test-key\n", encoding="utf-8")
            translator = VisionTranslator(
                base_url="https://proxy.invalid/v1",
                model="gemini-3-flash",
                api_key_file=str(key_path),
            )
            with patch("vision_translate.urllib.request.urlopen", fake_urlopen):
                result = translator.translate(b"image", "image/png")

        request = captured["request"]
        assert request is not None
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")  # type: ignore[union-attr]
        payload = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        self.assertEqual(payload["model"], "gemini-3-flash")
        self.assertEqual(result["summary"], "测试翻译")
        self.assertEqual(result["regions"][0]["x"], 0.0)
        self.assertEqual(result["regions"][0]["y"], 390.0)
        self.assertEqual(result["regions"][0]["width"], 640.0)
        self.assertEqual(result["regions"][0]["height"], 10.0)

    def test_native_gemini_request_uses_header_and_generate_content_shape(self) -> None:
        captured: dict[str, object] = {}
        model_result = {
            "candidates": [{"content": {"parts": [{"text": '{"regions": []}'}]}}],
        }

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(json.dumps(model_result).encode("utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "translation.key"
            key_path.write_text("gemini-key\n", encoding="utf-8")
            translator = VisionTranslator(
                base_url="https://proxy.invalid",
                model="gemini-3-flash",
                api_key_file=str(key_path),
                api_style="gemini",
            )
            with patch("vision_translate.urllib.request.urlopen", fake_urlopen):
                result = translator.translate(b"image", "image/png")

        request = captured["request"]
        assert request is not None
        self.assertEqual(request.get_header("X-goog-api-key"), "gemini-key")  # type: ignore[union-attr]
        self.assertIn("/v1beta/models/gemini-3-flash:generateContent", request.full_url)  # type: ignore[union-attr]
        payload = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        self.assertIn("system_instruction", payload)
        self.assertIn("inline_data", payload["contents"][0]["parts"][1])
        self.assertEqual(result["regions"], [])


if __name__ == "__main__":
    unittest.main()
