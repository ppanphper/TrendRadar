# coding=utf-8
"""
事件归并

不同平台对同一事件的报道合并为一条：字面相似度归并（不调用 AI）+ 定时摘要时的 AI 归并。
设计说明见 docs/design.md 第 6 节。
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)

LEXICAL_JACCARD = 0.5
# AI 归并的校验：组内每条与另一条的二元组重叠系数（交集 / 较短标题的二元组数）至少为该值，否则拆出。
# 2026-11-11 回放：没有共同实体的误合并为 0.00，正确的跨平台改写为 0.12~0.81
AI_MIN_OVERLAP = 0.1
CONTAIN_MIN_CHARS = 6
MAX_AI_ITEMS = 300


def _norm(title: str) -> str:
    return _NON_WORD.sub("", unicodedata.normalize("NFKC", title or "").lower())


def _grams(text: str) -> Set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def _sig(title: str) -> Tuple[str, Set[str]]:
    norm = _norm(title)
    return norm, _grams(norm) if norm else set()


def _similar_sig(a: Tuple[str, Set[str]], b: Tuple[str, Set[str]]) -> bool:
    na, ga = a
    nb, gb = b
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= CONTAIN_MIN_CHARS and short in long_:
        return True
    return len(ga & gb) / len(ga | gb) >= LEXICAL_JACCARD


def _overlap(a: Tuple[str, Set[str]], b: Tuple[str, Set[str]]) -> float:
    """重叠系数：长短标题差异大时不像 Jaccard 那样被稀释"""
    ga, gb = a[1], b[1]
    return len(ga & gb) / min(len(ga), len(gb)) if ga and gb else 0.0


def _english_tokens(title: str) -> Set[str]:
    return {t for t in re.findall(r"[a-z][a-z0-9]+", unicodedata.normalize("NFKC", title or "").lower()) if len(t) > 2}


def lexically_similar(a: str, b: str) -> bool:
    """两个标题是否字面上描述同一件事：二元组 Jaccard 足够高，或较短的标题被较长的完整包含"""
    return _similar_sig(_sig(a), _sig(b))


@dataclass
class _Candidate:
    kind: str            # "hot" / "rss"
    group_index: int
    title_index: int
    data: Dict
    cluster: int = -1

    @property
    def title(self) -> str:
        return self.data.get("title", "")

    def sort_key(self) -> Tuple[float, int]:
        score = self.data.get("relevance_score")
        ranks = [r for r in (self.data.get("ranks") or []) if isinstance(r, int) and r > 0]
        return (-(float(score) if score is not None else 0.0), min(ranks) if ranks else 9999)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class MergeResult:
    stats: List[Dict]
    rss_items: Optional[List[Dict]]
    headlines: List[str] = field(default_factory=list)   # 本次推送的事件标题，发送成功后记入 events 表
    merged: int = 0                                      # 被并入其他条目的数量
    seen_dropped: int = 0                                # 与已推送事件相同而移除的数量
    ai_used: bool = False
    ai_rejected: int = 0                                 # AI 分组中未通过字面校验而拆出的条目数
    rejected: List[Tuple[str, str]] = field(default_factory=list)  # (AI 标题, 被拆出的原标题)，用于调试
    clusters: List[Tuple[str, List[str]]] = field(default_factory=list)  # 多条合并的事件：(标题, ["来源｜原标题", ...])
    seen: List[str] = field(default_factory=list)                        # 因与已推送事件相同而移除的原标题


def parse_ai_events(text: str, count: int) -> Tuple[List[Tuple[List[int], str]], Set[int]]:
    """解析 AI 返回的 {"events": [{"ids": [...], "headline": "..."}], "seen": [...]}，编号从 1 开始"""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("AI 返回中没有 JSON 对象")
    raw = match.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        from json_repair import repair_json

        data = repair_json(raw, return_objects=True)
    if not isinstance(data, dict):
        raise ValueError("AI 返回的 JSON 不是对象")

    def valid_ids(values) -> List[int]:
        ids = []
        for v in values or []:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= count and i not in ids:
                ids.append(i)
        return ids

    events = []
    used: Set[int] = set()
    for event in data.get("events") or []:
        if not isinstance(event, dict):
            continue
        ids = [i for i in valid_ids(event.get("ids")) if i not in used]
        if len(ids) < 2:
            continue
        used.update(ids)
        events.append((ids, str(event.get("headline") or "").strip()[:60]))
    return events, set(valid_ids(data.get("seen")))


def merge_events(
    stats: List[Dict],
    rss_items: Optional[List[Dict]],
    *,
    recent_headlines: Sequence[str] = (),
    ai_chat: Optional[Callable[[List[Dict]], str]] = None,
    prompt: Tuple[str, str] = ("", ""),
) -> MergeResult:
    """
    归并同一事件的条目。

    - 与近期已推送事件字面相似的条目直接移除
    - 字面相似的条目合并；提供 ai_chat 时再让 AI 按事件归并并判断是否已推送过
    - 合并后保留相关度最高的条目，附加 event_headline / event_sources / event_size
    """
    candidates: List[_Candidate] = []
    for kind, groups in (("hot", stats or []), ("rss", rss_items or [])):
        for gi, group in enumerate(groups):
            for ti, data in enumerate(group.get("titles", [])):
                candidates.append(_Candidate(kind, gi, ti, data))

    result = MergeResult(stats=stats or [], rss_items=rss_items)
    if not candidates:
        return result

    sigs = [_sig(c.title) for c in candidates]

    # 1. 已推送过的事件（字面相似）
    removed: Set[int] = set()
    if recent_headlines:
        recent_sigs = [_sig(h) for h in recent_headlines]
        for idx in range(len(candidates)):
            if any(_similar_sig(sigs[idx], r) for r in recent_sigs):
                removed.add(idx)
        result.seen_dropped += len(removed)

    alive = [i for i in range(len(candidates)) if i not in removed]

    # 2. 字面归并
    uf = _UnionFind(len(candidates))
    for pos, i in enumerate(alive):
        for j in alive[pos + 1:]:
            if _similar_sig(sigs[i], sigs[j]):
                uf.union(i, j)

    def clusters() -> Dict[int, List[int]]:
        out: Dict[int, List[int]] = {}
        for i in alive:
            out.setdefault(uf.find(i), []).append(i)
        return out

    headlines: Dict[int, str] = {}

    # 3. AI 归并：每个字面簇取一个代表，按相关度取前 MAX_AI_ITEMS 个
    if ai_chat and len(alive) >= 2 and prompt[1]:
        reps = sorted(
            (min(members, key=lambda m: candidates[m].sort_key()) for members in clusters().values()),
            key=lambda m: candidates[m].sort_key(),
        )[:MAX_AI_ITEMS]
        news_list = "\n".join(
            f"{n}. [{candidates[m].data.get('source_name', '')}] {candidates[m].title}" for n, m in enumerate(reps, 1)
        )
        recent = "\n".join(f"- {h}" for h in list(recent_headlines)[-200:]) or "（无）"
        user = (
            prompt[1]
            .replace("{recent_events}", recent)
            .replace("{news_count}", str(len(reps)))
            .replace("{news_list}", news_list)
        )
        messages = ([{"role": "system", "content": prompt[0]}] if prompt[0] else []) + [{"role": "user", "content": user}]
        try:
            events, seen = parse_ai_events(ai_chat(messages), len(reps))
            for ids, headline in events:
                members = [reps[i - 1] for i in ids]
                # 校验：每条至少与组内另一条有字面重合或共同英文词，否则拆出
                checked = [
                    m for m in members
                    if any(
                        _overlap(sigs[m], sigs[o]) >= AI_MIN_OVERLAP
                        or _english_tokens(candidates[m].title) & _english_tokens(candidates[o].title)
                        for o in members if o != m
                    )
                ]
                result.ai_rejected += len(members) - len(checked)
                result.rejected.extend((headline, candidates[m].title) for m in members if m not in checked)
                if len(checked) < 2:
                    continue
                for m in checked[1:]:
                    uf.union(checked[0], m)
                if headline:
                    headlines[uf.find(checked[0])] = headline
            seen_roots = {uf.find(reps[i - 1]) for i in seen}
            for root, members in clusters().items():
                if root in seen_roots:
                    removed.update(members)
                    result.seen_dropped += len(members)
                    result.seen.extend(candidates[m].title for m in members)
            alive = [i for i in alive if i not in removed]
            result.ai_used = True
            # 合并后根节点可能变化，按最终根重新映射标题
            headlines = {uf.find(k): v for k, v in headlines.items()}
        except Exception as e:
            print(f"[事件归并] AI 归并失败，仅使用字面归并: {type(e).__name__}: {e}")

    # 4. 每个事件保留相关度最高的条目
    drop: Set[int] = set(removed)
    for root, members in clusters().items():
        members.sort(key=lambda m: candidates[m].sort_key())
        keep = candidates[members[0]]
        if len(members) > 1:
            sources: List[str] = []
            for m in members:
                source = candidates[m].data.get("source_name", "")
                if source and source not in sources:
                    sources.append(source)
            keep.data = dict(keep.data)
            keep.data["event_headline"] = headlines.get(root) or keep.title
            keep.data["event_sources"] = sources
            keep.data["event_size"] = len(members)
            drop.update(members[1:])
            result.merged += len(members) - 1
            result.clusters.append((
                keep.data["event_headline"],
                [f"{candidates[m].data.get('source_name', '')}｜{candidates[m].title}" for m in members],
            ))
        result.headlines.append(keep.data.get("event_headline") or keep.title)

    def rebuild(kind: str, groups: Optional[List[Dict]]) -> Optional[List[Dict]]:
        if groups is None:
            return None
        kept: Dict[Tuple[int, int], Dict] = {
            (c.group_index, c.title_index): c.data
            for idx, c in enumerate(candidates)
            if c.kind == kind and idx not in drop
        }
        out = []
        for gi, group in enumerate(groups):
            titles = [kept[(gi, ti)] for ti in range(len(group.get("titles", []))) if (gi, ti) in kept]
            if titles:
                out.append({**group, "titles": titles, "count": len(titles)})
        return out

    result.stats = rebuild("hot", stats) or []
    result.rss_items = rebuild("rss", rss_items)
    return result
