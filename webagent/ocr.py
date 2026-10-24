"""文字识别：调用 Windows 内置 OCR（通过 PowerShell 的 WinRT 投影）。

返回的坐标是输入图片像素坐标（相对截图左上角）。
零第三方依赖；仅在需要喂给文本模型或辅助视觉模型定位时使用。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

PKG_DIR = Path(__file__).resolve().parent
PS_OCR = PKG_DIR / "ps_ocr.ps1"
if not PS_OCR.exists():  # 兼容旧版仓库布局
    PS_OCR = PKG_DIR.parent / "tools" / "ps_ocr.ps1"


@dataclass(frozen=True)
class OcrWord:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass(frozen=True)
class OcrResult:
    language: str
    words: list[OcrWord]


class OcrError(RuntimeError):
    pass


def recognize(img: Image.Image, language: str = "zh-Hans", scale: float = 1.0) -> OcrResult:
    """对 PIL 图像做 OCR。scale>1 时先放大以提高小字识别率。"""
    if not PS_OCR.exists():
        raise OcrError(f"缺少 OCR 脚本: {PS_OCR}")

    if scale > 1.0:
        w, h = img.size
        img = img.resize((int(w * scale), int(h * scale)))

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        png = tmpdir / "shot.png"
        out = tmpdir / "ocr.json"
        img.save(png)

        cmd = [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(PS_OCR),
            "-Path", str(png), "-Out", str(out), "-Lang", language,
        ]
        proc = subprocess.run(
            cmd, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not out.exists():
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            raise OcrError(f"OCR 失败 (exit={proc.returncode}): {tail}")

        data = json.loads(out.read_text(encoding="utf-8-sig"))
        if "error" in data:
            raise OcrError(f"OCR 不可用: {data['error']} (语言 {language})")

        words = [
            OcrWord(
                text=w["t"],
                x=int(w["x"] / scale),
                y=int(w["y"] / scale),
                w=max(1, int(w["w"] / scale)),
                h=max(1, int(w["h"] / scale)),
            )
            for w in data.get("words", [])
        ]
        return OcrResult(language=data.get("lang", ""), words=words)


def lines_from_words(words: list[OcrWord], x_gap: float = 0.6, y_gap: float = 0.6) -> list[list[OcrWord]]:
    """把词聚成行（用于把文本和坐标一并交给模型）。"""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.y, w.x))
    med_h = sorted(w.h for w in words)[len(words) // 2] or 12
    rows: list[list[OcrWord]] = []
    cur: list[OcrWord] = []
    cur_y_center = None

    for w in ordered:
        cy = w.y + w.h / 2
        if cur_y_center is None or abs(cy - cur_y_center) <= y_gap * max(med_h, w.h):
            cur.append(w)
            cur_y_center = (cur_y_center * (len(cur) - 1) + cy) / len(cur) if cur_y_center else cy
        else:
            rows.append(cur)
            cur = [w]
            cur_y_center = cy
    if cur:
        rows.append(cur)

    result = []
    for row in rows:
        row = sorted(row, key=lambda w: w.x)
        merged: list[OcrWord] = []
        for w in row:
            if not merged:
                merged.append(w)
                continue
            prev = merged[-1]
            if w.x - prev.right <= x_gap * max(med_h, prev.h):
                # 合并为同一个文本段（中英文 OCR 常把 "1.414" 拆成多个词）
                merged[-1] = OcrWord(
                    text=prev.text + w.text,
                    x=prev.x, y=min(prev.y, w.y),
                    w=w.right - prev.x, h=max(prev.bottom, w.bottom) - min(prev.y, w.y),
                )
            else:
                merged.append(w)
        result.append(merged)
    return result
