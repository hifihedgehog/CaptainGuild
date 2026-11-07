"""Sends log entries to channels through a webhook, batched.

A busy server produces far more log entries than a channel can take one message at a time. Entries queue per
channel and go out as one message holding up to ten embeds, a few seconds apart at most. If the bot can't make a
webhook, it posts as itself. Anything with a file goes on its own, since a file can't be added to a batch cleanly.

When Discord or the network is down, a batch goes back in the queue and waits. The queue lives in memory, so what hasn't
gone out when the bot stops doesn't reach the channel. A stop logs how many that was. Entries from the logging feature are also
on the Log search page. A few other features post here without storing a copy, and those are gone with the queue.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import aiohttp
import discord

from .. import brand
from .messages import embed_size

log = logging.getLogger(__name__)

BATCH_EMBEDS = 10
BATCH_CHARS = 5500
FLUSH_SECONDS = 2.0
MAX_QUEUE = 2000
RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 300.0
SKIP_NOTE_SECONDS = 60.0
CLOSE_WAIT = 45.0  # the service manager gives a stop 90 seconds, and the database still has to close after this


class ChannelNotBack(Exception):
    """The channel isn't in the cache while the bot refills it after a READY, which says nothing about whether it still exists."""


def _worth_retrying(error: Exception) -> bool:
    if isinstance(error, ChannelNotBack):
        return True
    if isinstance(error, discord.HTTPException):
        return error.status >= 500 or error.status == 429
    return isinstance(error, (aiohttp.ClientError, OSError, asyncio.TimeoutError))


def _file_size(attached: Any) -> int:
    getbuffer = getattr(attached.fp, "getbuffer", None)
    return getbuffer().nbytes if getbuffer is not None else 0


