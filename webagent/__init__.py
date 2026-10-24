"""webagent —— 由大模型驱动的网页操作智能体。

屏幕内容（网页）→ 截图 + OCR → 任意 OpenAI 兼容模型 → 动作计划 → 鼠标/键盘执行。
"""

from __future__ import annotations

__version__ = "1.0.0"

# 进程启动即声明 DPI 感知，保证截图与点击坐标系一致。
from . import capture as _capture

_DPI_MODE = _capture.enable_dpi_awareness()

__all__ = ["__version__", "_DPI_MODE"]
