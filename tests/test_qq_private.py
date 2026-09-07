import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx
from qqbot_agent_sdk import QQApiClient

from qq_private import PrivateProbe, save_private


def event(user="owner", message_id="m1", content="测试"):
    return {"id": message_id, "content": content,
            "author": {"user_openid": user}, "timestamp": "2026-09-07T16:00:00+08:00"}


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.api = AsyncMock()
        self.api.next_msg_seq = lambda: 42
        self.probe = PrivateProbe(self.api, "123", self.data, delay=0)

    async def asyncTearDown(self):
        await self.probe.close()
        self.tmp.cleanup()

    async def bind(self):
        await self.probe.on_message("C2C_MESSAGE_CREATE", event(
            content="绑定 " + self.probe.pair_code))

    async def test_binding_requires_code_and_locks_recipient(self):
        await self.probe.on_message("C2C_MESSAGE_CREATE", event("stranger"))
        self.assertIsNone(self.probe.owner)
        self.api.send_text.assert_not_called()
        await self.bind()
        self.assertEqual(self.probe.owner, "owner")
        self.assertEqual(PrivateProbe(self.api, "123", self.data).owner, "owner")
        self.api.send_text.reset_mock()
        await self.probe.on_message("C2C_MESSAGE_CREATE", event("stranger", "m2", "主动测试"))
        self.api.send_text.assert_not_called()
        self.api.request.assert_not_called()

    async def test_expired_pairing_is_ignored(self):
        self.probe.pair_expires = 0
        await self.bind()
        self.assertIsNone(self.probe.owner)

    async def test_passive_reply_uses_message_id_and_deduplicates(self):
        await self.bind()
        self.api.send_text.reset_mock()
        await self.probe.on_message("C2C_MESSAGE_CREATE", event(message_id="m2"))
        await self.probe.on_message("C2C_MESSAGE_CREATE", event(message_id="m2"))
        self.api.send_text.assert_awaited_once()
        self.assertEqual(self.api.send_text.call_args.kwargs["reply_to"], "m2")

    async def test_group_events_are_ignored(self):
        await self.bind()
        self.api.send_text.reset_mock()
        raw = {"id": "g1", "content": "主动测试", "group_openid": "group",
               "author": {"member_openid": "owner"}}
        await self.probe.on_message("GROUP_AT_MESSAGE_CREATE", raw)
        self.api.send_text.assert_not_called()
        self.api.request.assert_not_called()

    async def test_active_send_wire_body_has_no_reply_credentials(self):
        await self.bind()
        captured = []

        def handle(request):
            captured.append(request)
            return httpx.Response(200, json={"id": "sent-1"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            real_api = QQApiClient("123", "not-a-real-secret")
            real_api.setup(http)
            real_api.ensure_token = AsyncMock(return_value="test-token")
            self.probe.api = real_api
            await self.probe.send_active()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url.path, "/v2/users/owner/messages")
        body = json.loads(captured[0].content)
        self.assertEqual(body["msg_type"], 0)
        for key in ("msg_id", "event_id", "message_reference"):
            self.assertNotIn(key, body)
        result = json.loads((self.data / "last_active_test.json").read_text())
        self.assertTrue(result["api_accepted"])
        self.assertFalse(result["delivery_confirmed"])

    async def test_active_failure_does_not_fall_back_to_reply(self):
        await self.bind()
        self.api.send_text.reset_mock()
        self.api.request.side_effect = RuntimeError("主动消息无权限")
        with self.assertRaises(RuntimeError):
            await self.probe.send_active()
        self.api.request.assert_awaited_once()
        self.api.send_text.assert_not_called()
        result = json.loads((self.data / "last_active_test.json").read_text())
        self.assertFalse(result["api_accepted"])

    async def test_application_error_is_not_success(self):
        await self.bind()
        self.api.request.return_value = {"code": 40034102, "message": "no permission"}
        with self.assertRaises(RuntimeError):
            await self.probe.send_active()

    async def test_repeat_active_commands_only_queue_once(self):
        await self.bind()
        self.api.request.return_value = {"id": "sent-1"}
        await self.probe.on_message("C2C_MESSAGE_CREATE", event(message_id="m2", content="主动测试"))
        await self.probe.on_message("C2C_MESSAGE_CREATE", event(message_id="m3", content="主动测试"))
        await self.probe.pending
        self.api.request.assert_awaited_once()

    async def test_changed_app_requires_explicit_rebinding(self):
        await self.bind()
        with self.assertRaises(ValueError):
            PrivateProbe(self.api, "456", self.data)

    async def test_unbound_active_send_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.probe.send_active()
        self.api.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
