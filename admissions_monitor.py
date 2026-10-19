"""Daily official admissions scan, DeepSeek summary, and QQ notification."""

import argparse
import asyncio
from datetime import date, datetime, timezone
import getpass
import hashlib
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from qqbot_agent_sdk import QQApiClient

from qq_private import DATA, read_json, save_private


LOG = logging.getLogger("admissions_monitor")
CONFIG_PATH = DATA / "admissions.json"
STATE_PATH = DATA / "admissions-state.json"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
REGISTRATION_URL = "https://www.gatzs.com.cn/z/gatyz/"
NEWS_URLS = (
    "https://yz.chsi.com.cn/kyzx/",
    "https://yz.chsi.com.cn/kyzx/zcdh/",
    "https://yz.chsi.com.cn/kyzx/yxzc/",
)
CHINA_TIME = ZoneInfo("Asia/Shanghai")
ARTICLE_PATH = re.compile(r"^/kyzx/[^/]+/\d{6}/\d{8}/\d+\.html$")
SPACE = re.compile(r"\s+")
REGISTRATION_DATES = re.compile(
    r"网上报名时间[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日"
    r"至(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日"
)


class PageParser(HTMLParser):
    """Extract readable page text and links without executing page scripts."""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.text_parts = []
        self.links = []
        self._ignored_depth = 0
        self._href = None
        self._anchor_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "a":
            href = dict(attrs).get("href")
            self._href = urljoin(self.base_url, href) if href else None
            self._anchor_parts = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and self._href:
            title = clean_text(" ".join(self._anchor_parts))
            if title:
                self.links.append({"title": title, "url": self._href})
            self._href = None
            self._anchor_parts = []

    def handle_data(self, data):
        if self._ignored_depth:
            return
        value = clean_text(data)
        if value:
            self.text_parts.append(value)
            if self._href:
                self._anchor_parts.append(value)

    @property
    def text(self):
        return clean_text(" ".join(self.text_parts))


def clean_text(value):
    return SPACE.sub(" ", value or "").strip()


def parse_page(html, base_url):
    parser = PageParser(base_url)
    parser.feed(html)
    return parser.text, parser.links


def extract_articles(pages):
    """Return unique official article links in source-page order."""
    found = []
    seen = set()
    for page in pages:
        for link in page.get("links", []):
            parsed = urlparse(link["url"])
            canonical = f"https://yz.chsi.com.cn{parsed.path}"
            if parsed.hostname != "yz.chsi.com.cn" or not ARTICLE_PATH.match(parsed.path):
                continue
            if canonical in seen:
                continue
            title = clean_text(link.get("title"))
            if len(title) < 4:
                continue
            seen.add(canonical)
            found.append({"title": title, "url": canonical})
    return found


def registration_status(text, today):
    match = REGISTRATION_DATES.search(text)
    if not match:
        return {"status": "unknown", "label": "无法确认", "period": "官网未识别到报名日期"}
    start_year, start_month, start_day, end_year, end_month, end_day = match.groups()
    start = date(int(start_year), int(start_month), int(start_day))
    end = date(int(end_year or start_year), int(end_month), int(end_day))
    if today < start:
        status, label = "upcoming", "尚未开始"
    elif today <= end:
        status, label = "open", "正在报名"
    else:
        status, label = "closed", "本轮已结束"
    return {
        "status": status,
        "label": label,
        "period": f"{start.isoformat()} 至 {end.isoformat()}",
    }


def initialize(replace=False):
    if CONFIG_PATH.exists() and not replace:
        print("研招监控配置已存在。更新密钥请运行：python admissions_monitor.py set-key")
        return
    api_key = getpass.getpass("DeepSeek API Key（输入不显示）: ").strip()
    if not api_key:
        raise ValueError("DeepSeek API Key 不能为空。")
    save_private(CONFIG_PATH, {
        "deepseek_api_key": api_key,
        "deepseek_model": "deepseek-chat",
        "max_new_items": 5,
    })
    print("研招监控配置已保存。可运行：python admissions_monitor.py check")


def set_api_key():
    config = read_json(CONFIG_PATH) if CONFIG_PATH.exists() else {}
    api_key = getpass.getpass("DeepSeek API Key（输入不显示）: ").strip()
    if not api_key:
        raise ValueError("DeepSeek API Key 不能为空。")
    config.setdefault("deepseek_model", "deepseek-chat")
    config.setdefault("max_new_items", 5)
    config["deepseek_api_key"] = api_key
    save_private(CONFIG_PATH, config)
    print("DeepSeek API Key 已更新。")


