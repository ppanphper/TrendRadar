"""推送反馈：消息映射、编号网格、反馈切换、权限、兴趣建议的生成与应用、分类提示附带反馈。"""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trendradar.bot import FeedbackBot
from trendradar.notification.feedback import FeedbackStore, feedback_db_path, grid_keyboard

NOW = datetime(2026, 11, 15, 21, 30, tzinfo=ZoneInfo("Asia/Shanghai"))  # 周日
ITEMS = [
    {"n": 1, "title": "大模型发布", "url": "https://a", "source": "知乎", "group": "大模型"},
    {"n": 2, "title": "明星八卦", "url": "https://b", "source": "微博", "group": "文化"},
]


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name) / "config-local"
        self.config_dir.mkdir()
        self.config = {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-100", "TELEGRAM_MESSAGE_THREAD_ID": "366",
            "TIMEZONE": "Asia/Shanghai",
            "STORAGE": {"LOCAL": {"DATA_DIR": str(Path(self.tmp.name) / "output")}},
            "AI_FILTER": {"INTERESTS_PATH": str(self.config_dir / "ai_interests.txt")},
        }
        self.bot = FeedbackBot(self.config)
        self.calls = []
        self.bot.api = lambda method, timeout=30, **payload: self.calls.append((method, payload)) or {}
        self.bot.now = lambda: NOW
        self.bot.store.save_message("-100", 10, ITEMS, NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def _callback(self, data, chat="-100", user=7):
        self.calls.clear()
        self.bot.handle_update({"update_id": 1, "callback_query": {
            "id": "cb", "from": {"id": user}, "data": data,
            "message": {"message_id": 10, "chat": {"id": int(chat)}},
        }})
        return dict(self.calls)

    def test_grid_and_toggle(self):
        calls = self._callback("fb:d")
        rows = calls["editMessageReplyMarkup"]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["callback_data"] for b in rows[0]], ["fv:d:1", "fv:d:2"])
        self.assertEqual(rows[-1][0]["callback_data"], "fb:back")

        calls = self._callback("fv:d:2")
        self.assertIn("明星八卦", calls["answerCallbackQuery"]["text"])
        self.assertEqual(calls["editMessageReplyMarkup"]["reply_markup"]["inline_keyboard"][0][1]["text"], "2✓")
        self.assertEqual([f["title"] for f in self.bot.store.recent("dislike")], ["明星八卦"])

        # 改点感兴趣会替换掉不感兴趣；再点一次取消
        self._callback("fv:l:2")
        self.assertEqual(self.bot.store.recent("dislike"), [])
        calls = self._callback("fv:l:2")
        self.assertTrue(calls["answerCallbackQuery"]["text"].startswith("已取消"))
        self.assertEqual(self.bot.store.recent("like"), [])

    def test_unauthorized_chat_or_user(self):
        calls = self._callback("fv:d:1", chat="-999")
        self.assertEqual(calls["answerCallbackQuery"]["text"], "没有权限")
        self.bot.allowed_users = {"1"}
        self._callback("fv:d:1", user=7)
        self.assertEqual(self.bot.store.recent("dislike"), [])

    def test_suggestion_flow_writes_interests_with_backup(self):
        (self.config_dir / "ai_interests.txt").write_text("关注 AI\n关注娱乐\n", encoding="utf-8")
        for n in (1, 2):
            self.bot.store.toggle("-100", 10, ITEMS[n - 1], "like" if n == 1 else "dislike", 7, NOW)
        self.bot.store.toggle("-100", 10, {**ITEMS[1], "n": 3}, "dislike", 7, NOW)
        with patch.object(FeedbackBot, "_ask_ai", return_value={"summary": "- 删除娱乐（多次不感兴趣）", "interests": "关注 AI"}):
            sid = self.bot.create_suggestion(manual=True)
        self.assertIsInstance(sid, int)
        method, payload = self.calls[-1]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["message_thread_id"], 366)
        self.assertIn("- 关注娱乐", payload["text"])
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"sg:a:{sid}")

        calls = self._callback(f"sg:a:{sid}")
        self.assertTrue(calls["answerCallbackQuery"]["text"].startswith("已应用"))
        self.assertEqual((self.config_dir / "ai_interests.txt").read_text(encoding="utf-8"), "关注 AI\n")
        self.assertEqual(len(list((self.config_dir / "backups").iterdir())), 1)
        calls = self._callback(f"sg:a:{sid}")
        self.assertEqual(calls["answerCallbackQuery"]["text"], "这条建议已处理过")

    def test_weekly_suggestion_needs_enough_feedback(self):
        self.bot.maybe_weekly_suggestion()
        self.assertEqual([c for c in self.calls if c[0] == "sendMessage"], [])
        # 已记占位，本周不再检查
        self.assertIsNotNone(self.bot.store.last_suggestion_at())

    def test_prompt_section_and_grid_cap(self):
        store = FeedbackStore(feedback_db_path(self.config["STORAGE"]["LOCAL"]["DATA_DIR"]))
        self.assertEqual(store.prompt_section(), "")
        store.toggle("-100", 10, ITEMS[0], "like", 7, NOW)
        self.assertIn("- 大模型发布", store.prompt_section())
        kb = grid_keyboard(list(range(1, 200)), "l", set())
        self.assertEqual(sum(len(r) for r in kb["inline_keyboard"][:-1]), 96)


if __name__ == "__main__":
    unittest.main()
