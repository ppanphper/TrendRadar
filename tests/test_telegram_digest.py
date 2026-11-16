"""Telegram 摘要版式：精选、折叠、长度上限、转义与提醒模式。"""

import re
import unittest
from datetime import datetime

from trendradar.notification.telegram_digest import (
    build_telegram_digest,
    build_telegram_digest_messages,
    shorten_title,
    to_plain_text,
    to_rich_html,
    visible_length,
)

NUMBERED = re.compile(r"<code>\d+</code> ")

NOW = datetime(2026, 11, 11, 18, 0)


def _title(title, source="知乎", rank=5, score=0.8, url="https://example.com/a?x=1&y=2"):
    return {"title": title, "source_name": source, "ranks": [rank], "relevance_score": score, "url": url}


def _report(groups):
    return {"stats": [{"word": word, "count": len(items), "titles": items} for word, items in groups]}


class RichHtmlTest(unittest.TestCase):
    TEXT = (
        "<b>📰 摘要 · 11-11 18:00</b>\n热榜 4\n\n"
        "<b>大模型</b> · 3\n<code>1</code> <a href=\"https://a\">A</a> <i>知乎</i>\n<code>2</code> B\n"
        "<blockquote expandable><code>3</code> C\n<i>…另有 2 条，见网页报告</i></blockquote>\n\n"
        "<blockquote expandable><b>✨ AI 热点分析</b>\n\n<b>核心</b>\n1. x</blockquote>"
    )

    def test_paragraphs_and_folded_quotes(self):
        html = to_rich_html(self.TEXT)
        self.assertTrue(html.startswith("<p><b>📰 摘要 · 11-11 18:00</b><br/>热榜 4</p><p><b>大模型</b> · 3<br/><code>1</code> "))
        self.assertIn("<blockquote expandable><code>3</code> C<br/><i>…另有 2 条，见网页报告</i></blockquote>", html)
        # AI 分析里的空行留在引用块内，不把引用块拆开
        self.assertIn("<blockquote expandable><b>✨ AI 热点分析</b><br/><br/><b>核心</b><br/>1. x</blockquote>", html)
        self.assertNotIn("tg-button", html)
        self.assertNotIn("\n", html)

    def test_picking_adds_buttons_and_unfolds(self):
        html = to_rich_html(self.TEXT, "d", {2})
        self.assertIn('<tg-button type="callback_data" data="fv:d:1">☐</tg-button> <code>1</code> ', html)
        self.assertIn('<tg-button type="callback_data" data="fv:d:2">✅</tg-button> <code>2</code> B', html)
        self.assertIn('<blockquote><tg-button type="callback_data" data="fv:d:3">☐</tg-button> <code>3</code> C', html)
        self.assertNotIn("expandable", html)
        self.assertEqual(html.count("<tg-button"), 3)

    def test_real_digest_round_trip(self):
        items = [_title(f"新闻{i}") for i in range(8)]
        msg = build_telegram_digest_messages(_report([("大模型", items)]), now=NOW)[0]
        html = to_rich_html(msg.text, "l")
        self.assertEqual(html.count("<tg-button"), len(msg.items))
        self.assertIn("&amp;y=2", html)


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


class ShortenTitleTest(unittest.TestCase):
    def test_no_limit_or_short_title_unchanged(self):
        self.assertEqual(shorten_title("很长" * 50, 0), "很长" * 50)
        self.assertEqual(shorten_title("短标题", 10), "短标题")
        self.assertEqual(shorten_title("刚好十个字的一个标题", 10), "刚好十个字的一个标题")

    def test_cuts_at_last_sentence_end(self):
        title = "物理学家分享了他与模型合作的经历。这是第一篇相关文章，后面还有很多细节内容"
        self.assertEqual(shorten_title(title, 25), "物理学家分享了他与模型合作的经历…")

    def test_keeps_question_mark(self):
        title = "这个功能真的有用吗？我试了几天之后发现还有很多问题需要解决"
        self.assertEqual(shorten_title(title, 20), "这个功能真的有用吗？…")

    def test_hard_cut_without_sentence_end(self):
        title = "这是一段没有任何句末标点而且非常非常长的推文内容需要被截断"
        short = shorten_title(title, 12)
        self.assertEqual(short, title[:11] + "…")
        self.assertEqual(len(short), 12)

    def test_english_fallback_cuts_at_word_boundary(self):
        title = "Physicist Nathaniel Craig shares how he worked with the model to develop a definition"
        self.assertEqual(shorten_title(title, 40), "Physicist Nathaniel Craig shares how he…")

    def test_ignores_sentence_end_too_close_to_start(self):
        title = "好。这一句很长很长很长很长很长很长很长很长很长很长"
        self.assertEqual(shorten_title(title, 12), title[:11] + "…")


class DigestTitleLengthTest(unittest.TestCase):
    LONG = "OpenAI：物理学家内森尼尔·克雷格分享了他如何与模型合作。这是该系列的第一篇文章，后面还有更多" + "细节" * 30

    def test_rss_titles_shortened_in_text_and_registry(self):
        rss = [{"word": "AI", "count": 1, "titles": [_title(self.LONG, source="X - AI", url="https://x.com/a/1")]}]
        msg = build_telegram_digest_messages({"stats": []}, rss, now=NOW, title_max_length=40)[0]
        self.assertIn("OpenAI：物理学家内森尼尔·克雷格分享了他如何与模型合作…</a>", msg.text)
        self.assertNotIn("细节细节", msg.text)
        self.assertEqual(msg.items[0]["title"], "OpenAI：物理学家内森尼尔·克雷格分享了他如何与模型合作…")
        self.assertEqual(msg.items[0]["url"], "https://x.com/a/1")

    def test_standalone_titles_shortened(self):
        standalone = {"platforms": [], "rss_feeds": [
            {"id": "x-ai", "name": "X - AI", "items": [{"title": self.LONG, "url": "https://x.com/a/2"}]},
        ]}
        msg = build_telegram_digest_messages({"stats": []}, None, standalone, now=NOW, title_max_length=40)[0]
        self.assertIn("模型合作…</a>", msg.text)
        self.assertNotIn("细节细节", msg.text)
        self.assertEqual(msg.items[0]["title"], "OpenAI：物理学家内森尼尔·克雷格分享了他如何与模型合作…")
        # 不改动传入的数据（网页报告等其他用途仍用全文）
        self.assertEqual(standalone["rss_feeds"][0]["items"][0]["title"], self.LONG)

    def test_merge_uses_full_titles(self):
        a = _title("同样的开头" * 6 + "后面讲的是甲", url="https://x.com/a/3")
        b = _title("同样的开头" * 6 + "后面讲的是乙", url="https://x.com/a/4")
        msg = build_telegram_digest_messages(_report([("AI", [a, b])]), now=NOW, title_max_length=10)[0]
        self.assertEqual(len(msg.items), 2)

    def test_escapes_after_shortening(self):
        title = "A&B 发布新模型，" + "性能大幅提升" * 10
        text = build_telegram_digest(_report([("AI", [_title(title)])]), now=NOW, title_max_length=20)[0]
        self.assertIn("A&amp;B 发布新模型", text)
        self.assertNotIn("性能大幅提升性能大幅提升性能大幅提升", text)

    def test_default_keeps_full_title(self):
        text = build_telegram_digest(_report([("AI", [_title(self.LONG)])]), now=NOW)[0]
        self.assertIn(self.LONG, text)


if __name__ == "__main__":
    unittest.main()
