"""QQ private-message pairing and active notification transport."""

import argparse
import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
import time

import httpx
from qqbot_agent_sdk import EventParser, QQApiClient, QQWebSocket, WSCallbacks

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG = logging.getLogger("qq_private")


def save_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def initialize(replace=False):
    path = DATA / "config.json"
    if path.exists() and not replace:
        print("配置已存在，保留原配置。修改凭据可使用 nano data/config.json。")
        return
    app_id = input("QQ 机器人 AppID: ").strip()
    secret = getpass.getpass("QQ 机器人 AppSecret / ClientSecret（输入不显示）: ").strip()
    if not app_id.isdecimal() or not secret:
        raise ValueError("AppID 应为数字，机器人密钥不能为空。")
    save_private(path, {"app_id": app_id, "client_secret": secret})
    print("已保存服务器本地配置。下一步运行：python qq_private.py listen")


class PrivateProbe:
    def __init__(self, api, app_id, data_dir=DATA, delay=15):
        self.api = api
        self.app_id = app_id
        self.data_dir = data_dir
        self.delay = delay
        self.parser = EventParser()
        self.owner = None
        self.pair_code = secrets.token_hex(8)
        self.pair_expires = time.monotonic() + 600
        self.lock = asyncio.Lock()
        self.seen = OrderedDict()
        self.pending = None
        self.last_probe = float("-inf")
        path = data_dir / "owner.json"
        if path.exists():
            owner = read_json(path)
            if owner.get("app_id") != app_id:
                raise ValueError("已绑定账号属于其他 AppID。确认更换机器人后移走 data/owner.json 再重新绑定。")
            self.owner = owner["user_openid"]

    async def reply(self, event, content):
        return await self.api.send_text(
            "c2c", event.chat_id, content,
            reply_to=event.message_id, markdown=False, retries=1,
        )

    async def on_message(self, event_type, raw):
        event = self.parser.parse(event_type, raw)
        if not event or event.chat_scope != "c2c" or not event.message_id:
            return
        async with self.lock:
            event_key = (event.chat_id, event.message_id)
            if event_key in self.seen:
                return
            self.seen[event_key] = True
            if len(self.seen) > 1024:
                self.seen.popitem(last=False)
            content = event.content.strip()
            if self.owner is None:
                if time.monotonic() > self.pair_expires:
                    return
                expected = "绑定 " + self.pair_code
                if not secrets.compare_digest(content.encode(), expected.encode()):
                    return
                save_private(self.data_dir / "owner.json", {
                    "app_id": self.app_id, "user_openid": event.chat_id,
                })
                self.owner = event.chat_id
                self.pair_code = ""
                LOG.info("已绑定你的 QQ 私聊。")
                await self.reply(event, "绑定成功。发送“测试”验证回复；发送“主动测试”，15 秒后尝试主动通知。")
                return
            if event.chat_id != self.owner:
                return
            if content in ("测试", "/test"):
                await self.reply(event, "私聊回复测试成功。此消息是回复，尚未证明自动开播通知可用。")
                LOG.info("回复请求成功，请在 QQ 确认收到。")
            elif content in ("主动测试", "/active-test"):
                if ((self.pending and not self.pending.done())
                        or time.monotonic() - self.last_probe < 60):
                    return
                await self.reply(event, "将在 15 秒后尝试发送一条主动通知，请留意 QQ。")
                self.last_probe = time.monotonic()
                self.pending = asyncio.create_task(self.delayed_probe())

    async def delayed_probe(self):
        await asyncio.sleep(self.delay)
        try:
            await self.send_active()
        except Exception as exc:
            LOG.error("主动测试失败：%s", exc)

    async def send_active(self):
        if not self.owner:
            raise ValueError("尚未绑定。请先运行 listen 并在 QQ 私聊中发送配对指令。")
        result = {"attempted_at": datetime.now(timezone.utc).isoformat(),
                  "api_accepted": False, "delivery_confirmed": False}
        try:
            # Deliberately omit msg_id, event_id and message_reference. No passive fallback.
            response = await self.api.request(
                "POST", f"/v2/users/{self.owner}/messages",
                {"content": "开播提醒机器人：QQ 主动通知测试。收到此消息说明这一次主动投递已到达。",
                 "msg_type": 0, "msg_seq": self.api.next_msg_seq()},
            )
            if response.get("code") not in (None, 0) or not response.get("id"):
                raise RuntimeError(f"QQ 未返回成功消息 ID：{response}")
            result["api_accepted"] = True
            LOG.info("主动发送 API 已接受请求。请在 QQ 确认收到；这不代表无限额度或长期可用。")
        finally:
            save_private(self.data_dir / "last_active_test.json", result)

    async def close(self):
        if self.pending and not self.pending.done():
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)


