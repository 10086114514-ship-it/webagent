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

# 64 位句柄必须显式声明，否则会被截断成 32 位
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

#: 常见浏览器进程名（小写）。window_match=browser 时用它认窗口，完全不看标题。
BROWSER_EXES = {
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "iexplore.exe", "360se.exe", "360chrome.exe",
    "qqbrowser.exe", "sogouexplorer.exe", "liebao.exe", "maxthon.exe",
}


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


def window_process_name(hwnd: int) -> str:
    """返回窗口所属进程的可执行文件名（小写），如 ``msedge.exe``。"""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)
    return ""


def find_browser_window(prefer_foreground: bool = True) -> WindowInfo | None:
    """按进程名找浏览器窗口，不依赖标签页标题。

    优先返回当前前台的浏览器窗口；否则返回面积最大的那个（最小化的排在后面）。
    """
    candidates: list[WindowInfo] = []
    for info in list_windows(min_width=400, min_height=300):
        try:
            if window_process_name(info.hwnd) in BROWSER_EXES:
                candidates.append(info)
        except Exception:  # pragma: no cover - 个别系统窗口取不到进程
            continue
    if not candidates:
        return None

    visible = [c for c in candidates if not c.iconic] or candidates
    if prefer_foreground:
        fg = foreground_hwnd()
        for info in visible:
            if info.hwnd == fg:
                return info
    return max(visible, key=lambda i: i.rect[2] * i.rect[3])


def find_process_window(process: str) -> WindowInfo | None:
    """按进程名列表（用 | 或逗号分隔）挑窗口，优先非最小化、面积最大者。"""
    exes = {p.strip().lower() for p in process.replace(",", "|").split("|") if p.strip()}
    if not exes:
        return None
    hits: list[WindowInfo] = []
    for info in list_windows(min_width=200, min_height=150):
        try:
            if window_process_name(info.hwnd) in exes:
                hits.append(info)
        except Exception:  # pragma: no cover
            continue
    if not hits:
        return None
    return max(
        [h for h in hits if not h.iconic] or hits,
        key=lambda i: i.rect[2] * i.rect[3],
    )


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
    """按配置算出目标区域；返回 (区域, 窗口句柄或 None)。

    支持三种定位方式：
    - ``window_match: "browser"``  : 按浏览器进程找窗口，完全不看标题（推荐）
    - ``window_match: "process"``  : 按 ``target.process`` 里的进程名找窗口
    - 其它（contains/regex/exact） : 按 ``target.window_title`` 匹配标题
    """
    match = (getattr(cfg.target, "window_match", "contains") or "contains").lower()
    title = getattr(cfg.target, "window_title", "") or ""

    info: WindowInfo | None
    if match in ("browser", "auto"):
        info = find_browser_window(prefer_foreground=True)
        if info is None:
            raise LookupError(
                "没找到任何浏览器窗口（msedge/chrome/firefox…）。请先打开网页；"
                "或把 target.window_match 改成 contains，并用 target.window_title 指定标题。"
            )
    elif match == "process":
        process = getattr(cfg.target, "process", "") or "msedge.exe"
        info = find_process_window(process)
        if info is None:
            raise LookupError(
                f"没找到进程属于 {process!r} 的窗口。用 `python -m webagent windows` "
                f"查看进程名，或在 target.process 里用 | 分隔多个进程。"
            )
    else:
        info = find_window(title, match)
        if info is None:
            raise LookupError(
                f"未找到标题匹配 {title!r} 的窗口。可运行 `python -m webagent windows` "
                f"查看可用标题；或把 target.window_match 改为 browser"
                f"（按浏览器进程查找，不用关心标题）。"
            )

    if user32.IsIconic(info.hwnd):
        user32.ShowWindow(info.hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    use_client = bool(getattr(cfg.target, "client_area", True))
    rect = Rect.from_tuple(client_rect(info.hwnd) if use_client else info.rect)
    if rect.width <= 0 or rect.height <= 0:
        raise ValueError(f"窗口区域无效: {rect}")
    return rect, info.hwnd
