"""网页操作智能体：截图 → OCR/图片 → 模型 → 动作计划 → 执行 → 循环。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from . import actions, capture, ocr
from .config import Config
from .models import ChatResponse, ModelClient, ModelError

#: 系统提示：定义“协议”（只输出 JSON、动作类型、坐标规范）。
SYSTEM_PROMPT = """你是一个网页操作智能体，负责在网页上执行任务。你会收到网页截图或网页可见文本（含归一化坐标），请输出一个 JSON 动作计划。

坐标系：bbox 用 [x1,y1,x2,y2] 表示，相对整张截图归一化到 0~1（左上 0,0，右下 1,1）。

可用动作：
- {"type":"click","bbox":[x1,y1,x2,y2],"reason":"..."}   点击该区域中心
- {"type":"type","bbox":[x1,y1,x2,y2],"text":"..."}      先点输入框再输入文本
- {"type":"scroll","direction":"down","amount":300}      up/down 滚动页面
- {"type":"key","key":"Enter"}                           支持 enter/tab/space/esc/backspace/up/down/left/right
- {"type":"wait","ms":800}                               等待

输出格式（只输出 JSON 对象本身，不要 markdown 代码块、不要多余解释）：
{"done": false, "reasoning": "简短中文说明", "actions": [ ... ], "result": null}
当任务已完成、或本页当前没有需要执行的动作时，置 done=true，并把结论写入 result（如选择题填 {"question":"…","answer":"B","confidence":0.9}，填空题填 {"answer":"…"}）。

工作方式（按顺序判断当前状态）：
1. 先看【当前状态】。如果显示“已选 X”或“已填 …”，说明本题已经作答，这一步要点击【可交互元素】里的“下一题/提交”按钮，绝对不要重复点击已经选中的选项。
2. 如果本题还没作答：选择题就点击正确选项；填空题就点击输入框并输入答案。
3. 只有【可交互元素】里没有对应元素时，才根据【可见文本】自己估计 bbox。
4. 点击坐标一律优先使用【可交互元素】给出的 bbox，不要改动、不要自己重算。

规则：
1. 只依据当前页面信息行动，不臆造页面里不存在的按钮或文本。
2. 选择题不确定时选最可能的一项，不要空着。
3. 填空题必须点中输入框再输入，输入文本就是答案本身（不要加“答案：”前缀）。
4. 【注意】里若提示“页面没有变化”，说明上一步动作无效，必须换动作，不要原样重试。
5. 长页面（很多题在同一页，只能靠滚动往下看）：本题做完后如果看不到新题目，
   说明内容在下方，先发 {"type":"scroll","direction":"down","amount":600}（done=false），
   滚完下一步再继续作答；滚到底也没有新内容，才置 done=true。
   注意：这种情况下**不要**因为找不到“下一题”按钮就 done=true。
6. 目标在折叠区域时先发 scroll（此时 done=false），下一步再点击。
7. 【已完成】里列出的题目已经答过，**不要重复点击**；滚动后如果又看到它们，直接继续往下。
8. 每次只规划“当前这一步”最合适的动作，不要一次性规划多轮。"""

#: 按钮/输入框关键词，用于从 OCR 里挑出可交互元素。
BUTTON_WORDS = ("下一题", "上一题", "下一步", "上一步", "提交", "确定", "保存",
                "继续", "开始", "返回", "完成", "发送", "登录", "搜索", "查询")
INPUT_WORDS = ("请在此填写", "请输入", "在这里输入", "填写答案", "输入答案", "输入内容")

#: 截图顶部这么多比例是浏览器标签栏/地址栏，识别题目时要跳过。
CHROME_TOP_RATIO = 0.12


DEFAULT_INSTRUCTION = (
    "当前网页是一个在线答题页面。请识别题目与选项，选出正确答案并点击它；"
    "如果是填空题就把答案填进输入框；需要时点击“下一题/提交”继续，直到全部完成。"
    "最终在 result 里汇总你做的题与答案。"
)

MOCK_PLAN: dict[str, Any] = {
    "done": False,
    "reasoning": "mock 模式：自动点击第一个形如选项的行用于自检",
    "actions": [],
    "result": None,
}


@dataclass
class RunReport:
    ok: bool
    steps: int = 0
    plan: dict[str, Any] | None = None
    executed: list[str] = field(default_factory=list)
    message: str = ""
    elapsed_ms: int = 0
    debug_images: list[str] = field(default_factory=list)
    verification: str = ""


def normalize_text(s: str) -> str:
    """全角转半角 + 去多余空白，便于匹配与展示。"""
    out: list[str] = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif ch in "．。·":
            out.append(".")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里稳健地取 JSON（容忍 markdown 围栏、前后废话）。"""
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # 尝试直接解析
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 找第一个 { 到最后一个 } 的平衡片段
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ModelError(f"无法从模型输出解析 JSON：{text[:300]}")


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _bbox_center(bbox: Any, rect: capture.Rect) -> tuple[int, int]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        raise ValueError(f"bbox 无效: {bbox!r}")
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    cx = (_clamp(x1) + _clamp(x2)) / 2
    cy = (_clamp(y1) + _clamp(y2)) / 2
    return int(rect.left + cx * rect.width), int(rect.top + cy * rect.height)


