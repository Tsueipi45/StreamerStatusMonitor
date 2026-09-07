import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

from youtube_monitor import YouTubeClient, YouTubeMonitor, fetch_feed, live_state


FEED = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <title>Channel Name</title>
  <entry><yt:videoId>video1</yt:videoId><title>Scheduled</title>
    <updated>2026-09-07T10:00:00+00:00</updated></entry>
</feed>'''


def feed(*video_ids):
    entries = "".join(
        f"<entry><yt:videoId>{video_id}</yt:videoId><title>{video_id}</title>"
        f"<updated>2026-09-07T10:00:00+00:00</updated></entry>"
        for video_id in video_ids
    )
    return (f'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
<title>Channel Name</title>{entries}</feed>''').encode()


def video(kind="live", video_id="video1"):
    details = {}
    if kind in ("live", "complete"):
        details["actualStartTime"] = "2026-09-07T10:00:00Z"
    if kind == "complete":
        details["actualEndTime"] = "2026-09-07T11:00:00Z"
    if kind == "upcoming":
        details["scheduledStartTime"] = "2026-09-08T10:00:00Z"
    return {"id": video_id, "snippet": {"title": "Live title"},
            "liveStreamingDetails": details}


class FeedAndClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_parsing(self):
        def handle(request):
            return httpx.Response(200, content=FEED)
        channel = {"channel_id": "UC123", "name": "Configured Name"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            result = await fetch_feed(http, channel)
        self.assertEqual(result[0]["video_id"], "video1")
        self.assertEqual(result[0]["channel_name"], "Configured Name")

    async def test_video_lookup_batches_and_does_not_log_key_in_error(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"items": [video()]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            found = await YouTubeClient(http, "secret-key").videos(["video1"])
        self.assertEqual(set(found), {"video1"})
        self.assertEqual(requests[0].url.params["part"], "snippet,liveStreamingDetails,status")

    def test_live_state(self):
        self.assertEqual(live_state(video("normal")), "normal")
        self.assertEqual(live_state(video("upcoming")), "upcoming")
        self.assertEqual(live_state(video("live")), "live")
        self.assertEqual(live_state(video("complete")), "complete")


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "youtube-state.json"
        self.youtube = AsyncMock()
        self.notifier = AsyncMock()
        self.channel = {"channel_id": "UC123", "name": "Channel Name"}
        self.monitor = YouTubeMonitor(
            self.youtube, self.notifier, "owner", [self.channel], self.state_path,
            now=lambda: 1000,
        )
        self.feed = FEED
        self.transport = httpx.MockTransport(lambda request: httpx.Response(200, content=self.feed))

    def tearDown(self):
        self.tmp.cleanup()

    async def call(self):
        async with httpx.AsyncClient(transport=self.transport) as http:
            return await self.monitor.poll_once(http)

    async def test_upcoming_is_not_notified_then_live_is(self):
        self.youtube.videos.return_value = {"video1": video("upcoming")}
        relevant = await self.call()
        self.assertEqual(relevant, {"video1": "upcoming"})
        self.notifier.send_text.assert_not_called()
        self.youtube.videos.return_value = {"video1": video("live")}
        await self.call()
        self.notifier.send_text.assert_awaited_once()
        message = self.notifier.send_text.call_args.args[2]
        self.assertIn("Channel Name", message)
        self.assertIn("2026-09-07 18:00（北京时间）", message)

    async def test_same_live_video_notified_once(self):
        self.youtube.videos.return_value = {"video1": video("live")}
        await self.call()
        await self.call()
        self.notifier.send_text.assert_awaited_once()

    async def test_completed_video_stops_being_polled(self):
        self.youtube.videos.return_value = {"video1": video("complete")}
        await self.call()
        await self.call()
        self.youtube.videos.assert_awaited_once()
        self.notifier.send_text.assert_not_called()

    async def test_notification_failure_retries_after_backoff(self):
        self.youtube.videos.return_value = {"video1": video("live")}
        self.notifier.send_text.side_effect = [RuntimeError("blocked"), {"id": "ok"}]
        await self.call()
        await self.call()
        self.assertEqual(self.notifier.send_text.await_count, 1)
        self.monitor.now = lambda: 1060
        await self.call()
        self.assertEqual(self.notifier.send_text.await_count, 2)
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["videos"]["video1"]["notified_video_id"], "video1")

    async def test_initial_feed_is_baselined_without_old_video_spam(self):
        self.youtube.videos.return_value = {"video1": video("normal")}
        await self.call()
        self.notifier.send_text.assert_not_called()
        state = json.loads(self.state_path.read_text())
        self.assertTrue(state["videos"]["video1"]["video_update_notified"])

    async def test_new_regular_video_sends_one_update(self):
        self.youtube.videos.return_value = {"video1": video("normal")}
        await self.call()
        self.feed = feed("video2", "video1")
        self.youtube.videos.return_value = {"video2": video("normal", "video2")}
        await self.call()
        await self.call()
        self.notifier.send_text.assert_awaited_once()
        self.assertIn("频道发布新影片", self.notifier.send_text.call_args.args[2])
        self.assertIn("video2", self.notifier.send_text.call_args.args[2])

    async def test_new_upcoming_notifies_then_live_notifies(self):
        self.youtube.videos.return_value = {"video1": video("normal")}
        await self.call()
        self.feed = feed("video2", "video1")
        self.youtube.videos.return_value = {"video2": video("upcoming", "video2")}
        await self.call()
        self.assertIn("新增直播预约", self.notifier.send_text.call_args.args[2])
        self.youtube.videos.return_value = {"video2": video("live", "video2")}
        await self.call()
        self.assertEqual(self.notifier.send_text.await_count, 2)
        self.assertIn("主播已开播", self.notifier.send_text.call_args.args[2])

    async def test_old_state_migration_does_not_notify_existing_videos(self):
        self.state_path.write_text(json.dumps({"videos": {
            "video1": {"video_id": "video1", "channel_id": "UC123",
                       "channel_name": "Channel Name", "feed_updated": "old", "kind": "normal"}
        }}))
        monitor = YouTubeMonitor(
            self.youtube, self.notifier, "owner", [self.channel], self.state_path,
            now=lambda: 1000,
        )
        self.youtube.videos.return_value = {"video1": video("normal")}
        async with httpx.AsyncClient(transport=self.transport) as http:
            await monitor.poll_once(http)
        self.notifier.send_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
