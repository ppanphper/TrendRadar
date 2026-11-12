# coding=utf-8
"""
Telegram 摘要版式

按 AI 相关度挑出精选，其余按分组折叠进可展开引用块，按 Telegram 的字符上限装箱。
设计说明见 docs/design.md。
"""

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

# Telegram 正文上限 4096 字符（去掉标签、还原实体后按 UTF-16 计），留出余量
TELEGRAM_TEXT_LIMIT = 3800
# 当前消息剩余空间小于该值时不再切分填充，直接换下一条
MIN_FILL = 300

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

_MODE_LABELS = {
    "daily": ("📰", "摘要"),
    "current": ("📊", "当前热点"),
    "incremental": ("🆕", "新增"),
}


def visible_length(text: str) -> int:
    """Telegram 计数口径：去掉 HTML 标签、还原实体后的 UTF-16 长度"""
    plain = html.unescape(_TAG_RE.sub("", text))
    return len(plain.encode("utf-16-le")) // 2


def to_plain_text(text: str) -> str:
    """去掉 HTML 标签，用于实体解析失败时改发纯文本"""
    return html.unescape(_TAG_RE.sub("", text))


def normalize_title(title: str) -> str:
    """规范化标题：全角转半角、去空白、小写，用于同一新闻跨平台合并与去重"""
    return _SPACE_RE.sub("", unicodedata.normalize("NFKC", title or "")).lower()


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _attr(text: str) -> str:
    return html.escape(text or "", quote=True)


@dataclass
class DigestItem:
    title: str
    url: str
    sources: List[str]
    group: str
    rank: Optional[int] = None
    score: Optional[float] = None

    def sort_key(self) -> Tuple[float, int]:
        # 分数高的在前；没有分数时按排名
        return (-(self.score if self.score is not None else 0.0), self.rank if self.rank is not None else 9999)


@dataclass
class _Block:
    heading: str                      # 已转义的 HTML
    lines: List[str] = field(default_factory=list)
    folded: List[str] = field(default_factory=list)
    raw: Optional[str] = None         # 整段 HTML（AI 分析），不按行处理


def _best_rank(ranks) -> Optional[int]:
    values = [r for r in (ranks or []) if isinstance(r, int) and r > 0]
    return min(values) if values else None


