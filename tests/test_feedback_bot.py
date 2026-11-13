"""推送反馈：消息映射、编号网格、正文勾选与批量提交、反馈切换、权限、后台应答与重绘、连接轮换、兴趣建议的生成与应用、分类提示附带反馈。"""

import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests

from trendradar.bot import ANSWER_WORKERS, SEND_TIMEOUT, FeedbackBot
from trendradar.notification.feedback import FeedbackStore, feedback_db_path, grid_keyboard

NOW = datetime(2026, 11, 15, 21, 30, tzinfo=ZoneInfo("Asia/Shanghai"))  # 周日
ITEMS = [
    {"n": 1, "title": "大模型发布", "url": "https://a", "source": "知乎", "group": "大模型"},
    {"n": 2, "title": "明星八卦", "url": "https://b", "source": "微博", "group": "文化"},
]
RICH_TEXT = "<b>📰 摘要</b>\n\n<b>大模型</b> · 2\n<code>1</code> 大模型发布\n<code>2</code> 明星八卦"


class InlineExecutor:
    """测试用：回调应答当场执行，断言不必等后台线程"""

    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait=True):
        pass


def reply(result):
    response = MagicMock()
    response.json.return_value = {"ok": True, "result": result}
    return response


def wait_until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("等待超时")
        time.sleep(0.01)


class FakeTelegram:
    """测试用的 Bot API：按方法名分派，记下每条连接的请求，并检查同一连接有没有被两个线程同时占用"""

    def __init__(self, batches=(), edit=None, answer=None):
        self.batches = list(batches)  # getUpdates 依次返回的批次
        self.edit, self.answer = edit, answer
        self.posted, self.edits, self.timeouts = [], [], {}
        self.sessions, self.overlaps = [], []
        self.lock = threading.Lock()

    def session(self):
        session = MagicMock()
        busy = threading.Lock()

        def post(url, json=None, timeout=None, **kw):
            method = url.rsplit("/", 1)[1]
            if not busy.acquire(blocking=False):
                self.overlaps.append(method)
                busy.acquire()
            try:
                with self.lock:
                    self.posted.append(method)
                    self.timeouts[method] = timeout
                if method == "getUpdates":
                    return reply(self.batches.pop(0) if self.batches else [])
                if method == "editMessageText":
                    if self.edit:
                        self.edit(json)
                    self.edits.append(json)
                elif method == "answerCallbackQuery" and self.answer:
                    self.answer(json)
                return reply(True)
            finally:
                busy.release()

        session.post.side_effect = post
        self.sessions.append(session)
        return session


