"""翻译降本：已是目标语言的标题不送 AI、译文缓存、本轮去重，以及相关配置加载。"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from trendradar.ai.translation_cache import TranslationCache, already_in_language
from trendradar.ai.translator import AITranslator, BatchTranslationResult, TranslationResult
from trendradar.notification.dispatcher import NotificationDispatcher

NOW = datetime(2026, 11, 16, 18, 0)


class AlreadyInLanguageTest(unittest.TestCase):
    def test_chinese_titles(self):
        for text in [
            "华为云发布具身世界模型 基于国产算力昇腾进行训练",
            "火山引擎宣布即梦2.5超高清API全面开放",
            "如何让agent跑长程任务并且保证质量",
        ]:
            self.assertTrue(already_in_language(text, "中文"), text)

    def test_needs_translation(self):
        for text in [
            "OpenAI: Physicist Nathaniel Craig shares how he worked with GPT-6 Astra",
            "Great talk with 张三 today about AI agents",
            "東京で新しいAIモデルが発表された",
            "삼성전자 신형 반도체 공개",
            "ｶﾀｶﾅ 東京",
            "Why 小红书 matters",
            "Remembering 李文亮",
            "Meet 王小明, CEO",
            "1234 🚀",
        ]:
            self.assertFalse(already_in_language(text, "中文"), text)

    def test_target_language_aliases(self):
        title = "华为云发布具身世界模型"
        for lang in ["中文", "简体中文", "Chinese", "zh-CN", "zh"]:
            self.assertTrue(already_in_language(title, lang), lang)
        # 其他目标语言暂不判断，一律送翻译
        self.assertFalse(already_in_language("Hello world", "English"))
        self.assertFalse(already_in_language(title, "Korean"))


class TranslationCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "meta" / "translation_cache.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_and_get_per_language(self):
        cache = TranslationCache(self.path, 30, now=lambda: NOW)
        cache.put_many("中文", {"Hello": "你好"})
        self.assertEqual(cache.get_many("中文", ["Hello", "Bye"]), {"Hello": "你好"})
        self.assertEqual(cache.get_many("Korean", ["Hello"]), {})
        # 换进程（重新打开）后仍然命中
        self.assertEqual(TranslationCache(self.path, 30, now=lambda: NOW).get_many("中文", ["Hello"]), {"Hello": "你好"})

    def test_prune_by_last_use(self):
        cache = TranslationCache(self.path, 30, now=lambda: NOW - timedelta(days=32))
        cache.put_many("中文", {"Old": "旧", "Used": "常用"})
        TranslationCache(self.path, 30, now=lambda: NOW - timedelta(days=5)).get_many("中文", ["Used"])
        reopened = TranslationCache(self.path, 30, now=lambda: NOW)
        self.assertEqual(reopened.get_many("中文", ["Old", "Used"]), {"Used": "常用"})


def _parse(response, n):
    return AITranslator._parse_batch_response(AITranslator.__new__(AITranslator), response, n)


class BatchAlignmentTest(unittest.TestCase):
    def test_exact_numbering_is_aligned(self):
        texts, count, aligned = _parse("[2] 乙\n[1] 甲\n[3] 丙", 3)
        self.assertEqual((texts, count, aligned), (["甲", "乙", "丙"], 3, True))

    def test_duplicate_or_missing_numbers_not_aligned(self):
        self.assertFalse(_parse("[1] 甲\n[2] 乙\n[2] 丙", 3)[2])
        self.assertFalse(_parse("[1] 甲\n[3] 丙", 3)[2])
        self.assertFalse(_parse("[1] 甲\n[2] 乙\n[3] 丙\n[4] 丁", 3)[2])

    def test_line_fallback_not_aligned(self):
        texts, count, aligned = _parse("以下是翻译：\n甲\n乙", 3)
        self.assertEqual(count, 3)
        self.assertFalse(aligned)

    def test_real_translator_duplicate_numbers_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = AITranslator.__new__(AITranslator)
            tr.enabled, tr.target_language = True, "中文"
            tr.scope = {"HOTLIST": True, "RSS": True, "STANDALONE": True}
            tr.system_prompt, tr.user_prompt_template = "", "{content}"
            tr.client = type("C", (), {"api_key": "k"})()
            config = {"AI_TRANSLATION": {"CACHE_DAYS": 30}, "STORAGE": {"LOCAL": {"DATA_DIR": tmp}}}
            d = NotificationDispatcher(config, get_time_func=lambda: NOW, split_content_func=None, translator=tr)
            titles = ["Google releases Gemini", "Meta buys startup", "Apple launches iPhone"]
            with patch.object(AITranslator, "_call_ai", return_value="[1] 谷歌发布 Gemini\n[2] Meta 收购初创公司\n[2] 苹果发布 iPhone"):
                d.translate_content({"stats": [], "new_titles": []}, _rss(*titles), None, None, DispatcherTranslationTest.REGIONS)
            cache = TranslationCache(Path(tmp) / "meta" / "translation_cache.db", 30, now=lambda: NOW)
            self.assertEqual(cache.get_many("中文", titles), {})


class FakeTranslator:
    enabled = True
    target_language = "中文"
    scope = {"HOTLIST": True, "RSS": True, "STANDALONE": True}

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def translate_batch(self, texts):
        self.calls.append(list(texts))
        result = BatchTranslationResult(total_count=len(texts), parsed_count=0 if self.fail else len(texts),
                                        aligned=not self.fail)
        for text in texts:
            if self.fail:
                result.results.append(TranslationResult(original_text=text, error="boom"))
                result.fail_count += 1
            else:
                result.results.append(TranslationResult(original_text=text, translated_text="译:" + text, success=True))
                result.success_count += 1
        return result


class MisalignedTranslator(FakeTranslator):
    """AI 返回的编号条数与请求不符"""

    def translate_batch(self, texts):
        result = super().translate_batch(texts)
        result.aligned = False
        return result


def _rss(*titles):
    return [{"word": "AI", "count": len(titles), "titles": [{"title": t, "url": f"https://e/{i}"} for i, t in enumerate(titles)]}]


def _standalone(*titles):
    return {"platforms": [{"id": "weibo", "name": "微博", "items": [{"title": t} for t in titles]}], "rss_feeds": []}


class DispatcherTranslationTest(unittest.TestCase):
    REGIONS = {"HOTLIST": True, "RSS": True, "NEW_ITEMS": True, "STANDALONE": True}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _dispatcher(self, translator, **trans):
        config = {"AI_TRANSLATION": trans, "STORAGE": {"LOCAL": {"DATA_DIR": self.tmp.name}}}
        return NotificationDispatcher(config, get_time_func=lambda: NOW, split_content_func=None, translator=translator)

    def _translate(self, dispatcher, rss, standalone=None):
        return dispatcher.translate_content({"stats": [], "new_titles": []}, rss, None, standalone, self.REGIONS)

    def test_skips_target_language_and_dedupes(self):
        tr = FakeTranslator()
        d = self._dispatcher(tr, SKIP_TARGET_LANGUAGE=True)
        _, rss, _, sa = self._translate(d, _rss("Hello world", "华为发布新模型", "Hello world"), _standalone("微博热搜第一", "Hello world"))
        self.assertEqual(tr.calls, [["Hello world"]])
        self.assertEqual([t["title"] for t in rss[0]["titles"]], ["译:Hello world", "华为发布新模型", "译:Hello world"])
        self.assertEqual([i["title"] for i in sa["platforms"][0]["items"]], ["微博热搜第一", "译:Hello world"])

    def test_cache_avoids_repeat_calls(self):
        tr = FakeTranslator()
        d = self._dispatcher(tr, CACHE_DAYS=30)
        self._translate(d, _rss("Hello world", "Good morning"))
        _, rss, _, _ = self._translate(self._dispatcher(tr, CACHE_DAYS=30), _rss("Good morning", "New post"))
        self.assertEqual(tr.calls, [["Hello world", "Good morning"], ["New post"]])
        self.assertEqual([t["title"] for t in rss[0]["titles"]], ["译:Good morning", "译:New post"])
        self.assertTrue((Path(self.tmp.name) / "meta" / "translation_cache.db").exists())

    def test_all_resolved_without_ai_call(self):
        tr = FakeTranslator()
        d = self._dispatcher(tr, SKIP_TARGET_LANGUAGE=True, CACHE_DAYS=30)
        self._translate(d, _rss("Hello world"))
        _, rss, _, _ = self._translate(d, _rss("Hello world", "华为发布新模型"))
        self.assertEqual(tr.calls, [["Hello world"]])
        self.assertEqual([t["title"] for t in rss[0]["titles"]], ["译:Hello world", "华为发布新模型"])

    def test_failures_are_not_cached(self):
        d = self._dispatcher(FakeTranslator(fail=True), CACHE_DAYS=30)
        _, rss, _, _ = self._translate(d, _rss("Hello world"))
        self.assertEqual(rss[0]["titles"][0]["title"], "Hello world")
        tr = FakeTranslator()
        self._translate(self._dispatcher(tr, CACHE_DAYS=30), _rss("Hello world"))
        self.assertEqual(tr.calls, [["Hello world"]])

    def test_misaligned_batch_is_used_but_not_cached(self):
        _, rss, _, _ = self._translate(self._dispatcher(MisalignedTranslator(), CACHE_DAYS=30), _rss("Hello world", "Good morning"))
        self.assertEqual(rss[0]["titles"][0]["title"], "译:Hello world")
        tr = FakeTranslator()
        self._translate(self._dispatcher(tr, CACHE_DAYS=30), _rss("Hello world"))
        self.assertEqual(tr.calls, [["Hello world"]])

    def test_defaults_send_everything_without_cache(self):
        tr = FakeTranslator()
        d = self._dispatcher(tr)
        _, rss, _, _ = self._translate(d, _rss("Hello world", "华为发布新模型"))
        self._translate(d, _rss("Hello world"))
        self.assertEqual(tr.calls, [["Hello world", "华为发布新模型"], ["Hello world"]])
        self.assertEqual(rss[0]["titles"][1]["title"], "译:华为发布新模型")
        self.assertFalse((Path(self.tmp.name) / "meta" / "translation_cache.db").exists())

    def test_batches_only_pending_titles(self):
        tr = FakeTranslator()
        d = self._dispatcher(tr, SKIP_TARGET_LANGUAGE=True, BATCH_SIZE=2, BATCH_INTERVAL=0)
        self._translate(d, _rss("中文一", "A one", "中文二", "B two", "C three"))
        self.assertEqual(tr.calls, [["A one", "B two"], ["C three"]])


class TranslationConfigTest(unittest.TestCase):
    def test_loads_new_keys_and_batch_settings(self):
        from trendradar.core.loader import _load_ai_translation_config

        self.assertEqual(
            {k: v for k, v in _load_ai_translation_config({}).items() if k in ("SKIP_TARGET_LANGUAGE", "CACHE_DAYS", "BATCH_SIZE", "BATCH_INTERVAL")},
            {"SKIP_TARGET_LANGUAGE": False, "CACHE_DAYS": 0, "BATCH_SIZE": 100, "BATCH_INTERVAL": 2},
        )
        loaded = _load_ai_translation_config({"ai_translation": {
            "skip_target_language": True, "cache_days": 30, "batch_size": 50, "batch_interval": 0,
        }})
        self.assertEqual((loaded["SKIP_TARGET_LANGUAGE"], loaded["CACHE_DAYS"], loaded["BATCH_SIZE"], loaded["BATCH_INTERVAL"]), (True, 30, 50, 0))


if __name__ == "__main__":
    unittest.main()
