"""核心逻辑单元测试（不需要浏览器、不联网）。

运行::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webagent.agent import _bbox_center, _extract_json, normalize_text  # noqa: E402
from webagent.capture import Rect  # noqa: E402
from webagent.config import DEFAULTS, Config, _deep_merge  # noqa: E402
from webagent.ocr import OcrWord, lines_from_words  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
