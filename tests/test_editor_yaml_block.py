"""Visual editor list rewrites keep fields, comments and valid YAML (docs/assets/yaml-block.js)."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

HELPER = Path(__file__).resolve().parents[1] / "docs" / "assets" / "yaml-block.js"
NODE = shutil.which("node")

DRIVER = """
const helper = require(process.argv[1]);
let input = '';
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const req = JSON.parse(input);
  let out;
  if (req.op === 'rewrite') out = helper.rewriteListItems(req.lines, req.old, req.new, req.options || {});
  else if (req.op === 'sequence') out = helper.replaceSequenceValue(req.lines, req.index, req.list);
  else if (req.op === 'split') out = helper.splitInlineComment(req.text);
  process.stdout.write(JSON.stringify(out));
});
"""

CONFIG = """platforms:
  enabled: true
  sources:
    - id: "toutiao"
      name: "今日头条"
      expected_domain: "toutiao.com"
    - id: "baidu"
      name: "百度热搜"
      expected_domain: "baidu.com"
    - id: "zhihu"
      name: "知乎"
      expected_domain: "zhihu.com"

rss:
  enabled: true
  feeds:
    - id: "hacker-news"
      name: "Hacker News"
      url: "https://hnrss.org/frontpage"

    - id: "ruanyifeng"
      name: "阮一峰的网络日志"
      url: "http://www.ruanyifeng.com/blog/atom.xml"
      enabled: false                  # 禁用
      # max_age_days: 3               # 示例

    # ---- 自建 RSSHub 源 ----
    - id: "rsshub-a"
      name: "A"
      url: "http://rsshub:1200/a#frag"
      max_age_days: 14                # 约每周一篇

    - id: "rsshub-b"
      name: "B"
      url: "http://rsshub:1200/b"

display:
  standalone:
    platforms: ["zhihu", "baidu"]      # 热榜平台 ID 列表
    rss_feeds:                         # RSS 源 ID 列表
      - "rsshub-a"
      - "rsshub-b"
    max_items: 20