async def fetch_page(http, url):
    response = await http.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; StreamerStatusMonitor/1.0; admissions checker)",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        follow_redirects=True,
    )
    response.raise_for_status()
    text, links = parse_page(response.text, str(response.url))
    return {"url": url, "text": text, "links": links}


class DeepSeekClient:
    def __init__(self, http, api_key, model="deepseek-chat"):
        self.http = http
        self.api_key = api_key
        self.model = model

    async def summarize(self, today, registration, registration_text, articles):
        source_data = {
            "current_date_china": today.isoformat(),
            "registration": {
                **registration,
                "official_url": REGISTRATION_URL,
                "official_page_text": registration_text[:12000],
            },
            "new_articles": [
                {"title": item["title"], "url": item["url"], "text": item.get("text", "")[:6000]}
                for item in articles
            ],
        }
        system = (
            "你是严谨的中国研究生招生资讯编辑。只能依据用户给出的官方网页材料整理，"
            "不得补充未提供的事实，不得改写网址。输出 JSON，结构为："
            '{"registration_summary":"一句话","overall_summary":"一句话",'
            '"items":[{"title":"标题","summary":"一到两句话","url":"原网址"}]}。'
            "报名状态以输入中的 status、period 为准。新资讯为空时 items 必须为空。"
        )
        response = await self.http.post(
            DEEPSEEK_API,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "请把以下 JSON 官方资料整理为指定 JSON：\n" + json.dumps(source_data, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 1200,
            },
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        result = json.loads(content)
        if not isinstance(result, dict) or not isinstance(result.get("items", []), list):
            raise ValueError("DeepSeek 返回的摘要结构不正确")
        allowed_urls = {item["url"] for item in articles}
        result["items"] = [
            item for item in result.get("items", [])
            if isinstance(item, dict) and item.get("url") in allowed_urls
        ]
        return result


class AdmissionsMonitor:
    def __init__(self, http, deepseek, notifier, owner, state_path=STATE_PATH,
                 max_new_items=5, today=lambda: datetime.now(CHINA_TIME).date()):
        self.http = http
        self.deepseek = deepseek
        self.notifier = notifier
        self.owner = owner
        self.state_path = state_path
        self.max_new_items = max(1, min(int(max_new_items), 10))
        self.today = today
        self.state = read_json(state_path) if state_path.exists() else {}

    async def collect(self):
        results = await asyncio.gather(
            *(fetch_page(self.http, url) for url in (REGISTRATION_URL, *NEWS_URLS)),
            return_exceptions=True,
        )
        failures = []
        pages = []
        for url, result in zip((REGISTRATION_URL, *NEWS_URLS), results):
            if isinstance(result, Exception):
                failures.append(f"{url}: {result}")
            else:
                pages.append(result)
        registration_page = next((page for page in pages if page["url"] == REGISTRATION_URL), None)
        if registration_page is None:
            raise RuntimeError("港澳台招生官网读取失败；" + "；".join(failures))
        news_pages = [page for page in pages if page["url"] in NEWS_URLS]
        if not news_pages:
            raise RuntimeError("研招网资讯页面全部读取失败；" + "；".join(failures))
        if failures:
            LOG.warning("部分官方来源读取失败：%s", "；".join(failures))
        return registration_page, extract_articles(news_pages)

    async def article_details(self, articles):
        results = await asyncio.gather(
            *(fetch_page(self.http, item["url"]) for item in articles),
            return_exceptions=True,
        )
        enriched = []
        for item, result in zip(articles, results):
            current = dict(item)
            if isinstance(result, Exception):
                LOG.warning("资讯正文读取失败，仍保留标题：%s", item["url"])
            else:
                current["text"] = result["text"]
            enriched.append(current)
        return enriched

    @staticmethod
    def fallback_summary(registration, articles):
        return {
            "registration_summary": f"港澳台研究生网上报名{registration['label']}，官网日期为 {registration['period']}。",
            "overall_summary": "研招网没有发现新的资讯。" if not articles else f"研招网发现 {len(articles)} 条新资讯。",
            "items": [
                {"title": item["title"], "summary": "请打开官方链接查看详情。", "url": item["url"]}
                for item in articles
            ],
        }

    @staticmethod
    def format_message(today, registration, summary, articles):
        lines = [
            "📚 港澳台考研 / 研招日报",
            f"日期：{today.isoformat()}（北京时间）",
            f"报名状态：{registration['label']}",
            summary.get("registration_summary") or f"官网报名期：{registration['period']}",
            "",
            summary.get("overall_summary") or (f"发现 {len(articles)} 条新资讯。" if articles else "今天没有发现新资讯。"),
        ]
        summarized = {item.get("url"): item for item in summary.get("items", [])}
        for index, article in enumerate(articles, 1):
            item = summarized.get(article["url"], {})
            lines.extend([
                "",
                f"{index}. {item.get('title') or article['title']}",
                clean_text(item.get("summary") or "请打开官方链接查看详情。"),
                article["url"],
            ])
        lines.extend(["", f"报名官网：{REGISTRATION_URL}", f"研招资讯：{NEWS_URLS[0]}"])
        message = "\n".join(lines)
        return message if len(message) <= 3500 else message[:3470] + "\n（内容过长，已截断）"

    async def run_once(self):
        today = self.today()
        registration_page, current_articles = await self.collect()
        seen = set(self.state.get("seen_urls", []))
        new_articles = [item for item in current_articles if item["url"] not in seen][:self.max_new_items]
        new_articles = await self.article_details(new_articles)
        registration = registration_status(registration_page["text"], today)
        try:
            summary = await self.deepseek.summarize(
                today, registration, registration_page["text"], new_articles,
            )
            ai_ok = True
        except Exception as exc:
            LOG.error("DeepSeek 整理失败，改发基础报告：%s", exc)
            summary = self.fallback_summary(registration, new_articles)
            ai_ok = False
        message = self.format_message(today, registration, summary, new_articles)
        await self.notifier.send_text("c2c", self.owner, message, markdown=False, retries=1)
        all_seen = list(dict.fromkeys([item["url"] for item in current_articles] + list(seen)))[:1000]
        self.state.update({
            "seen_urls": all_seen,
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
            "last_report_date": today.isoformat(),
            "last_registration_status": registration["status"],
            "registration_fingerprint": hashlib.sha256(registration_page["text"].encode()).hexdigest(),
        })
        save_private(self.state_path, self.state)
        LOG.info(
            "研招日报已发送：报名状态 %s，新增资讯 %d，DeepSeek %s。",
            registration["status"], len(new_articles), "成功" if ai_ok else "降级",
        )
        return {"registration": registration, "new_articles": new_articles, "ai_ok": ai_ok}


async def run_once():
    config = read_json(CONFIG_PATH)
    api_key = config.get("deepseek_api_key", "").strip()
    if not api_key:
        raise ValueError("尚未配置 DeepSeek API Key，请先运行 init 或 set-key。")
    qq_config = read_json(DATA / "config.json")
    owner = read_json(DATA / "owner.json")["user_openid"]
    timeout = httpx.Timeout(30, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as http:
        deepseek = DeepSeekClient(http, api_key, config.get("deepseek_model", "deepseek-chat"))
        notifier = QQApiClient(qq_config["app_id"], qq_config["client_secret"])
        notifier.setup(http)
        monitor = AdmissionsMonitor(
            http, deepseek, notifier, owner,
            max_new_items=config.get("max_new_items", 5),
        )
        await monitor.run_once()


def main():
    parser = argparse.ArgumentParser(description="港澳台考研报名与研招资讯日报")
    parser.add_argument("command", choices=("init", "set-key", "check"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("qqbot_agent_sdk").setLevel(logging.CRITICAL)
    config = {}
    try:
        if args.command == "init":
            initialize()
        elif args.command == "set-key":
            set_api_key()
        else:
            if CONFIG_PATH.exists():
                config = read_json(CONFIG_PATH)
            asyncio.run(run_once())
    except KeyboardInterrupt:
        print("已停止。")
    except FileNotFoundError as exc:
        LOG.error("缺少配置文件：%s", exc.filename)
        raise SystemExit(1)
    except Exception as exc:
        message = str(exc)
        secret = config.get("deepseek_api_key")
        if secret:
            message = message.replace(secret, "[REDACTED]")
        LOG.error("%s", message)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
