# coding=utf-8
"""
RSS 抓取器

负责从配置的 RSS 源抓取数据并转换为标准格式
"""

import time
import random
from dataclasses import dataclass
from typing import List, Dict, Optional, Set, Tuple
from urllib.parse import urlparse

import requests

from .parser import RSSParser
from trendradar.storage.base import RSSItem, RSSData
from trendradar.utils.time import get_configured_time, is_within_days, DEFAULT_TIMEZONE


ACCEPT_HEADERS = {
    "Accept": "application/feed+json, application/json, application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class _CurlResponse:
    """把 curl_cffi 响应适配为 fetch_feed 用到的 requests 接口"""

    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self.text = response.text

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} Error for url: {self._response.url}")
            error.response = self
            raise error


@dataclass
class RSSFeedConfig:
    """RSS 源配置"""
    id: str                     # 源 ID
    name: str                   # 显示名称
    url: str                    # RSS URL
    max_items: int = 0          # 最大条目数（0=不限制）
    enabled: bool = True        # 是否启用
    max_age_days: Optional[int] = None  # 文章最大年龄（天），覆盖全局设置；None=使用全局，0=禁用过滤
    use_proxy: Optional[bool] = None    # 是否走代理，覆盖全局 advanced.rss.use_proxy；None=使用全局
    impersonate: str = ""               # 模拟浏览器 TLS 指纹（如 "chrome"），用于 Cloudflare 拦截 Python 客户端的站点；需要 curl_cffi


def feed_config_from_dict(feed_config: Dict, default_max_items: int = 0) -> RSSFeedConfig:
    """从 config.yaml 的单个 feed 字典构建 RSSFeedConfig（校验 max_age_days、use_proxy）"""
    feed_id = feed_config.get("id", "unknown")

    max_age_days_raw = feed_config.get("max_age_days")
    max_age_days = None
    if max_age_days_raw is not None:
        try:
            max_age_days = int(max_age_days_raw)
            if max_age_days < 0:
                print(f"[警告] RSS feed '{feed_id}' 的 max_age_days 为负数，将使用全局默认值")
                max_age_days = None
        except (ValueError, TypeError):
            print(f"[警告] RSS feed '{feed_id}' 的 max_age_days 格式错误：{max_age_days_raw}")
            max_age_days = None

    use_proxy_raw = feed_config.get("use_proxy")
    use_proxy = use_proxy_raw if isinstance(use_proxy_raw, bool) else None
    if use_proxy_raw is not None and use_proxy is None:
        print(f"[警告] RSS feed '{feed_id}' 的 use_proxy 应为 true/false：{use_proxy_raw}，将使用全局设置")

    return RSSFeedConfig(
        id=feed_config.get("id", ""),
        name=feed_config.get("name", ""),
        url=feed_config.get("url", ""),
        max_items=feed_config.get("max_items", default_max_items),
        enabled=feed_config.get("enabled", True),
        max_age_days=max_age_days,  # None=使用全局，0=禁用，>0=覆盖
        use_proxy=use_proxy,
        impersonate=str(feed_config.get("impersonate") or "").strip(),
    )


class _HostRateLimited(Exception):
    """同一站点本轮已返回 429，跳过该站点剩余的源"""


