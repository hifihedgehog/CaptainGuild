"""Entry giveaways: a channel, a thread or a forum where each member's first message is their one entry.

In a channel or a thread the whole place is one giveaway. In a forum each post is its own. A member's later messages in the same giveaway
are deleted with a warning, other bots' messages are deleted, and the staff role posts freely and is never drawn. Closing a giveaway stops
all of that, so people can talk there. Deleting someone's entry lets them enter again. /draw reads what was posted while the giveaway was
open and picks winners at random from the members who posted, one chance each. Someone who has left the server isn't drawn.

Captain Guild's /giveaway is a different thing, a timed giveaway with a button. These commands keep the names /set_giveaway_channel,
/set_giveaway_staff, /mark_closed, /draw and /statistics.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import random
import re
import time
from typing import Any, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.module import Module, requires
from ..core.validate import Field, Schema, ValidationError, boolean, optional, snowflake
from ..web.api import ApiError

log = logging.getLogger(__name__)

SINGLE, MULTI = "single", "multi"
# What a member posts. Everything else in a channel is written by Discord or by a bot.
PERSON_TYPES = (discord.MessageType.default, discord.MessageType.reply)
DUPLICATE_WARNING = "{mention} Only one entry per giveaway is allowed! Duplicate message deleted."
BOT_WARNING = "Bot messages are not allowed in giveaway entry areas and will be deleted."
DUPLICATE_WARNING_SECONDS = 30
BOT_WARNING_SECONDS = 20
USER_WARNING_GAP = 20.0
CHANNEL_WARNING_GAP = 30.0
DELETE_GAP = 0.3
PROGRESS_EVERY = 100
ARCHIVED_SHOWN = 100
CHOICES = 25
OVERALL = "overall"
OVERALL_CHOICE = "\N{BAR CHART} Overall - All Active Giveaways"
NOT_ACTIVE = "No giveaway enforcement is currently active."
INVALID = "Invalid channel selection."
NOT_FOUND = "Channel/thread not found."
NOT_IN_FORUM = "Selected thread is not part of the active forum giveaway."
ID_IN_TEXT = re.compile(r"(\d{15,21})")
WARNINGS_KEPT = 5000
MESSAGE_LIMIT = 2000


def area_of(channel: Any, target: int, kind: str) -> int | None:
    """The giveaway a message in this channel belongs to: the target itself, or in a forum the post it's in. None outside the giveaway."""
    if kind == SINGLE:
        return target if channel.id == target else None
    if isinstance(channel, discord.Thread) and channel.parent_id == target:
        return channel.id
    return None


def label(channel: Any) -> str:
    """How a giveaway reads in a list: #channel, or #forum > post for a thread."""
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return f"#{parent.name} > {channel.name}" if parent is not None else f"#{channel.name}"
    return f"#{channel.name}"


def parse_area(text: str | None) -> int | None:
    """A channel picked from the list, typed as an ID or a mention."""
    found = ID_IN_TEXT.search(text or "")
    return int(found.group(1)) if found else None


def plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def snowflake_at(moment: float) -> int:
    """The lowest message ID Discord gives at that moment."""
    return discord.utils.time_snowflake(datetime.datetime.fromtimestamp(moment, datetime.timezone.utc))