def callback_update(update_id, data, message_id=20):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": {"id": 7}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": -100}},
    }}


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
        self.bot.answers = InlineExecutor()
        self.calls = []
        self.bot.api = lambda method, http_timeout=30, **payload: self.calls.append((method, payload)) or {}
        self.bot.now = lambda: NOW
        self.bot.store.save_message("-100", 10, ITEMS, NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def _callback(self, data, chat="-100", user=7, message_id=10):
        self.calls.clear()
        self.bot.handle_update({"update_id": 1, "callback_query": {
            "id": "cb", "from": {"id": user}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": int(chat)}},
        }})
        return dict(self.calls)

    def test_rich_message_picking_and_batch_submit(self):
        self.bot.store.save_message("-100", 20, ITEMS, NOW, text=RICH_TEXT)

        calls = self._callback("fb:d", message_id=20)
        self.assertEqual(calls["answerCallbackQuery"]["text"], "")
        self.assertNotIn("editMessageText", calls)  # 重绘留到这批更新处理完
        self.bot.flush_redraws()
        edit = dict(self.calls)["editMessageText"]
        self.assertEqual(edit["chat_id"], -100)
        self.assertIn('data="fv:d:1">☐</tg-button>', edit["rich_message"]["html"])
        self.assertEqual([b["callback_data"] for b in edit["reply_markup"]["inline_keyboard"][0]], ["fb:back", "fb:ok"])

        # 连续勾选：不弹提示、不写库，只编辑一次
        self._callback("fv:d:1", message_id=20)
        calls = self._callback("fv:d:2", message_id=20)
        self.assertEqual(calls["answerCallbackQuery"]["text"], "")
        self.assertEqual(self.bot.store.recent("dislike"), [])
        self.calls.clear()
        self.bot.flush_redraws()
        self.assertEqual([m for m, _ in self.calls], ["editMessageText"])
        edit = self.calls[0][1]
        self.assertIn('data="fv:d:2">✅</tg-button>', edit["rich_message"]["html"])
        self.assertEqual(edit["reply_markup"]["inline_keyboard"][0][1]["text"], "提交 👎 2 条")

        # 再点一次取消勾选，提交后写入并在按钮上显示条数
        self._callback("fv:d:1", message_id=20)
        calls = self._callback("fb:ok", message_id=20)
        self.assertEqual(calls["answerCallbackQuery"]["text"], "已记录 1 条不感兴趣")
        self.assertEqual([f["title"] for f in self.bot.store.recent("dislike")], ["明星八卦"])
        self.calls.clear()
        self.bot.flush_redraws()
        edit = self.calls[0][1]
        self.assertNotIn("tg-button", edit["rich_message"]["html"])
        row = edit["reply_markup"]["inline_keyboard"][0]
        self.assertEqual((row[0]["text"], row[0].get("style")), ("👍 感兴趣", None))
        self.assertEqual((row[1]["text"], row[1]["style"]), ("👎 不感兴趣 · 1", "danger"))

        # 改标感兴趣会去掉不感兴趣；取消则丢弃草稿
        self._callback("fb:l", message_id=20)
        self._callback("fv:l:2", message_id=20)
        self._callback("fb:back", message_id=20)
        self.assertEqual(self.bot.store.recent("like"), [])
        self._callback("fv:l:2", message_id=20)  # 没进勾选状态（如 bot 重启）也能接着勾
        self._callback("fb:ok", message_id=20)
        self.assertEqual([f["title"] for f in self.bot.store.recent("like")], ["明星八卦"])
        self.assertEqual(self.bot.store.recent("dislike"), [])

    def _real_bot(self, telegram):
        """走真实的 api()、连接池、应答线程池和重绘线程"""
        self.bot.store.save_message("-100", 20, ITEMS, NOW, text=RICH_TEXT)
        bot = FeedbackBot(self.config, session_factory=telegram.session)
        bot.now = lambda: NOW
        self.addCleanup(bot.close)
        return bot

    def test_polling_continues_while_redraw_in_flight(self):
        released = threading.Event()

        def slow_first_edit(payload):
            if not telegram.posted.count("editMessageText") > 1:
                released.wait(3)

        telegram = FakeTelegram(
            [[callback_update(5, "fb:d")], [callback_update(6, "fv:d:1")], [callback_update(7, "fv:d:2")]],
            edit=slow_first_edit,
        )
        bot = self._real_bot(telegram)
        bot.start_redrawer()
        self.assertEqual(bot.poll_once(timeout=0), 1)
        wait_until(lambda: "editMessageText" in telegram.posted)
        # 第一次重绘还没返回，轮询照常收到后两次勾选
        self.assertEqual(bot.poll_once(timeout=0), 1)
        self.assertEqual(bot.poll_once(timeout=0), 1)
        self.assertEqual(telegram.edits, [])
        released.set()
        # 重绘一条一条发，期间的勾选合并成一次，画的是最新状态
        wait_until(lambda: len(telegram.edits) == 2)
        time.sleep(0.1)
        self.assertEqual(len(telegram.edits), 2)
        html = telegram.edits[1]["rich_message"]["html"]
        self.assertIn('data="fv:d:1">✅</tg-button>', html)
        self.assertIn('data="fv:d:2">✅</tg-button>', html)
        self.assertEqual(telegram.overlaps, [])
        self.assertEqual(telegram.timeouts["editMessageText"], SEND_TIMEOUT)

    def test_answer_in_background_does_not_delay_redraw(self):
        released, answered = threading.Event(), []

        def slow_answer(payload):
            released.wait(5)
            answered.append(payload["callback_query_id"])

        telegram = FakeTelegram([[callback_update(5, "fb:d")]], answer=slow_answer)
        bot = self._real_bot(telegram)
        self.assertEqual(bot.poll_once(timeout=0), 1)
        bot.flush_redraws()
        # 应答还卡着，重绘已经发出，两者用的是不同连接
        self.assertEqual(len(telegram.edits), 1)
        self.assertEqual(answered, [])
        released.set()
        bot.answers.shutdown(wait=True)
        self.assertEqual(answered, ["cb5"])
        self.assertEqual(telegram.overlaps, [])
        self.assertEqual(telegram.timeouts["answerCallbackQuery"], SEND_TIMEOUT)

    def test_batch_answers_are_sent_concurrently(self):
        barrier, answered = threading.Barrier(ANSWER_WORKERS, timeout=2), []

        def answer(payload):
            barrier.wait()  # 串行应答时等不齐，超时报错
            answered.append(payload["callback_query_id"])

        updates = [callback_update(5 + i, f"fv:d:{i + 1}") for i in range(ANSWER_WORKERS)]
        telegram = FakeTelegram([updates], answer=answer)
        bot = self._real_bot(telegram)
        bot.poll_once(timeout=0)
        bot.flush_redraws()
        bot.answers.shutdown(wait=True)
        self.assertEqual(sorted(answered), [f"cb{5 + i}" for i in range(ANSWER_WORKERS)])
        self.assertEqual(len(telegram.edits), 1)

    def test_answer_failure_is_logged_without_blocking_redraw(self):
        def fail(payload):
            raise requests.ConnectionError("proxy down")

        telegram = FakeTelegram([[callback_update(5, "fb:d")]], answer=fail)
        bot = self._real_bot(telegram)
        out = io.StringIO()
        with redirect_stdout(out):
            bot.poll_once(timeout=0)
            bot.flush_redraws()
            bot.answers.shutdown(wait=True)
        self.assertEqual(len(telegram.edits), 1)
        self.assertIn("应答回调失败", out.getvalue())

    def test_redraw_retries_once_after_connection_error(self):
        failures = [requests.ConnectionError("reset")]

        def flaky(payload):
            if failures:
                raise failures.pop()

        telegram = FakeTelegram(edit=flaky)
        bot = self._real_bot(telegram)
        bot.answers = InlineExecutor()
        bot.handle_update(callback_update(5, "fb:d"))
        bot.flush_redraws()
        # 换一条连接重画成功，用户看到的勾选状态和草稿一致
        self.assertEqual(telegram.posted.count("editMessageText"), 2)
        self.assertEqual(len(telegram.edits), 1)

        # 连续失败只重试一次，记日志后不再占着重绘
        failures.extend([requests.ConnectionError("reset")] * 3)
        bot.handle_update(callback_update(6, "fv:d:1"))
        out = io.StringIO()
        with redirect_stdout(out):
            bot.flush_redraws()
        self.assertEqual(telegram.posted.count("editMessageText"), 4)
        self.assertIn("重绘消息 20 失败", out.getvalue())

    def test_sessions_rotate_so_none_sits_idle(self):
        telegram = FakeTelegram()
        bot = FeedbackBot(self.config, session_factory=telegram.session)
        self.addCleanup(bot.close)
        # 至少能同时容纳轮询、重绘和全部应答线程
        self.assertGreaterEqual(len(telegram.sessions), ANSWER_WORKERS + 2)
        # 空闲时长轮询依次用到每条连接，任何一条都不会闲置到被服务端断开
        for _ in range(len(telegram.sessions) * 2):
            bot.api("getUpdates", timeout=0)
        self.assertEqual([s.post.call_count for s in telegram.sessions], [2] * len(telegram.sessions))

    def test_grid_and_toggle(self):
        calls = self._callback("fb:d")
        rows = calls["editMessageReplyMarkup"]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["callback_data"] for b in rows[0]], ["fv:d:1", "fv:d:2"])
        self.assertEqual(rows[-1][0]["callback_data"], "fb:back")

        calls = self._callback("fv:d:2")
        self.assertIn("明星八卦", calls["answerCallbackQuery"]["text"])
        self.assertNotIn("editMessageText", calls)
        self.assertEqual(calls["editMessageReplyMarkup"]["reply_markup"]["inline_keyboard"][0][1]["text"], "2✓")
        self.assertEqual([f["title"] for f in self.bot.store.recent("dislike")], ["明星八卦"])

        # 改点感兴趣会替换掉不感兴趣；再点一次取消
        self._callback("fv:l:2")
        self.assertEqual(self.bot.store.recent("dislike"), [])
        calls = self._callback("fv:l:2")
        self.assertTrue(calls["answerCallbackQuery"]["text"].startswith("已取消"))
        self.assertEqual(self.bot.store.recent("like"), [])

        # 返回时按钮显示已标记的条数
        self._callback("fv:d:1")
        calls = self._callback("fb:back")
        self.assertEqual(calls["editMessageReplyMarkup"]["reply_markup"]["inline_keyboard"][0][1]["text"], "👎 不感兴趣 · 1")

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

    def test_poll_once_sends_long_poll_timeout_and_advances_offset(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"ok": True, "result": [
            {"update_id": 41, "message": {"text": "hi", "chat": {"id": -100}, "from": {"id": 7}}},
        ]}
        bot = FeedbackBot(self.config, session_factory=lambda: session)
        self.addCleanup(bot.close)
        self.assertEqual(bot.poll_once(timeout=50), 1)
        kwargs = session.post.call_args.kwargs
        self.assertEqual(kwargs["json"]["timeout"], 50)
        self.assertEqual(kwargs["json"]["allowed_updates"], ["callback_query", "message"])
        self.assertEqual(kwargs["timeout"], 60)
        self.assertEqual(bot.offset, 42)

    def test_check_ready_registers_command_menu(self):
        replies = {"getWebhookInfo": {"url": ""}, "getMe": {"username": "DSnow2Bot"}}
        self.bot.api = lambda method, http_timeout=30, **payload: self.calls.append((method, payload)) or replies.get(method, True)
        self.assertTrue(self.bot.check_ready())
        method, payload = self.calls[-1]
        self.assertEqual(method, "setMyCommands")
        self.assertEqual(payload["scope"], {"type": "chat", "chat_id": -100})
        self.assertEqual(payload["commands"][0]["command"], "interests")

    def test_interests_command_replies_in_same_topic(self):
        self.bot.username = "DSnow2Bot"
        message = {"text": "/interests@DSnow2Bot", "chat": {"id": -100}, "from": {"id": 7}, "message_thread_id": 12}
        self.bot.handle_update({"update_id": 2, "message": message})
        methods = [m for m, _ in self.calls]
        self.assertEqual(methods[0], "sendChatAction")
        sent = [p for m, p in self.calls if m == "sendMessage"][0]
        self.assertEqual(sent["message_thread_id"], 12)
        self.assertIn("反馈", sent["text"])  # 反馈不足时回复原因

        self.calls.clear()
        self.bot.handle_update({"update_id": 3, "message": {**message, "text": "/interests@OtherBot"}})
        self.bot.handle_update({"update_id": 4, "message": {**message, "text": "/interestsabc"}})
        self.assertEqual(self.calls, [])

    def test_prompt_section_and_grid_cap(self):
        store = FeedbackStore(feedback_db_path(self.config["STORAGE"]["LOCAL"]["DATA_DIR"]))
        self.assertEqual(store.prompt_section(), "")
        store.toggle("-100", 10, ITEMS[0], "like", 7, NOW)
        self.assertIn("- 大模型发布", store.prompt_section())
        kb = grid_keyboard(list(range(1, 200)), "l", set())
        self.assertEqual(sum(len(r) for r in kb["inline_keyboard"][:-1]), 96)


if __name__ == "__main__":
    unittest.main()