class LogSink:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._queues: dict[int, list[tuple[dict, Any]]] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._webhooks: dict[int, discord.Webhook | None] = {}
        self._dropped: dict[int, int] = {}
        self._noted: dict[int, float] = {}
        self._closing = False
        self._refused = 0
        self._gone_noted: set[int] = set()
        self.refused_listener: Callable[[int], None] | None = None  # told the channel when Discord answers 403, so staff can hear about it

    def post(self, channel_id: int | None, embed: dict | discord.Embed, *, file: Any = None) -> None:
        """Queue one entry and return at once. A channel of None means this event isn't logged, so it's dropped. `file` is one discord.File or a list of them."""
        if not channel_id:
            return
        if self._closing:
            self._refused += 1
            return
        data = embed.to_dict() if isinstance(embed, discord.Embed) else embed
        queue = self._queues.setdefault(channel_id, [])
        if len(queue) >= MAX_QUEUE:
            del queue[0]  # the oldest goes first, so a flood can't grow memory without limit
            self._dropped[channel_id] = self._dropped.get(channel_id, 0) + 1
        queue.append((data, file))
        worker = self._workers.get(channel_id)
        if worker is None or worker.done():
            self._workers[channel_id] = asyncio.ensure_future(self._drain(channel_id))

    def queued_files(self) -> int:
        """How many waiting entries hold files, which sit in memory until they're sent. A feature can hold back while a flood drains."""
        return sum(1 for queue in self._queues.values() for _, attached in queue if attached is not None)

    def queued_bytes(self) -> int:
        """How much the waiting files hold."""
        total = 0
        for queue in self._queues.values():
            for _, attached in queue:
                if attached is not None:
                    total += sum(_file_size(one) for one in (attached if isinstance(attached, list) else [attached]))
        return total

    async def close(self) -> None:
        self._closing = True
        workers = [w for w in self._workers.values() if not w.done()]
        if workers:
            await asyncio.wait(workers, timeout=CLOSE_WAIT)
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        left = sum(len(queue) for queue in self._queues.values()) + self._refused
        if left:
            log.warning("%d log entries didn't reach their channels before the bot stopped. The Log search page has those from the logging feature.", left)

    def _next_batch(self, queue: list[tuple[dict, Any]]) -> list[tuple[dict, Any]]:
        items: list[tuple[dict, Any]] = []
        size = 0
        while queue and len(items) < BATCH_EMBEDS:
            embed, attached = queue[0]
            try:
                entry = embed_size(embed)
            except (TypeError, AttributeError, KeyError):  # left in place, it would stop this channel's queue for good
                log.warning("Dropped a log entry that can't be measured: %r", embed)
                del queue[0]
                continue
            if items and (attached is not None or size + entry > BATCH_CHARS):
                break
            items.append(queue.pop(0))
            size += entry
            if attached is not None:
                break
        return items

    def _put_back(self, channel_id: int, queue: list[tuple[dict, Any]], items: list[tuple[dict, Any]]) -> None:
        queue[:0] = items
        over = len(queue) - MAX_QUEUE
        if over > 0:
            del queue[:over]
            self._dropped[channel_id] = self._dropped.get(channel_id, 0) + over

    async def _drain(self, channel_id: int) -> None:
        queue = self._queues[channel_id]
        failures = 0
        while queue:
            await asyncio.sleep(0 if self._closing else FLUSH_SECONDS)
            dropped = self._dropped.get(channel_id, 0)
            if dropped and time.monotonic() - self._noted.get(channel_id, -SKIP_NOTE_SECONDS) >= SKIP_NOTE_SECONDS:
                del self._dropped[channel_id]
                self._noted[channel_id] = time.monotonic()
                log.warning("The log queue for channel %s was full, so its %d oldest entries never reached it. The Log search page has those from the logging feature.", channel_id, dropped)
                note = {"title": "Log entries skipped", "description": f"This channel fell behind, so its {dropped} oldest waiting entries weren't posted. The Log search page has those from the logging feature.", "color": 0xF48942}
                queue.insert(0, (note, None))
            items = self._next_batch(queue)
            if not items:
                continue
            batch = [embed for embed, _ in items]
            try:
                await self._send(channel_id, batch, items[-1][1])
                failures = 0
            except asyncio.CancelledError:
                self._put_back(channel_id, queue, items)  # off the queue and not delivered, and close() counts what's on it
                raise
            except Exception as error:
                if _worth_retrying(error):
                    self._put_back(channel_id, queue, items)
                    if self._closing:
                        return  # close() says how many are left
                    failures += 1
                    log.warning("Could not post log entries to channel %s, so they wait: %s", channel_id, error)
                    await asyncio.sleep(min(MAX_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(failures, 10)))
                elif isinstance(error, discord.HTTPException) and error.status == 400 and len(batch) > 1:
                    await self._send_each(channel_id, batch)  # one entry Discord won't take shouldn't cost the others
                elif isinstance(error, discord.HTTPException):
                    log.warning("Could not post %d log entries to channel %s: %s", len(batch), channel_id, error)
                    if error.status == 403 and self.refused_listener is not None:
                        self.refused_listener(channel_id)
                else:
                    log.exception("Could not post %d log entries to channel %s", len(batch), channel_id)

    async def _send_each(self, channel_id: int, batch: list[dict]) -> None:
        for embed in batch:
            try:
                await self._send(channel_id, [embed], None)
            except Exception as error:
                log.warning("Could not post a log entry to channel %s: %s", channel_id, error)

    async def _send(self, channel_id: int, embeds: list[dict], file: Any) -> None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            settled = getattr(self.bot, "members_settled", None)
            if settled is not None and not settled.is_set():
                raise ChannelNotBack(f"Channel {channel_id} isn't back yet")
            if channel_id not in self._gone_noted:
                self._gone_noted.add(channel_id)
                log.warning("Log channel %s no longer exists, so entries for it are dropped.", channel_id)
            return
        objects = [discord.Embed.from_dict(e) for e in embeds]
        kwargs: dict[str, Any] = {"embeds": objects, "allowed_mentions": discord.AllowedMentions.none()}
        files = file if isinstance(file, list) else ([file] if file is not None else [])
        if files:
            kwargs["files"] = files
        for attached in files:  # an earlier try read them to the end, and a retry would upload nothing
            attached.reset()
        webhook = await self._webhook(channel)
        if webhook is not None:
            try:
                if isinstance(channel, discord.Thread):
                    kwargs["thread"] = channel
                await webhook.send(**kwargs)
                return
            except discord.NotFound:
                self._webhooks.pop(channel.parent_id if isinstance(channel, discord.Thread) else channel_id, None)  # someone deleted it, so make a new one next time
            except discord.HTTPException:
                log.warning("Webhook send failed in %s, posting as the bot", channel_id)
            kwargs.pop("thread", None)
            for attached in files:  # the failed send read them to the end, and a retry would upload nothing
                attached.reset()
        await channel.send(**kwargs)

    async def _webhook(self, channel: Any) -> discord.Webhook | None:
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        if parent is None:
            return None
        if parent.id in self._webhooks:
            return self._webhooks[parent.id]
        webhook = None
        try:
            for existing in await parent.webhooks():
                if existing.user and self.bot.user and existing.user.id == self.bot.user.id and existing.token:
                    webhook = existing
                    break
            if webhook is None:
                webhook = await parent.create_webhook(name=f"{brand.NAME} Logging", reason="For log entries")
        except discord.HTTPException as error:
            if _worth_retrying(error):
                return None  # it may work next time, so this isn't remembered
            webhook = None
        self._webhooks[parent.id] = webhook
        return webhook
