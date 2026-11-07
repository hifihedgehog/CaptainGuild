"""Scheduled channel cleanup: each listed channel loses its messages of a chosen kind at a fixed interval."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.context import unused_words
from ..core.durations import Duration, humanize
from ..core.module import Module, requires
from ..core.validate import ValidationError, choice, integer
from ..web.api import ApiError

log = logging.getLogger(__name__)

HOUR = 3600
MIN_INTERVAL = HOUR
MAX_INTERVAL = 14 * 86400
CHANNELS_MAX = 200
SCAN_LIMIT = 1000
RETRY_SECONDS = 30
TIMER = "autopurge.run"
TYPES = ("all", "humans", "bots", "links", "invites", "attachments")
TYPE_LABELS = {"all": "All messages", "humans": "Human messages", "bots": "Bot messages", "links": "Links", "invites": "Invites", "attachments": "Attachments"}
READS_TEXT = ("links", "invites", "attachments")
NEEDS_MANAGE_MESSAGES = "You need Manage Messages in that channel to purge it."
NO_ACCESS = "The bot needs Manage Messages and Read Message History in this channel."
NO_TEXT = "The bot can't read message text, so it can't tell which messages hold links, invites or attachments."
REFUSED = "Discord refused part of the last purge. Check the bot's Manage Messages permission in this channel."
STOPPED = "Discord stopped the last purge partway. What's left goes on the next run."
BROKE = "The last purge hit an error. The bot's log has the details."

_LINK = re.compile(r"https?://\S+", re.IGNORECASE)
_INVITE = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg)/[\w-]+", re.IGNORECASE)


def wanted_check(message_type: str) -> Callable[[Any], bool]:
    """Which messages this kind of purge takes. Pinned messages always stay, since a channel's pins are there on purpose."""
    checks: dict[str, Callable[[Any], bool]] = {
        "all": lambda m: True,
        "humans": lambda m: not m.author.bot,
        "bots": lambda m: m.author.bot,
        "links": lambda m: bool(_LINK.search(m.content or "")),
        "invites": lambda m: bool(_INVITE.search(m.content or "")),
        "attachments": lambda m: bool(m.attachments),
    }
    check = checks[message_type]
    return lambda message: not message.pinned and check(message)