def aggregate_verification(views: list[dict[str, Any]]) -> tuple[bool, str]:
    """汇总回读扫描结果。

    每个 view 形如::

        {"has_options": bool, "has_state": bool,
         "progress": (当前, 总数) | None, "question": str}

    返回 ``(是否通过, 说明)``。判定规则：
    - 某屏有选项但没有任何“已选/已填”标记 → 疑似漏答
    - 进度显示当前题号 < 总题号 → 没做完
    """
    if not views:
        return True, "未采集到页面内容"

    unanswered: list[str] = []
    seen: set[str] = set()
    for view in views:
        if view.get("has_options") and not view.get("has_state"):
            key = (view.get("question") or "").strip()[:36]
            if key and key not in seen:
                seen.add(key)
                unanswered.append(key)

    progress = [v["progress"] for v in views if v.get("progress")]
    cur, total = max(progress, key=lambda p: p[0]) if progress else (0, 0)

    parts = [f"扫描 {len(views)} 屏"]
    if unanswered:
        parts.append(f"{len(unanswered)} 屏未见“已选/已填”标记")
    if total:
        parts.append(f"进度 {cur}/{total}")
    if unanswered:
        parts.append("疑似未作答：" + "；".join(unanswered[:5]))

    ok = not unanswered and not (total and cur < total)
    return ok, "，".join(parts)


