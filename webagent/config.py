"""配置加载：JSON -> 嵌套只读命名空间，缺失项自动回落默认值。

配置里所有以 ``_`` 开头的顶层键仅作注释用途，会被忽略。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """配置错误。"""


#: 全部默认值。用户配置只需写需要覆盖的部分。
DEFAULTS: dict[str, Any] = {
    # —— 目标浏览器窗口 ——
    "target": {
        # browser = 按浏览器进程找窗口（推荐：换网页、换标题都不用改配置）
        # process = 按 target.process 里的进程名找
        # contains / regex / exact = 按 target.window_title 匹配标题
        "window_match": "browser",
        "window_title": "在线测验",  # 仅标题模式下使用
        "process": "",              # 仅 process 模式使用，如 "msedge.exe|chrome.exe"
        "client_area": True,        # True=只取网页视口；False=含浏览器边框
        "activate": True,           # 每次操作前把窗口切到前台
    },
    # —— 模型接入（OpenAI 兼容协议，可换任意服务商）——
    "model": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",               # 留空则读环境变量 api_key_env
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
        "vision": False,             # 模型是否支持图片输入（图片会以 base64 附加）
        "temperature": 0.0,
        "max_tokens": 2000,
        "timeout_s": 60,
        "mock": False,               # True=不联网，返回脚本化动作用于自检
        "extra_headers": {},         # 附加 HTTP 头，如 {"x-foo":"bar"}
        "extra_body": {},            # 合并进请求体的额外字段
    },
    # —— 文字识别（给文本模型“眼睛”，也帮视觉模型精确定位）——
    "ocr": {
        "enabled": True,
        "language": "zh-Hans",
        "scale": 1.0,                # OCR 前放大倍数，小字可设 1.5~2.0
    },
    # —— 智能体 ——
    "agent": {
        "instruction": "",           # 任务指令，空则用内置默认
        "max_steps": 20,             # 单次循环最多动作轮数
        "step_delay_s": 0.5,         # 每轮之间间隔
        "cooldown_s": 2.0,           # 同一“状态指纹”多久内不重复执行
        "done_guard": True,          # 模型想结束时，若还有未答题目则要求继续（单页多题用）
        "force_vision_when_no_ocr": True,
    },
    "action": {
        "type_mode": "paste",        # paste=剪贴板粘贴（中文可靠）；unicode=逐键输入
        "click_delay_ms": 120,
        "scroll_step": 300,          # 滚轮一次像素
    },
    "runtime": {
        "interval_s": 2.0,           # watch 模式采样间隔
        "hotkey_toggle": "F8",
        "hotkey_once": "F9",
        "hotkey_abort": "ESC",
        "debug_dir": "debug",
        "save_debug": True,
        "log_level": "INFO",
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Section:
    __slots__ = ("_data", "_path")

    def __init__(self, data: dict[str, Any], path: str = "") -> None:
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        data = object.__getattribute__(self, "_data")
        if name not in data:
            raise ConfigError(f"配置项不存在: {self._path}{name}")
        value = data[name]
        if isinstance(value, dict):
            return Section(value, f"{self._path}{name}.")
        return value

    def __getitem__(self, name: str) -> Any:
        data = object.__getattribute__(self, "_data")
        if name not in data:
            raise ConfigError(f"配置项不存在: {self._path}{name}")
        return data[name]

    def get(self, name: str, default: Any = None) -> Any:
        data = object.__getattribute__(self, "_data")
        return data.get(name, default)

    # 使 Section 可当只读字典用（dict(section)、section.keys() 等）
    def keys(self) -> list[str]:
        return list(object.__getattribute__(self, "_data").keys())

    def values(self) -> list[Any]:
        return list(object.__getattribute__(self, "_data").values())

    def items(self) -> list[tuple[str, Any]]:
        return list(object.__getattribute__(self, "_data").items())

    def __iter__(self):
        return iter(object.__getattribute__(self, "_data"))

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_data"))

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(object.__getattribute__(self, "_data"))

    def __contains__(self, name: str) -> bool:
        return name in object.__getattribute__(self, "_data")

    def __repr__(self) -> str:  # pragma: no cover
        return f"Section({self._path.rstrip('.')}={self._data!r})"


class Config:
    __slots__ = ("_root", "path", "base_dir")

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        object.__setattr__(self, "_root", Section(data, ""))
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "base_dir", path.parent)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_root"), name)

    def __getitem__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_root"), name)

    def resolve(self, value: str | Path) -> Path:
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = self.base_dir / p
        return p

    def as_dict(self) -> dict[str, Any]:
        return object.__getattribute__(self, "_root").as_dict()

    def set(self, dotted: str, value: Any) -> "Config":
        """按点号路径就地写入，如 cfg.set('model.mock', True)。"""
        data = object.__getattribute__(self, "_root")._data
        parts = dotted.split(".")
        d = data
        for p in parts[:-1]:
            if p not in d or not isinstance(d[p], dict):
                raise ConfigError(f"配置路径不存在: {dotted}")
            d = d[p]
        d[parts[-1]] = value
        return self

    def unknown_keys(self) -> list[str]:
        found: list[str] = []

        def walk(default: dict[str, Any], actual: dict[str, Any], prefix: str) -> None:
            for key, value in actual.items():
                if key.startswith("_"):
                    continue
                dotted = f"{prefix}{key}"
                if key not in default:
                    found.append(dotted)
                elif isinstance(value, dict) and isinstance(default[key], dict):
                    walk(default[key], value, dotted + ".")

        walk(DEFAULTS, self.as_dict(), "")
        return found


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path is not None else Path("config.json")
    p = p.expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()

    if not p.exists():
        if path is not None:
            raise ConfigError(f"配置文件不存在: {p}")
        return Config(copy.deepcopy(DEFAULTS), p)

    raw = json.loads(p.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是对象: {p}")
    return Config(_deep_merge(DEFAULTS, raw), p)


def write_default(path: str | Path = "config.json") -> Path:
    """写出带注释的默认配置，便于用户编辑。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    data = copy.deepcopy(DEFAULTS)
    data["_comment"] = {
        "说明": "所有字段均可省略，缺失时使用默认值。以 _ 开头的键仅作注释。",
        "model 接入": "base_url 指向任意 OpenAI 兼容服务，如 DeepSeek/OpenAI/Qwen/Moonshot/Ollama。api_key 留空时读取环境变量。",
        "vision 说明": "若模型支持图片请设 model.vision=true；否则保持 false，程序会自动用 OCR 文本喂给模型。",
        "窗口定位": "打开目标网页后运行: python -m webagent windows 查看可用窗口标题。",
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
