# coding=utf-8
"""
已推送记录：跨推送、跨天去重

推送前过滤掉已推送过的条目，发送成功后写入记录。设计说明见 docs/design.md。
"""

import copy
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from trendradar.notification.telegram_digest import normalize_title


def hotlist_key(title: str) -> str:
    return "h:" + normalize_title(title)


def rss_key(url: str, title: str) -> str:
    return "r:" + url.strip() if url and url.strip() else "r:" + normalize_title(title)


class PushLedger:
    """SQLite 记录已推送条目的键"""

    def __init__(self, db_path: str, retention_days: int = 7):
        self.db_path = Path(db_path)
        self.retention_days = max(1, int(retention_days or 7))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pushed ("
                " key TEXT PRIMARY KEY, title TEXT, pushed_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def pushed_keys(self, keys: Iterable[str]) -> Set[str]:
        keys = list(set(keys))
        found: Set[str] = set()
        with self._connect() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                rows = conn.execute(
                    f"SELECT key FROM pushed WHERE key IN ({','.join('?' * len(chunk))})", chunk
                ).fetchall()
                found.update(row[0] for row in rows)
        return found

    def record(self, entries: Dict[str, str], now: datetime) -> None:
        """写入本次推送的条目，并清理超出保留期的记录"""
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        cutoff = (now - timedelta(days=self.retention_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO pushed (key, title, pushed_at) VALUES (?, ?, ?)",
                [(key, title, stamp) for key, title in entries.items()],
            )
            conn.execute("DELETE FROM pushed WHERE pushed_at < ?", (cutoff,))


def _filter_stats(
    stats: Optional[List[Dict]],
    key_func,
    pushed: Set[str],
    seen: Dict[str, str],
    min_score: Optional[float],
) -> Optional[List[Dict]]:
    if stats is None:
        return None
    result = []
    for stat in stats:
        titles = []
        for data in stat.get("titles", []):
            if min_score is not None:
                score = data.get("relevance_score")
                if score is None or float(score) < min_score:
                    continue
            key = key_func(data)
            if key in pushed:
                continue
            # 同一次推送中不同分组出现的同一条目保留，只记录一次
            seen.setdefault(key, data.get("title", ""))
            titles.append(data)
        if titles:
            new_stat = dict(stat)
            new_stat["titles"] = titles
            new_stat["count"] = len(titles)
            result.append(new_stat)
    return result


def filter_push_content(
    stats: List[Dict],
    rss_items: Optional[List[Dict]],
    rss_new_items: Optional[List[Dict]],
    new_titles: Optional[Dict],
    standalone_data: Optional[Dict],
    *,
    ledger: Optional[PushLedger] = None,
    min_score: Optional[float] = None,
) -> Tuple[List[Dict], Optional[List[Dict]], Optional[List[Dict]], Optional[Dict], Optional[Dict], Dict[str, str]]:
    """
    过滤推送内容，返回过滤后的各区域数据和待记录的键（键 -> 标题）

    - ledger：移除已推送过的条目
    - min_score：只保留 AI 相关度不低于该值的条目，独立展示区和新增区（无分数）整体移除
    """
    hot_key = lambda d: hotlist_key(d.get("title", ""))
    feed_key = lambda d: rss_key(d.get("url", ""), d.get("title", ""))

    candidates: List[str] = []
    for stat in stats or []:
        candidates.extend(hot_key(d) for d in stat.get("titles", []))
    for group in (rss_items or []) + (rss_new_items or []):
        candidates.extend(feed_key(d) for d in group.get("titles", []))
    for titles in (new_titles or {}).values():
        candidates.extend(hotlist_key(t) for t in titles)
    for section in (standalone_data or {}).get("platforms", []):
        candidates.extend(hot_key(i) for i in section.get("items", []))
    for section in (standalone_data or {}).get("rss_feeds", []):
        candidates.extend(feed_key(i) for i in section.get("items", []))

    pushed = ledger.pushed_keys(candidates) if ledger else set()
    to_record: Dict[str, str] = {}

    stats = _filter_stats(stats, hot_key, pushed, to_record, min_score) or []
    rss_items = _filter_stats(rss_items, feed_key, pushed, to_record, min_score)
    rss_new_items = _filter_stats(rss_new_items, feed_key, pushed, to_record, min_score)

    if min_score is not None:
        new_titles = {}
        standalone_data = None
    else:
        if new_titles:
            new_titles = {
                source_id: {t: d for t, d in titles.items() if hotlist_key(t) not in pushed}
                for source_id, titles in new_titles.items()
            }
            new_titles = {k: v for k, v in new_titles.items() if v}
        if standalone_data:
            standalone_data = copy.copy(standalone_data)
            for kind, key_func in (("platforms", hot_key), ("rss_feeds", feed_key)):
                sections = []
                for section in standalone_data.get(kind, []):
                    items = []
                    for item in section.get("items", []):
                        key = key_func(item)
                        if key in pushed:
                            continue
                        to_record.setdefault(key, item.get("title", ""))
                        items.append(item)
                    if items:
                        sections.append({**section, "items": items})
                standalone_data[kind] = sections
            if not standalone_data.get("platforms") and not standalone_data.get("rss_feeds"):
                standalone_data = None

    return stats, rss_items, rss_new_items, new_titles, standalone_data, to_record
