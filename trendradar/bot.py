# coding=utf-8
"""
推送反馈 bot：长轮询接收 Telegram 按钮回调

- 👍 / 👎 按钮：Rich Message 摘要在正文每条前显示勾选按钮，勾完一次提交；普通消息展开编号网格，点编号即记录
- 每周日 21:00 后或收到 /interests：根据反馈生成兴趣描述修改建议，点「应用」写入 ai_filter.interests_path

运行：python -m trendradar.bot（需要独立 bot：同一 bot 只能有一个轮询方，且不能设置 webhook）
设计说明见 docs/design.md 第 7 节。
"""

import difflib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests

from trendradar.core.config import parse_multi_account_config
from trendradar.notification.feedback import (
    LABELS,
    FeedbackStore,
    base_keyboard,
    feedback_db_path,
    grid_keyboard,
    picking_keyboard,
    suggestion_keyboard,
)
from trendradar.notification.telegram_digest import to_rich_html
from trendradar.utils.time import DEFAULT_TIMEZONE, get_configured_time

POLL_TIMEOUT = 50
WEEKLY_WEEKDAY = 7       # 周日
WEEKLY_HOUR = 21
MIN_FEEDBACK_FOR_SUGGESTION = 3

# 注册到 Telegram 的命令菜单：在群里输入 / 时弹出，可按文字筛选
COMMANDS = [
    {"command": "interests", "description": "根据近期 👍/👎 反馈生成兴趣调整建议"},
]


class TelegramError(Exception):
    def __init__(self, status: int, description: str):
        super().__init__(f"{status}: {description}")
        self.status = status
        self.description = description


