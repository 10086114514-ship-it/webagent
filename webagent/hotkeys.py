"""全局热键轮询（GetAsyncKeyState，无需提权、无第三方依赖）。"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Callable

_VK = {
    "F8": 0x77, "F9": 0x78, "ESC": 0x1B,
    "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}


class HotkeyWatcher(threading.Thread):
    def __init__(self, mapping: dict[str, Callable[[], None]], poll_s: float = 0.05) -> None:
        super().__init__(daemon=True, name="hotkeys")
        self._map: dict[int, Callable[[], None]] = {}
        for name, fn in mapping.items():
            vk = _VK.get(name.upper())
            if vk is None:
                raise ValueError(f"不支持的热键: {name}（可用 {sorted(_VK)}）")
            self._map[vk] = fn
        self._prev = {vk: False for vk in self._map}
        self.poll_s = poll_s

    def run(self) -> None:
        while True:
            for vk, fn in self._map.items():
                down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not self._prev[vk]:
                    try:
                        fn()
                    except Exception:  # noqa: BLE001 - 热键回调不能崩线程
                        pass
                self._prev[vk] = down
            time.sleep(self.poll_s)
