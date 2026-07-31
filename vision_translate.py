#!/usr/bin/env python3
"""Server-side vision translation for browser game sessions."""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
IMAGE_SIZE = (640, 400)
ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}


class TranslationError(RuntimeError):
    """Base class for user-facing translation failures."""


class TranslationUnavailableError(TranslationError):
    """The Bridge has no usable translation provider configuration."""


class TranslationProviderError(TranslationError):
    """The configured translation provider could not complete the request."""


SYSTEM_PROMPT = """You translate Japanese game screenshots into Simplified Chinese.
Return JSON only, with this exact shape:
{
  "summary": "optional short overall translation",
  "regions": [
    {
      "x": 0,
      "y": 0,
      "width": 0,
      "height": 0,
      "original": "Japanese text",
      "translation": "Simplified Chinese translation"
    }
  ]
}

Rules:
- Read Japanese in dialog boxes, menus, status text, and vertical writing.
- Coordinates are pixels in the original 640x400 image, with (0,0) at the top left.
- Return one region per readable text block. Keep boxes tight but include all characters.
- Preserve names, honorifics, item names, and sound effects when a literal translation would be misleading.
- Do not invent text. If there is no readable Japanese text, return an empty regions array and an empty summary.
- Keep each translation concise enough to display over a 640x400 game screen.
"""


def _safe_timeout(value: str) -> float:
    try:
        return max(5.0, min(float(value), 120.0))
    except (TypeError, ValueError):
        return 30.0


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        )
    return ""


def _parse_model_json(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        result = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise TranslationProviderError("翻译模型返回了无法解析的结果") from exc
    if not isinstance(result, dict):
        raise TranslationProviderError("翻译模型返回了无效结果")
    return result


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _normalize_result(payload: dict[str, Any], model: str, target_language: str) -> dict[str, Any]:
    regions: list[dict[str, Any]] = []
    for item in payload.get("regions") or []:
        if not isinstance(item, dict):
            continue
        original = str(item.get("original") or "").strip()[:500]
        translation = str(item.get("translation") or "").strip()[:800]
        if not translation:
            continue
        x = max(0.0, min(_number(item.get("x")), IMAGE_SIZE[0]))
        y = max(0.0, min(_number(item.get("y")), IMAGE_SIZE[1]))
        width = max(1.0, min(_number(item.get("width"), 1.0), IMAGE_SIZE[0] - x))
        height = max(1.0, min(_number(item.get("height"), 1.0), IMAGE_SIZE[1] - y))
        regions.append({
            "x": round(x, 2),
            "y": round(y, 2),
            "width": round(width, 2),
            "height": round(height, 2),
            "original": original,
            "translation": translation,
        })
        if len(regions) >= 32:
            break
    summary = str(payload.get("summary") or "").strip()[:1200]
    return {
        "model": model,
        "target_language": target_language,
        "image_width": IMAGE_SIZE[0],
        "image_height": IMAGE_SIZE[1],
        "summary": summary,
        "regions": regions,
    }


class VisionTranslator:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_file: str,
        api_style: str = "openai",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.api_key_file = api_key_file.strip()
        self.api_style = api_style.strip().casefold() or "openai"
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "VisionTranslator":
        return cls(
            base_url=os.getenv("BRIDGE_TRANSLATION_BASE_URL", ""),
            model=os.getenv("BRIDGE_TRANSLATION_MODEL", "gemini-3-flash"),
            api_key_file=os.getenv("BRIDGE_TRANSLATION_API_KEY_FILE", ""),
            api_style=os.getenv("BRIDGE_TRANSLATION_API_STYLE", "openai"),
            timeout=_safe_timeout(os.getenv("BRIDGE_TRANSLATION_TIMEOUT", "30")),
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key_file)

    def _api_key(self) -> str:
        if not self.configured:
            raise TranslationUnavailableError("翻译服务尚未配置")
        try:
            key = Path(self.api_key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TranslationUnavailableError("翻译服务密钥文件不可读取") from exc
        if not key:
            raise TranslationUnavailableError("翻译服务密钥为空")
        return key

    def _openai_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _gemini_url(self) -> str:
        if ":generateContent" in self.base_url:
            return self.base_url
        model = urllib.parse.quote(self.model, safe="")
        if self.base_url.endswith("/v1beta"):
            return f"{self.base_url}/models/{model}:generateContent"
        return f"{self.base_url}/v1beta/models/{model}:generateContent"

    def _request(self, payload: dict[str, Any], key: str, mime_type: str) -> dict[str, Any]:
        if self.api_style in {"gemini", "google", "native-gemini"}:
            url = self._gemini_url()
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "romm-esde-bridge-vision/1",
                "x-goog-api-key": key,
            }
        else:
            url = self._openai_url()
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "romm-esde-bridge-vision/1",
                "Authorization": f"Bearer {key}",
            }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            with contextlib.suppress(Exception):
                exc.read(512)
            raise TranslationProviderError(f"翻译服务返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TranslationProviderError("翻译服务暂时无法连接") from exc
        if len(data) > MAX_RESPONSE_BYTES:
            raise TranslationProviderError("翻译服务返回内容过大")
        try:
            result = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise TranslationProviderError("翻译服务返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise TranslationProviderError("翻译服务返回了无效结果")
        return result

    @staticmethod
    def _openai_text(result: dict[str, Any]) -> str:
        choices = result.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message") or {}
        return _json_text(message.get("content"))

    @staticmethod
    def _gemini_text(result: dict[str, Any]) -> str:
        candidates = result.get("candidates") or []
        if not candidates or not isinstance(candidates[0], dict):
            return ""
        content = candidates[0].get("content") or {}
        return "".join(
            str(part.get("text") or "")
            for part in content.get("parts") or []
            if isinstance(part, dict)
        )

    def translate(self, image: bytes, mime_type: str, target_language: str = "zh-CN") -> dict[str, Any]:
        if mime_type not in ALLOWED_MIME_TYPES:
            raise TranslationError("只支持 PNG、JPEG 或 WebP 截图")
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise TranslationError("截图大小不合法")
        target_language = str(target_language or "zh-CN").strip()[:40]
        if not target_language:
            target_language = "zh-CN"
        key = self._api_key()
        encoded = base64.b64encode(image).decode("ascii")
        if self.api_style in {"gemini", "google", "native-gemini"}:
            payload = {
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [
                    {"text": f"Translate this screenshot to {target_language}."},
                    {"inline_data": {"mime_type": mime_type, "data": encoded}},
                ]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 1600,
                    "responseMimeType": "application/json",
                },
            }
        else:
            payload = {
                "model": self.model,
                "temperature": 0.1,
                "max_tokens": 1600,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": f"Translate this screenshot to {target_language}."},
                        {"type": "image_url", "image_url": {
                            "url": f"data:{mime_type};base64,{encoded}",
                            "detail": "high",
                        }},
                    ]},
                ],
            }
        result = self._request(payload, key, mime_type)
        text = self._gemini_text(result) if self.api_style in {"gemini", "google", "native-gemini"} else self._openai_text(result)
        if not text:
            raise TranslationProviderError("翻译服务没有返回文本")
        return _normalize_result(_parse_model_json(text), self.model, target_language)
