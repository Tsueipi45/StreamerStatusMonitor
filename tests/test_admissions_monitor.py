from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

from admissions_monitor import (
    AdmissionsMonitor,
    DeepSeekClient,
    NEWS_URLS,
    REGISTRATION_URL,
    extract_articles,
    filter_articles_for_year,
    parse_page,
    registration_status,
)


REGISTRATION_HTML = """
<html><head><script>ignore me</script></head><body>
<h1>2027年面向港澳台招收研究生网上报名</h1>
<p>网上报名时间：2027年1月4日至1月11日，全天24小时接受网上报名。</p>
</body></html>
"""

OLD_REGISTRATION_HTML = """
<html><body>
<h1>面向港澳台招收研究生网上报名</h1>
<p>网上报名时间：2026年1月4日至1月11日，全天24小时接受网上报名。</p>
</body></html>
"""

NEWS_HTML = """
<html><body>
<a href="/kyzx/jybzc/202610/20261018/123456.html">教育部发布2027年新的研招通知</a>
<a href="https://yz.chsi.com.cn/kyzx/jybzc/202610/20261018/123456.html?x=1">重复链接</a>
<a href="/sch/">院校库</a>
<a href="https://example.com/kyzx/other/202610/20261018/999.html">站外内容</a>
</body></html>
"""

ARTICLE_HTML = """
<html><body><h1>教育部发布新的研招通知</h1><p>这是一条官方招生资讯正文。</p></body></html>
"""


class ParserTests(unittest.TestCase):
    def test_page_parser_ignores_scripts_and_resolves_links(self):
        text, links = parse_page(NEWS_HTML, NEWS_URLS[0])
        self.assertNotIn("ignore me", text)
        self.assertEqual(links[0]["url"], "https://yz.chsi.com.cn/kyzx/jybzc/202610/20261018/123456.html")

    def test_article_filter_is_official_and_deduplicated(self):
        _, links = parse_page(NEWS_HTML, NEWS_URLS[0])
        articles = extract_articles([{"links": links}])
        self.assertEqual(articles, [{
            "title": "教育部发布2027年新的研招通知",
            "url": "https://yz.chsi.com.cn/kyzx/jybzc/202610/20261018/123456.html",
        }])

    def test_only_target_year_articles_are_kept(self):
        articles = [
            {"title": "2027年全国硕士研究生招生考试公告", "url": "https://example/2027"},
            {"title": "其他招生资讯", "url": "https://example/other"},
        ]
        self.assertEqual(filter_articles_for_year(articles, 2027), articles[:1])

    def test_registration_status_uses_official_dates(self):
        text, _ = parse_page(REGISTRATION_HTML, REGISTRATION_URL)
        self.assertEqual(registration_status(text, date(2026, 10, 19), 2027)["status"], "upcoming")
        self.assertEqual(registration_status(text, date(2027, 1, 7))["status"], "open")
        self.assertEqual(registration_status(text, date(2027, 1, 12))["status"], "closed")

    def test_old_registration_page_is_reported_as_2027_not_announced(self):
        text, _ = parse_page(OLD_REGISTRATION_HTML, REGISTRATION_URL)
        status = registration_status(text, date(2026, 10, 20), 2027)
        self.assertEqual(status["status"], "awaiting_announcement")
        self.assertEqual(status["label"], "报名安排暂未公布")
        self.assertNotIn("2026", status["period"])


class DeepSeekTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_summary_and_url_allowlist(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "registration_summary": "尚未开始",
                "overall_summary": "发现一条",
                "items": [
                    {"title": "有效", "summary": "摘要", "url": "https://yz.chsi.com.cn/kyzx/jybzc/202610/20261018/123456.html"},
                    {"title": "伪造", "summary": "摘要", "url": "https://example.com/fake"},
                ],
            }, ensure_ascii=False)}}]})

        article = {"title": "有效", "url": "https://yz.chsi.com.cn/kyzx/jybzc/202610/20261018/123456.html", "text": "正文"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = DeepSeekClient(http, "secret")
            result = await client.summarize(date(2026, 10, 19), 2027, {"status": "upcoming", "period": "x"}, "报名页", [article])
        self.assertEqual(len(result["items"]), 1)
        body = json.loads(requests[0].content)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(requests[0].headers["authorization"], "Bearer secret")


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.deepseek = AsyncMock()
        self.deepseek.summarize.return_value = {
            "registration_summary": "报名尚未开始。",
            "overall_summary": "发现一条新资讯。",
            "items": [{
                "title": "教育部发布2027年新的研招通知",
                "summary": "官方发布了一条通知。",
                "url": "https://yz.chsi.com.cn/kyzx/jybzc/202610/20261018/123456.html",
            }],
        }
        self.notifier = AsyncMock()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def transport():
        def handle(request):
            if str(request.url) == REGISTRATION_URL:
                return httpx.Response(200, text=REGISTRATION_HTML)
            if str(request.url) in NEWS_URLS:
                return httpx.Response(200, text=NEWS_HTML)
            return httpx.Response(200, text=ARTICLE_HTML)
        return httpx.MockTransport(handle)

    async def make_monitor(self, http):
        return AdmissionsMonitor(
            http, self.deepseek, self.notifier, "owner", self.state_path,
            today=lambda: date(2026, 10, 19),
        )

    async def test_first_report_sends_current_item_and_saves_state(self):
        async with httpx.AsyncClient(transport=self.transport()) as http:
            monitor = await self.make_monitor(http)
            result = await monitor.run_once()
        self.assertEqual(len(result["new_articles"]), 1)
        self.notifier.send_text.assert_awaited_once()
        args = self.notifier.send_text.call_args.args
        self.assertEqual(args[:2], ("c2c", "owner"))
        self.assertIn("2027 报名状态：尚未开始", args[2])
        self.assertIn("教育部发布2027年新的研招通知", args[2])
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["seen_urls"]), 1)

    async def test_second_report_says_no_new_information(self):
        async with httpx.AsyncClient(transport=self.transport()) as http:
            first = await self.make_monitor(http)
            await first.run_once()
            self.notifier.reset_mock()
            second = await self.make_monitor(http)
            await second.run_once()
        self.assertEqual(second.deepseek.summarize.call_args.args[-1], [])
        message = self.notifier.send_text.call_args.args[2]
        self.assertNotIn("1. 教育部发布", message)

    async def test_older_registration_page_does_not_report_old_closed_status(self):
        def handle(request):
            if str(request.url) == REGISTRATION_URL:
                return httpx.Response(200, text=OLD_REGISTRATION_HTML)
            if str(request.url) in NEWS_URLS:
                return httpx.Response(200, text=NEWS_HTML)
            return httpx.Response(200, text=ARTICLE_HTML)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            monitor = await self.make_monitor(http)
            await monitor.run_once()
        message = self.notifier.send_text.call_args.args[2]
        self.assertIn("2027 报名状态：报名安排暂未公布", message)
        self.assertNotIn("本轮已结束", message)
        deepseek_registration_text = self.deepseek.summarize.call_args.args[3]
        self.assertNotIn("2026", deepseek_registration_text)

    async def test_deepseek_failure_still_sends_basic_report(self):
        self.deepseek.summarize.side_effect = RuntimeError("temporary")
        async with httpx.AsyncClient(transport=self.transport()) as http:
            monitor = await self.make_monitor(http)
            result = await monitor.run_once()
        self.assertFalse(result["ai_ok"])
        self.assertIn("请打开官方链接查看详情", self.notifier.send_text.call_args.args[2])

    async def test_qq_failure_does_not_advance_seen_state(self):
        self.notifier.send_text.side_effect = RuntimeError("blocked")
        async with httpx.AsyncClient(transport=self.transport()) as http:
            monitor = await self.make_monitor(http)
            with self.assertRaises(RuntimeError):
                await monitor.run_once()
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
