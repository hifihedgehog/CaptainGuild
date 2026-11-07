"""Messages kept at the bottom of a channel, and saved templates for them.

A new message pushes the sticky up, so the bot posts it again and removes the old copy. A repost waits for a pause in the channel and
never comes sooner than MIN_GAP after the last one, so a busy channel can't run into Discord's limits. Another bot's sticky in the same
channel answers each post with its own, so reposts set off only by bots wait longer each time and then stop until a person talks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, messages
from ..core.durations import Duration, humanize, parse_duration
from ..core.module import Module, requires
from ..core.ops import OpsError
from ..core.tagscript import Scope, render_message
from ..core.validate import Invalid, ValidationError, integer
from ..web.api import ApiError

log = logging.getLogger(__name__)

EXPIRE = "sticky.expire"
MIN_SECONDS = 60
MAX_SECONDS = 365 * 86400
DEFAULT_SECONDS = 86400
MAX_STICKIES = 200
MAX_TEMPLATES = 200
NAME_MIN = 3
NAME_MAX = 100
CHANNEL_AT_END = re.compile(r"\s*<#(\d+)>\s*$")
CHANNEL_AT_START = re.compile(r"\s*<#(\d+)>\s*")


@dataclass
class _Live:
    """What the repost timing needs for one channel, kept in memory because every message in the server checks it."""
    guild_id: int
    message_id: int | None = None
    first_seen: float | None = None
    last_seen: float = 0.0
    last_post: float = 0.0
    not_before: float = 0.0
    posting: bool = False
    sleeping: bool = False
    bot_reposts: int = 0  # reposts in a row that only bots set off
    person_waiting: bool = False
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _seconds_field(value: Any) -> int:
    try:
        return integer(MIN_SECONDS, MAX_SECONDS)(value)
    except Invalid as error:
        raise ValidationError({"seconds": "Pick a duration between one minute and 365 days."}) from error


def _name_field(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if len(name) < NAME_MIN or len(name) > NAME_MAX:
        raise ValidationError({"name": f"A template name needs {NAME_MIN} to {NAME_MAX} characters."})
    return name


class ChannelTaken(Exception):
    """Two people stuck a message to one channel at once, and the other one got there first."""


class Sticky(Module):
    key = "sticky"
    row_limits = {"sticky_messages": MAX_STICKIES, "sticky_templates": MAX_TEMPLATES}
    category = "Sticky messages"
    migrations = [
        """
        CREATE TABLE sticky_messages (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL,
            summary TEXT NOT NULL,
            seconds INTEGER,
            expires_at REAL,
            message_id INTEGER,
            stale_message_id INTEGER,
            created_by INTEGER,
            created_by_name TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX sticky_messages_guild ON sticky_messages (guild_id);
        CREATE TABLE sticky_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            payload TEXT NOT NULL,
            summary TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX sticky_templates_name ON sticky_templates (guild_id, name COLLATE NOCASE);
        """,
    ]

    # Seconds. A repost waits for this much quiet, but never longer than MAX_WAIT from the first new message, and leaves MIN_GAP between posts.
    QUIET = 5.0
    MIN_GAP = 15.0
    MAX_WAIT = 60.0
    BACKOFF = 300.0
    BOT_REPOSTS_FREE = 3
    BOT_WAIT_MAX = 3600.0

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._live: dict[int, _Live] = {}

    async def on_load(self) -> None:
        self.bot.timers.handler(EXPIRE, self._expire_due)
        for row in await self.db.all("SELECT guild_id, channel_id, message_id FROM sticky_messages"):
            self._live[row["channel_id"]] = _Live(row["guild_id"], row["message_id"])

    async def on_guild_restored(self, guild_id: int) -> None:
        for channel_id, live in list(self._live.items()):
            if live.guild_id == guild_id:
                if live.task is not None:
                    live.task.cancel()
                del self._live[channel_id]
        for row in await self.db.all("SELECT guild_id, channel_id, message_id FROM sticky_messages WHERE guild_id = ?", (guild_id,)):
            self._live[row["channel_id"]] = _Live(row["guild_id"], row["message_id"])

    def cog_unload(self) -> None:
        for live in self._live.values():
            if live.task is not None:
                live.task.cancel()

    # ---- reposting ----

    async def _rendered(self, row: dict) -> dict:
        guild = self.bot.get_guild(row["guild_id"])
        channel = guild.get_channel_or_thread(row["channel_id"]) if guild else None
        return await render_message(json.loads(row["payload"]), Scope(guild=guild, channel=channel), guild_id=row["guild_id"])

    async def _post_copy(self, channel_id: int, live: _Live) -> None:
        """Post the sticky and remove the old copy, in that order, so a refusal from Discord can't leave the channel without one."""
        async with live.lock:
            row = await self.db.one("SELECT * FROM sticky_messages WHERE channel_id = ?", (channel_id,))
            if row is None or self._live.get(channel_id) is not live:
                return
            live.last_post = time.monotonic()
            live.posting = True
            try:
                sent = await self.bot.ops.send_message(channel_id, await self._rendered(row), silent=True)
            finally:
                live.posting = False
            live.message_id = sent["id"]
            # The old ID stays on the row until its copy is gone, so a crash or a refusal here gets finished on the next repost.
            stale = [m for m in (row["message_id"], row["stale_message_id"]) if m]
            await self.db.run("UPDATE sticky_messages SET message_id = ?, stale_message_id = ? WHERE channel_id = ?", (sent["id"], stale[0] if stale else None, channel_id))
            leftover = None
            for message_id in stale:
                try:
                    await self.bot.ops.delete_message(channel_id, message_id)
                except OpsError as error:
                    log.warning("Could not delete the old sticky %s in %s: %s", message_id, channel_id, error)
                    leftover = message_id
            if stale:
                await self.db.run("UPDATE sticky_messages SET stale_message_id = ? WHERE channel_id = ?", (leftover, channel_id))

    def current_copy(self, channel_id: int) -> int | None:
        """The message holding this channel's sticky right now, for a feature that clears out the channel."""
        live = self._live.get(channel_id)
        return live.message_id if live is not None else None

    def _gap(self, live: _Live) -> float:
        """The least time between posts. Past a few reposts in a row that only bots set off, it doubles each time."""
        over = live.bot_reposts - self.BOT_REPOSTS_FREE
        return self.MIN_GAP if over < 0 else min(self.MIN_GAP * 2 ** min(over + 1, 20), self.BOT_WAIT_MAX)

    def _nudge(self, channel_id: int, live: _Live, *, from_bot: bool = False) -> None:
        if not from_bot:
            live.person_waiting = True
            if live.bot_reposts and live.sleeping and live.task is not None:
                live.task.cancel()  # it may be waiting out an hour, and a person talking ends that
                live.task = None
            live.bot_reposts = 0
        now = time.monotonic()
        live.last_seen = now
        if live.first_seen is None:
            live.first_seen = now
        if live.task is None or live.task.done():
            live.task = asyncio.ensure_future(self._repost_soon(channel_id, live))

    async def _repost_soon(self, channel_id: int, live: _Live) -> None:
        # A message that lands while a copy is going out sets first_seen again, so this goes round once more for it.
        while live.first_seen is not None:
            soonest = max(live.last_post + self._gap(live), live.not_before)
            wanted = min(live.last_seen + self.QUIET, live.first_seen + self.MAX_WAIT)
            delay = max(soonest, wanted) - time.monotonic()
            if delay > 0:
                live.sleeping = True
                try:
                    await asyncio.sleep(delay)
                finally:
                    live.sleeping = False
                continue
            live.first_seen = None
            live.bot_reposts = 0 if live.person_waiting else live.bot_reposts + 1
            live.person_waiting = False
            try:
                await self._post_copy(channel_id, live)
            except OpsError as error:
                log.warning("Could not repost the sticky in %s: %s", channel_id, error)
                live.not_before = time.monotonic() + self.BACKOFF
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reposting the sticky in %s failed", channel_id)
                live.not_before = time.monotonic() + self.BACKOFF

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        live = self._live.get(message.channel.id)
        if live is None or message.guild is None:
            return
        if message.author.id == message.guild.me.id and (live.posting or message.id == live.message_id):
            return  # the sticky's own copy, which must not set off another
        from_bot = message.author.bot or message.webhook_id is not None
        if from_bot and self._gap(live) >= self.BOT_WAIT_MAX:
            return  # two sticky bots would answer each other forever, so this stops until a person talks
        self._nudge(message.channel.id, live, from_bot=from_bot)

    def _message_gone(self, channel_id: int, message_ids: set[int]) -> bool:
        live = self._live.get(channel_id)
        if live is None or live.message_id not in message_ids:
            return False
        live.message_id = None
        return True

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if self._message_gone(payload.channel_id, {payload.message_id}):
            await self.db.run("UPDATE sticky_messages SET message_id = NULL WHERE channel_id = ?", (payload.channel_id,))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if self._message_gone(payload.channel_id, set(payload.message_ids)):
            await self.db.run("UPDATE sticky_messages SET message_id = NULL WHERE channel_id = ?", (payload.channel_id,))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        if channel.id in self._live:
            await self._forget(channel.id)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        if payload.thread_id in self._live:
            await self._forget(payload.thread_id)

    async def _channel_is_gone(self, guild: Any, channel_id: int) -> bool:
        """Discord's own answer. An archived thread isn't in the cache either, and a sticky in one has to survive."""
        try:
            await guild.fetch_channel(channel_id)
        except discord.NotFound:
            return True
        except discord.DiscordException as error:
            log.info("Could not look up the channel of a sticky, %s: %s", channel_id, error)
        return False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Messages that arrived while the bot was away have pushed the sticky up, and no event will say so. A channel deleted meanwhile has no event either."""
        for channel_id, live in list(self._live.items()):
            guild = self.bot.get_guild(live.guild_id)
            if guild is None or getattr(guild, "unavailable", False):
                continue
            channel = guild.get_channel_or_thread(channel_id)
            if channel is None:
                if await self._channel_is_gone(guild, channel_id):
                    await self._forget(channel_id)
            elif live.message_id is None or getattr(channel, "last_message_id", None) != live.message_id:
                self._nudge(channel_id, live)

    # ---- creating and removing ----

    async def _expire_due(self, payload: dict, timer: dict) -> None:
        row = await self.db.one("SELECT * FROM sticky_messages WHERE channel_id = ?", (payload["channel_id"],))
        if row is not None and row["expires_at"] is not None and row["expires_at"] <= timer["due_at"] + 1:
            await self._remove(row["channel_id"])

    async def _forget(self, channel_id: int) -> dict | None:
        """Stop a sticky and return its last row. A copy going out right now finishes first, so its ID isn't lost and it can't stay behind."""
        live = self._live.pop(channel_id, None)
        if live is not None:
            if live.task is not None and not live.posting:
                live.task.cancel()
            async with live.lock:
                row = await self.db.one("SELECT * FROM sticky_messages WHERE channel_id = ?", (channel_id,))
        else:
            row = await self.db.one("SELECT * FROM sticky_messages WHERE channel_id = ?", (channel_id,))
        await self.db.run("DELETE FROM sticky_messages WHERE channel_id = ?", (channel_id,))
        await self.bot.timers.cancel_key(EXPIRE, str(channel_id))
        return row

    async def _remove(self, channel_id: int) -> None:
        row = await self._forget(channel_id)
        for message_id in (row["message_id"], row["stale_message_id"]) if row else ():
            if message_id:
                try:
                    await self.bot.ops.delete_message(channel_id, message_id)
                except OpsError as error:
                    log.info("Could not delete the sticky %s: %s", message_id, error)

    async def _set_expiry(self, guild_id: int, channel_id: int, seconds: int | None) -> float | None:
        if seconds is None:
            await self.bot.timers.cancel_key(EXPIRE, str(channel_id))
            return None
        expires_at = time.time() + seconds
        await self.bot.timers.schedule(EXPIRE, expires_at, {"channel_id": channel_id}, guild_id=guild_id, key=str(channel_id))
        return expires_at

    async def create(self, guild_id: int, channel_id: int, payload: dict, *, seconds: int | None, actor: Any) -> dict:
        """Stick a message to a channel and post it. `seconds` of None keeps it until someone removes it. Raises OpsError when Discord refuses, and ChannelTaken when the channel already has one."""
        expires_at = time.time() + seconds if seconds is not None else None
        added = await self.db.run(
            "INSERT INTO sticky_messages (guild_id, channel_id, payload, summary, seconds, expires_at, created_by, created_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (channel_id) DO NOTHING",
            (guild_id, channel_id, json.dumps(payload), messages.summary(payload), seconds, expires_at, actor.id, actor.name, time.time()),
        )
        if not added:
            raise ChannelTaken
        live = _Live(guild_id)
        self._live[channel_id] = live
        try:
            await self._post_copy(channel_id, live)
        except OpsError:
            await self._forget(channel_id)
            raise
        await self._set_expiry(guild_id, channel_id, seconds)
        return await self.db.one("SELECT * FROM sticky_messages WHERE channel_id = ?", (channel_id,))

    async def _room_for_one(self, guild_id: int) -> bool:
        return await self.db.scalar("SELECT COUNT(*) FROM sticky_messages WHERE guild_id = ?", (guild_id,)) < MAX_STICKIES

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/sticky", self._list_endpoint)
        router.post("/sticky", self._create_endpoint)
        router.patch(r"/sticky/{channel_id:\d+}", self._update_endpoint)
        router.delete(r"/sticky/{channel_id:\d+}", self._delete_endpoint)
        router.post("/sticky/templates", self._create_template_endpoint)
        router.patch(r"/sticky/templates/{template_id:\d+}", self._update_template_endpoint)
        router.delete(r"/sticky/templates/{template_id:\d+}", self._delete_template_endpoint)

    @staticmethod
    def _view(row: dict) -> dict:
        return {key: (json.loads(value) if key == "payload" else value) for key, value in row.items() if key != "stale_message_id"}

    async def _list_endpoint(self, req: Any) -> dict:
        stickies = await self.db.all("SELECT * FROM sticky_messages WHERE guild_id = ? ORDER BY created_at", (req.guild_id,))
        templates = await self.db.all("SELECT * FROM sticky_templates WHERE guild_id = ? ORDER BY name COLLATE NOCASE", (req.guild_id,))
        return {"stickies": [self._view(row) for row in stickies if req.can_see(row["channel_id"])], "templates": [self._view(row) for row in templates]}

    @staticmethod
    def _duration_form(form: dict) -> int | None:
        return None if form.get("forever") else _seconds_field(form.get("seconds"))

    async def _create_endpoint(self, req: Any) -> dict:
        form = await req.json()
        channel_id = req.channel(form.get("channel_id"), posting=True)
        payload = messages.required_payload(form.get("payload"), templates=True)
        seconds = self._duration_form(form)
        taken = ApiError(409, "That channel already has a sticky message. Edit that one, or remove it first.")
        if channel_id in self._live:
            raise taken
        if not await self._room_for_one(req.guild_id):
            raise ApiError(409, f"A server can hold {MAX_STICKIES} sticky messages. Remove one first.")
        try:
            row = await self.create(req.guild_id, channel_id, payload, seconds=seconds, actor=req.actor)
        except ChannelTaken:
            raise taken from None
        await req.record("sticky.create", detail=messages.summary(payload, 80))
        return self._view(row)

    async def _row_or_404(self, req: Any) -> dict:
        row = await self.db.one("SELECT * FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (req.guild_id, req.int_param("channel_id")))
        if row is None or not req.can_see(row["channel_id"]):
            raise ApiError(404, "That channel has no sticky message.")
        return row

    async def _update_endpoint(self, req: Any) -> dict:
        row = await self._row_or_404(req)
        form = await req.json()
        channel_id = row["channel_id"]
        if "payload" in form:
            payload = messages.required_payload(form["payload"], templates=True)
            await self.db.run("UPDATE sticky_messages SET payload = ?, summary = ? WHERE channel_id = ?", (json.dumps(payload), messages.summary(payload), channel_id))
            if json.dumps(payload, sort_keys=True) != json.dumps(json.loads(row["payload"]), sort_keys=True):
                # A backup restored since the last start can hold a row this process hasn't seen.
                await self._post_copy(channel_id, self._live.setdefault(channel_id, _Live(req.guild_id, row["message_id"])))
        if form.get("forever") or "seconds" in form:
            seconds = self._duration_form(form)
            expires_at = await self._set_expiry(req.guild_id, channel_id, seconds)
            await self.db.run("UPDATE sticky_messages SET seconds = ?, expires_at = ? WHERE channel_id = ?", (seconds, expires_at, channel_id))
        await req.record("sticky.update", detail=row["summary"][:80])
        return self._view(await self.db.one("SELECT * FROM sticky_messages WHERE channel_id = ?", (channel_id,)))

    async def _delete_endpoint(self, req: Any) -> dict:
        row = await self._row_or_404(req)
        await self._remove(row["channel_id"])
        await req.record("sticky.remove", detail=row["summary"][:80])
        return {"ok": True}

    async def _template_taken(self, guild_id: int, name: str, leave_out: int = 0) -> bool:
        return await self.db.scalar("SELECT COUNT(*) FROM sticky_templates WHERE guild_id = ? AND name = ? COLLATE NOCASE AND id != ?", (guild_id, name, leave_out)) > 0

    async def _create_template_endpoint(self, req: Any) -> dict:
        form = await req.json()
        name = _name_field(form.get("name"))
        payload = messages.required_payload(form.get("payload"), templates=True)
        if await self._template_taken(req.guild_id, name):
            raise ValidationError({"name": "A template with that name already exists."})
        if await self.db.scalar("SELECT COUNT(*) FROM sticky_templates WHERE guild_id = ?", (req.guild_id,)) >= MAX_TEMPLATES:
            raise ApiError(409, f"A server can hold {MAX_TEMPLATES} templates. Remove one first.")
        template_id = await self.db.insert(
            "INSERT INTO sticky_templates (guild_id, name, payload, summary, updated_at) VALUES (?, ?, ?, ?, ?)",
            (req.guild_id, name, json.dumps(payload), messages.summary(payload), time.time()),
        )
        await req.record("sticky.template_create", detail=name)
        return {"id": template_id, "name": name, "payload": payload, "summary": messages.summary(payload)}

    async def _template_or_404(self, req: Any) -> dict:
        row = await self.db.one("SELECT * FROM sticky_templates WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("template_id")))
        if row is None:
            raise ApiError(404, "That template is gone.")
        return row

    async def _update_template_endpoint(self, req: Any) -> dict:
        row = await self._template_or_404(req)
        form = await req.json()
        name = _name_field(form.get("name", row["name"]))
        payload = messages.required_payload(form["payload"], templates=True) if "payload" in form else json.loads(row["payload"])
        if await self._template_taken(req.guild_id, name, row["id"]):
            raise ValidationError({"name": "A template with that name already exists."})
        await self.db.run(
            "UPDATE sticky_templates SET name = ?, payload = ?, summary = ?, updated_at = ? WHERE guild_id = ? AND id = ?",
            (name, json.dumps(payload), messages.summary(payload), time.time(), req.guild_id, row["id"]),
        )
        await req.record("sticky.template_update", detail=name)
        return {"id": row["id"], "name": name, "payload": payload, "summary": messages.summary(payload)}

    async def _delete_template_endpoint(self, req: Any) -> dict:
        row = await self._template_or_404(req)
        await self.db.run("DELETE FROM sticky_templates WHERE guild_id = ? AND id = ?", (req.guild_id, row["id"]))
        await req.record("sticky.template_remove", detail=row["name"])
        return {"ok": True}

    # ---- /sticky ----

    @commands.hybrid_group(name="sticky", fallback="list", description="Show the sticky messages in this server")
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @requires("manage_guild")
    async def sticky(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT * FROM sticky_messages WHERE guild_id = ? ORDER BY created_at", (ctx.guild.id,))
        rows = [row for row in rows if self._shown_to(ctx.author, row["channel_id"])]
        if not rows:
            await ctx.send("No sticky messages are set.", ephemeral=True)
            return
        lines = ["Each one comes back a few seconds after new messages stop."]
        for row in rows:
            ending = f"ends <t:{int(row['expires_at'])}:R>, set for {humanize(row['seconds'], parts=2)}" if row["expires_at"] else "stays until removed"
            lines.append(f"<#{row['channel_id']}>: {discord.utils.escape_markdown(row['summary'][:80])} ({ending})")
        text = "\n".join(lines)
        await ctx.send(text[:1990], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _shown_to(member: Any, channel_id: int) -> bool:
        """A channel that's gone has nothing left to hide, and stays listed so it can be removed."""
        channel = member.guild.get_channel_or_thread(channel_id)
        return channel is None or access.can_view(member, channel)

    async def _stick_from_command(self, ctx: commands.Context, payload: dict, seconds: int | None, channel: Any) -> None:
        target = channel or ctx.channel
        refusal = access.post_refusal(ctx.author, target)
        if refusal is not None:
            await ctx.fail(refusal)
            return
        taken = f"{target.mention} already has a sticky message. Remove it first with `/sticky remove`."
        if target.id in self._live:
            await ctx.fail(taken)
            return
        if not await self._room_for_one(ctx.guild.id):
            await ctx.fail(f"A server can hold {MAX_STICKIES} sticky messages. Remove one first.")
            return
        duration = DEFAULT_SECONDS if seconds is None else seconds
        if not MIN_SECONDS <= duration <= MAX_SECONDS:
            await ctx.fail("Pick a duration between one minute and 365 days.")
            return
        try:
            await self.create(ctx.guild.id, target.id, payload, seconds=duration, actor=self.bot.actor(ctx.author))
        except ChannelTaken:
            await ctx.fail(taken)
            return
        except OpsError as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "sticky.create", detail=messages.summary(payload, 80))
        await ctx.ok(f"That message now sticks to {target.mention} for {humanize(duration, parts=2)}.")

    @sticky.command(name="add", description="Keep a message at the bottom of a channel")
    @requires("manage_guild")
    @app_commands.describe(message="The message's text", duration="How long it stays, such as 12h or 7 days. Defaults to a day.", channel="Which channel. Defaults to here.")
    async def sticky_add(self, ctx: commands.Context, *, message: str, duration: Duration | None = None, channel: discord.TextChannel | None = None) -> None:
        await ctx.defer(ephemeral=True)
        if ctx.interaction is None:
            # A text command takes the rest of the line as one argument, so a channel and a duration come off its start or its end.
            at_front, length, message = self._leading_channel_and_time(ctx, message)
            channel, duration = channel or at_front, duration if duration is not None else length
            at_end = CHANNEL_AT_END.search(message)
            found = ctx.guild.get_channel(int(at_end.group(1))) if at_end else None
            if channel is None and found is not None and hasattr(found, "send"):
                channel, message = found, message[:at_end.start()]
            first, _, last = message.rpartition(" ")
            if duration is None and first and parse_duration(last) is not None:
                duration, message = parse_duration(last), first
        try:
            payload = messages.required_payload({"content": message})
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return
        await self._stick_from_command(ctx, payload, duration, channel)

    @staticmethod
    def _leading_channel_and_time(ctx: commands.Context, words: str) -> tuple[Any, int | None, str]:
        """A channel mention and a length of time off the front of a typed line. The length needs a unit, so a message that opens with a number stays whole."""
        channel = None
        at_start = CHANNEL_AT_START.match(words)
        found = ctx.guild.get_channel(int(at_start.group(1))) if at_start else None
        if found is not None and hasattr(found, "send"):
            channel, words = found, words[at_start.end():]
        first, _, rest = words.partition(" ")
        if rest.strip() and parse_duration(first) is not None:
            return channel, parse_duration(first), rest.strip()
        return channel, None, words

    @sticky.command(name="fromtemplate", with_app_command=False, description="Stick a saved template, with the channel and length first")
    @requires("manage_guild")
    async def sticky_fromtemplate(self, ctx: commands.Context, *, words: str) -> None:
        channel, duration, name = self._leading_channel_and_time(ctx, words)
        await self.sticky_template.callback(self, ctx, name.strip().strip("\""), duration, channel)

    @sticky.command(name="remove", description="Stop keeping a message at the bottom of a channel")
    @requires("manage_guild")
    @app_commands.describe(channel="Which channel. Defaults to here.")
    async def sticky_remove(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await ctx.defer(ephemeral=True)
        target = channel or ctx.channel
        refusal = access.view_refusal(ctx.author, target)
        if refusal is not None:
            await ctx.fail(refusal)
            return
        row = await self.db.one("SELECT * FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (ctx.guild.id, target.id))
        if row is None:
            await ctx.fail(f"{target.mention} has no sticky message.")
            return
        await self._remove(target.id)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "sticky.remove", detail=row["summary"][:80])
        await ctx.ok(f"Removed the sticky message from {target.mention}.")

    @sticky.command(name="template", description="Stick a saved template to a channel")
    @requires("manage_guild")
    @app_commands.describe(template_name="The template's name", duration="How long it stays, such as 12h or 7 days. Defaults to a day.", channel="Which channel. Defaults to here.")
    async def sticky_template(self, ctx: commands.Context, template_name: str, duration: Duration | None = None, channel: discord.TextChannel | None = None) -> None:
        await ctx.defer(ephemeral=True)
        row = await self.db.one("SELECT * FROM sticky_templates WHERE guild_id = ? AND name = ? COLLATE NOCASE", (ctx.guild.id, template_name))
        if row is None:
            await ctx.fail("There is no template with that name. Templates are made on the dashboard.")
            return
        await self._stick_from_command(ctx, json.loads(row["payload"]), duration, channel)

    @sticky_template.autocomplete("template_name")
    async def _template_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = await self.db.all(
            "SELECT name FROM sticky_templates WHERE guild_id = ? AND name LIKE ? ORDER BY name COLLATE NOCASE LIMIT 25",
            (interaction.guild_id, f"%{current.replace('%', '').replace('_', '')}%"),
        )
        return [app_commands.Choice(name=row["name"][:100], value=row["name"][:100]) for row in rows]
