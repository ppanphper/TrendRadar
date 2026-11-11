# coding=utf-8
"""
RSS 快照

把开启 archive 的源的条目全文（RSS 自带的 HTML）存成本地网页，防止原帖下架后看不到。
同一条目只保存首次抓到的版本。设计说明见 docs/design.md。
"""

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

_DANGEROUS_TAGS = re.compile(r"<(script|style|iframe|object|embed|form)\b.*?</\1\s*>", re.I | re.S)
_SELF_CLOSING_DANGEROUS = re.compile(r"<(script|iframe|object|embed|link|meta)\b[^>]*/?>", re.I)
_EVENT_ATTRS = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_URLS = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I)
_IMG_SRC = re.compile(r"(<img\b[^>]*?\ssrc\s*=\s*)([\"'])(https?://[^\"']+)\2", re.I)
_SRCSET = re.compile(r"\s+srcset\s*=\s*(\"[^\"]*\"|'[^']*')", re.I)

MAX_IMAGES_PER_PAGE = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title}</title>
<style>
body {{ max-width: 760px; margin: 2rem auto; padding: 0 1rem; font: 16px/1.7 -apple-system, "PingFang SC", sans-serif; color: #1f2328; }}
header {{ border-bottom: 1px solid #d0d7de; margin-bottom: 1.5rem; padding-bottom: .75rem; }}
h1 {{ font-size: 1.5rem; margin: 0 0 .5rem; }}
.meta {{ color: #59636e; font-size: .875rem; }}
img {{ max-width: 100%; height: auto; }}
pre {{ overflow-x: auto; background: #f6f8fa; padding: .75rem; }}
blockquote {{ border-left: 3px solid #d0d7de; margin-left: 0; padding-left: 1rem; color: #59636e; }}
</style>
</head>
<body>
<header>
<h1>{title}</h1>
<div class="meta">{meta}</div>
<div class="meta"><a href="{url}">原文</a> · 快照于 {archived_at}</div>
</header>
<main>
{content}
</main>
</body>
</html>
"""

_INDEX = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RSS 快照</title>
<style>
body {{ max-width: 860px; margin: 2rem auto; padding: 0 1rem; font: 15px/1.6 -apple-system, "PingFang SC", sans-serif; color: #1f2328; }}
li {{ margin: .35rem 0; }}
.meta {{ color: #59636e; font-size: .8125rem; }}
</style>
</head>
<body>
<h1>RSS 快照 · {count} 篇</h1>
<ul>
{items}
</ul>
</body>
</html>
"""


def local_time(published_at: Optional[str], now: datetime) -> str:
    """把发布时间换算到 now 的时区，返回 YYYY-MM-DD HH:MM；无时区信息的按 UTC 处理（feedparser 的约定）"""
    if not published_at:
        return ""
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return published_at[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is not None:
        dt = dt.astimezone(now.tzinfo)
    return dt.strftime("%Y-%m-%d %H:%M")


def sanitize_html(content: str) -> str:
    """去掉脚本、内嵌框架和事件属性；快照通过本地网页服务打开，不执行来源页面的脚本"""
    content = _DANGEROUS_TAGS.sub("", content or "")
    content = _SELF_CLOSING_DANGEROUS.sub("", content)
    content = _EVENT_ATTRS.sub("", content)
    return _JS_URLS.sub(r'\1="#"', content)


def localize_images(content: str, files_dir: Path, rel_prefix: str, fetch_bytes: Callable[[str], Optional[bytes]]) -> str:
    """下载正文中的图片到 files_dir 并改写为相对路径；失败的保留远程地址"""
    content = _SRCSET.sub("", content)  # 去掉响应式图片候选，浏览器只用本地 src
    saved: Dict[str, str] = {}

    def replace(match):
        url = match.group(3)
        if url not in saved:
            if len(saved) >= MAX_IMAGES_PER_PAGE:
                return match.group(0)
            data = fetch_bytes(url)
            if not data or len(data) > MAX_IMAGE_BYTES:
                saved[url] = ""
            else:
                ext = Path(urlparse(url).path).suffix.lower()
                name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] + (ext if ext in _IMAGE_EXTS else ".img")
                files_dir.mkdir(parents=True, exist_ok=True)
                (files_dir / name).write_bytes(data)
                saved[url] = f"{rel_prefix}/{name}"
        local = saved[url]
        return f"{match.group(1)}{match.group(2)}{local}{match.group(2)}" if local else match.group(0)

    return _IMG_SRC.sub(replace, content)


class RSSArchiver:
    """把条目全文写入 <archive_dir>/<feed_id>/<YYYY-MM>/<hash>.html，并维护索引页"""

    def __init__(self, archive_dir: str, fetch_bytes: Optional[Callable[[str], Optional[bytes]]] = None):
        self.root = Path(archive_dir)
        self.manifest_path = self.root / "index.json"
        self.fetch_bytes = fetch_bytes  # 下载图片；None 时保留远程地址

    def _load_manifest(self) -> List[Dict]:
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []

    def save(self, feed_id: str, feed_name: str, items, now: datetime) -> int:
        """保存尚未存档的条目，返回新增快照数量"""
        manifest = self._load_manifest()
        known = {entry["path"] for entry in manifest}
        added = 0
        for item in items:
            content = getattr(item, "content_html", None)
            if not content:
                continue
            identity = item.guid or item.url or item.title
            digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
            published = local_time(item.published_at, now)
            month = (published or now.strftime("%Y-%m"))[:7]
            rel_path = f"{feed_id}/{month}/{digest}.html"
            if rel_path in known or (self.root / rel_path).exists():
                continue

            meta = " · ".join(p for p in [
                html.escape(feed_name),
                html.escape(item.author or ""),
                html.escape(published),
            ] if p)
            target = self.root / rel_path
            content = sanitize_html(content)
            if self.fetch_bytes:
                content = localize_images(content, target.parent / f"{digest}_files", f"{digest}_files", self.fetch_bytes)
            page = _PAGE.format(
                title=html.escape(item.title),
                meta=meta,
                url=html.escape(item.url, quote=True),
                archived_at=now.strftime("%Y-%m-%d %H:%M"),
                content=content,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(page, encoding="utf-8")
            manifest.append({
                "feed_id": feed_id,
                "feed_name": feed_name,
                "title": item.title,
                "url": item.url,
                "published_at": published,
                "archived_at": now.strftime("%Y-%m-%d %H:%M"),
                "path": rel_path,
            })
            known.add(rel_path)
            added += 1

        if added:
            self.root.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
            self._write_index(manifest)
        return added

    def _write_index(self, manifest: List[Dict]) -> None:
        entries = sorted(manifest, key=lambda e: e.get("published_at") or e.get("archived_at", ""), reverse=True)
        lines = []
        for e in entries:
            when = e.get("published_at") or e.get("archived_at", "")
            lines.append(
                f'<li><a href="{html.escape(e["path"], quote=True)}">{html.escape(e["title"])}</a> '
                f'<span class="meta">{html.escape(e["feed_name"])} · {html.escape(when)} · '
                f'<a href="{html.escape(e["url"], quote=True)}">原文</a></span></li>'
            )
        (self.root / "index.html").write_text(
            _INDEX.format(count=len(entries), items="\n".join(lines)), encoding="utf-8"
        )


def archive_items(
    archive_dir: Optional[str], feed, parsed_items, now: datetime,
    fetch_bytes: Optional[Callable[[str], Optional[bytes]]] = None,
) -> None:
    """抓取器调用的入口：失败只打印日志，不影响抓取结果"""
    if not archive_dir or not getattr(feed, "archive", False):
        return
    try:
        added = RSSArchiver(archive_dir, fetch_bytes).save(feed.id, feed.name, parsed_items, now)
        if added:
            print(f"[RSS] {feed.name}: 新增 {added} 篇快照")
    except Exception as e:
        print(f"[RSS] {feed.name}: 保存快照失败: {e}")