class FeedbackBot:
    def __init__(self, config: Dict, session: Optional[requests.Session] = None):
        self.config = config
        tokens = parse_multi_account_config(config.get("TELEGRAM_BOT_TOKEN", ""))
        self.token = tokens[0] if tokens else ""
        self.chat_ids = {c for c in parse_multi_account_config(config.get("TELEGRAM_CHAT_ID", "")) if c}
        threads = parse_multi_account_config(config.get("TELEGRAM_MESSAGE_THREAD_ID", ""))
        self.thread_id = threads[0] if threads else ""
        self.allowed_users = {u for u in parse_multi_account_config(config.get("TELEGRAM_FEEDBACK_USER_IDS", "")) if u}
        self.timezone = config.get("TIMEZONE", DEFAULT_TIMEZONE)
        data_dir = config.get("STORAGE", {}).get("LOCAL", {}).get("DATA_DIR", "output")
        self.store = FeedbackStore(feedback_db_path(data_dir))
        self.session = session or requests.Session()
        proxy = config.get("DEFAULT_PROXY") if config.get("USE_PROXY") else None
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.offset: Optional[int] = None
        self.username = ""
        # 正文勾选的草稿：(chat_id, message_id) -> (反馈代码, 已勾选编号)；提交前只存在内存里
        self.drafts: Dict[Tuple[str, int], Tuple[str, Set[int]]] = {}
        # 待重绘的 Rich Message；一批更新处理完再统一重绘，连续勾选只编辑一次
        self.dirty: Set[Tuple[str, int]] = set()

    # --- Telegram API ---

    def api(self, method: str, http_timeout: int = 30, **payload):
        # http_timeout 是本地请求超时；payload 里的 timeout 是 getUpdates 长轮询的服务端等待时间
        response = self.session.post(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json=payload, proxies=self.proxies, timeout=http_timeout,
        )
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(response.status_code, response.text[:200])
        if not data.get("ok"):
            raise TelegramError(response.status_code, data.get("description", ""))
        return data.get("result")

    def now(self) -> datetime:
        return get_configured_time(self.timezone)

    # --- 轮询 ---

    def check_ready(self) -> bool:
        info = self.api("getWebhookInfo")
        if info.get("url"):
            print("[反馈bot] 该 bot 设置了 webhook，无法用 getUpdates 轮询；请确认这是独立 bot 后再删除 webhook")
            return False
        me = self.api("getMe")
        self.username = me.get("username") or ""
        print(f"[反馈bot] 已连接 @{self.username}，监听 chat: {', '.join(sorted(self.chat_ids))}")
        self.register_commands()
        return True

    def register_commands(self) -> None:
        """按群注册命令菜单；失败只记日志，不影响按钮处理"""
        for chat in sorted(self.chat_ids):
            chat_id = int(chat) if chat.lstrip("-").isdigit() else chat
            try:
                self.api("setMyCommands", commands=COMMANDS, scope={"type": "chat", "chat_id": chat_id})
            except Exception as e:
                print(f"[反馈bot] 注册命令菜单失败（chat {chat}）: {e}")

    def poll_once(self, timeout: int = POLL_TIMEOUT) -> int:
        payload = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        updates = self.api("getUpdates", http_timeout=timeout + 10, **payload)
        for update in updates:
            self.offset = update["update_id"] + 1
            try:
                self.handle_update(update)
            except Exception as e:
                print(f"[反馈bot] 处理更新 {update.get('update_id')} 失败: {type(e).__name__}: {e}")
        self.flush_redraws()
        return len(updates)

    def flush_redraws(self) -> None:
        while self.dirty:
            chat_id, message_id = self.dirty.pop()
            try:
                self.redraw(chat_id, message_id)
            except TelegramError as e:
                retry = re.search(r"retry after (\d+)", e.description)
                if e.status == 429 and retry:
                    time.sleep(min(int(retry.group(1)), 10))
                    self.dirty.add((chat_id, message_id))
                elif "not modified" not in e.description:
                    print(f"[反馈bot] 重绘消息 {message_id} 失败: {e}")
            except Exception as e:
                print(f"[反馈bot] 重绘消息 {message_id} 失败: {type(e).__name__}: {e}")

    def redraw(self, chat_id: str, message_id: int) -> None:
        """按草稿或已提交的反馈重绘 Rich Message 摘要"""
        text = self.store.message_text(chat_id, message_id)
        if not text:
            return
        draft = self.drafts.get((chat_id, message_id))
        if draft:
            code, picked = draft
            html_text, markup = to_rich_html(text, code, picked), picking_keyboard(code, len(picked))
        else:
            html_text, markup = to_rich_html(text), base_keyboard(self.store.counts(chat_id, message_id))
        chat = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        self.api("editMessageText", chat_id=chat, message_id=message_id, rich_message={"html": html_text},
                 reply_markup=markup)

    def run_forever(self) -> None:
        while True:
            try:
                if self.check_ready():
                    break
            except Exception as e:
                print(f"[反馈bot] 连接 Telegram 失败: {e}")
            time.sleep(300)

        backoff = 5
        while True:
            try:
                self.poll_once()
                backoff = 5
                self.maybe_weekly_suggestion()
            except TelegramError as e:
                if e.status == 409:
                    print("[反馈bot] 409 冲突：另有程序在用同一 bot 轮询或设置了 webhook，30 秒后重试")
                    time.sleep(30)
                else:
                    print(f"[反馈bot] Telegram 返回错误: {e}")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 300)
            except requests.RequestException as e:
                print(f"[反馈bot] 网络错误: {e.__class__.__name__}，{backoff} 秒后重试")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    # --- 更新处理 ---

    def _authorized(self, chat_id, user_id) -> bool:
        if str(chat_id) not in self.chat_ids:
            return False
        return not self.allowed_users or str(user_id) in self.allowed_users

    def handle_update(self, update: Dict) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])

    def handle_callback(self, cq: Dict) -> None:
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        user_id = (cq.get("from") or {}).get("id")
        data = cq.get("data") or ""
        if not self._authorized(chat_id, user_id):
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text="没有权限")
            return

        def answer(text: str = ""):
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text=text[:200])

        def set_markup(markup: Dict):
            try:
                self.api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=markup)
            except TelegramError as e:
                if "not modified" not in e.description:
                    raise

        key = (str(chat_id), int(message_id or 0))
        if message_id and data.startswith(("fb:", "fv:")) and self.store.message_text(chat_id, message_id):
            self.handle_rich_callback(key, data, user_id, answer)
            return

        if data in ("fb:l", "fb:d"):
            code = data[-1]
            items = self.store.message_items(chat_id, message_id)
            if not items:
                answer("这条消息没有可反馈的条目")
                return
            selected = self.store.selected(chat_id, message_id, LABELS[code])
            set_markup(grid_keyboard([i["n"] for i in items], code, selected))
            answer("点编号标记" + ("感兴趣" if code == "l" else "不感兴趣") + "，再点取消")
        elif data.startswith("fv:"):
            _, code, n = data.split(":")
            items = self.store.message_items(chat_id, message_id)
            item = next((i for i in items if i["n"] == int(n)), None)
            if not item:
                answer("找不到这条")
                return
            label = LABELS[code]
            on = self.store.toggle(chat_id, message_id, item, label, user_id, self.now())
            icon = "👍" if label == "like" else "👎"
            answer(f"{icon} {item['title']}" if on else f"已取消：{item['title']}")
            set_markup(grid_keyboard([i["n"] for i in items], code, self.store.selected(chat_id, message_id, label)))
        elif data == "fb:back":
            set_markup(base_keyboard(self.store.counts(chat_id, message_id)))
            answer()
        elif data.startswith("sg:"):
            _, action, sid = data.split(":")
            if action == "a":
                text = self.apply_suggestion(int(sid))
            else:
                self.store.set_suggestion_status(int(sid), "skipped")
                text = "已忽略"
            set_markup({"inline_keyboard": []})
            answer(text)
        else:
            answer()

    def handle_rich_callback(self, key: Tuple[str, int], data: str, user_id, answer) -> None:
        """Rich Message 摘要：勾选只改草稿、不弹提示，提交时一次写入；重绘留到这批更新处理完"""
        chat_id, message_id = key
        if data in ("fb:l", "fb:d"):
            code = data[-1]
            self.drafts[key] = (code, self.store.selected(chat_id, message_id, LABELS[code]))
            answer()
        elif data.startswith("fv:"):
            _, code, n = data.split(":")
            draft = self.drafts.get(key)
            if not draft or draft[0] != code:
                # bot 重启后草稿丢失，从已提交的反馈接着勾
                draft = (code, self.store.selected(chat_id, message_id, LABELS[code]))
                self.drafts[key] = draft
            draft[1].symmetric_difference_update({int(n)})
            answer()
        elif data == "fb:ok":
            draft = self.drafts.pop(key, None)
            if not draft:
                answer()
            else:
                code, picked = draft
                items = self.store.message_items(chat_id, message_id)
                count = self.store.replace(chat_id, message_id, items, LABELS[code], picked, user_id, self.now())
                answer(f"已记录 {count} 条" + ("感兴趣" if code == "l" else "不感兴趣"))
        elif data == "fb:back":
            self.drafts.pop(key, None)
            answer()
        else:
            answer()
            return
        self.dirty.add(key)

    def handle_message(self, message: Dict) -> None:
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        match = re.match(r"^/(\w+)(?:@(\w+))?(?:\s|$)", text)
        if not match or not self._authorized(chat_id, user_id):
            return
        command, target = match.group(1).lower(), match.group(2)
        # 群里有多个 bot 时，带 @ 的命令只处理发给自己的
        if target and self.username and target.lower() != self.username.lower():
            return
        if command != "interests":
            return
        thread = message.get("message_thread_id")
        action = {"chat_id": chat_id, "action": "typing"}
        if thread:
            action["message_thread_id"] = thread
        try:
            self.api("sendChatAction", **action)
        except Exception:
            pass
        result = self.create_suggestion(manual=True, chat_id=chat_id, thread_id=thread)
        if isinstance(result, str):
            self.send(result, chat_id=chat_id, thread_id=thread)

    # --- 兴趣调整建议 ---

    def interests_path(self) -> Optional[Path]:
        from trendradar.ai.filter import AIFilter

        return AIFilter.resolve_interests_path(self.config.get("AI_FILTER", {}).get("INTERESTS_PATH"))

    def current_interests(self) -> str:
        path = self.interests_path()
        if path and path.exists():
            return path.read_text(encoding="utf-8").strip()
        default = Path(__file__).parent.parent / "config" / "ai_interests.txt"
        return default.read_text(encoding="utf-8").strip() if default.exists() else ""

    def maybe_weekly_suggestion(self) -> None:
        now = self.now()
        if now.isoweekday() != WEEKLY_WEEKDAY or now.hour < WEEKLY_HOUR:
            return
        last = self.store.last_suggestion_at()
        if last and last > (now - timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S"):
            return
        self.create_suggestion(manual=False)

    def create_suggestion(self, manual: bool, chat_id=None, thread_id=None):
        """生成建议并发到话题；返回建议 ID，或说明未生成原因的字符串"""
        now = self.now()
        since = self.store.last_suggestion_at()
        liked = self.store.recent("like", 50, since=since)
        disliked = self.store.recent("dislike", 50, since=since)
        if len(liked) + len(disliked) < MIN_FEEDBACK_FOR_SUGGESTION:
            msg = f"上次建议以来只有 {len(liked) + len(disliked)} 条反馈，至少 {MIN_FEEDBACK_FOR_SUGGESTION} 条才生成建议"
            if not manual:
                # 记一条占位，本周不再重复检查
                self.store.set_suggestion_status(self.store.add_suggestion(msg, "", now), "noop")
            return msg

        current = self.current_interests()
        try:
            proposal = self._ask_ai(current, liked, disliked)
        except Exception as e:
            print(f"[反馈bot] 生成兴趣建议失败: {type(e).__name__}: {e}")
            return "生成建议失败，稍后再试"
        new_text = (proposal.get("interests") or "").strip()
        summary = (proposal.get("summary") or "").strip()
        if not new_text or new_text == current:
            sid = self.store.add_suggestion(summary or "无需修改", new_text or current, now)
            self.store.set_suggestion_status(sid, "noop")
            return "根据近期反馈，兴趣描述无需调整"

        sid = self.store.add_suggestion(summary, new_text, now)
        diff = [
            f"{line[0]} {line[1:]}"
            for line in difflib.unified_diff(current.splitlines(), new_text.splitlines(), lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        ][:40]
        text = (
            f"<b>🧭 兴趣调整建议</b>\n依据 {len(liked)} 条感兴趣、{len(disliked)} 条不感兴趣的反馈\n\n"
            f"{html.escape(summary)}\n\n"
            "<blockquote expandable>" + html.escape("\n".join(diff) or "（仅格式调整）") + "</blockquote>"
        )
        self.send(text, chat_id=chat_id, thread_id=thread_id, reply_markup=suggestion_keyboard(sid))
        return sid

    def _ask_ai(self, current: str, liked: List[Dict], disliked: List[Dict]) -> Dict:
        from trendradar.ai.client import AIClient
        from trendradar.ai.prompt_loader import load_prompt_template

        system, user = load_prompt_template("ai_interests_update_prompt.txt", label="反馈bot")
        fmt = lambda items: "\n".join(f"- [{i['group'] or i['source']}] {i['title']}" for i in items) or "（无）"
        user = user.replace("{interests_content}", current).replace("{liked}", fmt(liked)).replace("{disliked}", fmt(disliked))
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        reply = AIClient(self.config.get("AI", {})).chat(messages)
        match = re.search(r"\{.*\}", reply or "", re.S)
        if not match:
            raise ValueError("AI 返回中没有 JSON")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            from json_repair import repair_json

            return repair_json(match.group(0), return_objects=True)

    def apply_suggestion(self, suggestion_id: int) -> str:
        suggestion = self.store.suggestion(suggestion_id)
        if not suggestion or suggestion["status"] != "pending":
            return "这条建议已处理过"
        path = self.interests_path()
        if not path:
            return "未配置 ai_filter.interests_path，无法写入"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup_dir = path.parent / "backups"
            backup_dir.mkdir(exist_ok=True)
            stamp = self.now().strftime("%Y%m%d-%H%M%S")
            (backup_dir / f"{path.stem}-{stamp}{path.suffix}").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(suggestion["content"].rstrip() + "\n", encoding="utf-8")
        os.replace(tmp, path)
        self.store.set_suggestion_status(suggestion_id, "applied")
        return "已应用，下一轮抓取按新兴趣重建标签"

    def send(self, text: str, chat_id=None, thread_id=None, reply_markup: Optional[Dict] = None) -> None:
        payload = {
            "chat_id": chat_id or sorted(self.chat_ids)[0],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        thread = thread_id or self.thread_id
        if thread:
            payload["message_thread_id"] = int(thread) if str(thread).isdigit() else thread
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.api("sendMessage", **payload)


def main() -> None:
    from trendradar.core.loader import load_config

    config = load_config()
    bot = FeedbackBot(config)
    if not bot.token or not bot.chat_ids:
        print("[反馈bot] 未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，退出")
        return
    bot.run_forever()


if __name__ == "__main__":
    main()
