"""Discover channel videos via Atom feeds and confirm live state via YouTube Data API."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import getpass
import logging
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import httpx
from qqbot_agent_sdk import QQApiClient

from qq_private import DATA, read_json, save_private

LOG = logging.getLogger("youtube_monitor")
CONFIG_PATH = DATA / "youtube.json"
STATE_PATH = DATA / "youtube-state.json"
YT_API = "https://www.googleapis.com/youtube/v3"
ATOM = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def api_error(response):
    try:
        details = response.json().get("error", {})
        message = details.get("message") or "未知错误"
    except Exception:
        message = response.text[:200]
    return RuntimeError(f"YouTube API HTTP {response.status_code}: {message}")


def set_api_key():
    config = read_json(CONFIG_PATH)
    key = getpass.getpass("YouTube Data API Key（输入不显示）: ").strip()
    if not key:
        raise ValueError("API Key 不能为空。")
    config["api_key"] = key
    save_private(CONFIG_PATH, config)
    print("YouTube API Key 已保存。下一步运行：python youtube_monitor.py check")


class YouTubeClient:
    def __init__(self, http, api_key):
        self.http = http
        self.api_key = api_key

    async def videos(self, video_ids):
        found = {}
        for start in range(0, len(video_ids), 50):
            chunk = video_ids[start:start + 50]
            response = await self.http.get(f"{YT_API}/videos", params={
                "part": "snippet,liveStreamingDetails,status",
                "id": ",".join(chunk),
                "key": self.api_key,
            })
            if response.status_code >= 400:
                raise api_error(response)
            for video in response.json().get("items", []):
                found[video["id"]] = video
        return found


async def fetch_feed(http, channel):
    response = await http.get(
        "https://www.youtube.com/feeds/videos.xml",
        params={"channel_id": channel["channel_id"]},
        headers={"User-Agent": "StreamerStatusMonitor/1.0"},
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    entries = []
    for node in root.findall("atom:entry", ATOM):
        video_id = node.findtext("yt:videoId", namespaces=ATOM)
        if not video_id:
            continue
        entries.append({
            "video_id": video_id,
            "channel_id": channel["channel_id"],
            "channel_name": channel.get("name") or root.findtext("atom:title", default="未知频道", namespaces=ATOM),
            "feed_title": node.findtext("atom:title", default="", namespaces=ATOM),
            "feed_updated": node.findtext("atom:updated", default="", namespaces=ATOM),
        })
    return entries


def live_state(video):
    details = video.get("liveStreamingDetails") or {}
    if details.get("actualEndTime"):
        return "complete"
    if details.get("actualStartTime"):
        return "live"
    if details.get("scheduledStartTime"):
        return "upcoming"
    return "normal"


class YouTubeMonitor:
    def __init__(self, youtube, notifier, owner, channels, state_path=STATE_PATH, now=time.time):
        self.youtube = youtube
        self.notifier = notifier
        self.owner = owner
        self.channels = channels
        self.state_path = state_path
        self.now = now
        state_exists = state_path.exists()
        self.state = read_json(state_path) if state_exists else {"videos": {}}
        self.fresh_state = not state_exists
        # Upgrade from the live-only monitor without notifying every old feed item.
        if state_exists and not self.state.get("video_updates_initialized"):
            for record in self.state.setdefault("videos", {}).values():
                record["video_update_notified"] = True
            self.state["video_updates_initialized"] = True

    @staticmethod
    def message(video, channel_name, initial):
        snippet = video.get("snippet") or {}
        details = video.get("liveStreamingDetails") or {}
        started = details.get("actualStartTime") or "未知"
        if started != "未知":
            try:
                parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
                started = parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M（北京时间）")
            except ValueError:
                pass
        lead = "🔴 监测启动时发现已开播" if initial else "🔴 主播已开播"
        return "\n".join([
            f"{lead} · YouTube",
            f"频道：{channel_name}",
            f"标题：{snippet.get('title') or '未提供'}",
            f"开始时间：{started}",
            f"https://www.youtube.com/watch?v={video['id']}",
        ])

    @staticmethod
    def update_message(video, channel_name, kind):
        snippet = video.get("snippet") or {}
        details = video.get("liveStreamingDetails") or {}
        if kind == "upcoming":
            lead = "🗓️ 新增直播预约 · YouTube"
            when = details.get("scheduledStartTime")
            time_label = "预约时间"
        elif kind == "complete":
            lead = "📺 频道发布了直播回放 · YouTube"
            when = snippet.get("publishedAt")
            time_label = "发布时间"
        else:
            lead = "📺 频道发布新影片 · YouTube"
            when = snippet.get("publishedAt")
            time_label = "发布时间"
        if when:
            try:
                parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
                when = parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M（北京时间）")
            except ValueError:
                pass
        else:
            when = "未知"
        return "\n".join([
            lead,
            f"频道：{channel_name}",
            f"标题：{snippet.get('title') or '未提供'}",
            f"{time_label}：{when}",
            f"https://www.youtube.com/watch?v={video['id']}",
        ])

    async def discover(self, http):
        records = self.state.setdefault("videos", {})
        discovered = set()
        for channel in self.channels:
            for entry in await fetch_feed(http, channel):
                current = records.get(entry["video_id"])
                if current is None:
                    records[entry["video_id"]] = {
                        **entry, "kind": "unknown", "video_update_notified": False,
                    }
                    discovered.add(entry["video_id"])
                elif current.get("feed_updated") != entry["feed_updated"]:
                    # Metadata changes can turn a normal upload into a scheduled live item.
                    current.update(entry)
                    current["kind"] = "unknown"
        return discovered

    async def poll_once(self, http):
        initial_scan = not self.state.get("scan_initialized")
        await self.discover(http)
        records = self.state.setdefault("videos", {})
        if self.fresh_state and not self.state.get("video_updates_initialized"):
            for record in records.values():
                record["video_update_notified"] = True
            self.state["video_updates_initialized"] = True
            self.fresh_state = False
        candidates = [vid for vid, record in records.items()
                      if (record.get("kind") in ("unknown", "upcoming", "live")
                          or not record.get("video_update_notified", False))]
        videos = await self.youtube.videos(candidates) if candidates else {}
        current_time = self.now()
        for video_id in candidates:
            record = records[video_id]
            video = videos.get(video_id)
            if video is None:
                record["kind"] = "unavailable"
                continue
            kind = live_state(video)
            record["kind"] = kind
            record["last_checked_at"] = datetime.now(timezone.utc).isoformat()
            if kind == "live":
                # A newly discovered live broadcast gets the live alert only.
                record["video_update_notified"] = True
                needs_notification = record.get("notified_video_id") != video_id
                if needs_notification and current_time >= float(record.get("next_notify_at", 0)):
                    try:
                        await self.notifier.send_text(
                            "c2c", self.owner,
                            self.message(video, record.get("channel_name", "未知频道"), initial_scan),
                            markdown=False, retries=1,
                        )
                        record["notified_video_id"] = video_id
                        record["notify_attempts"] = 0
                        record.pop("next_notify_at", None)
                        LOG.info("已发送 YouTube 开播通知：%s", video_id)
                    except Exception as exc:
                        attempts = int(record.get("notify_attempts", 0)) + 1
                        delay = (60, 300, 900, 3600)[min(attempts - 1, 3)]
                        record["notify_attempts"] = attempts
                        record["next_notify_at"] = current_time + delay
                        LOG.error("发送 YouTube 开播通知失败，%s 秒后再试：%s", delay, exc)
            elif (not record.get("video_update_notified", False)
                  and current_time >= float(record.get("next_update_notify_at", 0))):
                try:
                    await self.notifier.send_text(
                        "c2c", self.owner,
                        self.update_message(video, record.get("channel_name", "未知频道"), kind),
                        markdown=False, retries=1,
                    )
                    record["video_update_notified"] = True
                    record["update_notify_attempts"] = 0
                    record.pop("next_update_notify_at", None)
                    LOG.info("已发送 YouTube 影片更新通知：%s (%s)", video_id, kind)
                except Exception as exc:
                    attempts = int(record.get("update_notify_attempts", 0)) + 1
                    delay = (60, 300, 900, 3600)[min(attempts - 1, 3)]
                    record["update_notify_attempts"] = attempts
                    record["next_update_notify_at"] = current_time + delay
                    LOG.error("发送 YouTube 影片更新通知失败，%s 秒后再试：%s", delay, exc)
        self.state["scan_initialized"] = True
        # Bound state growth while retaining every item still relevant to live monitoring.
        finished = [(vid, record) for vid, record in records.items()
                    if record.get("kind") in ("normal", "complete", "unavailable")]
        for video_id, _ in finished[:-100]:
            records.pop(video_id, None)
        save_private(self.state_path, self.state)
        return {video_id: record["kind"] for video_id, record in records.items()
                if record.get("kind") in ("upcoming", "live")}


async def run_once_or_forever(once=False):
    config = read_json(CONFIG_PATH)
    api_key = config.get("api_key", "").strip()
    if not api_key:
        raise ValueError("尚未配置 YouTube API Key，请先运行 set-key。")
    qq_config = read_json(DATA / "config.json")
    owner = read_json(DATA / "owner.json")["user_openid"]
    channels = config.get("channels") or []
    if not channels:
        raise ValueError("YouTube 频道列表为空。")
    poll_seconds = max(30, int(config.get("poll_seconds", 60)))
    async with httpx.AsyncClient(timeout=20) as http:
        youtube = YouTubeClient(http, api_key)
        notifier = QQApiClient(qq_config["app_id"], qq_config["client_secret"])
        notifier.setup(http)
        monitor = YouTubeMonitor(youtube, notifier, owner, channels)
        while True:
            try:
                relevant = await monitor.poll_once(http)
                live_count = sum(kind == "live" for kind in relevant.values())
                upcoming_count = sum(kind == "upcoming" for kind in relevant.values())
                LOG.info("YouTube 查询成功：频道 %d 个，直播中 %d，预约 %d。",
                         len(channels), live_count, upcoming_count)
            except Exception as exc:
                text = str(exc).replace(api_key, "[REDACTED]")
                LOG.error("YouTube 查询失败，保留原状态：%s", text)
                if once:
                    raise RuntimeError(text) from None
            if once:
                return
            await asyncio.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description="YouTube 开播监测")
    parser.add_argument("command", choices=("set-key", "check", "run"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("qqbot_agent_sdk").setLevel(logging.CRITICAL)
    try:
        if args.command == "set-key":
            set_api_key()
        else:
            asyncio.run(run_once_or_forever(once=args.command == "check"))
    except KeyboardInterrupt:
        print("已停止。")
    except Exception as exc:
        LOG.error("%s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
