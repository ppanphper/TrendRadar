"""Telegram 摘要版式：精选、折叠、长度上限、转义与提醒模式。"""

import re
import unittest
from datetime import datetime

from trendradar.notification.telegram_digest import (
    build_telegram_digest,
    build_telegram_digest_messages,
    to_plain_text,
    visible_length,
)

NUMBERED = re.compile(r"<code>\d+</code> ")

NOW = datetime(2026, 11, 11, 18, 0)


def _title(title, source="知乎", rank=5, score=0.8, url="https://example.com/a?x=1&y=2"):
    return {"title": title, "source_name": source, "ranks": [rank], "relevance_score": score, "url": url}


def _report(groups):
    return {"stats": [{"word": word, "count": len(items), "titles": items} for word, items in groups]}


class VisibleLengthTest(unittest.TestCase):
    def test_counts_text_without_tags_and_entities(self):
        self.assertEqual(visible_length('<b>a&amp;b</b> <a href="https://x">链接</a>'), len("a&b 链接"))

    def test_emoji_counts_as_two_utf16_units(self):
        self.assertEqual(visible_length("⭐"), 1)
        self.assertEqual(visible_length("🔔"), 2)


class DigestTest(unittest.TestCase):
    def test_picks_by_score_and_folds_rest(self):
        items = [_title(f"新闻{i}", score=0.7 + i / 100) for i in range(10)]
        msgs = build_telegram_digest(_report([("大模型", items)]), now=NOW, headline="午间摘要")
        self.assertEqual(len(msgs), 1)
        text = msgs[0]
        self.assertTrue(text.startswith("<b>📰 午间摘要 · 11-11 18:00</b>"))
        picks = text.split("<b>⭐ 精选</b>")[1].split("\n\n")[0]
        self.assertIn("新闻9", picks.splitlines()[1])  # 分数最高的排第一
        # 精选取走 5 条，分组剩 5 条：显示 3 条，2 条折叠
        group = text.split("<b>大模型</b> · 5")[1]
        self.assertEqual(group.count("<blockquote expandable>"), 1)
        self.assertEqual(re.search(r"<blockquote expandable>(.*?)</blockquote>", group, re.S).group(1).count("<code>"), 2)

    def test_small_push_has_no_picks_section(self):
        msgs = build_telegram_digest(_report([("大模型", [_title("唯一一条")])]), now=NOW)
        self.assertNotIn("精选", msgs[0])
        self.assertIn("唯一一条", msgs[0])

    def test_same_title_across_platforms_is_merged(self):
        items = [_title("同一条", source="微博", rank=3), _title("同一条", source="知乎", rank=1)]
        text = build_telegram_digest(_report([("大模型", items)]), now=NOW)[0]
        self.assertEqual(text.count("同一条"), 1)
        self.assertIn("微博 / 知乎 #1", text)

    def test_escapes_html_in_titles_and_urls(self):
        item = _title('A<B> & "C"', url='https://e.com/?q="x"&a=<1>')
        text = build_telegram_digest(_report([("组", [item])]), now=NOW)[0]
        self.assertIn("A&lt;B&gt; &amp; \"C\"", text)
        self.assertIn('href="https://e.com/?q=&quot;x&quot;&amp;a=&lt;1&gt;"', text)

    def test_respects_limit_and_numbers_messages(self):
        groups = [(f"分组{g}", [_title(f"很长的标题{g}-{i}" * 3) for i in range(40)]) for g in range(4)]
        msgs = build_telegram_digest(_report(groups), now=NOW, limit=1500)
        self.assertGreater(len(msgs), 1)
        for i, msg in enumerate(msgs, 1):
            self.assertLessEqual(visible_length(msg), 1500)
            self.assertIn(f" · {i}/{len(msgs)}</b>", msg.splitlines()[0])
            self.assertEqual(msg.count("<blockquote expandable>"), msg.count("</blockquote>"))

    def test_fold_cap_shows_remaining_count(self):
        items = [_title(f"条目{i}", score=0.7) for i in range(30)]
        text = "".join(build_telegram_digest(_report([("组", items)]), now=NOW, picks=0, max_folded=10))
        folded = re.search(r"<blockquote expandable>(.*?)</blockquote>", text, re.S).group(1)
        self.assertEqual(len(NUMBERED.findall(folded)), 10)
        self.assertIn("…另有 17 条", folded)

    def test_ai_analysis_is_folded(self):
        text = build_telegram_digest(
            _report([("组", [_title("一条")])]), ai_content="<b>✨ AI 热点分析</b>\n要点", now=NOW
        )[-1]
        self.assertIn("<blockquote expandable><b>✨ AI 热点分析</b>\n要点</blockquote>", text)

    def test_alert_lists_items_without_groups_or_ai(self):
        items = [_title(f"重要{i}", score=0.9 + i / 100) for i in range(8)]
        text = build_telegram_digest(_report([("大模型", items)]), ai_content="AI", now=NOW, alert=True)[0]
        self.assertTrue(text.startswith("<b>🔔 重要提醒"))
        self.assertNotIn("精选", text)
        self.assertNotIn("AI", text.split("\n", 1)[1].replace("大模型", ""))
        self.assertIn("重要7", text.splitlines()[3])

    def test_standalone_sections_and_empty(self):
        standalone = {"platforms": [], "rss_feeds": [{"id": "ld", "name": "LinuxDO", "items": [
            {"title": f"帖子{i}", "url": f"https://linux.do/t/{i}"} for i in range(5)
        ]}]}
        text = build_telegram_digest({"stats": []}, standalone_data=standalone, now=NOW)[0]
        self.assertIn("<b>📌 LinuxDO</b> · 5", text)
        self.assertEqual(build_telegram_digest({"stats": []}, now=NOW), [])

    def test_numbers_map_to_items_per_message(self):
        groups = [(f"分组{g}", [_title(f"标题{g}-{i}" * 4) for i in range(20)]) for g in range(3)]
        msgs = build_telegram_digest_messages(_report(groups), now=NOW, limit=1500, ai_content="1. AI 列表\n2. 第二点")
        self.assertGreater(len(msgs), 1)
        seen = []
        for m in msgs:
            numbers = [int(x) for x in re.findall(r"<code>(\d+)</code> ", m.text)]
            self.assertEqual([i["n"] for i in m.items], numbers)
            seen.extend(numbers)
        # 编号全局连续且不重复；AI 分析里的「1.」不算条目
        self.assertEqual(seen, list(range(1, len(seen) + 1)))
        first = msgs[0].items[0]
        self.assertIn("title", first)
        self.assertIn("url", first)

    def test_event_headline_and_sources(self):
        item = _title("原始标题", source="百度")
        item.update({"event_headline": "阿里宣布平头哥赴港上市", "event_sources": ["百度", "微博", "头条", "知乎"]})
        text = build_telegram_digest(_report([("科技", [item])]), now=NOW)[0]
        self.assertIn("阿里宣布平头哥赴港上市", text)
        self.assertNotIn("原始标题", text)
        self.assertIn("百度 / 微博 / 头条 等 4 家", text)

    def test_plain_text_fallback(self):
        self.assertEqual(to_plain_text('<b>a &amp; b</b> <a href="x">c</a>'), "a & b c")


if __name__ == "__main__":
    unittest.main()