def _score(data: Dict) -> Optional[float]:
    value = data.get("relevance_score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def collect_groups(report_data: Dict, rss_items: Optional[List[Dict]]) -> Dict[str, List[DigestItem]]:
    """合并热榜与 RSS 的分组；同一分组内同标题的条目合并来源"""
    groups: Dict[str, List[DigestItem]] = {}
    index: Dict[Tuple[str, str], DigestItem] = {}

    def add(stats: List[Dict]):
        for stat in stats or []:
            group = stat.get("word", "")
            for data in stat.get("titles", []):
                title = data.get("title", "")
                if not title:
                    continue
                key = (group, normalize_title(title))
                source = data.get("source_name", "")
                rank = _best_rank(data.get("ranks"))
                score = _score(data)
                existing = index.get(key)
                if existing:
                    if source and source not in existing.sources:
                        existing.sources.append(source)
                    if rank is not None and (existing.rank is None or rank < existing.rank):
                        existing.rank = rank
                    if score is not None and (existing.score is None or score > existing.score):
                        existing.score = score
                    continue
                event_sources = [x for x in (data.get("event_sources") or []) if x]
                item = DigestItem(
                    title=data.get("event_headline") or title,
                    url=data.get("mobile_url") or data.get("url", ""),
                    sources=event_sources or ([source] if source else []),
                    group=group,
                    rank=rank,
                    score=score,
                )
                index[key] = item
                groups.setdefault(group, []).append(item)

    add(report_data.get("stats", []))
    add(rss_items or [])
    return groups


def _number(n: Optional[int]) -> str:
    # 编号用等宽样式，便于对照反馈按钮，也不会与 AI 分析里的列表序号混淆
    return f"<code>{n}</code> " if n is not None else "• "


def _item_line(item: DigestItem, show_group: bool = False, n: Optional[int] = None) -> str:
    title = _esc(item.title)
    link = f'<a href="{_attr(item.url)}">{title}</a>' if item.url else title
    meta = " / ".join(item.sources[:3])
    if len(item.sources) > 3:
        meta += f" 等 {len(item.sources)} 家"
    if item.rank is not None:
        meta = f"{meta} #{item.rank}" if meta else f"#{item.rank}"
    if show_group and item.group:
        meta = f"{meta} · {item.group}" if meta else item.group
    return f"{_number(n)}{link} <i>{_esc(meta)}</i>" if meta else f"{_number(n)}{link}"


def _standalone_line(data: Dict, n: Optional[int] = None) -> str:
    title = _esc(data.get("title", ""))
    url = data.get("mobile_url") or data.get("url", "")
    link = f'<a href="{_attr(url)}">{title}</a>' if url else title
    rank = data.get("rank")
    return f"{_number(n)}{link} <i>#{rank}</i>" if isinstance(rank, int) and rank > 0 else f"{_number(n)}{link}"


_NUMBER_RE = re.compile(r"(?:^|\n|<blockquote expandable>)<code>(\d+)</code> ")
_QUOTE_RE = re.compile(r"<blockquote expandable>(.*?)</blockquote>", re.S)
_LINE_NUMBER_RE = re.compile(r"^<code>(\d+)</code> ")


def to_rich_html(text: str, picking: Optional[str] = None, selected: Optional[Set[int]] = None) -> str:
    """把摘要消息（Telegram HTML，\\n 换行）转成 Rich Message 的 HTML

    picking 为反馈代码（l / d）时进入勾选状态：每条编号前加勾选按钮，已勾选的显示 ✅；
    折叠引用块改为展开，免得每次重绘后都要重新展开。
    """
    selected = selected or set()

    def line(raw: str) -> str:
        m = _LINE_NUMBER_RE.match(raw)
        if not picking or not m:
            return raw
        n = int(m.group(1))
        mark = "✅" if n in selected else "☐"
        return f'<tg-button type="callback_data" data="fv:{picking}:{n}">{mark}</tg-button> {raw}'

    def paragraphs(segment: str) -> List[str]:
        out, cur = [], []
        for raw in segment.split("\n") + [""]:
            if raw.strip():
                cur.append(line(raw))
            elif cur:
                out.append("<p>" + "<br/>".join(cur) + "</p>")
                cur = []
        return out

    parts: List[str] = []
    pos = 0
    for m in _QUOTE_RE.finditer(text):
        parts.extend(paragraphs(text[pos:m.start()]))
        tag = "<blockquote>" if picking else "<blockquote expandable>"
        parts.append(tag + "<br/>".join(line(raw) for raw in m.group(1).strip("\n").split("\n")) + "</blockquote>")
        pos = m.end()
    parts.extend(paragraphs(text[pos:]))
    return "".join(parts)


@dataclass
class DigestMessage:
    text: str
    items: List[Dict] = field(default_factory=list)   # 本条消息中带编号的条目：n、title、url、source、group


def _render_block(block: _Block, lines: List[str], folded: List[str], heading_suffix: str = "") -> str:
    parts = [block.heading + heading_suffix] if block.heading else []
    parts.extend(lines)
    if folded:
        parts.append("<blockquote expandable>" + "\n".join(folded) + "</blockquote>")
    return "\n".join(parts)


def _split_block(block: _Block, first_limit: int, limit: int) -> List[str]:
    """把超长分组按行切成多段：第一段填满 first_limit，之后每段不超过 limit；每段补上分组标题和引用块标签"""
    if block.raw is not None:
        return _split_raw(block.raw, first_limit, limit)

    chunks: List[str] = []
    units = [(line, False) for line in block.lines] + [(line, True) for line in block.folded]
    cur_lines: List[str] = []
    cur_folded: List[str] = []
    for line, is_folded in units:
        trial_lines = cur_lines + ([] if is_folded else [line])
        trial_folded = cur_folded + ([line] if is_folded else [])
        suffix = "（续）" if chunks else ""
        cap = limit if chunks else first_limit
        if (cur_lines or cur_folded) and visible_length(_render_block(block, trial_lines, trial_folded, suffix)) > cap:
            chunks.append(_render_block(block, cur_lines, cur_folded, suffix))
            cur_lines, cur_folded = ([], [line]) if is_folded else ([line], [])
        else:
            cur_lines, cur_folded = trial_lines, trial_folded
    if cur_lines or cur_folded:
        chunks.append(_render_block(block, cur_lines, cur_folded, "（续）" if chunks else ""))
    return chunks


def _split_raw(raw: str, first_limit: int, limit: int) -> List[str]:
    """AI 分析等整段内容：按行切开，每段各自包进折叠引用块"""
    open_tag, close_tag = "<blockquote expandable>", "</blockquote>"
    inner = raw[len(open_tag):-len(close_tag)] if raw.startswith(open_tag) and raw.endswith(close_tag) else raw
    chunks, cur = [], []
    for line in inner.split("\n"):
        trial = cur + [line]
        cap = limit if chunks else first_limit
        if cur and visible_length("\n".join(trial)) > cap:
            chunks.append(open_tag + "\n".join(cur) + close_tag)
            cur = [line]
        else:
            cur = trial
    if cur:
        chunks.append(open_tag + "\n".join(cur) + close_tag)
    return chunks


def _fold(items: List, per_group: int, max_folded: int) -> Tuple[List, List, int]:
    """前 per_group 条直接显示，其余折叠；折叠部分超过 max_folded 时只留数量"""
    shown, folded = items[:per_group], items[per_group:]
    hidden = 0
    if max_folded >= 0 and len(folded) > max_folded:
        hidden = len(folded) - max_folded
        folded = folded[:max_folded]
    return shown, folded, hidden


def _hidden_line(hidden: int) -> List[str]:
    return [f"<i>…另有 {hidden} 条，见网页报告</i>"] if hidden else []


def build_telegram_digest(
    report_data: Dict,
    rss_items: Optional[List[Dict]] = None,
    standalone_data: Optional[Dict] = None,
    ai_content: Optional[str] = None,
    *,
    mode: str = "daily",
    now: Optional[datetime] = None,
    headline: Optional[str] = None,
    alert: bool = False,
    picks: int = 5,
    per_group: int = 3,
    max_folded: int = 10,
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> List[str]:
    """生成 Telegram 摘要消息（HTML），返回按上限切好的消息列表；无内容时返回空列表"""
    return [m.text for m in build_telegram_digest_messages(
        report_data, rss_items, standalone_data, ai_content,
        mode=mode, now=now, headline=headline, alert=alert,
        picks=picks, per_group=per_group, max_folded=max_folded, limit=limit,
    )]


def build_telegram_digest_messages(
    report_data: Dict,
    rss_items: Optional[List[Dict]] = None,
    standalone_data: Optional[Dict] = None,
    ai_content: Optional[str] = None,
    *,
    mode: str = "daily",
    now: Optional[datetime] = None,
    headline: Optional[str] = None,
    alert: bool = False,
    picks: int = 5,
    per_group: int = 3,
    max_folded: int = 10,
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> List[DigestMessage]:
    """同 build_telegram_digest，另外返回每条消息中带编号的条目，供反馈按钮使用"""
    now = now or datetime.now()
    groups = collect_groups(report_data, rss_items)
    all_items = [item for items in groups.values() for item in items]

    standalone_sections = []
    for section in (standalone_data or {}).get("platforms", []) + (standalone_data or {}).get("rss_feeds", []):
        items = [i for i in section.get("items", []) if i.get("title")]
        if items:
            standalone_sections.append((section.get("name") or section.get("id", ""), items))
    standalone_count = sum(len(items) for _, items in standalone_sections)

    if not all_items and not standalone_count and not ai_content:
        return []

    # 标题行
    icon, label = ("🔔", "重要提醒") if alert else _MODE_LABELS.get(mode, ("📰", "推送"))
    if headline and not alert:
        label = headline
    hot_count = sum(len(s.get("titles", [])) for s in report_data.get("stats", []))
    rss_count = sum(len(s.get("titles", [])) for s in (rss_items or []))
    counts = []
    if hot_count:
        counts.append(f"热榜 {hot_count}")
    if rss_count:
        counts.append(f"RSS {rss_count}")
    if standalone_count:
        counts.append(f"独立源 {standalone_count}")
    title_line = f"<b>{icon} {_esc(label)} · {now.strftime('%m-%d %H:%M')}</b>"
    header = title_line + ("\n" + " · ".join(counts) if counts else "")

    registry: Dict[int, Dict] = {}

    def number(title: str, url: str, sources: List[str], group: str) -> int:
        n = len(registry) + 1
        registry[n] = {"n": n, "title": title, "url": url, "source": " / ".join(sources[:3]), "group": group}
        return n

    def item_line(item: DigestItem, show_group: bool = False) -> str:
        return _item_line(item, show_group, number(item.title, item.url, item.sources, item.group))

    blocks: List[_Block] = []
    if alert:
        ordered = sorted(all_items, key=DigestItem.sort_key)
        blocks.append(_Block(heading="", lines=[item_line(i, show_group=True) for i in ordered]))
    else:
        picked = sorted(all_items, key=DigestItem.sort_key)[:picks] if len(all_items) > picks + per_group else []
        picked_ids = {id(i) for i in picked}
        if picked:
            blocks.append(_Block(heading="<b>⭐ 精选</b>", lines=[item_line(i, show_group=True) for i in picked]))
        for group, items in groups.items():
            rest = sorted((i for i in items if id(i) not in picked_ids), key=DigestItem.sort_key)
            if not rest:
                continue
            shown, folded, hidden = _fold(rest, per_group, max_folded)
            blocks.append(_Block(
                heading=f"<b>{_esc(group)}</b> · {len(rest)}",
                lines=[item_line(i) for i in shown],
                folded=[item_line(i) for i in folded] + _hidden_line(hidden),
            ))

    for name, items in standalone_sections:
        shown, folded, hidden = _fold(items, per_group, max_folded)

        def sa_line(data: Dict) -> str:
            url = data.get("mobile_url") or data.get("url", "")
            return _standalone_line(data, number(data.get("title", ""), url, [name], name))

        blocks.append(_Block(
            heading=f"<b>📌 {_esc(name)}</b> · {len(items)}",
            lines=[sa_line(i) for i in shown],
            folded=[sa_line(i) for i in folded] + _hidden_line(hidden),
        ))

    if ai_content and not alert:
        blocks.append(_Block(heading="", raw="<blockquote expandable>" + ai_content.strip() + "</blockquote>"))

    # 装箱：放不下的分组先切一段填满当前消息，剩余部分进下一条
    messages: List[str] = []
    current = header
    full_room = limit - visible_length(title_line) - 2
    for block in blocks:
        rendered = block.raw if block.raw is not None else _render_block(block, block.lines, block.folded)
        if visible_length(current + "\n\n" + rendered) <= limit:
            current += "\n\n" + rendered
            continue
        room = limit - visible_length(current) - 2
        if room < MIN_FILL:
            messages.append(current)
            current, room = title_line, full_room
            if visible_length(rendered) <= room:
                current += "\n\n" + rendered
                continue
        for j, piece in enumerate(_split_block(block, room, full_room)):
            if j > 0 or visible_length(current + "\n\n" + piece) > limit:
                messages.append(current)
                current = title_line
            current += "\n\n" + piece
    messages.append(current)

    if len(messages) > 1:
        total = len(messages)
        messages = [
            msg.replace(title_line, title_line[:-4] + f" · {i}/{total}</b>", 1)
            for i, msg in enumerate(messages, 1)
        ]
    return [
        DigestMessage(text=msg, items=[registry[int(n)] for n in _NUMBER_RE.findall(msg) if int(n) in registry])
        for msg in messages
    ]
