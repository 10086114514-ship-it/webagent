"""鼠标 / 键盘 / 滚轮 / 剪贴板操作（纯 ctypes，无第三方依赖）。

所有坐标均为物理像素。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# —— 关键：声明 64 位句柄类型，否则 GlobalAlloc/GlobalLock 返回值会被截断成 32 位，
#         导致写剪贴板时访问 0 地址崩溃。——
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p

user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wt.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p

# —— 常量 ——
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

VK = {
    "enter": 0x0D, "tab": 0x09, "space": 0x20, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "ctrl": 0x11, "shift": 0x10,
    "alt": 0x12, "v": 0x56, "a": 0x41, "c": 0x43, "f5": 0x74,
}
# 字母/数字键：A-Z = 0x41-0x5A，0-9 = 0x30-0x39
for _i, _ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    VK.setdefault(_ch.lower(), 0x41 + _i)
for _i in range(10):
    VK.setdefault(str(_i), 0x30 + _i)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTUNION)]


def _send_input(*inputs: INPUT) -> None:
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput 失败: 发送 {sent}/{len(inputs)}")


def _key_input(vk: int, up: bool = False) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    return inp


def move(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def click(x: int, y: int, delay_ms: int = 120) -> None:
    move(x, y)
    time.sleep(0.03)
    _send_input(
        _mouse_input(MOUSEEVENTF_LEFTDOWN),
        _mouse_input(MOUSEEVENTF_LEFTUP),
    )
    time.sleep(delay_ms / 1000)


def _mouse_input(flags: int, wheel: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = MOUSEINPUT(0, 0, wheel, flags, 0, None)
    return inp


def scroll(direction: str, amount: int = 300) -> None:
    """direction: up | down"""
    sign = -1 if direction == "up" else 1
    clicks = max(1, amount // WHEEL_DELTA)
    for _ in range(clicks):
        _send_input(_mouse_input(MOUSEEVENTF_WHEEL, sign * WHEEL_DELTA))
        time.sleep(0.01)


def press_key(name: str) -> None:
    vk = VK.get(str(name).lower(), None)
    if vk is None:
        raise ValueError(f"未知按键: {name!r}（支持: {sorted(VK)}）")
    _send_input(_key_input(vk, False), _key_input(vk, True))


def hotkey(*names: str) -> None:
    """组合键，如 hotkey('ctrl','v')。"""
    vks = [VK[str(n).lower()] for n in names]
    down = [_key_input(v, False) for v in vks]
    up = [_key_input(v, True) for v in reversed(vks)]
    _send_input(*down)
    time.sleep(0.03)
    _send_input(*up)


def type_text(text: str) -> None:
    """逐字符 Unicode 输入（对 IME 场景可能不生效，优先用 paste_text）。"""
    for ch in text:
        vk = 0
        scan = ord(ch)
        _send_input(_unicode_input(scan, False), _unicode_input(scan, True))
        time.sleep(0.005)


def _unicode_input(scan: int, up: bool) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    inp.union.ki = KEYBDINPUT(0, scan, flags, 0, None)
    return inp


def set_clipboard(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not hmem:
                    raise OSError(f"GlobalAlloc 失败: {ctypes.get_last_error()}")
                ptr = kernel32.GlobalLock(hmem)
                if not ptr:
                    kernel32.GlobalFree(hmem)
                    raise OSError(f"GlobalLock 失败: {ctypes.get_last_error()}")
                try:
                    ctypes.memmove(ptr, data, len(data))
                finally:
                    kernel32.GlobalUnlock(hmem)
                if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
                    kernel32.GlobalFree(hmem)
                    raise OSError(f"SetClipboardData 失败: {ctypes.get_last_error()}")
                return
            finally:
                user32.CloseClipboard()
        time.sleep(0.05)
    raise OSError("无法打开剪贴板（可能被其他程序占用）")


def paste_text(text: str) -> None:
    set_clipboard(text)
    time.sleep(0.05)
    hotkey("ctrl", "v")
