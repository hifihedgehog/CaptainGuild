"""The real sink and the real library, with only the network replaced."""
import asyncio
import io
from unittest import mock

import aiohttp
import discord

from captainguild.core import logsink
from captainguild.core.logsink import LogSink
from tests.gateway import FakeHTTPResponse, GatewayTest


class SinkTest(GatewayTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.retry = mock.patch.object(logsink, "RETRY_SECONDS", 0)
        self.retry.start()
        self.sink = self.bot.logs
        self.channel_id = int(self.log_channel["id"])
        self.sends = 0
        self.failures: list[Exception] = []
        real_webhook, real_http = self.net.webhook, self.net.http

        async def webhook(adapter, route, session=None, **kwargs):
            if route.path.startswith("/webhooks/") and route.method == "POST":
                self.sends += 1
                if self.failures:
                    # a failure from Discord tries the channel next, and a network failure goes straight back to waiting
                    raise self.failures[0] if isinstance(self.failures[0], discord.HTTPException) else self.failures.pop(0)
            return await real_webhook(adapter, route, session, **kwargs)

        async def http(route, **kwargs):
            if route.path == "/channels/{channel_id}/messages" and route.method == "POST":
                self.sends += 1
                if self.failures:
                    raise self.failures.pop(0)
            return await real_http(route, **kwargs)

        self.net.webhook = webhook
        self.bot.http.request = http

    async def asyncTearDown(self) -> None:
        self.retry.stop()
        await super().asyncTearDown()

    def titles(self) -> list[str]:
        return [embed["title"] for message in self.net.posted for embed in message.get("embeds", [])]

    def post(self, count: int, first: int = 0) -> None:
        for number in range(first, first + count):
            self.sink.post(self.channel_id, {"title": f"entry {number}"})

    async def drained(self) -> None:
        for _ in range(5000):
            if not [worker for worker in self.sink._workers.values() if not worker.done()]:
                return
            await asyncio.sleep(0.001)
        self.fail("The queue never drained")

    async def test_a_batch_that_fails_goes_back_and_arrives_in_order(self):
        self.post(25)
        self.failures = [discord.DiscordServerError(FakeHTTPResponse(503), "down")]  # the webhook and then the channel both fail once
        await self.drained()
        self.assertEqual(self.titles(), [f"entry {number}" for number in range(25)])

    async def test_a_network_failure_waits_too(self):
        self.post(3)
        self.failures = [aiohttp.ClientOSError(104, "connection reset")]
        await self.drained()
        self.assertEqual(self.titles(), ["entry 0", "entry 1", "entry 2"])

    async def test_a_rate_limit_with_no_header_waits_too(self):
        self.post(3)
        self.failures = [discord.HTTPException(FakeHTTPResponse(429), "cloudflare")]
        await self.drained()
        self.assertEqual(self.titles(), ["entry 0", "entry 1", "entry 2"])

    async def test_a_long_outage_loses_nothing(self):
        self.post(3)
        self.failures = [discord.DiscordServerError(FakeHTTPResponse(503), "down") for _ in range(1100)]  # a growing wait mustn't outgrow what a float can hold
        with self.assertLogs("captainguild.core.logsink", "WARNING"):
            await self.drained()
        self.assertEqual(self.titles(), ["entry 0", "entry 1", "entry 2"])

    async def test_one_entry_discord_refuses_costs_only_itself(self):
        real_webhook = self.net.webhook

        async def webhook(adapter, route, session=None, **kwargs):
            if route.path.startswith("/webhooks/") and route.method == "POST":
                body = kwargs.get("payload") or {}
                if any(embed.get("title") == "entry 3" for embed in body.get("embeds", [])):
                    raise discord.HTTPException(FakeHTTPResponse(400), "Invalid Form Body")
            return await real_webhook(adapter, route, session, **kwargs)

        real_http = self.bot.http.request

        async def http(route, **kwargs):
            if route.path == "/channels/{channel_id}/messages" and route.method == "POST":
                body = kwargs.get("json") or {}
                if any(embed.get("title") == "entry 3" for embed in body.get("embeds", [])):
                    raise discord.HTTPException(FakeHTTPResponse(400), "Invalid Form Body")
            return await real_http(route, **kwargs)

        self.net.webhook = webhook
        self.bot.http.request = http
        self.post(5)
        await self.drained()
        self.assertEqual(self.titles(), ["entry 0", "entry 1", "entry 2", "entry 4"])
        self.assert_no_errors()

    async def test_a_channel_it_cant_post_in_drops_the_batch_and_moves_on(self):
        self.failures = [discord.Forbidden(FakeHTTPResponse(403), "Missing Permissions")]
        self.post(3)
        await self.drained()
        self.post(1, first=3)
        await self.drained()
        self.assertEqual(self.titles(), ["entry 3"])
        self.assert_no_errors()

    async def test_a_file_survives_a_retry(self):
        self.sink.post(self.channel_id, {"title": "with a file"}, file=discord.File(io.BytesIO(b"contents"), filename="a.txt"))
        self.failures = [discord.DiscordServerError(FakeHTTPResponse(503), "down")]
        await self.drained()
        self.assertEqual(self.titles(), ["with a file"])
        attached = self.sink._queues[self.channel_id]
        self.assertEqual(attached, [])

    async def test_a_full_queue_says_how_much_it_skipped(self):
        with mock.patch.object(logsink, "MAX_QUEUE", 5), mock.patch.object(logsink, "FLUSH_SECONDS", 0.01):
            self.post(8)
            await self.drained()
        self.assertEqual(self.titles()[0], "Log entries skipped")
        self.assertEqual(self.titles()[1:], [f"entry {number}" for number in range(3, 8)])
        notice = next(embed for message in self.net.posted for embed in message.get("embeds", []) if embed["title"] == "Log entries skipped")
        self.assertIn("3 oldest", notice["description"])

    async def test_a_long_flood_notes_it_once_a_minute(self):
        with mock.patch.object(logsink, "MAX_QUEUE", 5):
            self.post(8)
            await self.drained()
            self.post(8, first=8)
            await self.drained()
        titles = self.titles()
        self.assertEqual(titles.count("Log entries skipped"), 1)
        self.assertEqual(self.sink._dropped[self.channel_id], 3)  # counted for the next note

    async def test_putting_a_batch_back_keeps_the_limit(self):
        with mock.patch.object(logsink, "MAX_QUEUE", 5):
            queue = [({"title": "newer"}, None)] * 5
            self.sink._put_back(self.channel_id, queue, [({"title": "older"}, None)] * 3)
        self.assertEqual(len(queue), 5)
        self.assertEqual(self.sink._dropped[self.channel_id], 3)

    async def test_stopping_gives_a_backlog_time_and_then_counts_what_is_left(self):
        release = asyncio.Event()
        real_webhook = self.net.webhook

        async def slow(adapter, route, session=None, **kwargs):
            await release.wait()
            return await real_webhook(adapter, route, session, **kwargs)

        self.net.webhook = slow
        self.post(30)
        with mock.patch.object(logsink, "CLOSE_WAIT", 0.05), self.assertLogs("captainguild.core.logsink", "WARNING") as logs:
            await self.sink.close()
        self.assertIn("didn't reach their channels", logs.output[0])
        self.assertTrue(all(worker.done() for worker in self.sink._workers.values()))

    async def test_stopping_stops_retrying(self):
        self.post(3)
        self.failures = [discord.DiscordServerError(FakeHTTPResponse(503), "down") for _ in range(200)]
        with self.assertLogs("captainguild.core.logsink", "WARNING"):
            await self.sink.close()
        self.assertTrue(all(worker.done() for worker in self.sink._workers.values()))
        self.assertLess(self.sends, 20)

    async def test_a_webhook_that_cant_be_made_right_now_isnt_remembered(self):
        channel = self.bot.get_channel(self.channel_id)
        with mock.patch.object(discord.TextChannel, "webhooks", new=mock.AsyncMock(side_effect=discord.DiscordServerError(FakeHTTPResponse(503), "down"))):
            self.assertIsNone(await self.sink._webhook(channel))
        self.assertNotIn(channel.id, self.sink._webhooks)
        with mock.patch.object(discord.TextChannel, "webhooks", new=mock.AsyncMock(side_effect=discord.Forbidden(FakeHTTPResponse(403), "no"))):
            self.assertIsNone(await self.sink._webhook(channel))
        self.assertIn(channel.id, self.sink._webhooks)

    def test_what_counts_as_worth_retrying(self):
        self.assertTrue(logsink._worth_retrying(discord.DiscordServerError(FakeHTTPResponse(502), "bad")))
        self.assertTrue(logsink._worth_retrying(discord.HTTPException(FakeHTTPResponse(429), "slow")))
        self.assertTrue(logsink._worth_retrying(OSError("down")))
        self.assertTrue(logsink._worth_retrying(asyncio.TimeoutError()))
        self.assertFalse(logsink._worth_retrying(discord.Forbidden(FakeHTTPResponse(403), "no")))
        self.assertFalse(logsink._worth_retrying(ValueError("a bug")))

    async def test_queued_bytes_counts_what_waits(self):
        sink = LogSink(self.bot)
        sink._closing = False
        sink._queues[1] = [({}, discord.File(io.BytesIO(b"12345"), filename="a")), ({}, None), ({}, [discord.File(io.BytesIO(b"123"), filename="b"), discord.File(io.BytesIO(b"1"), filename="c")])]
        self.assertEqual(sink.queued_bytes(), 9)