"""


def call(payload):
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(HELPER)], input=json.dumps(payload),
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def block(lines, start_marker, end_marker):
    """Region the editor passes in: after `start_marker` up to the last data line before `end_marker`."""
    start = lines.index(start_marker) + 1
    end = lines.index(end_marker)
    while not lines[end - 1].strip():
        end -= 1
    return start, end


@unittest.skipUnless(NODE, "node is required to run the editor helper")
class EditorYamlBlockTest(unittest.TestCase):
    def setUp(self):
        self.lines = CONFIG.split("\n")
        self.doc = yaml.safe_load(CONFIG)

    def rewrite(self, section, new_items, **options):
        if section == "platforms":
            start, end = block(self.lines, "  sources:", "rss:")
            old = self.doc["platforms"]["sources"]
        else:
            start, end = block(self.lines, "  feeds:", "display:")
            old = self.doc["rss"]["feeds"]
        region = call({"op": "rewrite", "lines": self.lines[start:end], "old": old, "new": new_items,
                       "options": {"keyField": "id", **options}})
        text = "\n".join(self.lines[:start] + region + self.lines[end:])
        return text, yaml.safe_load(text)

    def test_unchanged_lists_are_byte_identical(self):
        text, _ = self.rewrite("platforms", self.doc["platforms"]["sources"])
        self.assertEqual(text, CONFIG)
        text, _ = self.rewrite("rss", self.doc["rss"]["feeds"], blankLineBetween=True)
        self.assertEqual(text, CONFIG)

    def test_deleting_platform_keeps_expected_domain(self):
        remaining = self.doc["platforms"]["sources"][1:]
        text, doc = self.rewrite("platforms", remaining)
        self.assertEqual(doc["platforms"]["sources"], remaining)
        self.assertEqual(text.count("expected_domain:"), 2)
        self.assertNotIn("toutiao", text.split("rss:")[0])

    def test_reordering_platforms_moves_whole_items(self):
        sources = self.doc["platforms"]["sources"]
        reordered = [sources[2], sources[0], sources[1]]
        _, doc = self.rewrite("platforms", reordered)
        self.assertEqual(doc["platforms"]["sources"], reordered)

    def test_toggling_feed_keeps_comments(self):
        feeds = [dict(feed) for feed in self.doc["rss"]["feeds"]]
        feeds[1]["enabled"] = True
        text, doc = self.rewrite("rss", feeds, blankLineBetween=True)
        self.assertEqual(doc["rss"]["feeds"], feeds)
        self.assertIn("      enabled: true                  # 禁用", text)
        self.assertIn("      # max_age_days: 3               # 示例", text)
        self.assertIn("    # ---- 自建 RSSHub 源 ----", text)

    def test_editing_feed_keeps_other_fields_and_inline_comments(self):
        feeds = [dict(feed) for feed in self.doc["rss"]["feeds"]]
        feeds[2] = {**feeds[2], "url": "http://rsshub:1200/a2", "max_age_days": 7}
        text, doc = self.rewrite("rss", feeds, blankLineBetween=True)
        self.assertEqual(doc["rss"]["feeds"], feeds)
        self.assertIn('      url: "http://rsshub:1200/a2"', text)
        self.assertIn("      max_age_days: 7                # 约每周一篇", text)

    def test_deleting_feed_moves_group_comment_to_next_item(self):
        feeds = [feed for feed in self.doc["rss"]["feeds"] if feed["id"] != "rsshub-a"]
        text, doc = self.rewrite("rss", feeds, blankLineBetween=True)
        self.assertEqual(doc["rss"]["feeds"], feeds)
        comment_at = text.index("# ---- 自建 RSSHub 源 ----")
        self.assertLess(comment_at, text.index('- id: "rsshub-b"'))
        self.assertGreater(comment_at, text.index('- id: "ruanyifeng"'))

    def test_adding_feed_writes_all_fields(self):
        feeds = self.doc["rss"]["feeds"] + [{"id": "new", "name": "New \"quoted\"", "url": "https://x/#a", "enabled": False, "max_age_days": 3}]
        text, doc = self.rewrite("rss", feeds, blankLineBetween=True)
        self.assertEqual(doc["rss"]["feeds"], feeds)
        self.assertIn('\n\n    - id: "new"\n      name: "New \\"quoted\\""', text)

    def test_mismatched_text_falls_back_to_full_rewrite(self):
        old = [{"id": "a", "name": "A", "expected_domain": "a.com"}]
        out = call({"op": "rewrite", "lines": ['    # comment'], "old": old, "new": old, "options": {"keyField": "id"}})
        parsed = yaml.safe_load("sources:\n" + "\n".join(out))
        self.assertEqual(parsed["sources"], old)

    def test_standalone_block_list_rewritten_in_place(self):
        index = self.lines.index("    rss_feeds:                         # RSS 源 ID 列表")
        lines = call({"op": "sequence", "lines": self.lines, "index": index, "list": ["rsshub-b"]})
        text = "\n".join(lines)
        doc = yaml.safe_load(text)
        self.assertEqual(doc["display"]["standalone"]["rss_feeds"], ["rsshub-b"])
        self.assertEqual(doc["display"]["standalone"]["max_items"], 20)
        self.assertIn("    rss_feeds:                         # RSS 源 ID 列表\n      - \"rsshub-b\"\n    max_items: 20", text)

    def test_standalone_flow_and_empty_lists(self):
        index = self.lines.index('    platforms: ["zhihu", "baidu"]      # 热榜平台 ID 列表')
        text = "\n".join(call({"op": "sequence", "lines": self.lines, "index": index, "list": ["zhihu"]}))
        self.assertIn('    platforms: ["zhihu"]      # 热榜平台 ID 列表', text)
        index = self.lines.index("    rss_feeds:                         # RSS 源 ID 列表")
        text = "\n".join(call({"op": "sequence", "lines": self.lines, "index": index, "list": []}))
        doc = yaml.safe_load(text)
        self.assertEqual(doc["display"]["standalone"]["rss_feeds"], [])
        self.assertIn("    rss_feeds: []                         # RSS 源 ID 列表\n    max_items: 20", text)

    def test_split_inline_comment_ignores_hash_in_quotes(self):
        self.assertEqual(call({"op": "split", "text": ' "http://x/#a"   # note'}), [' "http://x/#a"', "   # note"])
        self.assertEqual(call({"op": "split", "text": " a#b"}), [" a#b", ""])


if __name__ == "__main__":
    unittest.main()