class WebAgent:
    def __init__(self, cfg: Config, logger: Any) -> None:
        self.cfg = cfg
        self.log = logger
        self.client = ModelClient(cfg.model)
        self.instruction = cfg.agent.instruction or DEFAULT_INSTRUCTION
        self.mock = bool(cfg.model.get("mock", False))
        self._last_fp: str = ""
        self._last_fp_at: float = 0.0
        self._step_note: str = ""
        # 单页多题场景的“作答台账”：记录已作答题目的摘要与答案，避免重复作答
        self.ledger: list[str] = []
        self._ledger_keys: set[str] = set()
        self._done_nudges: int = 0

    # ------------------------------------------------------------------ #
    def snapshot(self) -> tuple[Image.Image, capture.Rect, int | None, ocr.OcrResult | None]:
        rect, hwnd = capture.current_region(self.cfg)
        if self.cfg.target.activate and hwnd:
            capture.activate_window(hwnd)
            time.sleep(0.15)
        img = capture.grab(rect)

        result = None
        if self.cfg.ocr.enabled:
            try:
                result = ocr.recognize(
                    img,
                    language=self.cfg.ocr.language,
                    scale=float(self.cfg.ocr.scale),
                )
            except ocr.OcrError as e:
                self.log.warning("OCR 失败：%s", e)
        return img, rect, hwnd, result

    # ------------------------------------------------------------------ #
    def _page_text(self, img: Image.Image, result: ocr.OcrResult | None) -> tuple[str, list[str]]:
        """把 OCR 结果整理成带坐标的文本块；返回 (纯文本, 行描述列表)。"""
        if result is None:
            return "", []
        w, h = img.size
        rows = ocr.lines_from_words(result.words, x_gap=0.4, y_gap=0.6)
        lines: list[str] = []
        plain: list[str] = []
        for row in rows:
            text = normalize_text(" ".join(w.text for w in row))
            if not text:
                continue
            l = min(w.x for w in row)
            t = min(w.y for w in row)
            r = max(w.right for w in row)
            b = max(w.bottom for w in row)
            box = [round(l / w, 3), round(t / h, 3), round(r / w, 3), round(b / h, 3)]
            lines.append(f'{json.dumps(box)} "{text}"')
            plain.append(text)
        return "\n".join(plain), lines

    def _fingerprint(self, img: Image.Image, plain: str) -> str:
        small = img.resize((64, 40)).convert("L")
        digest = hashlib.md5(small.tobytes()).hexdigest()[:8]
        return f"{digest}:{hashlib.md5(plain.encode('utf-8')).hexdigest()[:8]}"

    @staticmethod
    def _page_changed(before: Image.Image, after: Image.Image,
                      mean_threshold: float = 0.6, ratio_threshold: float = 0.003) -> bool:
        """判断两次截图之间“内容”是否真的变了。

        只看中间区域：倒计时/时钟一般在页眉页脚，裁剪后基本为零。
        判据取“平均灰度差”或“显著变化像素占比”任一超阈值——
        页面以白底小字为主，整体均值很小（实测点击选项约 2.4，换题约 3.5），
        所以阈值必须放低，否则真变化会被误判成“没变化”。
        """
        if before.size != after.size:
            return True
        w, h = before.size
        box = (int(w * 0.15), int(h * 0.25), int(w * 0.85), int(h * 0.85))
        a = before.crop(box).resize((160, 100)).convert("L").tobytes()
        b = after.crop(box).resize((160, 100)).convert("L").tobytes()
        if not a:
            return True

        total = 0
        strong = 0
        for x, y in zip(a, b):
            d = abs(x - y)
            total += d
            if d > 25:
                strong += 1
        n = len(a)
        return (total / n) > mean_threshold or (strong / n) > ratio_threshold

    # ------------------------------------------------------------------ #
    def _elements(self, img: Image.Image, result: ocr.OcrResult | None) -> tuple[list[str], str]:
        """从 OCR 结果里挑出可交互元素（选项/按钮/输入框）及其精确 bbox，并推断页面状态。

        按“文本段”而不是“整行”给 bbox，避免“已选 C 下一题”被合成一个大框导致点空。
        """
        if result is None:
            return [], ""
        w, h = img.size
        top_limit = h * CHROME_TOP_RATIO  # 跳过浏览器标签栏/地址栏
        items: list[str] = []
        all_text: list[str] = []

        for row in ocr.lines_from_words(result.words, x_gap=0.4, y_gap=0.6):
            for seg in row:
                if seg.y < top_limit:
                    continue
                text = normalize_text(seg.text)
                if not text:
                    continue
                all_text.append(text)
                box = f"[{seg.x / w:.3f},{seg.y / h:.3f},{seg.right / w:.3f},{seg.bottom / h:.3f}]"

                m = re.match(r"^[（(]?\s*([A-Ha-h])\s*[.．、)）:：]\s*\S", text)
                if m:
                    items.append(f"- 选项 {m.group(1).upper()}: bbox={box} 文本={text!r}")
                elif any(k in text for k in BUTTON_WORDS):
                    items.append(f"- 按钮: bbox={box} 文本={text!r}")
                elif any(k in text for k in INPUT_WORDS):
                    items.append(f"- 输入框: bbox={box} 提示={text!r}")

        joined = " ".join(all_text)
        state: list[str] = []
        m = re.search(r"已选\s*[:：]?\s*([A-Ha-h])", joined)
        if m:
            state.append(f"已选 {m.group(1).upper()}（本题已作答）")
        m = re.search(r"已填\s*[:：]?\s*(\S+)", joined)
        if m:
            state.append(f"已填“{m.group(1)}”（本题已作答）")
        return items, "；".join(state)

    # ------------------------------------------------------------------ #
    def _question_text(self, img: Image.Image, result: ocr.OcrResult | None) -> str:
        """从 OCR 里猜出“当前题目”的文本（排除选项、按钮、状态行、浏览器工具栏）。"""
        if result is None:
            return ""
        w, h = img.size
        top_limit = h * CHROME_TOP_RATIO
        cands: list[str] = []
        for row in ocr.lines_from_words(result.words, x_gap=0.4, y_gap=0.6):
            if min(seg.y for seg in row) < top_limit:
                continue
            text = normalize_text(" ".join(seg.text for seg in row))
            if not text:
                continue
            if re.match(r"^[（(]?\s*[A-Ha-h]\s*[.．、)）:：]\s*\S", text):
                continue
            if any(k in text for k in BUTTON_WORDS) or "已选" in text or "已填" in text:
                continue
            if "://" in text or "C:\\" in text or ".html" in text.lower():
                continue
            if len(text) >= 6:  # 太短的多半是页码/图标
                cands.append(text)
        return max(cands, key=len) if cands else ""

    def _progress(self, img: Image.Image, result: ocr.OcrResult | None) -> tuple[int, int] | None:
        """解析“第 3 / 20 题”“3/20”这类进度，返回 (当前, 总数)；总数未知时为 0。"""
        if result is None:
            return None
        w, h = img.size
        top_limit = h * CHROME_TOP_RATIO
        texts = [
            normalize_text(seg.text)
            for row in ocr.lines_from_words(result.words, x_gap=0.4, y_gap=0.6)
            for seg in row
            if seg.y >= top_limit
        ]
        joined = " ".join(texts)
        m = re.search(r"(\d{1,3})\s*/\s*(\d{1,3})", joined)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if 0 < cur <= total <= 999:
                return cur, total
        m = re.search(r"第\s*(\d{1,3})\s*题", joined)
        if m:
            return int(m.group(1)), 0
        return None

    def _update_ledger(self, img: Image.Image, result: ocr.OcrResult | None) -> None:
        """把“已作答”的题目记进台账（同一题只记一次）。"""
        if result is None:
            return
        _elements, state = self._elements(img, result)
        if not state:
            return
        question = self._question_text(img, result)
        key = question[:36]
        if not key or key in self._ledger_keys:
            return
        self._ledger_keys.add(key)
        answer = state.replace("（本题已作答）", "")
        self.ledger.append(f"{len(self.ledger) + 1}. {key} → {answer}")

    def _unfinished_reason(self, img: Image.Image, result: ocr.OcrResult | None) -> str | None:
        """模型想收工时，检查是否还有明显没做完的题目。"""
        if result is None:
            return None
        elements, state = self._elements(img, result)
        if any(item.startswith("- 选项") for item in elements) and not state:
            return "当前视图里还有未作答的选项"
        prog = self._progress(img, result)
        if prog and prog[1] and prog[0] < prog[1]:
            return f"进度显示第 {prog[0]}/{prog[1]} 题"
        return None

    # ------------------------------------------------------------------ #
    # 收工前回读校验：滚回顶部 → 逐屏扫描 → 汇总未作答/进度
    # ------------------------------------------------------------------ #
    def _scroll_to_top(self, rect: capture.Rect, hwnd: int | None = None) -> None:
        last: Image.Image | None = None
        for _ in range(12):
            if hwnd:
                capture.activate_window(hwnd)
            img = capture.grab(rect)
            if last is not None and not self._page_changed(last, img):
                break
            last = img
            actions.scroll("up", 900, at=rect.center)
            time.sleep(0.25)

    def _scan_page(self, rect: capture.Rect, max_scrolls: int, hwnd: int | None = None) -> list[dict[str, Any]]:
        """从当前位置向下逐屏扫描，记录每一屏的选项/已选状态/进度。"""
        views: list[dict[str, Any]] = []
        last_img: Image.Image | None = None
        unchanged = 0
        scale = float(self.cfg.ocr.scale)

        for _ in range(max(1, max_scrolls)):
            if hwnd:
                capture.activate_window(hwnd)  # 防止别的小窗挡住目标页面
            img = capture.grab(rect)
            result = None
            if self.cfg.ocr.enabled:
                try:
                    result = ocr.recognize(img, language=self.cfg.ocr.language, scale=scale)
                except ocr.OcrError as e:
                    self.log.warning("回读时 OCR 失败：%s", e)
            elements, state = self._elements(img, result) if result is not None else ([], "")
            views.append({
                "has_options": any(item.startswith("- 选项") for item in elements),
                "has_state": bool(state),
                "progress": self._progress(img, result),
                "question": self._question_text(img, result),
            })

            if last_img is not None and not self._page_changed(last_img, img):
                unchanged += 1
                if unchanged >= 2:  # 连续两屏没变化 = 到底了
                    break
            else:
                unchanged = 0
            last_img = img

            actions.scroll("down", int(rect.height * 0.75), at=rect.center)
            time.sleep(0.35)

        return views

    def _verify_pass(self, rect: capture.Rect, hwnd: int | None = None) -> tuple[bool, str]:
        """回读校验：滚回顶部后逐屏扫描，判断是否还有没作答的题。"""
        self._scroll_to_top(rect, hwnd)
        views = self._scan_page(rect, int(getattr(self.cfg.agent, "verify_max_scrolls", 12)), hwnd)
        ok, summary = aggregate_verification(views)
        self.log.info("回读校验：%s", summary)
        return ok, summary

    # ------------------------------------------------------------------ #
    def _user_message(self, img: Image.Image, rect: capture.Rect, result: ocr.OcrResult | None, step: int) -> str:
        plain, lines = self._page_text(img, result)
        w, h = img.size
        parts = [f"【任务指令】\n{self.instruction}\n", f"【页面尺寸】{w}x{h}"]

        if result is not None:
            elements, state = self._elements(img, result)
            if state:
                parts.append(f"【当前状态】{state}")
            if elements:
                parts.append("【可交互元素】请直接使用下面的 bbox，不要自己估计坐标：")
                parts.extend(elements[:80])
            if elements and not any(item.startswith("- 按钮") for item in elements):
                parts.append(
                    "【提示】当前视图没有检测到“下一题/提交”这类按钮。"
                    "如果还有题目没做完，说明内容在下方，请先发 scroll 向下滚动（done=false），"
                    "下一步再继续作答；不要因为找不到按钮就直接 done=true。"
                )
            if self.ledger:
                parts.append("【已完成（不要重复作答）】")
                parts.extend(self.ledger[-20:])

        if lines:
            parts.append("【可见文本】每行格式 bbox \"文本\"（归一化坐标）")
            parts.extend(lines[:200])
        elif not self.client.vision:
            parts.append("（本次没有识别到文字，且模型不支持图片，无法继续。）")

        if self._step_note:
            parts.append(f"【注意】{self._step_note}")

        parts.append(f"【进度】这是第 {step + 1} 步（最多 {self.cfg.agent.max_steps} 步）。"
                     f"请依据当前页面给出下一步动作计划。")
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    def _plan(self, img: Image.Image, result: ocr.OcrResult | None, step: int) -> tuple[dict[str, Any], ChatResponse | None]:
        user = self._user_message(img, capture.Rect(0, 0, img.size[0], img.size[1]), result, step)
        if self.mock:
            plan = dict(MOCK_PLAN)
            plan["actions"] = self._mock_actions(img, result)
            plan["done"] = True  # 自检只点一步
            return plan, None
        resp = self.client.chat(SYSTEM_PROMPT, user, image=img if self.client.vision else None)
        return _extract_json(resp.content), resp

    def _mock_actions(self, img: Image.Image, result: ocr.OcrResult | None) -> list[dict[str, Any]]:
        """无网络的自动化自检：点击第一个形如选项(A/B/C/D)的行。"""
        if result is not None:
            w, h = img.size
            for row in ocr.lines_from_words(result.words, x_gap=0.4, y_gap=0.6):
                text = normalize_text(" ".join(wd.text for wd in row))
                if re.match(r"^[（(]?\s*([A-Ha-h])\s*[.．、)）:：]\s*\S", text):
                    l = min(wd.x for wd in row)
                    t = min(wd.y for wd in row)
                    r = max(wd.right for wd in row)
                    b = max(wd.bottom for wd in row)
                    return [{"type": "click",
                             "bbox": [l / w, t / h, r / w, b / h],
                             "reason": f"mock：点击选项 {text}"}]
        cx, cy = img.size[0] // 2, img.size[1] // 2
        return [{"type": "click", "bbox": [cx / img.size[0] - 0.05, cy / img.size[1] - 0.02,
                                          cx / img.size[0] + 0.05, cy / img.size[1] + 0.02],
                 "reason": "mock：点击画面中心"}]

    # ------------------------------------------------------------------ #
    def execute(self, plan: dict[str, Any], img: Image.Image, rect: capture.Rect) -> list[str]:
        log: list[str] = []
        type_mode = self.cfg.action.type_mode

        for act in plan.get("actions", []):
            kind = (act.get("type") or "").lower()
            if kind == "click":
                x, y = _bbox_center(act.get("bbox"), rect)
                actions.click(x, y, delay_ms=int(self.cfg.action.click_delay_ms))
                log.append(f"click ({x},{y})")
            elif kind == "type":
                text = str(act.get("text", ""))
                bbox = act.get("bbox")
                if bbox:
                    x, y = _bbox_center(bbox, rect)
                    actions.click(x, y, delay_ms=150)
                    time.sleep(0.05)
                    actions.hotkey("ctrl", "a")  # 选中已有内容，避免追加
                    time.sleep(0.03)
                if type_mode == "paste":
                    actions.paste_text(text)
                else:
                    actions.type_text(text)
                log.append(f"type {text!r}")
            elif kind == "scroll":
                actions.scroll(
                    str(act.get("direction", "down")),
                    int(act.get("amount", self.cfg.action.scroll_step)),
                    at=rect.center,  # 滚轮只作用于光标下的窗口，先把光标移进页面
                )
                log.append(f"scroll {act.get('direction', 'down')}")
                time.sleep(0.35)  # 等平滑滚动/懒加载稳定，避免截到中间态
            elif kind == "key":
                actions.press_key(str(act.get("key", "Enter")))
                log.append(f"key {act.get('key')}")
            elif kind == "wait":
                time.sleep(min(5.0, max(0, int(act.get("ms", 500))) / 1000))
                log.append(f"wait {act.get('ms')}ms")
            else:
                self.log.warning("忽略未知动作类型: %s", act)
        return log

    # ------------------------------------------------------------------ #
    def run_once(self, force: bool = False, dry_run: bool | None = None) -> RunReport:
        t0 = time.time()
        report = RunReport(ok=False)
        if dry_run is None:
            dry_run = True  # 默认只规划不执行

        try:
            if not self.mock and not self.client.has_key:
                raise ModelError(
                    "未配置 API key。请在 config.json 的 model.api_key 填写，"
                    f"或设置环境变量 {self.cfg.model.api_key_env}；"
                    "若只想自检流水线，可开启 model.mock=true。"
                )

            img, rect, hwnd, result = self.snapshot()
            plain, _ = self._page_text(img, result)
            fp = self._fingerprint(img, plain)

            # 每轮重新开始记账
            self.ledger.clear()
            self._ledger_keys.clear()
            self._done_nudges = 0
            self._update_ledger(img, result)

            if not force and fp == self._last_fp and time.time() - self._last_fp_at < float(self.cfg.agent.cooldown_s):
                report.ok = True
                report.message = "页面未变化，跳过（冷却中）"
                report.elapsed_ms = int((time.time() - t0) * 1000)
                return report
            self._last_fp, self._last_fp_at = fp, time.time()

            # 逐轮规划/执行（模型每次只走一步）；dry-run 只规划一步
            max_steps = 1 if dry_run else int(self.cfg.agent.max_steps)
            final_plan: dict[str, Any] | None = None
            stuck = 0
            step = 0
            for step in range(max_steps):
                plan, _resp = self._plan(img, result, step)
                final_plan = plan
                report.plan = plan
                self.log.info(
                    "步骤 %d%s: %s",
                    step + 1, " [dry-run]" if dry_run else "",
                    json.dumps(plan, ensure_ascii=False),
                )

                actions_list = plan.get("actions", [])
                if not dry_run and actions_list:
                    report.executed.extend(self.execute(plan, img, rect))

                if bool(plan.get("done")):
                    guard = bool(getattr(self.cfg.agent, "done_guard", True))
                    reason = None if (dry_run or not guard) else self._unfinished_reason(img, result)
                    if reason and self._done_nudges < 2:
                        self._done_nudges += 1
                        self._step_note = (
                            f"检测到可能还没做完：{reason}。不要结束——继续作答；"
                            f"若题目在下方，请先发 scroll 向下滚动，下一步再继续。"
                        )
                        self.log.info("模型想结束，但%s，已要求继续", reason)
                        continue

                    # 收工前回读校验：滚回顶部逐屏扫描，确认没有漏答
                    verify = bool(getattr(self.cfg.agent, "verify_before_finish", True))
                    if not dry_run and verify:
                        ok, summary = self._verify_pass(rect, hwnd)
                        report.verification = summary
                        if not ok and self._done_nudges < 3:
                            self._done_nudges += 1
                            self._step_note = (
                                f"回读校验发现问题：{summary}。请回到页面上方逐题检查，"
                                f"把没作答的题补上，再结束。"
                            )
                            self.log.info("回读未通过：%s，已要求继续", summary)
                            self._scroll_to_top(rect, hwnd)
                            img, rect, hwnd, result = self.snapshot()
                            continue
                    break

                if dry_run:
                    break

                time.sleep(float(self.cfg.agent.step_delay_s))
                before_img = img  # 动作前的画面，用于判断“这一步到底有没有效果”
                img, rect, _hwnd, result = self.snapshot()
                plain2, _ = self._page_text(img, result)

                # 输入类动作的落库校验：文字没出现在页面上就重试一次
                typed = "".join(
                    str(a.get("text", "")) for a in actions_list
                    if (a.get("type") or "").lower() == "type"
                )
                if typed and typed not in plain2:
                    self.log.warning("输入内容未出现在页面，重试一次")
                    actions.hotkey("ctrl", "a")
                    time.sleep(0.05)
                    actions.paste_text(typed)
                    time.sleep(0.4)
                    img, rect, _hwnd, result = self.snapshot()
                    plain2, _ = self._page_text(img, result)

                # 记账：这一步如果让某题变成了“已作答”，记下来防止重复
                self._update_ledger(img, result)

                # 无变化自检：动作没生效就别原样重试，避免死循环
                if not self._page_changed(before_img, img):
                    stuck += 1
                    self._step_note = (
                        "上一步动作后页面没有任何变化，说明该动作没有生效（可能点空了或元素不可点）。"
                        "禁止重复同样动作：本题若已作答，请点击“下一题/提交”或向下 scroll；否则换一个元素。"
                    )
                    if stuck >= 3:
                        self.log.warning("连续 %d 步页面无变化，停止本轮", stuck)
                        report.message = "连续多步页面无变化，已停止（坐标可能不准或页面卡住）"
                        break
                else:
                    stuck = 0
                    self._step_note = ""

            self._save_debug(img, "final")
            report.ok = True
            report.steps = step + 1
            res = final_plan.get("result") if final_plan else None
            report.message = f"完成。结果: {json.dumps(res, ensure_ascii=False) if res else '（无 result）'}"
            if report.verification:
                report.message += f"　回读：{report.verification}"
            report.elapsed_ms = int((time.time() - t0) * 1000)
            return report

        except (ModelError, LookupError, ValueError, OSError, ocr.OcrError) as e:
            report.ok = False
            report.message = f"{type(e).__name__}: {e}"
            report.elapsed_ms = int((time.time() - t0) * 1000)
            self.log.error("%s", report.message)
            return report

    # ------------------------------------------------------------------ #
    def _save_debug(self, img: Image.Image, tag: str) -> None:
        if not self.cfg.runtime.save_debug:
            return
        d = self.cfg.resolve(self.cfg.runtime.debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{tag}_{time.strftime('%H%M%S')}.png"
        img.save(p)
        self.log.debug("已保存截图: %s", p)


