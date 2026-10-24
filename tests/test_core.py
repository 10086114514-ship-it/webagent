"""核心逻辑单元测试（不需要浏览器、不联网）。

运行::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from webagent.agent import (  # noqa: E402
    WebAgent, _bbox_center, _extract_json, aggregate_verification, normalize_text,
)
from webagent.capture import Rect  # noqa: E402
from webagent.config import DEFAULTS, Config, _deep_merge  # noqa: E402
from webagent.ocr import OcrResult, OcrWord, lines_from_words  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_fullwidth_to_halfwidth(self):
        self.assertEqual(normalize_text("Ａ．１４１４"), "A.1414")

    def test_punctuation(self):
        self.assertEqual(normalize_text("１．下列？"), "1.下列?")

    def test_whitespace_collapse(self):
        self.assertEqual(normalize_text("  a   b  "), "a b")


class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_extract_json('{"done": true}'), {"done": True})

    def test_fenced(self):
        text = '```json\n{"done": false, "actions": []}\n```'
        self.assertEqual(_extract_json(text), {"done": False, "actions": []})

    def test_with_chatter(self):
        text = '好的，计划如下：{"a": 1} 完毕'
        self.assertEqual(_extract_json(text), {"a": 1})


class TestBbox(unittest.TestCase):
    def setUp(self):
        self.rect = Rect(100, 50, 1000, 500)

    def test_center(self):
        self.assertEqual(_bbox_center([0.2, 0.4, 0.4, 0.6], self.rect), (400, 300))

    def test_clamp(self):
        # 越界坐标会被夹到 0~1
        self.assertEqual(_bbox_center([-1, -1, 2, 2], self.rect), (600, 300))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            _bbox_center([0.1], self.rect)


class TestOcrLines(unittest.TestCase):
    def test_merge_small_gaps_only(self):
        # 模拟 Windows OCR 把 "A. 1.414" 切成 A. / 1 / ． / 414 四个词
        words = [
            OcrWord("A.", 60, 103, 22, 18),
            OcrWord("1", 92, 103, 7, 18),
            OcrWord("．", 105, 118, 4, 3),
            OcrWord("414", 110, 103, 42, 18),
        ]
        rows = lines_from_words(words, x_gap=0.4, y_gap=0.6)
        self.assertEqual(len(rows), 1)
        texts = [seg.text for seg in rows[0]]
        self.assertEqual(texts, ["A.", "1．414"])

    def test_two_rows(self):
        words = [
            OcrWord("第一行", 10, 10, 60, 20),
            OcrWord("第二行", 10, 80, 60, 20),
        ]
        rows = lines_from_words(words)
        self.assertEqual(len(rows), 2)


class TestConfig(unittest.TestCase):
    def _cfg(self, override):
        return Config(_deep_merge(DEFAULTS, override), Path("config.json"))

    def test_merge_defaults(self):
        cfg = self._cfg({"model": {"api_key": "sk-x"}})
        self.assertEqual(cfg.model.api_key, "sk-x")
        self.assertEqual(cfg.model.base_url, DEFAULTS["model"]["base_url"])

    def test_set(self):
        cfg = self._cfg({})
        cfg.set("model.mock", True)
        self.assertTrue(cfg.model.get("mock"))

    def test_unknown_keys(self):
        cfg = self._cfg({"modle": {"api_key": "x"}})
        self.assertIn("modle", cfg.unknown_keys())

    def test_unknown_nested_key(self):
        cfg = self._cfg({"model": {"apikey": "x"}})
        self.assertIn("model.apikey", cfg.unknown_keys())

    def test_section_mapping(self):
        cfg = self._cfg({})
        self.assertIn("base_url", dict(cfg.model))


class TestAgentHelpers(unittest.TestCase):
    """单页多题：题目识别 / 进度解析 / 台账去重。"""

    def setUp(self):
        cfg = Config(_deep_merge(DEFAULTS, {}), Path("config.json"))
        self.agent = WebAgent(cfg, logging.getLogger("test"))

    @staticmethod
    def _result(*texts):
        # y 从 300 开始，落在浏览器工具栏(顶部 12%)之外
        words = [OcrWord(t, 10, 300 + i * 40, 200, 24) for i, t in enumerate(texts)]
        return OcrResult("zh-Hans", words)

    @staticmethod
    def _img():
        return Image.new("RGB", (1000, 1000), "white")

    def test_question_text_skips_options(self):
        res = self._result(
            "第 3 / 20 题",
            "下列哪个选项是正确的？",
            "A. 第一个选项",
            "B. 第二个选项",
        )
        self.assertEqual(self.agent._question_text(self._img(), res), "下列哪个选项是正确的?")

    def test_question_text_skips_browser_chrome(self):
        words = [
            OcrWord("C:\\Users\\me\\demo.html", 10, 40, 300, 24),   # 地址栏，应被忽略
            OcrWord("这一行才是题目内容", 10, 400, 300, 24),
        ]
        res = OcrResult("zh-Hans", words)
        self.assertEqual(self.agent._question_text(self._img(), res), "这一行才是题目内容")

    def test_progress_slash(self):
        res = self._result("第 3 / 20 题", "题目内容")
        self.assertEqual(self.agent._progress(self._img(), res), (3, 20))

    def test_progress_chinese(self):
        res = self._result("第 7 题", "题目内容")
        self.assertEqual(self.agent._progress(self._img(), res), (7, 0))

    def test_progress_none(self):
        res = self._result("一些普通文字")
        self.assertIsNone(self.agent._progress(self._img(), res))


class TestVerification(unittest.TestCase):
    """回读校验的汇总逻辑。"""

    @staticmethod
    def _view(options=True, state=True, progress=None, question=""):
        return {
            "has_options": options,
            "has_state": state,
            "progress": progress,
            "question": question,
        }

    def test_all_answered(self):
        ok, summary = aggregate_verification([self._view(True, True, (5, 5), "第5题")])
        self.assertTrue(ok)
        self.assertIn("扫描 1 屏", summary)

    def test_unanswered_detected(self):
        ok, summary = aggregate_verification([self._view(True, False, None, "第7题: 缺答")])
        self.assertFalse(ok)
        self.assertIn("疑似未作答", summary)

    def test_incomplete_progress(self):
        ok, summary = aggregate_verification([self._view(False, False, (3, 20), "")])
        self.assertFalse(ok)
        self.assertIn("3/20", summary)

    def test_empty_scan_passes(self):
        ok, _ = aggregate_verification([])
        self.assertTrue(ok)

    def test_duplicate_questions_deduped(self):
        views = [self._view(True, False, None, "同一道题"), self._view(True, False, None, "同一道题")]
        ok, summary = aggregate_verification(views)
        self.assertFalse(ok)
        self.assertEqual(summary.count("同一道题"), 1)


if __name__ == "__main__":
    unittest.main()
