"""屏幕/窗口捕获（纯 ctypes GDI，无第三方依赖）+ 窗口定位。

启动即声明 Per-Monitor DPI 感知，保证 ``GetWindowRect`` / GDI 截图 /
``SetCursorPos`` 使用同一套物理像素坐标。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import threading
import time
from dataclasses import dataclass

from PIL import Image

from .config import Config

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # pragma: no cover
    shcore = None


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #
def enable_dpi_awareness() -> str:
    if hasattr(user32, "SetProcessDpiAwarenessContext"):
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PM_AWARE_V2
            return "per-monitor-v2"
    if shcore is not None and hasattr(shcore, "SetProcessDpiAwareness"):
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:  # pragma: no cover
        return "none"


def virtual_screen_rect() -> tuple[int, int, int, int]:
    x = user32.GetSystemMetrics(76)
    y = user32.GetSystemMetrics(77)
    w = user32.GetSystemMetrics(78)
    h = user32.GetSystemMetrics(79)
    return x, y, w, h


# --------------------------------------------------------------------------- #
# 窗口
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]
    pid: int
    iconic: bool = False

    @property
    def human(self) -> str:
        l, t, w, h = self.rect
        flag = " [最小化]" if self.iconic else ""
        return f"[{self.hwnd}] {self.title!r} {w}x{h}@{l},{t} pid={self.pid}{flag}"


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wt.UINT),
        ("flags", wt.UINT),
        ("showCmd", wt.UINT),
        ("ptMinPosition", wt.POINT),
        ("ptMaxPosition", wt.POINT),
        ("rcNormalPosition", wt.RECT),
        ("rcDevice", wt.RECT),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def list_windows(min_width: int = 300, min_height: int = 150) -> list[WindowInfo]:
    items: list[WindowInfo] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)

        iconic = bool(user32.IsIconic(hwnd))
        rect = wt.RECT()
        if iconic:
            # 最小化时 GetWindowRect 返回 (-21333,-21333)，改用恢复后的位置
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
            if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                rect = wp.rcNormalPosition
            else:
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
        else:
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True

        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if not iconic and (w < min_width or h < min_height):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        items.append(WindowInfo(hwnd, buf.value, (rect.left, rect.top, w, h), pid.value, iconic))
        return True

    user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return items


def find_window(pattern: str, match: str = "contains") -> WindowInfo | None:
    if not pattern:
        return None
    if match == "regex":
        rx = re.compile(pattern, re.IGNORECASE)
        predicate = lambda t: bool(rx.search(t))  # noqa: E731
    elif match == "exact":
        predicate = lambda t: t == pattern  # noqa: E731
    else:
        needle = pattern.lower()
        predicate = lambda t: needle in t.lower()  # noqa: E731

    for info in list_windows():
        if predicate(info.title):
            return info
    return None


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetClientRect 失败: hwnd={hwnd}")
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow())


def activate_window(hwnd: int) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if foreground_hwnd() == hwnd:
        return True
    current = kernel32.GetCurrentThreadId()
    target = user32.GetWindowThreadProcessId(hwnd, None)
    user32.AttachThreadInput(current, target, True)
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        user32.AttachThreadInput(current, target, False)
    return ok or foreground_hwnd() == hwnd


# --------------------------------------------------------------------------- #
# 截图
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @classmethod
    def from_tuple(cls, t: tuple[int, int, int, int]) -> "Rect":
        return cls(*[int(v) for v in t])

    def to_tuple(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.width, self.height

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2

    def __str__(self) -> str:
        return f"{self.width}x{self.height} @ {self.left},{self.top}"


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0

_capture_lock = threading.Lock()


def grab(rect: Rect) -> Image.Image:
    """截取屏幕区域，返回 RGB PIL 图像（物理像素）。"""
    w, h = max(1, rect.width), max(1, rect.height)
    with _capture_lock:
        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        old = gdi32.SelectObject(hdc_mem, hbmp)
        try:
            gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, rect.left, rect.top, SRCCOPY)

            buf = ctypes.create_string_buffer(w * h * 4)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h  # 负高度 = 自上而下
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB
            got = gdi32.GetDIBits(
                hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS
            )
            if got == 0:
                raise OSError(f"GetDIBits 失败: {ctypes.get_last_error()}")
            return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        finally:
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)


def current_region(cfg: Config) -> tuple[Rect, int | None]:
    """按配置算出目标区域；返回 (区域, 窗口句柄或 None)。"""
    title = cfg.target.window_title
    info = find_window(title, cfg.target.window_match)
    if info is None:
        raise LookupError(
            f"未找到标题匹配 {title!r} 的窗口。请打开目标网页后运行 "
            f"`python -m webagent windows` 查看可用标题。"
        )
    if user32.IsIconic(info.hwnd):
        user32.ShowWindow(info.hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    use_client = bool(getattr(cfg.target, "client_area", True))
    rect = Rect.from_tuple(client_rect(info.hwnd) if use_client else info.rect)
    if rect.width <= 0 or rect.height <= 0:
        raise ValueError(f"窗口区域无效: {rect}")
    return rect, info.hwnd