class RSSFetcher:
    """RSS 抓取器"""

    def __init__(
        self,
        feeds: List[RSSFeedConfig],
        request_interval: int = 2000,
        timeout: int = 15,
        use_proxy: bool = False,
        proxy_url: str = "",
        timezone: str = DEFAULT_TIMEZONE,
        freshness_enabled: bool = True,
        default_max_age_days: int = 3,
    ):
        """
        初始化抓取器

        Args:
            feeds: RSS 源配置列表
            request_interval: 请求间隔（毫秒）
            timeout: 请求超时（秒）
            use_proxy: 是否使用代理
            proxy_url: 代理 URL
            timezone: 时区配置（如 'Asia/Shanghai'）
            freshness_enabled: 是否启用新鲜度过滤
            default_max_age_days: 默认最大文章年龄（天）
        """
        self.feeds = [f for f in feeds if f.enabled]
        self.request_interval = request_interval
        self.timeout = timeout
        self.use_proxy = use_proxy
        self.proxy_url = proxy_url
        self.timezone = timezone
        self.freshness_enabled = freshness_enabled
        self.default_max_age_days = default_max_age_days

        self.parser = RSSParser()
        self.session = self._create_session()
        self._impersonate_session = None
        self._rate_limited_hosts: Set[str] = set()

    def _create_session(self) -> requests.Session:
        """创建请求会话"""
        session = requests.Session()
        session.headers.update({
            "User-Agent": "TrendRadar/2.0 RSS Reader (https://github.com/trendradar)",
            **ACCEPT_HEADERS,
        })

        if self.use_proxy and self.proxy_url:
            session.proxies = {
                "http": self.proxy_url,
                "https": self.proxy_url,
            }

        return session

    def _feed_proxies(self, feed: RSSFeedConfig) -> Optional[Dict[str, str]]:
        """单个源的代理设置；None 表示沿用会话默认值"""
        if feed.use_proxy is None:
            return None
        if feed.use_proxy:
            return {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        # 显式关闭：值为 None 的键会从会话代理中移除
        return {"http": None, "https": None}

    def _get_impersonate_session(self):
        """延迟创建 curl_cffi 会话，未安装时给出明确错误"""
        if self._impersonate_session is None:
            try:
                from curl_cffi import requests as curl_requests
            except ImportError as e:
                raise RuntimeError("impersonate 需要安装 curl_cffi") from e
            self._impersonate_session = curl_requests.Session()
        return self._impersonate_session

    def _request(self, feed: RSSFeedConfig):
        """发起请求并返回响应；HTTP 错误统一抛出 requests.HTTPError 以复用重试判断"""
        proxies = self._feed_proxies(feed)
        if not feed.impersonate:
            kwargs = {"timeout": self.timeout}
            if proxies is not None:
                kwargs["proxies"] = proxies
            return self.session.get(feed.url, **kwargs)

        use_proxy = self.use_proxy if feed.use_proxy is None else feed.use_proxy
        session = self._get_impersonate_session()
        try:
            response = session.get(
                feed.url,
                impersonate=feed.impersonate,
                headers=ACCEPT_HEADERS,  # 不覆盖 User-Agent，保持与 TLS 指纹一致
                proxies={"http": self.proxy_url, "https": self.proxy_url} if use_proxy and self.proxy_url else None,
                timeout=self.timeout,
            )
        except Exception as e:
            name = type(e).__name__
            if "Timeout" in name:
                raise requests.Timeout(str(e)) from e
            raise requests.ConnectionError(f"{name}: {e}") from e
        return _CurlResponse(response)

    def fetch_feed(self, feed: RSSFeedConfig) -> Tuple[List[RSSItem], Optional[str]]:
        """
        抓取单个 RSS 源

        Args:
            feed: RSS 源配置

        Returns:
            (条目列表, 错误信息) 元组
        """
        try:
            # 瞬时请求故障只额外尝试一次，不增加等待。
            host = urlparse(feed.url).hostname or ""
            if host in self._rate_limited_hosts:
                raise _HostRateLimited(host)

            for attempt in range(2):
                try:
                    response = self._request(feed)
                    response.raise_for_status()
                    break
                except requests.RequestException as e:
                    error_response = e.response
                    retryable = isinstance(e, (requests.Timeout, requests.ConnectionError)) and not isinstance(
                        e, requests.exceptions.SSLError
                    )
                    if isinstance(e, requests.HTTPError):
                        retryable = error_response is not None and error_response.status_code in {
                            408, 500, 502, 503, 504
                        }
                        # 429 说明整个站点在限流，本轮不再请求同一站点的其他源
                        if error_response is not None and error_response.status_code == 429 and host:
                            self._rate_limited_hosts.add(host)
                    # 不提前重试带服务端等待指令的失败响应。
                    if (
                        not retryable
                        or attempt == 1
                        or (error_response is not None and "Retry-After" in error_response.headers)
                    ):
                        raise

            parsed_items = self.parser.parse(response.text, feed.url)

            # 限制条目数量（0=不限制）
            if feed.max_items > 0:
                parsed_items = parsed_items[:feed.max_items]

            # 转换为 RSSItem（使用配置的时区）
            now = get_configured_time(self.timezone)
            crawl_time = now.strftime("%H:%M")
            items = []

            for parsed in parsed_items:
                item = RSSItem(
                    title=parsed.title,
                    feed_id=feed.id,
                    feed_name=feed.name,
                    url=parsed.url,
                    guid=parsed.guid or "",
                    published_at=parsed.published_at or "",
                    summary=parsed.summary or "",
                    author=parsed.author or "",
                    crawl_time=crawl_time,
                    first_time=crawl_time,
                    last_time=crawl_time,
                    count=1,
                )
                items.append(item)

            # 注意：新鲜度过滤已移至推送阶段（_convert_rss_items_to_list）
            # 这样所有文章都会存入数据库，但旧文章不会推送
            print(f"[RSS] {feed.name}: 获取 {len(items)} 条")
            return items, None

        except _HostRateLimited as e:
            error = f"跳过：{e} 本轮已返回 429 限流"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except requests.Timeout:
            error = f"请求超时 ({self.timeout}s)"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except requests.RequestException as e:
            error = f"请求失败: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except ValueError as e:
            error = f"解析失败: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except Exception as e:
            error = f"未知错误: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

    def fetch_all(self) -> RSSData:
        """
        抓取所有 RSS 源

        Returns:
            RSSData 对象
        """
        all_items: Dict[str, List[RSSItem]] = {}
        id_to_name: Dict[str, str] = {}
        failed_ids: List[str] = []

        # 使用配置的时区
        now = get_configured_time(self.timezone)
        crawl_time = now.strftime("%H:%M")
        crawl_date = now.strftime("%Y-%m-%d")

        print(f"[RSS] 开始抓取 {len(self.feeds)} 个 RSS 源...")

        for i, feed in enumerate(self.feeds):
            # 请求间隔（带随机波动）；已限流站点的源不发请求，也不必等待
            if i > 0 and (urlparse(feed.url).hostname or "") not in self._rate_limited_hosts:
                interval = self.request_interval / 1000
                jitter = random.uniform(-0.2, 0.2) * interval
                time.sleep(interval + jitter)

            items, error = self.fetch_feed(feed)

            id_to_name[feed.id] = feed.name

            if error:
                failed_ids.append(feed.id)
            else:
                all_items[feed.id] = items

        total_items = sum(len(items) for items in all_items.values())
        print(f"[RSS] 抓取完成: {len(all_items)} 个源成功, {len(failed_ids)} 个失败, 共 {total_items} 条")

        return RSSData(
            date=crawl_date,
            crawl_time=crawl_time,
            items=all_items,
            id_to_name=id_to_name,
            failed_ids=failed_ids,
        )

    @classmethod
    def from_config(cls, config: Dict) -> "RSSFetcher":
        """
        从配置字典创建抓取器

        Args:
            config: 配置字典，格式如下：
                {
                    "enabled": true,
                    "request_interval": 2000,
                    "freshness_filter": {
                        "enabled": true,
                        "max_age_days": 3
                    },
                    "feeds": [
                        {"id": "hacker-news", "name": "Hacker News", "url": "...", "max_age_days": 1}
                    ]
                }

        Returns:
            RSSFetcher 实例
        """
        # 读取新鲜度过滤配置
        freshness_config = config.get("freshness_filter", {})
        freshness_enabled = freshness_config.get("enabled", True)  # 默认启用
        default_max_age_days = freshness_config.get("max_age_days", 3)  # 默认3天

        feeds = []
        for feed_config in config.get("feeds", []):
            feed = feed_config_from_dict(feed_config)
            if feed.id and feed.url:
                feeds.append(feed)

        return cls(
            feeds=feeds,
            request_interval=config.get("request_interval", 2000),
            timeout=config.get("timeout", 15),
            use_proxy=config.get("use_proxy", False),
            proxy_url=config.get("proxy_url", ""),
            timezone=config.get("timezone", DEFAULT_TIMEZONE),
            freshness_enabled=freshness_enabled,
            default_max_age_days=default_max_age_days,
        )
