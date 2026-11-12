# coding=utf-8
"""
推送反馈：消息与条目的映射、感兴趣 / 不感兴趣记录、兴趣调整建议

主程序发送摘要时写入消息映射，trendradar.bot 处理按钮回调时读写反馈。
设计说明见 docs/design.md 第 7 节。
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

LABELS = {"l": "like", "d": "dislike"}
LABEL_TEXT = {"like": "👍 感兴趣", "dislike": "👎 不感兴趣"}
GRID_COLUMNS = 8
GRID_MAX = 96


def feedback_db_path(data_dir: str) -> Path:
    return Path(data_dir) / "meta" / "feedback.db"


def base_keyboard(counts: Optional[Dict[str, int]] = None) -> Dict:
    """消息底部的 👍 / 👎；已标记过的显示条数并变色，便于看出这条消息处理过"""
    counts = counts or {}
    row = []
    for code, style in (("l", "success"), ("d", "danger")):
        label = LABELS[code]
        button = {"text": LABEL_TEXT[label], "callback_data": f"fb:{code}"}
        if counts.get(label):
            button["text"] += f" · {counts[label]}"
            button["style"] = style
        row.append(button)
    return {"inline_keyboard": [row]}


def picking_keyboard(code: str, count: int) -> Dict:
    """正文勾选状态的底部按钮：勾选只是草稿，提交后才记录"""
    icon = LABEL_TEXT[LABELS[code]].split()[0]
    return {"inline_keyboard": [[
        {"text": "取消", "callback_data": "fb:back"},
        {"text": f"提交 {icon} {count} 条", "callback_data": "fb:ok"},
    ]]}


def grid_keyboard(numbers: List[int], code: str, selected: Set[int]) -> Dict:
    """编号网格：点编号记录反馈，已选的带 ✓；最后一行返回"""
    rows, row = [], []
    for n in numbers[:GRID_MAX]:
        row.append({"text": f"{n}✓" if n in selected else str(n), "callback_data": f"fv:{code}:{n}"})
        if len(row) == GRID_COLUMNS:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": f"‹ 返回（{LABEL_TEXT[LABELS[code]]}）", "callback_data": "fb:back"}])
    return {"inline_keyboard": rows}


def suggestion_keyboard(suggestion_id: int) -> Dict:
    return {"inline_keyboard": [[
        {"text": "✅ 应用", "callback_data": f"sg:a:{suggestion_id}"},
        {"text": "忽略", "callback_data": f"sg:s:{suggestion_id}"},
    ]]}


class FeedbackStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, sent_at TEXT NOT NULL,
                    items TEXT NOT NULL, PRIMARY KEY (chat_id, message_id));
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, n INTEGER NOT NULL,
                    label TEXT NOT NULL, title TEXT, url TEXT, source TEXT, grp TEXT,
                    user_id TEXT, created_at TEXT NOT NULL,
                    UNIQUE (chat_id, message_id, n, label));
                CREATE TABLE IF NOT EXISTS suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    status TEXT NOT NULL, summary TEXT, content TEXT NOT NULL);
                """
            )
            # text：以 Rich Message 发出的摘要原文，回调时据此重绘；为空表示普通消息，沿用编号网格
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            if "text" not in columns:
                try:
                    conn.execute("ALTER TABLE messages ADD COLUMN text TEXT")
                except sqlite3.OperationalError as e:
                    # 主程序和 bot 同时初始化时，另一方可能已经加上
                    if "duplicate column" not in str(e):
                        raise

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    # --- 消息映射 ---

    def save_message(self, chat_id, message_id: int, items: List[Dict], now: datetime, text: Optional[str] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages (chat_id, message_id, sent_at, items, text) VALUES (?, ?, ?, ?, ?)",
                (str(chat_id), int(message_id), now.strftime("%Y-%m-%d %H:%M:%S"),
                 json.dumps(items, ensure_ascii=False), text),
            )
            cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("DELETE FROM messages WHERE sent_at < ?", (cutoff,))

    def message_items(self, chat_id, message_id: int) -> List[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT items FROM messages WHERE chat_id = ? AND message_id = ?", (str(chat_id), int(message_id))
            ).fetchone()
        return json.loads(row[0]) if row else []

    def message_text(self, chat_id, message_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT text FROM messages WHERE chat_id = ? AND message_id = ?", (str(chat_id), int(message_id))
            ).fetchone()
        return row[0] if row else None

    # --- 反馈 ---

    def toggle(self, chat_id, message_id: int, item: Dict, label: str, user_id, now: datetime) -> bool:
        """记录反馈；同一条目同一类反馈再点一次即取消。返回操作后是否处于已选状态"""
        key = (str(chat_id), int(message_id), int(item["n"]), label)
        with self._connect() as conn:
            deleted = conn.execute(
                "DELETE FROM feedback WHERE chat_id = ? AND message_id = ? AND n = ? AND label = ?", key
            ).rowcount
            if deleted:
                return False
            opposite = "dislike" if label == "like" else "like"
            conn.execute(
                "DELETE FROM feedback WHERE chat_id = ? AND message_id = ? AND n = ? AND label = ?", key[:3] + (opposite,)
            )
            conn.execute(
                "INSERT INTO feedback (chat_id, message_id, n, label, title, url, source, grp, user_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                key + (item.get("title", ""), item.get("url", ""), item.get("source", ""), item.get("group", ""),
                       str(user_id or ""), now.strftime("%Y-%m-%d %H:%M:%S")),
            )
        return True

    def selected(self, chat_id, message_id: int, label: str) -> Set[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT n FROM feedback WHERE chat_id = ? AND message_id = ? AND label = ?",
                (str(chat_id), int(message_id), label),
            ).fetchall()
        return {row[0] for row in rows}

    def replace(self, chat_id, message_id: int, items: List[Dict], label: str, selected: Set[int], user_id,
                now: datetime) -> int:
        """批量提交：本条消息的 label 反馈改成 selected 这些条目，选中的条目去掉相反反馈；返回记录条数"""
        by_n = {int(i["n"]): i for i in items}
        selected = {n for n in selected if n in by_n}
        key = (str(chat_id), int(message_id))
        opposite = "dislike" if label == "like" else "like"
        with self._connect() as conn:
            existing = {row[0] for row in conn.execute(
                "SELECT n FROM feedback WHERE chat_id = ? AND message_id = ? AND label = ?", key + (label,)
            )}
            for n in existing - selected:
                conn.execute("DELETE FROM feedback WHERE chat_id = ? AND message_id = ? AND n = ? AND label = ?",
                             key + (n, label))
            for n in sorted(selected - existing):
                item = by_n[n]
                conn.execute("DELETE FROM feedback WHERE chat_id = ? AND message_id = ? AND n = ? AND label = ?",
                             key + (n, opposite))
                conn.execute(
                    "INSERT INTO feedback (chat_id, message_id, n, label, title, url, source, grp, user_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    key + (n, label, item.get("title", ""), item.get("url", ""), item.get("source", ""),
                           item.get("group", ""), str(user_id or ""), now.strftime("%Y-%m-%d %H:%M:%S")),
                )
        return len(selected)

    def counts(self, chat_id, message_id: int) -> Dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT label, COUNT(*) FROM feedback WHERE chat_id = ? AND message_id = ? GROUP BY label",
                (str(chat_id), int(message_id)),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def recent(self, label: str, limit: int = 20, since: Optional[str] = None) -> List[Dict]:
        sql = "SELECT title, source, grp, created_at FROM feedback WHERE label = ?"
        args: list = [label]
        if since:
            sql += " AND created_at > ?"
            args.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [{"title": r[0], "source": r[1], "group": r[2], "created_at": r[3]} for r in rows]

    def prompt_section(self, limit: int = 20) -> str:
        """附在分类用户偏好后面的反馈示例；没有反馈时返回空串"""
        liked, disliked = self.recent("like", limit), self.recent("dislike", limit)
        if not liked and not disliked:
            return ""
        lines = ["", "## 用户反馈（最近明确表态的新闻，用于校准相关度）"]
        if liked:
            lines.append("感兴趣，同类新闻应给更高分：")
            lines.extend(f"- {f['title']}" for f in liked)
        if disliked:
            lines.append("不感兴趣，同类新闻应降低分数或不归类：")
            lines.extend(f"- {f['title']}" for f in disliked)
        return "\n".join(lines)

    # --- 兴趣调整建议 ---

    def add_suggestion(self, summary: str, content: str, now: datetime) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO suggestions (created_at, status, summary, content) VALUES (?, 'pending', ?, ?)",
                (now.strftime("%Y-%m-%d %H:%M:%S"), summary, content),
            )
            return int(cur.lastrowid)

    def suggestion(self, suggestion_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, created_at, status, summary, content FROM suggestions WHERE id = ?", (int(suggestion_id),)
            ).fetchone()
        return dict(zip(("id", "created_at", "status", "summary", "content"), row)) if row else None

    def set_suggestion_status(self, suggestion_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE suggestions SET status = ? WHERE id = ?", (status, int(suggestion_id)))

    def last_suggestion_at(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(created_at) FROM suggestions").fetchone()
        return row[0] if row else None
