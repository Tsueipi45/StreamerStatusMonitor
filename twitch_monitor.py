"""Poll Twitch stream state and send deduplicated QQ private notifications."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import time

import httpx
from qqbot_agent_sdk import QQApiClient

from qq_private import DATA, read_json, save_private

LOG = logging.getLogger("twitch_monitor")
CONFIG_PATH = DATA / "twitch.json"
STATE_PATH = DATA / "twitch-state.json"
TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"
TWITCH_VALIDATE = "https://id.twitch.tv/oauth2/validate"


def normalize_login(value):
    value = value.strip().rstrip("/")
    for prefix in ("https://www.twitch.tv/", "https://twitch.tv/", "www.twitch.tv/", "twitch.tv/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.split("/", 1)[0].split("?", 1)[0].strip().lower()
    if not value or not all(ch.isalnum() or ch == "_" for ch in value):
        raise ValueError(f"无法识别 Twitch 频道：{value!r}")
    return value


def initialize():
    if CONFIG_PATH.exists():
        print("Twitch 配置已存在。修改前请先备份 data/twitch.json。")
        return
    client_id = input("Twitch Client ID: ").strip()
    client_secret = getpass.getpass("Twitch Client Secret（输入不显示）: ").strip()
    raw_channels = input("Twitch 频道名或链接（多个用英文逗号分隔）: ")
    channels = list(dict.fromkeys(normalize_login(item) for item in raw_channels.split(",") if item.strip()))
    if not client_id or not client_secret or not channels:
        raise ValueError("Client ID、Client Secret 和至少一个频道均为必填。")
    save_private(CONFIG_PATH, {
        "client_id": client_id,
        "client_secret": client_secret,
        "channels": channels,
        "poll_seconds": 60,
    })
    print("Twitch 配置已保存。")


class TwitchClient:
    def __init__(self, http, client_id, client_secret):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expires_at = 0.0
        self.token_validated_at = 0.0

    async def ensure_token(self, force=False):
        if not force and self.token and time.time() < self.token_expires_at - 120:
            if time.time() - self.token_validated_at < 3600:
                return self.token
            response = await self.http.get(
                TWITCH_VALIDATE, headers={"Authorization": f"OAuth {self.token}"},
            )
            if response.status_code != 401:
                response.raise_for_status()
                if response.json().get("client_id") != self.client_id:
                    raise RuntimeError("Twitch 令牌校验返回了不匹配的 Client ID")
                self.token_validated_at = time.time()
                return self.token
        response = await self.http.post(TWITCH_TOKEN, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        })
        response.raise_for_status()
        data = response.json()
        self.token = data["access_token"]
        self.token_expires_at = time.time() + int(data.get("expires_in", 3600))
        self.token_validated_at = time.time()
        return self.token

    async def streams(self, logins):
        found = {}
        for start in range(0, len(logins), 100):
            chunk = logins[start:start + 100]
            response = None
            for attempt in range(2):
                token = await self.ensure_token(force=attempt == 1)
                response = await self.http.get(
                    f"{TWITCH_API}/streams",
                    params=[("user_login", login) for login in chunk],
                    headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"},
                )
                if response.status_code != 401:
                    break
            response.raise_for_status()
            for stream in response.json().get("data", []):
                found[stream["user_login"].lower()] = stream
        return found


class TwitchMonitor:
    def __init__(self, twitch, notifier, owner, channels, state_path=STATE_PATH, now=time.time):
        self.twitch = twitch
        self.notifier = notifier
        self.owner = owner
        self.channels = channels
        self.state_path = state_path
        self.now = now
        self.state = read_json(state_path) if state_path.exists() else {"channels": {}}

    def message(self, stream, initial):
        lead = "🟢 监测启动时发现已开播" if initial else "🟢 主播已开播"
        started = stream.get("started_at")
        if started:
            try:
                parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
                started = parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M（北京时间）")
            except ValueError:
                pass
        else:
            started = "未知"
        return "\n".join(filter(None, [
            f"{lead} · Twitch",
            f"主播：{stream.get('user_name') or stream['user_login']}",
            f"标题：{stream.get('title') or '未提供'}",
            f"分类：{stream.get('game_name') or '未分类'}",
            f"开始时间：{started}",
            f"观看人数：{stream.get('viewer_count', '未知')}",
            f"https://www.twitch.tv/{stream['user_login']}",
        ]))

    async def poll_once(self):
        live = await self.twitch.streams(self.channels)
        changed = False
        current_time = self.now()
        states = self.state.setdefault("channels", {})
        for login in self.channels:
            previous = states.get(login)
            stream = live.get(login)
            if stream is None:
                if previous is None or previous.get("online"):
                    states[login] = {
                        "online": False,
                        "last_seen_at": datetime.now(timezone.utc).isoformat(),
                        "notified_stream_id": previous.get("notified_stream_id") if previous else None,
                    }
                    changed = True
                continue
            stream_id = str(stream["id"])
            initial = previous is None
            record = dict(previous or {})
            record.update({
                "online": True,
                "stream_id": stream_id,
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            })
            needs_notification = record.get("notified_stream_id") != stream_id
            if needs_notification and current_time >= float(record.get("next_notify_at", 0)):
                try:
                    await self.notifier.send_text(
                        "c2c", self.owner, self.message(stream, initial),
                        markdown=False, retries=1,
                    )
                    record["notified_stream_id"] = stream_id
                    record["notify_attempts"] = 0
                    record.pop("next_notify_at", None)
                    LOG.info("已发送 Twitch 开播通知：%s (%s)", login, stream_id)
                except Exception as exc:
                    attempts = int(record.get("notify_attempts", 0)) + 1
                    delay = (60, 300, 900, 3600)[min(attempts - 1, 3)]
                    record["notify_attempts"] = attempts
                    record["next_notify_at"] = current_time + delay
                    LOG.error("发送 Twitch 通知失败，%s 秒后再试：%s", delay, exc)
            if record != previous:
                states[login] = record
                changed = True
        if changed:
            save_private(self.state_path, self.state)
        return live


async def run_once_or_forever(once=False):
    twitch_config = read_json(CONFIG_PATH)
    qq_config = read_json(DATA / "config.json")
    owner = read_json(DATA / "owner.json")["user_openid"]
    channels = [normalize_login(value) for value in twitch_config["channels"]]
    poll_seconds = max(30, int(twitch_config.get("poll_seconds", 60)))
    async with httpx.AsyncClient(timeout=20) as http:
        twitch = TwitchClient(http, twitch_config["client_id"], twitch_config["client_secret"])
        notifier = QQApiClient(qq_config["app_id"], qq_config["client_secret"])
        notifier.setup(http)
        monitor = TwitchMonitor(twitch, notifier, owner, channels)
        while True:
            try:
                live = await monitor.poll_once()
                LOG.info("Twitch 查询成功：监测 %d 个频道，在线 %d 个。", len(channels), len(live))
            except Exception as exc:
                LOG.error("Twitch 查询失败，保留原状态：%s", exc)
                if once:
                    raise
            if once:
                return
            await asyncio.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description="Twitch 开播监测")
    parser.add_argument("command", choices=("init", "check", "run"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("qqbot_agent_sdk").setLevel(logging.CRITICAL)
    try:
        if args.command == "init":
            initialize()
        else:
            asyncio.run(run_once_or_forever(once=args.command == "check"))
    except KeyboardInterrupt:
        print("已停止。")
    except FileNotFoundError as exc:
        LOG.error("缺少配置文件：%s", exc.filename)
        raise SystemExit(1)
    except Exception as exc:
        LOG.error("%s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