class AutoPurge(Module):
    key = "autopurge"
    category = "Automod"
    migrations = [
        """
        CREATE TABLE autopurge_channels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            interval_seconds INTEGER NOT NULL,
            message_type TEXT NOT NULL,
            next_run REAL NOT NULL,
            last_run REAL,
            last_deleted INTEGER,
            PRIMARY KEY (guild_id, channel_id)
        );
        """,
        "ALTER TABLE autopurge_channels ADD COLUMN last_error TEXT;",
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._running: dict[int, asyncio.Task] = {}

    async def on_load(self) -> None:
        self.bot.timers.handler(TIMER, self._due)

    async def cog_unload(self) -> None:
        for task in self._running.values():
            task.cancel()

    async def _rows(self, guild_id: int) -> list[dict]:
        return await self.db.all("SELECT * FROM autopurge_channels WHERE guild_id = ? ORDER BY next_run", (guild_id,))

    async def set_channel(self, guild_id: int, channel_id: int, interval_seconds: int, message_type: str) -> dict:
        """Add or change a channel. A new channel's first purge comes one interval from now, and a new interval starts that count again. Any other change leaves the next purge where it was."""
        count = await self.db.scalar("SELECT COUNT(*) FROM autopurge_channels WHERE guild_id = ? AND channel_id != ?", (guild_id, channel_id))
        if count >= CHANNELS_MAX:
            raise ValidationError({"channel_id": f"A server can list at most {CHANNELS_MAX} channels."})
        current = await self._row(guild_id, channel_id)
        next_run = current["next_run"] if current is not None and current["interval_seconds"] == interval_seconds else time.time() + interval_seconds
        await self.db.run(
            "INSERT INTO autopurge_channels (guild_id, channel_id, interval_seconds, message_type, next_run) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, channel_id) DO UPDATE SET interval_seconds = excluded.interval_seconds, message_type = excluded.message_type, next_run = excluded.next_run",
            (guild_id, channel_id, interval_seconds, message_type, next_run),
        )
        await self.bot.timers.schedule(TIMER, next_run, {"guild_id": guild_id, "channel_id": channel_id}, guild_id=guild_id, key=f"{guild_id}:{channel_id}")
        return await self._row(guild_id, channel_id)

    async def _row(self, guild_id: int, channel_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM autopurge_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))

    async def remove_channel(self, guild_id: int, channel_id: int) -> bool:
        await self.bot.timers.cancel_key(TIMER, f"{guild_id}:{channel_id}")
        return await self.db.run("DELETE FROM autopurge_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)) > 0

    async def _due(self, payload: dict, row: dict) -> None:
        guild_id, channel_id = payload["guild_id"], payload["channel_id"]
        settings = await self._row(guild_id, channel_id)
        if settings is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None or getattr(guild, "unavailable", False):
            # An unavailable server's channel list is empty, and that doesn't mean its channels are gone. Look again later, with the settings left alone.
            await self.bot.timers.schedule(TIMER, time.time() + RETRY_SECONDS, payload, guild_id=guild_id, key=row.get("key"))
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "purge"):
            await self.remove_channel(guild_id, channel_id)
            return
        next_run = time.time() + settings["interval_seconds"]
        await self.db.run("UPDATE autopurge_channels SET next_run = ? WHERE guild_id = ? AND channel_id = ?", (next_run, guild_id, channel_id))
        await self.bot.timers.schedule(TIMER, next_run, payload, guild_id=guild_id, key=f"{guild_id}:{channel_id}")
        # Old messages delete one at a time, which can take many minutes. The timer loop also runs unbans and unmutes, so this can't hold it up.
        running = self._running.get(channel_id)
        if running is None or running.done():
            self._running[channel_id] = asyncio.ensure_future(self._purge(guild, channel, settings["message_type"]))

    def _wanted(self, channel: Any, message_type: str) -> Callable[[Any], bool]:
        """A channel's sticky stays, since it only comes back with the next message and a quiet channel would go without it."""
        wanted = wanted_check(message_type)
        current_copy = getattr(self.bot.module("sticky"), "current_copy", None)
        if current_copy is None:
            return wanted
        return lambda message: message.id != current_copy(channel.id) and wanted(message)

    async def _failed(self, guild: Any, channel: Any, sentence: str) -> int:
        """What the channel's row on the page shows until a purge works."""
        await self.db.run("UPDATE autopurge_channels SET last_error = ? WHERE guild_id = ? AND channel_id = ?", (sentence, guild.id, channel.id))
        return 0

    async def _purge(self, guild: Any, channel: Any, message_type: str) -> int:
        mine = channel.permissions_for(guild.me)
        if not mine.manage_messages or not mine.read_message_history:
            log.info("Auto purge in %s skipped: the bot lacks Manage Messages or Read Message History", channel.id)
            return await self._failed(guild, channel, NO_ACCESS)
        if message_type in READS_TEXT and not self.bot.reads_message_text:
            return await self._failed(guild, channel, NO_TEXT)
        for second_try in (False, True):
            try:
                deleted = await channel.purge(limit=SCAN_LIMIT, check=self._wanted(channel, message_type), reason="Auto purge")
                break
            except discord.Forbidden as error:
                log.info("Auto purge in %s refused: %s", channel.id, error)
                return await self._failed(guild, channel, REFUSED)
            except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
                # A message someone else just deleted or a Discord hiccup ends the library's walk through the channel. What it hadn't reached is still there.
                log.info("Auto purge in %s stopped: %s", channel.id, error)
                retry = not isinstance(error, discord.HTTPException) or error.status >= 500 or error.status == 404
                if second_try or not retry:
                    return await self._failed(guild, channel, STOPPED)
                await asyncio.sleep(RETRY_SECONDS)
            except Exception:
                # This runs as its own task, where nothing else would report a surprise.
                log.exception("Auto purge in %s failed", channel.id)
                return await self._failed(guild, channel, BROKE)
        await self.db.run("UPDATE autopurge_channels SET last_run = ?, last_deleted = ?, last_error = NULL WHERE guild_id = ? AND channel_id = ?", (time.time(), len(deleted), guild.id, channel.id))
        return len(deleted)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        await self.remove_channel(channel.guild.id, channel.id)

    # ---- commands ----

    @staticmethod
    def _shown_to(member: Any, rows: list[dict]) -> list[dict]:
        """The rows this person may know about. A channel that's gone has nothing left to hide, and stays so it can be removed."""
        kept = []
        for row in rows:
            channel = member.guild.get_channel(row["channel_id"])
            if channel is None or access.can_view(member, channel):
                kept.append(row)
        return kept

    @commands.hybrid_group(name="autopurge", fallback="config", description="Show which channels are purged on a schedule")
    @commands.guild_only()
    @requires("manage_guild")
    @app_commands.default_permissions(manage_guild=True)
    async def autopurge(self, ctx: commands.Context) -> None:
        rows = self._shown_to(ctx.author, await self._rows(ctx.guild.id))
        lines = [f"<#{row['channel_id']}>: {TYPE_LABELS[row['message_type']].lower()} every {humanize(row['interval_seconds'])}, next <t:{int(row['next_run'])}:R>" for row in rows]
        await ctx.send("\n".join(lines)[:1900] or "No channels are purged on a schedule.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @autopurge.command(name="set", description="Purge a channel on a schedule, from one hour to 14 days")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel", interval="How often, such as 6h or 2d", message_type="Which messages to delete")
    async def autopurge_set(self, ctx: commands.Context, channel: discord.TextChannel, interval: Duration, message_type: Literal["all", "humans", "bots", "links", "invites", "attachments"] = "all") -> None:
        if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
            await ctx.fail("The interval must be from one hour to 14 days.")
            return
        refusal = access.view_refusal(ctx.author, channel)
        current = await self._row(ctx.guild.id, channel.id)
        if refusal is None and (current is None or current["message_type"] != message_type) and not access.can_manage_messages(ctx.author, channel):
            refusal = NEEDS_MANAGE_MESSAGES
        if refusal is not None:
            await ctx.fail(refusal)
            return
        try:
            row = await self.set_channel(ctx.guild.id, channel.id, interval, message_type)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autopurge.set", detail=channel.name)
        mine = channel.permissions_for(ctx.guild.me)
        note = "" if mine.manage_messages and mine.read_message_history else " The bot needs Manage Messages and Read Message History there for this to work."
        await ctx.ok(f"{channel.mention} loses its {TYPE_LABELS[message_type].lower()} every {humanize(interval)}, starting <t:{int(row['next_run'])}:R>.{note}")

    @autopurge.command(name="remove", ignore_extra=True, description="Stop purging a channel, or every channel")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel. Leave out to stop all the ones you can see")
    async def autopurge_remove(self, ctx: commands.Context, *, channel: discord.TextChannel | None = None) -> None:
        if channel is not None:
            refusal = access.view_refusal(ctx.author, channel)
            if refusal is not None:
                await ctx.fail(refusal)
                return
            if not await self.remove_channel(ctx.guild.id, channel.id):
                await ctx.fail(f"{channel.mention} isn't on the list.")
                return
            await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autopurge.remove", detail=channel.name)
            await ctx.ok(f"{channel.mention} is no longer purged.")
            return
        unread = unused_words(ctx)
        if unread:
            await ctx.fail(f"I can't find a channel called `{unread}`, so nothing changed. Leave the channel out to stop all of them.")
            return
        rows = self._shown_to(ctx.author, await self._rows(ctx.guild.id))
        for row in rows:
            await self.remove_channel(ctx.guild.id, row["channel_id"])
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autopurge.remove", detail="all channels")
        await ctx.ok(f"Stopped purging {len(rows)} channels.")

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/autopurge", self.api_list)
        router.put("/autopurge/{channel_id:\\d+}", self.api_set)
        router.delete("/autopurge/{channel_id:\\d+}", self.api_remove)

    async def api_list(self, req: Any) -> list[dict]:
        guild = self.bot.get_guild(req.guild_id)
        known = guild is not None and not getattr(guild, "unavailable", False)
        rows = [row for row in await self._rows(req.guild_id) if req.can_see(row["channel_id"])]
        for row in rows:
            channel = guild.get_channel(row["channel_id"]) if known else None
            mine = channel.permissions_for(guild.me) if channel is not None else None
            row["gone"] = known and channel is None
            row["can_purge"] = mine is None or bool(mine.manage_messages and mine.read_message_history)
        return rows

    async def api_set(self, req: Any) -> dict:
        form = await req.json()
        if not isinstance(form, dict):
            raise ValidationError({"form": "Expected a set of fields."})
        channel_id = req.channel(req.param("channel_id"))
        errors = {}
        interval = message_type = None
        try:
            interval = integer(MIN_INTERVAL, MAX_INTERVAL)(form.get("interval_seconds"))
        except ValueError:
            errors["interval_seconds"] = "The interval must be from one hour to 14 days."
        try:
            message_type = choice(*TYPES)(form.get("message_type") or "all")
        except ValueError:
            errors["message_type"] = "Pick one of the listed message types."
        if errors:
            raise ValidationError(errors)
        current = await self._row(req.guild_id, channel_id)
        if (current is None or current["message_type"] != message_type) and not req.can_manage_messages(channel_id):
            raise ValidationError({"channel_id": NEEDS_MANAGE_MESSAGES})
        row = await self.set_channel(req.guild_id, channel_id, interval, message_type)
        await req.record("autopurge.set", detail=str(channel_id))
        return row

    async def api_remove(self, req: Any) -> dict:
        channel_id = req.int_param("channel_id")
        if not req.can_see(channel_id) or not await self.remove_channel(req.guild_id, channel_id):
            raise ApiError(404, "That channel isn't on the list.")
        await req.record("autopurge.remove", detail=str(channel_id))
        return {"ok": True}