async def run(command, config):
    async with httpx.AsyncClient(timeout=20) as http:
        api = QQApiClient(config["app_id"], config["client_secret"])
        api.setup(http)
        probe = PrivateProbe(api, config["app_id"])
        await api.ensure_token()
        if command == "send-test":
            await probe.send_active()
            return
        loop = asyncio.get_running_loop()
        stopped = asyncio.Event()
        fatal_errors = []
        session = [None, None]

        def fatal(code, message):
            fatal_errors.append(f"QQ 网关错误 {code}: {message}")
            loop.call_soon_threadsafe(stopped.set)

        def set_session(sid, seq):
            session[:] = [sid, seq]

        async def receive(event_type, raw):
            try:
                await probe.on_message(event_type, raw)
            except Exception as exc:
                LOG.error("处理私聊消息失败：%s", exc)

        ws = QQWebSocket(WSCallbacks(
            on_message_event=receive,
            on_connected=lambda: LOG.info("QQ 网关已连接。"),
            on_disconnected=lambda: LOG.warning("QQ 网关连接断开，SDK 将按错误类型处理重连。"),
            on_fatal_error=fatal,
            get_token=api.ensure_token_sync,
            get_session=lambda: tuple(session),
            set_session=set_session,
            set_heartbeat_interval=lambda interval: None,
            clear_token=api.clear_token,
            fail_pending=lambda reason: LOG.warning("网关状态：%s", reason),
            get_gateway_url=api.get_gateway_url_sync,
        ))
        gateway = await api.get_gateway_url()
        ws.start(gateway, loop)
        if probe.owner:
            print("已加载私聊绑定。请向机器人发送：测试", flush=True)
        else:
            print(f"请在 10 分钟内，用你的 QQ 私聊机器人发送：绑定 {probe.pair_code}", flush=True)
        print("保持此终端运行；Ctrl+C 停止。这只是通知测试，尚未监听主播。", flush=True)
        try:
            await stopped.wait()
            if fatal_errors:
                raise RuntimeError(fatal_errors[0])
        finally:
            await ws.async_stop()
            await probe.close()


def main():
    parser = argparse.ArgumentParser(description="QQ 私聊通知接入测试")
    parser.add_argument("command", choices=("init", "replace-credentials", "listen", "send-test"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Keep SDK diagnostic payloads out of logs; surface exceptions through our own logger.
    logging.getLogger("qqbot_agent_sdk").setLevel(logging.CRITICAL)
    config = {}
    try:
        if args.command == "init":
            initialize()
        elif args.command == "replace-credentials":
            initialize(replace=True)
        else:
            config = read_json(DATA / "config.json")
            asyncio.run(run(args.command, config))
    except KeyboardInterrupt:
        print("已停止。")
    except FileNotFoundError:
        print("缺少配置，请先运行：python qq_private.py init")
        raise SystemExit(1)
    except Exception as exc:
        message = str(exc)
        secret = config.get("client_secret")
        if secret:
            message = message.replace(secret, "[REDACTED]")
        LOG.error("%s", message)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
