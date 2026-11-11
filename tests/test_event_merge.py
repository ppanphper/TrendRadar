"""事件归并：字面归并、AI 归并与已推送事件剔除、AI 失败回退。"""

import json
import unittest

from trendradar.notification.events import lexically_similar, merge_events, parse_ai_events

PROMPT = ("system", "最近：\n{recent_events}\n共 {news_count} 条\n{news_list}")


def _t(title, source, score=0.8, rank=5):
    return {"title": title, "source_name": source, "relevance_score": score, "ranks": [rank]}


def _stats(*groups):
    return [{"word": w, "count": len(ts), "titles": list(ts)} for w, ts in groups]


class LexicalTest(unittest.TestCase):
    def test_similarity_rules(self):
        self.assertTrue(lexically_similar("阿里宣布平头哥赴港独立上市", "阿里宣布平头哥赴港上市"))
        self.assertTrue(lexically_similar("俄军机罕见降落以色列机场", "“不同寻常”！俄军机罕见降落以色列机场"))
        self.assertFalse(lexically_similar("英伟达发布新显卡", "英伟达股价大跌"))


class MergeTest(unittest.TestCase):
    def test_lexical_merge_keeps_best_and_lists_sources(self):
        stats = _stats(
            ("科技", [_t("阿里宣布平头哥赴港独立上市", "百度", 0.8), _t("苹果发布新品", "知乎")]),
            ("金融", [_t("阿里宣布平头哥赴港上市", "微博", 0.9)]),
        )
        result = merge_events(stats, None)
        titles = [(g["word"], t["title"]) for g in result.stats for t in g["titles"]]
        self.assertEqual(titles, [("科技", "苹果发布新品"), ("金融", "阿里宣布平头哥赴港上市")])
        kept = result.stats[1]["titles"][0]
        self.assertEqual(kept["event_sources"], ["微博", "百度"])
        self.assertEqual(kept["event_size"], 2)
        self.assertEqual(result.merged, 1)
        self.assertEqual(len(result.headlines), 2)

    def test_recent_headline_removes_repeat(self):
        stats = _stats(("科技", [_t("阿里宣布平头哥赴港上市", "百度"), _t("苹果发布新品", "知乎")]))
        result = merge_events(stats, None, recent_headlines=["阿里宣布平头哥赴港独立上市"])
        self.assertEqual([t["title"] for g in result.stats for t in g["titles"]], ["苹果发布新品"])
        self.assertEqual(result.seen_dropped, 1)

    def test_ai_groups_paraphrases_and_drops_seen(self):
        stats = _stats(("军事", [
            _t("中美两军举行驼峰航线美军援华抗日飞行员遗骸移交仪式", "澎湃", 0.9),
            _t("中方移交美军援华抗日飞行员遗骸", "B站", 0.7),
        ]), ("宏观", [_t("美国10月CPI超预期", "华尔街见闻")]))
        rss = [{"word": "军事", "count": 1, "titles": [{"title": "旧闻重提", "source_name": "RSS", "url": "u"}]}]
        sent = []

        def fake_chat(messages):
            sent.append(messages)
            prompt = messages[-1]["content"]
            lines = [l for l in prompt.splitlines() if l[:1].isdigit()]
            ids = {l.split(". ", 1)[1]: int(l.split(".", 1)[0]) for l in lines}
            pair = [i for t, i in ids.items() if "遗骸" in t]
            seen = [i for t, i in ids.items() if "旧闻" in t]
            return json.dumps({"events": [{"ids": pair, "headline": "中方向美方移交援华飞行员遗骸"}], "seen": seen})

        result = merge_events(stats, rss, recent_headlines=["某件旧事"], ai_chat=fake_chat, prompt=PROMPT)
        self.assertTrue(result.ai_used)
        self.assertIn("某件旧事", sent[0][-1]["content"])
        kept = result.stats[0]["titles"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["event_headline"], "中方向美方移交援华飞行员遗骸")
        self.assertEqual(kept[0]["event_sources"], ["澎湃", "B站"])
        self.assertEqual(result.rss_items, [])
        self.assertEqual(result.seen_dropped, 1)
        self.assertIn("中方向美方移交援华飞行员遗骸", result.headlines)

    def test_ai_failure_falls_back_to_lexical(self):
        stats = _stats(("科技", [_t("阿里宣布平头哥赴港独立上市", "百度"), _t("阿里宣布平头哥赴港上市", "微博")]))

        def broken(messages):
            raise RuntimeError("timeout")

        result = merge_events(stats, None, ai_chat=broken, prompt=PROMPT)
        self.assertFalse(result.ai_used)
        self.assertEqual(result.merged, 1)

    def test_parse_ai_events_ignores_invalid_ids(self):
        events, seen = parse_ai_events('结果：{"events": [{"ids": [1, 99, 2], "headline": "x"}, {"ids": [3]}], "seen": [2, "a", 0]}', 3)
        self.assertEqual(events, [([1, 2], "x")])
        self.assertEqual(seen, {2})


if __name__ == "__main__":
    unittest.main()
