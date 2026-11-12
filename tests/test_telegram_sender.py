"""Telegram 发送：话题、静音、反馈按钮与消息映射、HTML 解析失败回退纯文本。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from trendradar.notification.feedback import FeedbackStore
from trendradar.notification.senders import send_to_telegram

REPORT = {"stats": [{"word": "大模型", "count": 1, "titles": [
    {"title": "A<B", "source_name": "知乎", "ranks": [1], "url": "https://a", "relevance_score": 0.9},
]}]}


def _ok(message_id):
    resp = MagicMock(status_code=200, text='{"ok":true}')
    resp.json.return_value = {"ok": True, "result": {"message_id": message_id}}
    return resp


class SenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "feedback.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _send(self, **kwargs):
        return send_to_telegram(
            "token", "-100", REPORT, "增量分析", mode="incremental", style="digest",
            message_thread_id="366", get_time_func=lambda: datetime(2026, 11, 12, 10, 0), batch_interval=0, **kwargs,
        )

    def test_digest_with_feedback_sends_rich_message_and_mapping(self):
        with patch("trendradar.notification.senders.requests.post", return_value=_ok(42)) as post:
            self.assertTrue(self._send(silent=True, feedback_db=self.db))
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].endswith("/sendRichMessage"))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["message_thread_id"], 366)
        self.assertTrue(payload["disable_notification"])
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][1]["callback_data"], "fb:d")
        self.assertNotIn("text", payload)
        self.assertIn("<code>1</code> ", payload["rich_message"]["html"])
        self.assertIn("A&lt;B", payload["rich_message"]["html"])
        store = FeedbackStore(self.db)
        self.assertEqual([(i["n"], i["title"]) for i in store.message_items("-100", 42)], [(1, "A<B")])
        self.assertIn("<code>1</code> ", store.message_text("-100", 42))

    def test_rich_failure_falls_back_to_plain_message_with_grid(self):
        bad = MagicMock(status_code=400, text='{"ok":false,"description":"Bad Request: rich"}')
        bad.json.return_value = {"ok": False, "description": "Bad Request: rich"}
        with patch("trendradar.notification.senders.requests.post", side_effect=[bad, _ok(7)]) as post:
            self.assertTrue(self._send(feedback_db=self.db))
        self.assertTrue(post.call_args.args[0].endswith("/sendMessage"))
        self.assertEqual(post.call_args.kwargs["json"]["parse_mode"], "HTML")
        store = FeedbackStore(self.db)
        self.assertEqual(len(store.message_items("-100", 7)), 1)
        self.assertIsNone(store.message_text("-100", 7))

    def test_no_keyboard_without_feedback(self):
        with patch("trendradar.notification.senders.requests.post", return_value=_ok(1)) as post:
            self.assertTrue(self._send())
        self.assertTrue(post.call_args.args[0].endswith("/sendMessage"))
        self.assertNotIn("reply_markup", post.call_args.kwargs["json"])

    def test_parse_error_falls_back_to_plain_text(self):
        rich_bad = MagicMock(status_code=400, text='{"ok":false}')
        rich_bad.json.return_value = {"ok": False}
        bad = MagicMock(status_code=400, text='{"ok":false,"description":"Bad Request: can\'t parse entities"}')
        with patch("trendradar.notification.senders.requests.post", side_effect=[rich_bad, bad, _ok(5)]) as post:
            self.assertTrue(self._send(feedback_db=self.db))
        retry = post.call_args_list[2].kwargs["json"]
        self.assertNotIn("parse_mode", retry)
        self.assertIn("A<B", retry["text"])
        self.assertEqual(len(FeedbackStore(self.db).message_items("-100", 5)), 1)


if __name__ == "__main__":
    unittest.main()