def split_lines(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Text as messages of at most `limit` characters, breaking only between lines."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        joined = f"{current}\n{line}" if current else line
        if len(joined) > limit and current:
            parts.append(current)
            current = line
        else:
            current = joined
    parts.append(current[:limit])
    return [part for part in parts if part.strip()]


class EntryGiveaways(Module):
    key = "entrygiveaways"
    category = "Utilities"
    command_pack = True
    schema = Schema({
        "enabled": Field(False, boolean),
        "staff_role": Field(None, optional(snowflake)),
    })
    migrations = [
        """
        CREATE TABLE entry_giveaway_setup (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            set_by INTEGER,
            set_by_name TEXT,
            set_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX entry_giveaway_setup_guild ON entry_giveaway_setup (guild_id);
        CREATE TABLE entry_giveaway_entries (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            entered_at REAL NOT NULL,
            PRIMARY KEY (guild_id, channel_id, user_id)
        );
        CREATE INDEX entry_giveaway_entries_message ON entry_giveaway_entries (guild_id, message_id);
        CREATE TABLE entry_giveaway_closed (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            closed_by INTEGER,
            closed_by_name TEXT,
            closed_at REAL NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        );
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._setup: dict[int, tuple[int, str]] = {}
        self._closed: dict[int, set[int]] = {}
        self._deletions: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._warned_people: dict[int, float] = {}
        self._warned_channels: dict[int, float] = {}
        self._clock = time.monotonic
        self._random = random.SystemRandom()
        self._since = time.time()  # when this session began: deletes from before it never arrive
        self._checked: set[tuple[int, int, int]] = set()

    def staff_roles(self, settings: dict) -> dict[str, set[int]]:
        """The staff role posts as often as it likes and is never drawn, so only the owner or an Administrator sets it, as before."""
        return {"staff_role": {settings["staff_role"]} if settings.get("staff_role") else set()}

    async def on_load(self) -> None:
        await self._load()
        self._worker = asyncio.ensure_future(self._delete_worker())

    def cog_unload(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    async def _load(self, guild_id: int | None = None) -> None:
        """What on_message reads for every message, from the tables. One server's, or every server's."""
        where, values = ("WHERE guild_id = ?", (guild_id,)) if guild_id is not None else ("", ())
        if guild_id is not None:
            self._setup.pop(guild_id, None)
            self._closed.pop(guild_id, None)
        else:
            self._setup.clear()
            self._closed.clear()
        for row in await self.db.all(f"SELECT guild_id, channel_id, kind FROM entry_giveaway_setup {where}", values):
            self._setup[row["guild_id"]] = (row["channel_id"], row["kind"])
        for row in await self.db.all(f"SELECT guild_id, channel_id FROM entry_giveaway_closed {where}", values):
            self._closed.setdefault(row["guild_id"], set()).add(row["channel_id"])

    async def on_guild_restored(self, guild_id: int) -> None:
        await self._load(guild_id)

    # ---- the giveaway's place ----

    def setup_of(self, guild_id: int) -> tuple[int, str] | None:
        return self._setup.get(guild_id)

    def is_closed(self, guild_id: int, area_id: int) -> bool:
        return area_id in self._closed.get(guild_id, set())

    async def set_place(self, guild_id: int, channel: Any, actor: Any) -> str:
        """Points the giveaway at a channel, thread or forum, and starts every count over, as setting it always did. Returns its kind."""
        kind = MULTI if isinstance(channel, discord.ForumChannel) or getattr(channel, "type", None) in (discord.ChannelType.forum, discord.ChannelType.media) else SINGLE
        async with self.db.transaction():
            await self.db.run("DELETE FROM entry_giveaway_entries WHERE guild_id = ?", (guild_id,))
            await self.db.run("DELETE FROM entry_giveaway_closed WHERE guild_id = ?", (guild_id,))
            await self.db.run(
                "INSERT INTO entry_giveaway_setup (guild_id, channel_id, kind, set_by, set_by_name, set_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET channel_id = excluded.channel_id, kind = excluded.kind, set_by = excluded.set_by, set_by_name = excluded.set_by_name, set_at = excluded.set_at",
                (guild_id, channel.id, kind, actor.id if actor else None, actor.name if actor else None, time.time()),
            )
        self._setup[guild_id] = (channel.id, kind)
        self._closed.pop(guild_id, None)
        await self.bot.activity_log.record(guild_id, actor, "entrygiveaways.set", detail=f"#{getattr(channel, 'name', channel.id)}")
        return kind

    async def clear_place(self, guild_id: int, actor: Any) -> None:
        """Stops the giveaway: no more limit, and its entries and closed marks go. The staff role stays."""
        async with self.db.transaction():
            await self.db.run("DELETE FROM entry_giveaway_setup WHERE guild_id = ?", (guild_id,))
            await self.db.run("DELETE FROM entry_giveaway_entries WHERE guild_id = ?", (guild_id,))
            await self.db.run("DELETE FROM entry_giveaway_closed WHERE guild_id = ?", (guild_id,))
        self._setup.pop(guild_id, None)
        self._closed.pop(guild_id, None)
        if actor is not None:
            await self.bot.activity_log.record(guild_id, actor, "entrygiveaways.clear")

    async def close(self, guild_id: int, area_id: int, actor: Any) -> bool:
        """Marks a giveaway closed. False when it already was."""
        added = await self.db.run(
            "INSERT OR IGNORE INTO entry_giveaway_closed (guild_id, channel_id, closed_by, closed_by_name, closed_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, area_id, actor.id if actor else None, actor.name if actor else None, time.time()),
        )
        self._closed.setdefault(guild_id, set()).add(area_id)
        if added:
            await self.bot.activity_log.record(guild_id, actor, "entrygiveaways.close", detail=str(area_id))
        return bool(added)

    async def reopen(self, guild_id: int, area_id: int, actor: Any) -> bool:
        removed = await self.db.run("DELETE FROM entry_giveaway_closed WHERE guild_id = ? AND channel_id = ?", (guild_id, area_id))
        self._closed.get(guild_id, set()).discard(area_id)
        if removed:
            await self.bot.activity_log.record(guild_id, actor, "entrygiveaways.reopen", detail=str(area_id))
        return bool(removed)

    async def counts(self, guild_id: int) -> dict[int, int]:
        """Entries per giveaway."""
        rows = await self.db.all("SELECT channel_id, COUNT(*) AS n FROM entry_giveaway_entries WHERE guild_id = ? GROUP BY channel_id", (guild_id,))
        return {row["channel_id"]: row["n"] for row in rows}

    # ---- messages ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None:
            return
        setup = self._setup.get(guild.id)
        if setup is None or not self.on_in(guild.id):
            return
        area = area_of(message.channel, *setup)
        if area is None or self.is_closed(guild.id, area):
            return
        me = self.bot.user
        if me is None or message.author.id == me.id or (message.application_id is not None and message.application_id in (me.id, self.bot.application_id)):
            return  # the bot's own posts, its webhooks' among them
        if message.is_system() or message.id == message.channel.id:
            return  # a pin notice or a join line isn't an entry, and a forum post's first message is the giveaway itself
        if message.author.bot or message.webhook_id is not None:
            await self._delete(message)
            await self._warn_channel(message.channel)
            return
        if message.type not in PERSON_TYPES:
            return  # a thread's opening line and a poll's result come from Discord, not from the member
        staff = self.cfg(guild.id).get("staff_role")
        if staff and isinstance(message.author, discord.Member) and message.author.get_role(staff) is not None:
            return
        if await self._enter(guild.id, area, message) or await self._entry_gone(guild.id, area, message):
            return
        await self._warn_person(message)
        await self._delete(message)

    async def _enter(self, guild_id: int, area: int, message: discord.Message) -> bool:
        """Records the message as its author's entry. False when they already have one."""
        return bool(await self.db.run(
            "INSERT OR IGNORE INTO entry_giveaway_entries (guild_id, channel_id, user_id, message_id, entered_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, area, message.author.id, message.id, time.time()),
        ))

    async def _entry_gone(self, guild_id: int, area: int, message: discord.Message) -> bool:
        """Whether the member's stored entry was deleted where the bot couldn't see it. Discord replays no delete to a new session, so an
        entry made before this one is checked once a session, when its member posts again. When it's gone, the new message takes its place."""
        key = (guild_id, area, message.author.id)
        if key in self._checked:
            return False
        row = await self.db.one("SELECT message_id, entered_at FROM entry_giveaway_entries WHERE guild_id = ? AND channel_id = ? AND user_id = ?", key)
        if row is None:
            return await self._enter(guild_id, area, message)  # its delete arrived a moment ago
        if row["entered_at"] >= self._since:
            return False
        self._checked.add(key)
        try:
            await message.channel.fetch_message(row["message_id"])
        except discord.NotFound:
            await self.db.run("UPDATE entry_giveaway_entries SET message_id = ?, entered_at = ? WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
                              (message.id, time.time(), *key))
            return True
        except discord.HTTPException as error:
            log.info("Could not check entry %s in %s: %s", row["message_id"], area, error)
        return False

    @commands.Cog.listener()
    async def on_connect(self) -> None:
        """A new session, at the start or after one that couldn't resume. Deletes from before it never arrive."""
        self._since = time.time()
        self._checked.clear()

    async def _delete(self, message: discord.Message) -> None:
        await self._deletions.put(message)

    def _throttled(self, seen: dict[int, float], key: int, gap: float) -> bool:
        now = self._clock()
        if now - seen.get(key, float("-inf")) < gap:
            return True
        seen[key] = now
        if len(seen) > WARNINGS_KEPT:
            for old in [name for name, at in seen.items() if now - at >= gap]:
                seen.pop(old)
        return False

    async def _warn_person(self, message: discord.Message) -> None:
        if self._throttled(self._warned_people, message.author.id, USER_WARNING_GAP):
            return
        try:
            await message.channel.send(DUPLICATE_WARNING.format(mention=message.author.mention), delete_after=DUPLICATE_WARNING_SECONDS, allowed_mentions=discord.AllowedMentions(users=[message.author]))
        except discord.HTTPException as error:
            log.info("Could not warn about a second entry in %s: %s", message.channel.id, error)

    async def _warn_channel(self, channel: Any) -> None:
        if self._throttled(self._warned_channels, channel.id, CHANNEL_WARNING_GAP):
            return
        try:
            await channel.send(BOT_WARNING, delete_after=BOT_WARNING_SECONDS, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.info("Could not post the bot warning in %s: %s", channel.id, error)

    async def _delete_worker(self) -> None:
        """Deletes queued messages one at a time with a short gap, so a flood of second entries can't use up the bot's rate limit for every server."""
        while True:
            message = await self._deletions.get()
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as error:
                if error.status == 429:
                    await asyncio.sleep(float(getattr(error, "retry_after", 5) or 5) + 1)
                    await self._deletions.put(message)
                else:
                    log.info("Could not delete message %s in a giveaway: %s", message.id, error)
            except Exception:  # the worker serves every server, so it must outlive any one message
                log.exception("Deleting message %s in a giveaway failed", message.id)
            await asyncio.sleep(DELETE_GAP)

    async def finish_deletions(self, seconds: float = 10.0) -> None:
        """Wait for queued deletions, while the worker runs and for at most this long. The tests use this."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while not self._deletions.empty() and self._worker is not None and not self._worker.done() and loop.time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(DELETE_GAP + 0.05)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """A deleted entry frees its member to enter again, whoever deleted it."""
        if payload.guild_id is not None and payload.guild_id in self._setup:
            await self.db.run("DELETE FROM entry_giveaway_entries WHERE guild_id = ? AND message_id = ?", (payload.guild_id, payload.message_id))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is None or payload.guild_id not in self._setup or not payload.message_ids:
            return
        ids = sorted(payload.message_ids)
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            await self.db.run(f"DELETE FROM entry_giveaway_entries WHERE guild_id = ? AND message_id IN ({', '.join('?' * len(part))})", (payload.guild_id, *part))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        setup = self._setup.get(channel.guild.id)
        if setup is not None and setup[0] == channel.id:
            await self.clear_place(channel.guild.id, None)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        """A deleted giveaway thread takes its entries and its closed mark along. When the thread was the whole giveaway, it stops."""
        setup = self._setup.get(payload.guild_id)
        if setup is None:
            return
        target, kind = setup
        if payload.thread_id == target:
            await self.clear_place(payload.guild_id, None)
        elif kind == MULTI and payload.parent_id == target:
            await self.db.run("DELETE FROM entry_giveaway_entries WHERE guild_id = ? AND channel_id = ?", (payload.guild_id, payload.thread_id))
            await self.db.run("DELETE FROM entry_giveaway_closed WHERE guild_id = ? AND channel_id = ?", (payload.guild_id, payload.thread_id))
            self._closed.get(payload.guild_id, set()).discard(payload.thread_id)

    # ---- choosing a giveaway in a command ----

    async def _areas(self, guild: Any, *, archived: bool = True) -> list[tuple[Any, bool]]:
        """The giveaways a command can pick, each with whether it came from the archive: the one channel or thread, or a forum's posts."""
        setup = self._setup.get(guild.id)
        if setup is None:
            return []
        target, kind = setup
        if kind == SINGLE:
            found = guild.get_channel_or_thread(target)
            return [(found, False)] if found is not None else []
        forum = guild.get_channel(target)
        if not isinstance(forum, discord.ForumChannel):
            return []
        found: list[tuple[Any, bool]] = []
        if archived:
            try:
                async for thread in forum.archived_threads(limit=ARCHIVED_SHOWN):
                    found.append((thread, True))
            except discord.HTTPException:
                pass
        found += [(thread, False) for thread in forum.threads]
        return found

    async def _choices(self, interaction: discord.Interaction, current: str, *, closed: bool, overall: bool = False) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None or guild.id not in self._setup:
            return []
        needle = current.lower()
        choices = []
        if overall and (not current or "overall" in needle or "all" in needle):
            choices.append(app_commands.Choice(name=OVERALL_CHOICE, value=OVERALL))
        for channel, archived in await self._areas(guild):
            if current and needle not in channel.name.lower():
                continue
            shut = self.is_closed(guild.id, channel.id)
            if shut and not closed:
                continue
            name = label(channel) + (" (archived)" if archived else "") + (" (closed)" if shut else "")
            choices.append(app_commands.Choice(name=name[:100], value=str(channel.id)))
        return choices[:CHOICES]

    async def _find(self, guild: Any, channel_id: int) -> Any:
        """A channel or thread of this server, asking Discord for an archived thread the cache doesn't hold."""
        found = guild.get_channel_or_thread(channel_id)
        if found is not None:
            return found
        try:
            found = await guild.fetch_channel(channel_id)
        except (discord.HTTPException, discord.InvalidData):
            return None
        return found if getattr(found, "guild", None) is not None and found.guild.id == guild.id else None

    # ---- commands ----

    @commands.hybrid_command(name="set_giveaway_channel", description="Enforces one entry per user in a giveaway channel/thread (or one per giveaway thread in a forum)")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(target="The text channel, thread, or forum channel (omit to disable)")
    async def set_giveaway_channel(self, ctx: commands.Context, target: Optional[Union[discord.TextChannel, discord.Thread, discord.ForumChannel]] = None) -> None:
        actor = self.bot.actor(ctx.author)
        if target is None:
            setup = self._setup.get(ctx.guild.id)
            if setup is None:
                await ctx.send("No enforcement is currently active.", ephemeral=True)
                return
            old = ctx.guild.get_channel_or_thread(setup[0])
            await self.clear_place(ctx.guild.id, actor)
            await ctx.send(f"Enforcement disabled for {old.mention if old else '(deleted target)'}. Tracking cleared.", ephemeral=True)
            return
        refusal = self.place_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        kind = await self.set_place(ctx.guild.id, target, actor)
        what = "forum channel" if kind == MULTI else "thread" if isinstance(target, discord.Thread) else "text channel"
        await ctx.send(f"Enforcement enabled on {what} {target.mention}. Participant tracking reset.", ephemeral=True)

    @staticmethod
    def place_refusal(member: Any, channel: Any) -> str | None:
        """The bot deletes messages in a giveaway for whoever set it up, so they must be able to do that there themselves."""
        refusal = access.view_refusal(member, channel)
        if refusal:
            return refusal
        return None if access.can_manage_messages(member, channel) else "You need Manage Messages in that channel to limit entries there."

    @commands.hybrid_command(name="set_giveaway_staff", description="Set the role exempt from entry limits (staff can post freely and are excluded from draws)")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(role="The staff role (omit to unset)")
    async def set_giveaway_staff(self, ctx: commands.Context, role: Optional[discord.Role] = None) -> None:
        refusal = access.staff_role_refusal(ctx.author, role)
        if refusal:
            await ctx.fail(refusal)
            return
        old = self.cfg(ctx.guild.id).get("staff_role")
        await self.bot.settings.save(ctx.guild.id, self.key, {"staff_role": role.id if role else None}, actor=self.bot.actor(ctx.author), action="entrygiveaways.staff")
        if role is not None:
            await ctx.send(f"Giveaway staff role set to {role.mention}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        elif old:
            await ctx.send("Giveaway staff role unset.", ephemeral=True)
        else:
            await ctx.send("No staff role was previously set.", ephemeral=True)

    @commands.hybrid_command(name="mark_closed", description="Mark a giveaway as closed to stop new entries but allow free posting")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(channel="The giveaway channel/thread to close (use autocomplete)")
    async def mark_closed(self, ctx: commands.Context, channel: str) -> None:
        setup = self._setup.get(ctx.guild.id)
        if setup is None:
            await ctx.send("No active giveaway enforcement.", ephemeral=True)
            return
        target, kind = setup
        area_id = parse_area(channel)
        if area_id is None:
            await ctx.send(INVALID, ephemeral=True)
            return
        found = await self._find(ctx.guild, area_id)
        if found is None:
            await ctx.send(NOT_FOUND, ephemeral=True)
            return
        if kind == SINGLE:
            if area_id != target:
                await ctx.send("Selected channel/thread is not the active giveaway.", ephemeral=True)
                return
            if not await self.close(ctx.guild.id, area_id, self.bot.actor(ctx.author)):
                await ctx.send("This giveaway is already closed.", ephemeral=True)
                return
            await ctx.send(f"Giveaway in {found.mention} marked as closed. New entries stopped, free posting allowed.", ephemeral=True)
            return
        if not isinstance(found, discord.Thread) or found.parent_id != target:
            await ctx.send(NOT_IN_FORUM, ephemeral=True)
            return
        if not await self.close(ctx.guild.id, area_id, self.bot.actor(ctx.author)):
            await ctx.send("This giveaway thread is already closed.", ephemeral=True)
            return
        await ctx.send(f"Giveaway thread {found.mention} marked as closed. New entries stopped, free posting allowed.", ephemeral=True)

    @commands.hybrid_command(name="statistics", description="View current entry statistics for active giveaway(s)")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(channel="One giveaway, or leave it blank or pick Overall for totals across all of them")
    async def statistics(self, ctx: commands.Context, channel: Optional[str] = None) -> None:
        setup = self._setup.get(ctx.guild.id)
        if setup is None:
            await ctx.send(NOT_ACTIVE, ephemeral=True)
            return
        target, kind = setup
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        embed = discord.Embed(title="\N{PARTY POPPER} Giveaway Statistics", color=discord.Color.gold())
        counts = await self.counts(ctx.guild.id)
        if channel is None or channel.strip().lower() == OVERALL:
            embed.description = "Totals across all active giveaways"
            shown: list[tuple[str, int]] = []
            if kind == SINGLE:
                found = ctx.guild.get_channel_or_thread(target)
                if found is not None:
                    shown.append((label(found) + (" (closed)" if self.is_closed(ctx.guild.id, target) else ""), counts.get(target, 0)))
            else:
                for area_id, count in sorted(counts.items()):
                    thread = ctx.guild.get_channel_or_thread(area_id)
                    name = label(thread) if thread is not None else f"Post {area_id}"
                    shown.append((name + (" (closed)" if self.is_closed(ctx.guild.id, area_id) else ""), count))
            if not shown:
                embed.add_field(name="No entries yet", value="0 unique participants across all giveaways", inline=False)
            else:
                for name, count in shown[:24]:
                    embed.add_field(name=name[:256], value=f"{count} unique {plural(count, 'entry', 'entries')}", inline=False)
                areas = [target] if kind == SINGLE else list(counts)
                total = sum(counts.get(area, 0) for area in areas)
                unique = await self.db.scalar("SELECT COUNT(DISTINCT user_id) FROM entry_giveaway_entries WHERE guild_id = ?", (ctx.guild.id,)) if kind == MULTI else counts.get(target, 0)
                embed.add_field(name="**Grand Totals**", value=f"**{total:,}** total entries\n**{unique:,}** unique participants", inline=False)
            await ctx.send(embed=embed, ephemeral=True)
            return
        area_id = parse_area(channel)
        if area_id is None:
            await ctx.send(INVALID, ephemeral=True)
            return
        found = await self._find(ctx.guild, area_id)
        if found is None:
            await ctx.send(NOT_FOUND, ephemeral=True)
            return
        if kind == SINGLE and found.id != target:
            await ctx.send("Selected channel is not the active giveaway.", ephemeral=True)
            return
        if kind == MULTI and (not isinstance(found, discord.Thread) or found.parent_id != target):
            await ctx.send(NOT_IN_FORUM, ephemeral=True)
            return
        count = counts.get(found.id, 0)
        embed.description = f"Current entries in {found.mention}{' (closed)' if self.is_closed(ctx.guild.id, found.id) else ''}"
        embed.add_field(name="Unique Entries", value=f"{count:,} {plural(count, 'participant', 'participants')}")
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="draw", description="Draw random winner(s) from all valid entries in a channel or thread")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(channel="The giveaway channel/thread (only currently tracked giveaways are shown in autocomplete)", winners="Number of winners to draw (must be 1 or more)")
    async def draw(self, ctx: commands.Context, channel: str, winners: int) -> None:
        if winners < 1:
            await ctx.send("Number of winners must be at least 1.", ephemeral=True)
            return
        place_id = parse_area(channel)
        if place_id is None:
            await ctx.send(INVALID, ephemeral=True)
            return
        place = await self._find(ctx.guild, place_id)
        if place is None or not isinstance(place, (discord.TextChannel, discord.Thread)):
            await ctx.send("Selected channel/thread not found or invalid.", ephemeral=True)
            return
        refusal = access.read_refusal(ctx.author, place)
        if refusal:
            await ctx.fail(refusal)
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        await self.run_draw(ctx, place, winners)

    @mark_closed.autocomplete("channel")
    async def _close_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._choices(interaction, current, closed=False)

    @statistics.autocomplete("channel")
    async def _statistics_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._choices(interaction, current, closed=True, overall=True)

    @draw.autocomplete("channel")
    async def _draw_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._choices(interaction, current, closed=True)

    async def run_draw(self, ctx: commands.Context, place: Any, winners: int) -> None:
        guild = ctx.guild
        color = getattr(ctx.author, "color", None) or discord.Color.default()
        progress = discord.Embed(title="Scanning Giveaway Entries", description="Fetching all messages... This may take a while for large history.", color=color)
        shown = await ctx.send(embed=progress, ephemeral=True)

        async def show(text: str) -> None:
            nonlocal shown
            progress.description = text
            if shown is None:
                return
            try:
                await shown.edit(embed=progress)
            except discord.HTTPException:
                shown = None  # an interaction's messages can't be edited after 15 minutes, and the scan goes on

        staff_id = self.cfg(guild.id).get("staff_role")
        whole_list = bool(getattr(guild, "chunked", False))
        after, before = await self._window(guild.id, place)
        latest: dict[int, int] = {}
        can_win: dict[int, bool] = {}
        scanned = 0
        async for message in place.history(limit=None, after=after, before=before):
            scanned += 1
            author = message.author
            if not (author.bot or message.webhook_id is not None or message.type not in PERSON_TYPES or message.id == place.id):
                if author.id not in can_win:
                    can_win[author.id] = await self._can_win(guild, author.id, staff_id, whole_list)
                if can_win[author.id]:
                    latest[author.id] = max(latest.get(author.id, 0), message.id)
            if scanned % PROGRESS_EVERY == 0:
                await show(f"Scanned {scanned:,} messages...\nFound {len(latest):,} unique participants so far.")
        total = len(latest)
        await show(f"Scan complete!\nFound {total:,} unique participants with valid entries.")
        if total == 0:
            await self._announce(ctx, "No valid entry messages found in the channel/thread.", [])
            await show(f"Scan complete!\nFound {total:,} unique participants with valid entries.\nNo winners drawn.")
            return
        count = min(winners, total)
        picked = self._random.sample(sorted(latest), count)
        jump = f"https://discord.com/channels/{guild.id}/{place.id}"
        lines = [f"\N{PARTY POPPER} **WINNER{'S' if count > 1 else ''} DRAWN!** \N{PARTY POPPER}", ""]
        if count == 1:
            lines.append(f"Congratulations <@{picked[0]}>!\nClick [here]({jump}/{latest[picked[0]]}) to view their latest entry.")
        else:
            lines.append("Congratulations to our winners:\n")
            lines += [f"{index}. <@{user_id}> - [View latest entry]({jump}/{latest[user_id]})" for index, user_id in enumerate(picked, 1)]
            if count < winners:
                lines.append(f"\n*Note: Only {total:,} unique participants found, so {count} winner{'s' if count > 1 else ''} selected.*")
        lines.append(f"\nDrawn fairly from **{total:,}** unique participant{'s' if total > 1 else ''}.")
        await self._announce(ctx, "\n".join(lines), picked)
        await show(f"Draw complete! Selected {count} unique winner{'s' if count > 1 else ''} from {total:,} participants.")

    async def _window(self, guild_id: int, place: Any) -> tuple[discord.Object | None, discord.Object | None]:
        """Where the giveaway's entries lie in a channel or post's history: after its start and before its close. Its start is when it was
        set, or its oldest entry when that's older, as an imported giveaway's are. A place the giveaway doesn't track is read whole."""
        setup = self._setup.get(guild_id)
        area = area_of(place, *setup) if setup is not None else None
        if area is None:
            return None, None
        set_at = await self.db.scalar("SELECT set_at FROM entry_giveaway_setup WHERE guild_id = ?", (guild_id,))
        oldest = await self.db.scalar("SELECT MIN(message_id) FROM entry_giveaway_entries WHERE guild_id = ? AND channel_id = ?", (guild_id, area))
        closed_at = await self.db.scalar("SELECT closed_at FROM entry_giveaway_closed WHERE guild_id = ? AND channel_id = ?", (guild_id, area))
        starts = [snowflake_at(set_at)] if set_at is not None else []
        if oldest is not None:
            starts.append(oldest - 1)
        start = min(starts, default=0)
        end = snowflake_at(closed_at) if closed_at is not None else 0
        return (discord.Object(start) if start > 0 else None), (discord.Object(end) if end > 0 else None)

    async def _can_win(self, guild: Any, user_id: int, staff_id: int | None, whole_list: bool) -> bool:
        """Whether someone who posted can be drawn: still in the server, and without the staff role. The server's member list answers, or
        Discord when that list isn't whole. A lookup that fails for another reason costs nobody their chance."""
        member = guild.get_member(user_id)
        if member is None and not whole_list:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return False
            except discord.HTTPException:
                return True
        if member is None:
            return False
        return not (staff_id and member.get_role(staff_id) is not None)

    async def _announce(self, ctx: commands.Context, text: str, winners: list[int]) -> None:
        """Public posts that ping only the winners, each under Discord's 2,000 characters with no line split across two, and each naming only the winners it holds.
        When the command's own answer can't go out anymore, they go in the channel."""
        for part in split_lines(text):
            named = [discord.Object(user_id) for user_id in winners if f"<@{user_id}>" in part]
            mentions = discord.AllowedMentions(everyone=False, roles=False, users=named)
            try:
                await ctx.send(part, allowed_mentions=mentions)
            except discord.HTTPException:
                await ctx.channel.send(part, allowed_mentions=mentions)

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/entrygiveaways", self._view_endpoint)
        router.put("/entrygiveaways/channel", self._place_endpoint)
        router.post(r"/entrygiveaways/areas/{area_id:\d+}/close", self._close_endpoint)
        router.post(r"/entrygiveaways/areas/{area_id:\d+}/reopen", self._reopen_endpoint)

    async def _view(self, guild_id: int) -> dict:
        row = await self.db.one("SELECT channel_id, kind, set_by_name, set_at FROM entry_giveaway_setup WHERE guild_id = ?", (guild_id,))
        counts = await self.counts(guild_id)
        closed = self._closed.get(guild_id, set())
        areas = sorted(set(counts) | closed | ({row["channel_id"]} if row and row["kind"] == SINGLE else set()))
        unique = await self.db.scalar("SELECT COUNT(DISTINCT user_id) FROM entry_giveaway_entries WHERE guild_id = ?", (guild_id,))
        guild = self.bot.get_guild(guild_id)
        named = {area: label(found) for area in {*areas, *([row["channel_id"]] if row else [])} if guild is not None and (found := guild.get_channel_or_thread(area)) is not None}
        return {
            "setup": {**row, "name": named.get(row["channel_id"])} if row else None,
            "areas": [{"area_id": area, "name": named.get(area), "entries": counts.get(area, 0), "closed": area in closed} for area in areas],
            "total": sum(counts.values()),
            "unique": unique,
        }

    async def _view_endpoint(self, req: Any) -> dict:
        return await self._view(req.guild_id)

    async def _place_endpoint(self, req: Any) -> dict:
        form = await req.json()
        value = form.get("channel_id")
        if value in (None, ""):
            if req.guild_id in self._setup:
                await self.clear_place(req.guild_id, req.actor)
            return await self._view(req.guild_id)
        channel_id = req.channel(value)
        if not req.can_manage_messages(channel_id):
            raise ValidationError({"channel_id": "You need Manage Messages in that channel to limit entries there."})
        row = req.ops.channel(req.guild_id, channel_id) or {}
        kinds = {discord.ChannelType.text.value, discord.ChannelType.news.value, discord.ChannelType.forum.value, discord.ChannelType.media.value, *(kind.value for kind in (discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread))}
        if row.get("type") not in kinds:
            raise ValidationError({"channel_id": "Pick a text channel, a thread or a forum."})
        guild = self.bot.get_guild(req.guild_id)
        found = guild.get_channel_or_thread(channel_id) if guild is not None else None
        place = found if found is not None else _Place(channel_id, row.get("name") or str(channel_id), row["type"])
        await self.set_place(req.guild_id, place, req.actor)
        return await self._view(req.guild_id)

    def _area_or_404(self, req: Any) -> int:
        area_id = req.int_param("area_id")
        setup = self._setup.get(req.guild_id)
        if setup is None:
            raise ApiError(404, NOT_ACTIVE)
        target, kind = setup
        if kind == SINGLE and area_id != target:
            raise ApiError(404, "That isn't the giveaway.")
        if kind == MULTI:
            guild = self.bot.get_guild(req.guild_id)
            found = guild.get_channel_or_thread(area_id) if guild is not None else None
            known = area_id in self._closed.get(req.guild_id, set())
            if not known and not (isinstance(found, discord.Thread) and found.parent_id == target):
                raise ApiError(404, NOT_IN_FORUM)
        return area_id

    async def _close_endpoint(self, req: Any) -> dict:
        await self.close(req.guild_id, self._area_or_404(req), req.actor)
        return await self._view(req.guild_id)

    async def _reopen_endpoint(self, req: Any) -> dict:
        await self.reopen(req.guild_id, self._area_or_404(req), req.actor)
        return await self._view(req.guild_id)

    async def seed_demo(self, guild_id: int) -> None:
        """A sample giveaway for the demo dashboard."""
        info = self.bot.ops.describe_guild(guild_id) or {}
        channels = {row["name"]: row for row in info.get("channels", [])}
        roles = {row["name"]: row["id"] for row in info.get("roles", [])}
        forum = channels.get("help-forum")
        if forum is None:
            return
        now = time.time()
        await self.db.run("INSERT INTO entry_giveaway_setup (guild_id, channel_id, kind, set_by, set_by_name, set_at) VALUES (?, ?, ?, ?, ?, ?)", (guild_id, forum["id"], MULTI, guild_id + 2, "Demo Admin", now - 86400))
        posts = (900000000000300001, 900000000000300002)
        for index in range(12):
            await self.db.run("INSERT INTO entry_giveaway_entries (guild_id, channel_id, user_id, message_id, entered_at) VALUES (?, ?, ?, ?, ?)",
                              (guild_id, posts[index % 2], 900000000000200000 + index, 900000000000310000 + index, now - 3600 + index))
        await self.db.run("INSERT INTO entry_giveaway_closed (guild_id, channel_id, closed_by, closed_by_name, closed_at) VALUES (?, ?, ?, ?, ?)", (guild_id, posts[1], guild_id + 2, "Demo Admin", now - 600))
        await self.bot.settings.save(guild_id, self.key, {"enabled": True, "staff_role": roles.get("Moderator")})
        await self._load(guild_id)


class _Place:
    """A channel the dashboard named, when the bot's cache doesn't hold it, as set_place reads one."""

    def __init__(self, channel_id: int, name: str, kind: int) -> None:
        self.id = channel_id
        self.name = name
        self.type = discord.ChannelType(kind)

