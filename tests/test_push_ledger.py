"""已推送记录：跨次去重、相关度门槛、保留期清理。"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from trendradar.notification.ledger import PushLedger, filter_push_content, hotlist_key, rss_key

NOW = datetime(2026, 11, 11, 18, 0)


def _stats(*titles, score=0.8):
    return [{"word": "组", "count": len(titles), "titles": [
        {"title": t, "source_name": "知乎", "relevance_score": score} for t in titles
    ]}]


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = PushLedger(str(Path(self.tmp.name) / "meta" / "push_ledger.db"), retention_days=7)

    def tearDown(self):
        self.tmp.cleanup()

    def test_removes_already_pushed_items_across_sections(self):
        self.ledger.record({hotlist_key("旧闻"): "旧闻", rss_key("https://linux.do/t/1", ""): "帖子"}, NOW)
        rss = [{"word": "组", "count": 2, "titles": [
            {"title": "帖子", "url": "https://linux.do/t/1"}, {"title": "新帖", "url": "https://linux.do/t/2"},
        ]}]
        standalone = {"platforms": [{"id": "weibo", "name": "微博", "items": [{"title": "旧 闻"}, {"title": "热搜"}]}],
                      "rss_feeds": []}
        stats, rss_items, _, new_titles, standalone, entries = filter_push_content(
            _stats("旧闻", "新闻"), rss, None, {"zhihu": {"旧闻": {}, "新闻": {}}}, standalone, ledger=self.ledger,
        )
        self.assertEqual([t["title"] for t in stats[0]["titles"]], ["新闻"])
        self.assertEqual(stats[0]["count"], 1)
        self.assertEqual([t["title"] for t in rss_items[0]["titles"]], ["新帖"])
        self.assertEqual(list(new_titles["zhihu"]), ["新闻"])
        # 规范化后 "旧 闻" 与 "旧闻" 是同一条
        self.assertEqual([i["title"] for i in standalone["platforms"][0]["items"]], ["热搜"])
        self.assertEqual(set(entries), {hotlist_key("新闻"), rss_key("https://linux.do/t/2", ""), hotlist_key("热搜")})

    def test_record_then_filter_is_empty(self):
        _, _, _, _, _, entries = filter_push_content(_stats("一条"), None, None, None, None, ledger=self.ledger)
        self.ledger.record(entries, NOW)
        stats, *_ = filter_push_content(_stats("一条"), None, None, None, None, ledger=self.ledger)
        self.assertEqual(stats, [])

    def test_min_score_drops_low_scores_and_unscored_sections(self):
        stats = _stats("低分", score=0.8) + _stats("高分", score=0.95)
        standalone = {"platforms": [{"id": "weibo", "name": "微博", "items": [{"title": "热搜"}]}], "rss_feeds": []}
        stats, _, _, new_titles, standalone, entries = filter_push_content(
            stats, None, None, {"zhihu": {"低分": {}}}, standalone, min_score=0.9,
        )
        self.assertEqual([t["title"] for s in stats for t in s["titles"]], ["高分"])
        self.assertEqual(new_titles, {})
        self.assertIsNone(standalone)
        self.assertEqual(list(entries), [hotlist_key("高分")])

    def test_prunes_records_past_retention(self):
        self.ledger.record({"h:old": "old"}, NOW - timedelta(days=8))
        self.ledger.record({"h:new": "new"}, NOW)
        with sqlite3.connect(self.ledger.db_path) as conn:
            keys = {row[0] for row in conn.execute("SELECT key FROM pushed")}
        self.assertEqual(keys, {"h:new"})


if __name__ == "__main__":
    unittest.main()
