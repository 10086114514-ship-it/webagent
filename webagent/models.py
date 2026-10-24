"""OpenAI 兼容的模型客户端（纯标准库 urllib，无第三方依赖）。

支持任意兼容 ``/chat/completions`` 的服务商：DeepSeek、OpenAI、Moonshot、
Qwen(DashScope 兼容模式)、Groq、Ollama 本地模型等。用户只需在 config.json
里填 ``base_url`` / ``api_key`` / ``model``。

视觉模型：设置 ``model.vision=true`` 后，会以 ``data:image/png;base64``
把截图附加到用户消息里；纯文本模型则忽略图片。
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from PIL import Image

from .config import Section


class ModelError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ChatResponse:
    content: str
    raw: dict[str, Any]


class ModelClient:
    def __init__(self, cfg: Section) -> None:
        self.base_url = str(cfg.base_url).rstrip("/")
        self.api_key = self._resolve_key(cfg)
        self.model = cfg.model
        self.vision = bool(cfg.vision)
        self.temperature = float(cfg.get("temperature", 0.0))
        self.max_tokens = int(cfg.get("max_tokens", 2000))
        self.timeout_s = float(cfg.get("timeout_s", 60))
        self.extra_headers = dict(cfg.get("extra_headers", {}) or {})
        self.extra_body = dict(cfg.get("extra_body", {}) or {})

    @staticmethod
    def _resolve_key(cfg: Section) -> str:
        direct = str(cfg.get("api_key", "") or "").strip()
        if direct:
            return direct
        env_name = cfg.get("api_key_env", "") or ""
        if env_name:
            return os.environ.get(env_name, "")
        return ""

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def endpoint(self) -> str:
        # 允许 base_url 已带 /chat/completions 或 /v1
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.endpoint()
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise ModelError(
                f"模型接口返回 HTTP {e.code}: {body[:400]}", status=e.code, body=body
            ) from e
        except urllib.error.URLError as e:
            raise ModelError(f"无法连接模型接口 {url}: {e.reason}") from e
        except TimeoutError as e:
            raise ModelError(f"模型接口超时（>{self.timeout_s}s）: {url}") from e

        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ModelError(f"模型返回非 JSON: {body[:300]}") from e

    def chat(self, system: str, user_text: str, image: Image.Image | None = None) -> ChatResponse:
        """一次对话：system + 用户文本（+ 可选图片）。"""
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

        if image is not None and self.vision:
            png = io.BytesIO()
            image.save(png, format="PNG")
            b64 = base64.b64encode(png.getvalue()).decode("ascii")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": user_text})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        payload.update(self.extra_body)

        data = self._request(payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ModelError(f"响应结构异常: {json.dumps(data, ensure_ascii=False)[:300]}") from e
        if isinstance(content, list):  # 部分服务商 content 是分段
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return ChatResponse(content=str(content or ""), raw=data)


def png_base64(img: Image.Image) -> str:
    png = io.BytesIO()
    img.save(png, format="PNG")
    return base64.b64encode(png.getvalue()).decode("ascii")
