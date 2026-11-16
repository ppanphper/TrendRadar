# coding=utf-8
"""
翻译降本

- already_in_language：判断标题是否已是目标语言（目前只识别中文），是则不送 AI。
- TranslationCache：按目标语言 + 原文缓存译文，同一标题跨轮次只翻译一次；
  按最近使用时间保留 N 天，命中时刷新，长期在榜的标题不会过期。
"""

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable

_CHINESE_TARGETS = {"中文", "简体中文", "汉语", "chinese", "simplified chinese", "zh", "zh-cn", "zh-hans"}

_HAN_RE = re.compile(r"[㐀-䶿一-鿿]")
# 假名（含半角片假名）、谚文：含这些字符说明是日文或韩文，即使夹有汉字也要翻译
_KANA_HANGUL_RE = re.compile(r"[぀-ヿｦ-ﾟᄀ-ᇿ가-힯]")
_LATIN_RE = re.compile(r"[A-Za-zÀ-ɏ]")


def already_in_language(text: str, language: str) -> bool:
    """标题已是目标语言时返回 True；无法判断的目标语言一律返回 False（照常翻译）

    中文：含汉字、不含假名和谚文，且汉字数不少于拉丁字母数。宁可多送：夹带较多英文的中文标题照常翻译，
    避免把夹了中文人名的英文标题当成中文。不含假名的日文（如「東京株式市場 大幅続落」）、
    目标为简体时的繁体标题无法区分，会被当作中文跳过。
    """
    if (language or "").strip().lower() not in _CHINESE_TARGETS:
        return False
    han = len(_HAN_RE.findall(text or ""))
    if not han or _KANA_HANGUL_RE.search(text):
        return False
    return han >= len(_LATIN_RE.findall(text))


def translation_cache_path(data_dir: str) -> Path:
    return Path(data_dir) / "meta" / "translation_cache.db"


class TranslationCache:
    """译文缓存（SQLite）。打开时清理超过 retention_days 未使用的条目。"""

    def __init__(self, path, retention_days: int, now: Callable[[], datetime] = datetime.now):
        self.path = Path(path)
        self.retention_days = retention_days
        self.now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS translations ("
                " key TEXT PRIMARY KEY, translated TEXT NOT NULL, used_at TEXT NOT NULL)"
            )
            cutoff = (self.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("DELETE FROM translations WHERE used_at < ?", (cutoff,))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _key(language: str, text: str) -> str:
        return hashlib.sha256(f"{language}\n{text}".encode("utf-8")).hexdigest()

    def _stamp(self) -> str:
        return self.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_many(self, language: str, texts: Iterable[str]) -> Dict[str, str]:
        """返回命中的 {原文: 译文}，并刷新命中条目的使用时间"""
        keys = {self._key(language, t): t for t in texts}
        if not keys:
            return {}
        found: Dict[str, str] = {}
        with self._connect() as conn:
            items = list(keys.items())
            for i in range(0, len(items), 500):
                chunk = dict(items[i:i + 500])
                marks = ",".join("?" * len(chunk))
                for key, translated in conn.execute(
                    f"SELECT key, translated FROM translations WHERE key IN ({marks})", list(chunk)
                ):
                    found[chunk[key]] = translated
            if found:
                stamp = self._stamp()
                conn.executemany(
                    "UPDATE translations SET used_at = ? WHERE key = ?",
                    [(stamp, self._key(language, t)) for t in found],
                )
        return found

    def put_many(self, language: str, pairs: Dict[str, str]) -> None:
        if not pairs:
            return
        stamp = self._stamp()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO translations (key, translated, used_at) VALUES (?, ?, ?)",
                [(self._key(language, src), dst, stamp) for src, dst in pairs.items()],
            )
