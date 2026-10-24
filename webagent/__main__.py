"""命令行入口。

用法示例::

    python -m webagent init            # 生成默认 config.json
    python -m webagent doctor          # 环境自检
    python -m webagent windows         # 列出可定位的窗口标题
    python -m webagent snapshot        # 截图 + OCR，看识别到的文本
    python -m webagent run --dry-run   # 只让模型规划，不执行（默认）
    python -m webagent run --live      # 真正执行
    python -m webagent run --mock --live   # 不联网，点击首个选项（自检）
    python -m webagent watch --live    # 持续循环，F8 暂停/继续，F9 手动，Esc 退出
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import _DPI_MODE, __version__, capture, ocr
from .agent import WebAgent
from .config import Config, ConfigError, load_config, write_default
from .models import ModelError


def _setup_logger(level: str) -> logging.Logger:
    logger = logging.getLogger("webagent")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def _load(args) -> Config:
    path = args.config or (os.environ.get("WEBAGENT_CONFIG") or "config.json")
    cfg = load_config(path)
    unknown = cfg.unknown_keys()
    if unknown:
        print(f"[提示] 配置里有未识别字段（可能拼写错误）: {', '.join(unknown)}")
    return cfg


# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    p = write_default(args.config or "config.json")
    print(f"已生成默认配置: {p}")
    print("编辑 model.base_url / model.api_key_env / model.model / target.window_title 后即可使用。")
    return 0


def cmd_windows(_args) -> int:
    for w in capture.list_windows():
        print(w.human)
    return 0


def cmd_doctor(args) -> int:
    cfg = _load(args)
    print(f"Python        : {sys.version.split()[0]}")
    print(f"DPI 感知      : {_DPI_MODE}")

    try:
        from PIL import Image  # noqa: F401
        import PIL
        print(f"Pillow        : {PIL.__version__}")
    except Exception as e:  # pragma: no cover
        print(f"Pillow        : 缺失（{e}）")
        return 1

    vs = capture.virtual_screen_rect()
    print(f"虚拟屏幕      : {vs[2]}x{vs[3]} @ {vs[0]},{vs[1]}")

    title = cfg.target.window_title
    info = capture.find_window(title, cfg.target.window_match)
    print(f"目标窗口      : {title!r} -> {'找到 ' + info.human if info else '未找到（先打开网页）'}")

    ocr_ok = ocr.PS_OCR.exists()
    print(f"OCR 脚本      : {'存在' if ocr_ok else '缺失 ' + str(ocr.PS_OCR)}")

    m = cfg.model
    masked = ""
    key = m.api_key or os.environ.get(m.api_key_env, "")
    if key:
        masked = key[:4] + "…" + key[-4:] if len(key) > 8 else "***"
    print(f"模型          : {m.base_url} / {m.model}  vision={m.vision}  mock={m.get('mock', False)}")
    print(f"API key       : {'已配置 ' + masked if masked else '未配置（模型需要 key）'}")
    print(f"执行模式      : 默认 dry-run（需 --live 才真正点击）")
    return 0


def cmd_snapshot(args) -> int:
    cfg = _load(args)
    logger = _setup_logger(cfg.runtime.log_level)
    agent = WebAgent(cfg, logger)
    img, rect, _hwnd, result = agent.snapshot()
    print(f"区域: {rect}  尺寸: {img.size[0]}x{img.size[1]}")
    out = args.out or cfg.resolve(cfg.runtime.debug_dir) / "snapshot.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"截图已保存: {out}")

    if result:
        print(f"OCR 语言: {result.language}  词数: {len(result.words)}")
        plain, lines = agent._page_text(img, result)
        print("—— 识别文本 ——")
        print(plain)
        if args.lines:
            print("—— 带坐标行 ——")
            for ln in lines:
                print(ln)
    else:
        print("（未启用 OCR，或 OCR 不可用）")
    return 0


def cmd_run(args) -> int:
    cfg = _load(args)
    if args.instruction:
        cfg.set("agent.instruction", args.instruction)
    if args.mock or args.mock_live:
        cfg.set("model.mock", True)

    logger = _setup_logger(cfg.runtime.log_level)
    dry_run = not args.live and not args.mock_live
    agent = WebAgent(cfg, logger)
    report = agent.run_once(force=bool(args.force), dry_run=dry_run)
    if args.verbose and report.plan:
        import json
        print(json.dumps(report.plan, ensure_ascii=False, indent=2))
    print(f"ok={report.ok} steps={report.steps} | {report.message}")
    return 0 if report.ok else 1


def cmd_watch(args) -> int:
    cfg = _load(args)
    if args.instruction:
        cfg.set("agent.instruction", args.instruction)
    if args.mock or args.mock_live:
        cfg.set("model.mock", True)

    logger = _setup_logger(cfg.runtime.log_level)
    dry_run = not args.live and not args.mock_live
    agent = WebAgent(cfg, logger)

    pause = threading.Event()
    stop = threading.Event()
    trigger = threading.Event()

    def on_toggle():
        if pause.is_set():
            pause.clear(); logger.info("▶ 继续")
        else:
            pause.set(); logger.info("⏸ 暂停")

    from .hotkeys import HotkeyWatcher
    HotkeyWatcher({
        cfg.runtime.hotkey_toggle: on_toggle,
        cfg.runtime.hotkey_once: trigger.set,
        cfg.runtime.hotkey_abort: stop.set,
    }).start()

    interval = float(cfg.runtime.interval_s)
    logger.info("watch 模式：%s（F8 暂停/继续，F9 立即执行一次，Esc 退出）",
                "dry-run" if dry_run else "live")
    try:
        while not stop.is_set():
            if not pause.is_set() or trigger.is_set():
                trigger.clear()
                report = agent.run_once(force=False, dry_run=dry_run)
                if not report.ok:
                    logger.warning(report.message)
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("已退出")
    return 0


def cmd_key(args) -> int:
    """把 API key 直接写入 config.json，免手动编辑。"""
    cfg_path = args.config or os.environ.get("WEBAGENT_CONFIG") or "config.json"
    p = Path(cfg_path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    if not p.exists():
        write_default(p)

    data = json.loads(p.read_text(encoding="utf-8-sig"))
    data.setdefault("model", {})

    key = (args.value or "").strip()
    if not key:
        key = input("请粘贴 API key 后回车: ").strip()
    if not key:
        print("没有输入 key，已取消。")
        return 1

    data["model"]["api_key"] = key
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    masked = key[:4] + "…" + key[-4:] if len(key) > 8 else "***"
    print(f"已写入: {p}")
    print(f"API key: {masked}")
    print("提示：若你的服务商不是 DeepSeek，请再把 model.base_url 和 model.model 改掉。")
    return 0


def cmd_demo(args) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    page = os.path.join(here, "demo", "quiz.html")
    if not os.path.exists(page):
        print("未找到 demo/quiz.html")
        return 1
    print("请在浏览器打开以下文件（推荐 Edge/Chrome）：")
    print(f"  file:///{page.replace(os.sep, '/')}")
    print("然后把 config.json 的 target.window_title 设为该页面标题（默认就是“在线测验”）。")
    try:
        os.startfile(page)  # type: ignore[attr-defined]
        print("已尝试用默认浏览器打开。")
    except Exception as e:
        print(f"自动打开失败（可手动打开）: {e}")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webagent", description="大模型驱动的网页操作智能体")
    p.add_argument("-V", "--version", action="version", version=f"webagent {__version__}")
    p.add_argument("--config", help="配置文件路径（默认 config.json 或 $WEBAGENT_CONFIG）")

    # 让 --config 也可写在子命令之后（argparse 用 SUPPRESS 避免覆盖主解析器的值）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", dest="config", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common], help="生成默认配置 config.json").set_defaults(func=cmd_init)
    sub.add_parser("doctor", parents=[common], help="环境自检").set_defaults(func=cmd_doctor)
    sub.add_parser("windows", parents=[common], help="列出可定位的窗口").set_defaults(func=cmd_windows)

    kp = sub.add_parser("key", parents=[common], help="写入 API key（免手工编辑配置文件）")
    kp.add_argument("value", nargs="?", help="API key；不填则交互式粘贴")
    kp.set_defaults(func=cmd_key)

    sp = sub.add_parser("snapshot", parents=[common], help="截图 + OCR 预览")
    sp.add_argument("--out", help="截图输出路径")
    sp.add_argument("--lines", action="store_true", help="同时打印带坐标的行")
    sp.set_defaults(func=cmd_snapshot)

    rp = sub.add_parser("run", parents=[common], aliases=["once"], help="执行一轮")
    rp.add_argument("--live", action="store_true", help="真正执行动作（默认 dry-run 只规划）")
    rp.add_argument("--dry-run", action="store_true", help="只规划不执行（默认行为）")
    rp.add_argument("--mock", action="store_true", help="不联网，用脚本化计划")
    rp.add_argument("--mock-live", action="store_true", help="mock 且真正执行（点击首个选项）")
    rp.add_argument("--force", action="store_true", help="忽略冷却，强制执行")
    rp.add_argument("--instruction", help="覆盖任务指令")
    rp.add_argument("--verbose", "-v", action="store_true", help="打印完整计划 JSON")
    rp.set_defaults(func=cmd_run)

    wp = sub.add_parser("watch", parents=[common], help="持续循环")
    wp.add_argument("--live", action="store_true", help="真正执行动作")
    wp.add_argument("--mock", action="store_true", help="不联网")
    wp.add_argument("--mock-live", action="store_true", help="mock 且执行")
    wp.add_argument("--instruction", help="覆盖任务指令")
    wp.set_defaults(func=cmd_watch)

    dp = sub.add_parser("demo", parents=[common], help="打开本地示例网页")
    dp.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    # 统一 UTF-8 输出，避免中文在重定向/管道下乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ModelError, ocr.OcrError, LookupError, ValueError, OSError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
