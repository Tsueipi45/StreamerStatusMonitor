import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

from twitch_monitor import TwitchClient, TwitchMonitor, normalize_login


def live(stream_id="s1", login="alice"):
    return {
        "id": stream_id,
        "user_login": login,
        "user_name": login.title(),
        "title": "A live stream",
        "game_name": "Game",
        "viewer_count": 12,
        "started_at": "2026-09-07T10:00:00Z",
    }


class TwitchClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_and_batched_stream_request(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.host == "id.twitch.tv":
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            self.assertEqual(request.headers["client-id"], "cid")
            self.assertEqual(request.headers["authorization"], "Bearer token")
            return httpx.Response(200, json={"data": [live()]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = TwitchClient(http, "cid", "secret")
            result = await client.streams(["alice", "bob"])
        self.assertEqual(set(result), {"alice"})
        self.assertEqual(requests[1].url.params.get_list("user_login"), ["alice", "bob"])

    async def test_unauthorized_refreshes_token_once(self):
        token_count = 0

        def handle(request):
            nonlocal token_count
            if request.url.host == "id.twitch.tv":
                token_count += 1
                return httpx.Response(200, json={"access_token": f"token-{token_count}", "expires_in": 3600})
            if request.headers["authorization"] == "Bearer token-1":
                return httpx.Response(401, json={"message": "invalid token"})
            return httpx.Response(200, json={"data": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            await TwitchClient(http, "cid", "secret").streams(["alice"])
        self.assertEqual(token_count, 2)

    async def test_existing_token_is_validated_hourly(self):
        hosts = []

        def handle(request):
            hosts.append(request.url.path)
            if request.url.path == "/oauth2/validate":
                self.assertEqual(request.headers["authorization"], "OAuth existing")
                return httpx.Response(200, json={"client_id": "cid", "expires_in": 3000})
            return httpx.Response(200, json={"data": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = TwitchClient(http, "cid", "secret")
            client.token = "existing"
            client.token_expires_at = 99999999999
            client.token_validated_at = 0
            await client.streams(["alice"])
        self.assertEqual(hosts, ["/oauth2/validate", "/helix/streams"])


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.twitch = AsyncMock()
        self.notifier = AsyncMock()
        self.monitor = TwitchMonitor(
            self.twitch, self.notifier, "owner", ["alice"], self.state_path, now=lambda: 1000,
        )

    def tearDown(self):
        self.tmp.cleanup()

    async def test_first_offline_does_not_notify(self):
        self.twitch.streams.return_value = {}
        await self.monitor.poll_once()
        self.notifier.send_text.assert_not_called()
        state = json.loads(self.state_path.read_text())
        self.assertFalse(state["channels"]["alice"]["online"])

    async def test_first_online_notifies_once(self):
        self.twitch.streams.return_value = {"alice": live()}
        await self.monitor.poll_once()
        await self.monitor.poll_once()
        self.notifier.send_text.assert_awaited_once()
        args = self.notifier.send_text.call_args.args
        self.assertEqual(args[:2], ("c2c", "owner"))
        self.assertIn("监测启动时发现已开播", args[2])
        self.assertIn("2026-09-07 18:00（北京时间）", args[2])

    async def test_offline_then_new_stream_notifies_again(self):
        self.twitch.streams.return_value = {"alice": live("s1")}
        await self.monitor.poll_once()
        self.twitch.streams.return_value = {}
        await self.monitor.poll_once()
        self.twitch.streams.return_value = {"alice": live("s2")}
        await self.monitor.poll_once()
        self.assertEqual(self.notifier.send_text.await_count, 2)

    async def test_notification_failure_is_backed_off_and_retried(self):
        self.twitch.streams.return_value = {"alice": live()}
        self.notifier.send_text.side_effect = [RuntimeError("no permission"), {"id": "ok"}]
        await self.monitor.poll_once()
        await self.monitor.poll_once()
        self.assertEqual(self.notifier.send_text.await_count, 1)
        self.monitor.now = lambda: 1060
        await self.monitor.poll_once()
        self.assertEqual(self.notifier.send_text.await_count, 2)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["channels"]["alice"]["notified_stream_id"], "s1")

    def test_channel_normalization(self):
        self.assertEqual(normalize_login("https://www.twitch.tv/Some_Name?x=1"), "some_name")
        with self.assertRaises(ValueError):
            normalize_login("bad name")


if __name__ == "__main__":
    unittest.main()
