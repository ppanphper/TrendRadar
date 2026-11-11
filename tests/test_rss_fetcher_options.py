"""RSS 单源抓取选项：use_proxy 覆盖、impersonate 浏览器指纹、429 站点跳过。"""

import unittest
from unittest.mock import MagicMock, patch

import requests

from trendradar.crawler.rss.fetcher import RSSFetcher, feed_config_from_dict


RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>帖子一</title><link>https://example.com/t/1</link><guid>1</guid></item>
</channel></rss>"""


def _response(status, text=RSS_XML, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.headers = headers or {}
    resp.url = "https://example.com/feed"
    if status >= 400:
        error = requests.HTTPError(f"{status} Error")
        error.response = resp
        resp.raise_for_status.side_effect = error
    else:
        resp.raise_for_status.return_value = None
    return resp


def _fetcher(feed_dicts, use_proxy=False):
    feeds = [feed_config_from_dict(d) for d in feed_dicts]
    return RSSFetcher(
        feeds=feeds, request_interval=0, use_proxy=use_proxy,
        proxy_url="http://proxy:7890", freshness_enabled=False,
    )


class FeedConfigTest(unittest.TestCase):
    def test_parses_proxy_and_impersonate(self):
        feed = feed_config_from_dict({
            "id": "ld", "name": "LD", "url": "https://linux.do/latest.rss",
            "use_proxy": True, "impersonate": " chrome ",
        })
        self.assertTrue(feed.use_proxy)
        self.assertEqual(feed.impersonate, "chrome")

    def test_invalid_use_proxy_falls_back_to_global(self):
        feed = feed_config_from_dict({"id": "a", "url": "https://a/rss", "use_proxy": "yes"})
        self.assertIsNone(feed.use_proxy)
        self.assertEqual(feed.impersonate, "")


class FetchOptionsTest(unittest.TestCase):
    def test_per_feed_proxy_override(self):
        fetcher = _fetcher([
            {"id": "on", "url": "https://a.example/rss", "use_proxy": True},
            {"id": "off", "url": "https://b.example/rss", "use_proxy": False},
            {"id": "default", "url": "https://c.example/rss"},
        ])
        fetcher.session.get = MagicMock(return_value=_response(200))
        fetcher.fetch_all()

        calls = {c.args[0]: c.kwargs for c in fetcher.session.get.call_args_list}
        self.assertEqual(calls["https://a.example/rss"]["proxies"]["https"], "http://proxy:7890")
        self.assertEqual(calls["https://b.example/rss"]["proxies"], {"http": None, "https": None})
        self.assertNotIn("proxies", calls["https://c.example/rss"])

    def test_429_skips_remaining_feeds_on_same_host(self):
        fetcher = _fetcher([
            {"id": "ld1", "url": "https://linux.do/c/news/34.rss"},
            {"id": "ld2", "url": "https://linux.do/latest.rss"},
            {"id": "other", "url": "https://other.example/rss"},
        ])
        fetcher.session.get = MagicMock(side_effect=[_response(429), _response(200)])
        data = fetcher.fetch_all()

        requested = [c.args[0] for c in fetcher.session.get.call_args_list]
        self.assertEqual(requested, ["https://linux.do/c/news/34.rss", "https://other.example/rss"])
        self.assertEqual(sorted(data.failed_ids), ["ld1", "ld2"])
        self.assertIn("other", data.items)

    def test_impersonate_uses_curl_session_with_proxy(self):
        fetcher = _fetcher(
            [{"id": "ld", "url": "https://linux.do/latest.rss", "impersonate": "chrome"}],
            use_proxy=True,
        )
        curl_session = MagicMock()
        curl_session.get.return_value = _response(200)
        fetcher.session.get = MagicMock()
        with patch.object(fetcher, "_get_impersonate_session", return_value=curl_session):
            data = fetcher.fetch_all()

        fetcher.session.get.assert_not_called()
        kwargs = curl_session.get.call_args.kwargs
        self.assertEqual(kwargs["impersonate"], "chrome")
        self.assertEqual(kwargs["proxies"]["https"], "http://proxy:7890")
        self.assertNotIn("User-Agent", kwargs["headers"])
        self.assertEqual(len(data.items["ld"]), 1)

    def test_impersonate_http_error_is_reported(self):
        fetcher = _fetcher([{"id": "ld", "url": "https://linux.do/latest.rss", "impersonate": "chrome"}])
        curl_session = MagicMock()
        curl_session.get.return_value = _response(403, text="Just a moment...")
        with patch.object(fetcher, "_get_impersonate_session", return_value=curl_session):
            data = fetcher.fetch_all()
        self.assertEqual(data.failed_ids, ["ld"])


if __name__ == "__main__":
    unittest.main()
