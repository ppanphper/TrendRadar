"""RSS 快照：保存全文、只存首个版本、清理脚本、索引页。"""

import json
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from trendradar.crawler.rss.archive import RSSArchiver, sanitize_html
from trendradar.crawler.rss.parser import RSSParser

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

DISCOURSE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>LINUX DO</title>
<item>
  <title>分享一个工具</title>
  <dc:creator><![CDATA[alice]]></dc:creator>
  <description><![CDATA[<p>首帖正文，很长很长。</p><img src="https://cdn3.ldstatic.com/a.png"><script>alert(1)</script>]]></description>
  <link>https://linux.do/t/topic/123</link>
  <pubDate>Tue, 11 Nov 2026 10:00:00 +0000</pubDate>
  <guid isPermaLink="false">linux.do-topic-123</guid>
</item>
</channel></rss>"""


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "archive"
        self.items = RSSParser().parse(DISCOURSE_RSS, "https://linux.do/latest.rss")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parser_keeps_full_content(self):
        self.assertIn("首帖正文", self.items[0].content_html)
        self.assertIn("<img", self.items[0].content_html)

    def test_saves_page_once_and_builds_index(self):
        archiver = RSSArchiver(str(self.root))
        self.assertEqual(archiver.save("linux-do-latest", "LinuxDO 最新", self.items, NOW), 1)

        manifest = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        page = (self.root / manifest[0]["path"]).read_text(encoding="utf-8")
        self.assertTrue(manifest[0]["path"].startswith("linux-do-latest/2026-11/"))
        self.assertIn("首帖正文", page)
        self.assertIn('src="https://cdn3.ldstatic.com/a.png"', page)
        self.assertNotIn("<script>alert", page)
        self.assertIn('href="https://linux.do/t/topic/123"', page)
        self.assertIn("分享一个工具", (self.root / "index.html").read_text(encoding="utf-8"))
        # pubDate 10:00 +0000 显示为北京时间
        self.assertIn("2026-11-11 18:00", page)

        # 帖子被编辑后再次抓到，仍保留首次快照
        self.items[0].content_html = "<p>已被修改</p>"
        self.assertEqual(archiver.save("linux-do-latest", "LinuxDO 最新", self.items, NOW), 0)
        self.assertIn("首帖正文", (self.root / manifest[0]["path"]).read_text(encoding="utf-8"))

    def test_localizes_images_and_keeps_failed_remote(self):
        self.items[0].content_html = (
            '<p>图</p><img src="https://cdn3.ldstatic.com/a.png" srcset="https://cdn3.ldstatic.com/a_2x.png 2x">'
            '<img src="https://cdn3.ldstatic.com/missing.png">'
        )
        fetched = []

        def fetch(url):
            fetched.append(url)
            return b"PNG" if url.endswith("/a.png") else None

        RSSArchiver(str(self.root), fetch).save("ld", "LD", self.items, NOW)
        manifest = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        page_path = self.root / manifest[0]["path"]
        page = page_path.read_text(encoding="utf-8")
        files_dir = page_path.with_name(page_path.stem + "_files")
        local = next(files_dir.iterdir())
        self.assertEqual(local.read_bytes(), b"PNG")
        self.assertIn(f'src="{files_dir.name}/{local.name}"', page)
        self.assertIn('src="https://cdn3.ldstatic.com/missing.png"', page)
        self.assertNotIn("srcset", page)

    def test_same_item_in_two_feeds_is_saved_once(self):
        archiver = RSSArchiver(str(self.root))
        self.assertEqual(archiver.save("linux-do-34", "前沿快讯", self.items, NOW), 1)
        self.assertEqual(archiver.save("linux-do-top", "Top", self.items, NOW), 0)

    def test_sanitize_removes_scripts_and_handlers(self):
        cleaned = sanitize_html('<a href="javascript:x()" onclick="y()">a</a><iframe src="z"></iframe><b>ok</b>')
        self.assertNotIn("javascript:", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertNotIn("iframe", cleaned)
        self.assertIn("<b>ok</b>", cleaned)


if __name__ == "__main__":
    unittest.main()
