"""Feeds and autofeeds: announcements that ping a role, sent now or on a schedule.

A feed pairs a name with a role and a channel. A role that members can't ping themselves is made mentionable just long enough to send,
unless the bot holds Mention Everyone in that channel, which needs no change to the role at all.
"""
from __future__ import annotations

import calendar
import datetime
import json
import logging
import re
import time
import zoneinfo
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, delivery, messages
from ..core.durations import parse_duration
from ..core.module import Module, requires
from ..core.tagscript import Scope, render_message
from ..core.validate import Invalid, ValidationError, integer, number, snowflake, text
from ..web.api import ApiError

log = logging.getLogger(__name__)

MAX_FEEDS = 200
MAX_AUTOFEEDS = 200
LEAD_SECONDS = 60
LONGEST_LEAD = 5 * 365 * 86400
SHORTEST_REPEAT = 3600
REPEAT_LIMITS = (120, 3650, 8760, 525600)  # months, days, hours, minutes
CATCH_UP_STEPS = 10_000
SILENT, EVERYONE, HERE = "silent", "everyone", "here"
SPECIAL_MENTIONS = (SILENT, EVERYONE, HERE)
RETRY_SECONDS = 3600
BAD_REPEAT = "This schedule's repeat is out of range, so it posted and stopped. Edit it to set a new one."
ROLE_GONE = "Posted, but the role no longer exists, so nobody was pinged. Edit the autofeed to pick another one."
NOT_PINGED = "Posted, but the ping didn't go out. The bot needs Mention Everyone in that channel, or Manage Roles and a place above the role."
FEED_ROLE_GONE = "but the role wasn't pinged, because it no longer exists. Delete this feed and make it again with a new role, or change the role on the dashboard."


def _zone(name: str | None) -> Any:
    if not name:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None


def clean_zone(value: Any) -> str | None:
    """A time zone name from a browser, or None when this machine doesn't know it. The schedule then runs on UTC."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_+\-/]{1,64}", value) and _zone(value) is not None:
        return value
    return None


def add_months(moment: datetime.datetime, months: int) -> datetime.datetime:
    """The same day of a later month, or that month's last day when it's shorter."""
    total = moment.month - 1 + months
    year, month = moment.year + total // 12, total % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def next_run(current: float, repeat: tuple[int, int, int, int], now: float, zone_name: str | None = None) -> float | None:
    """The next time after `now` on this schedule, or None if it doesn't repeat. Months and days keep the local clock time across a clock change."""
    months, days, hours, minutes = repeat
    # A schedule can come from a backup file someone edited, and this runs on the thread the whole bot shares, so it can't go backward, run past what a date can hold or take long. Nothing outside what a form accepts counts as a repeat.
    if not any(repeat) or min(repeat) < 0 or any(count > limit for count, limit in zip(repeat, REPEAT_LIMITS)) or repeat_seconds(repeat) < SHORTEST_REPEAT:
        return None
    zone = _zone(zone_name) or datetime.timezone.utc
    moment = current
    try:
        for _ in range(CATCH_UP_STEPS):
            step = datetime.datetime.fromtimestamp(moment, zone)
            if months:
                step = add_months(step, months)
            if days:
                step += datetime.timedelta(days=days)
            following = step.timestamp() + hours * 3600 + minutes * 60
            if following <= moment:
                return None
            moment = following
            if moment > now:
                return moment
    except (ValueError, OverflowError, OSError):
        return None
    return now + repeat_seconds(repeat)


def repeat_seconds(repeat: tuple[int, int, int, int]) -> int:
    return repeat[0] * 2592000 + repeat[1] * 86400 + repeat[2] * 3600 + repeat[3] * 60


def repeat_text(repeat: tuple[int, int, int, int]) -> str:
    parts = [f"{count} {name}{'' if count == 1 else 's'}" for count, name in zip(repeat, ("month", "day", "hour", "minute")) if count]
    return ", ".join(parts) or "never"


def parse_when(value: str, now: float) -> float | None:
    """A length of time from now, such as 18h, or a UTC date and time, such as 2026-12-24 18:00."""
    seconds = parse_duration(value)
    if seconds is not None:
        return now + seconds
    try:
        moment = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)).timestamp()


def mention_label(row: dict) -> str:
    if row["mention"] == EVERYONE:
        return "@everyone"
    if row["mention"] == HERE:
        return "@here"
    return f"<@&{row['role_id']}>" if row["role_id"] else "silent"


def _ping_refusal(member: Any, channel: Any, mention: str, role_id: int | None) -> str | None:
    """A ping of @everyone, @here or a role members can't ping goes out through the bot, so it takes what it would take them."""
    if mention in (EVERYONE, HERE) or (mention not in SPECIAL_MENTIONS and role_id and delivery.needs_ping_right(member.guild, role_id)):
        return access.ping_refusal(member, channel)
    return None


class ConfirmClear(discord.ui.View):
    """Asks before deleting a whole list."""

    def __init__(self, author_id: int, clear: Any) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.clear = clear

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
        return False

    @discord.ui.button(label="Delete them all", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content=await self.clear(), view=None)

    @discord.ui.button(label="Keep them", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Nothing was deleted.", view=None)


class Feeds(Module):
    key = "autofeeds"
    category = "Feeds"
    role_timers = ("autofeed",)
    row_limits = {"feeds": MAX_FEEDS, "autofeeds": MAX_AUTOFEEDS}
    migrations = [
        """
        CREATE TABLE feeds (
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            role_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            created_by INTEGER,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, name)
        );
        CREATE TABLE autofeeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            mention TEXT NOT NULL,
            role_id INTEGER,
            payload TEXT NOT NULL,
            next_at REAL NOT NULL,
            repeat_months INTEGER NOT NULL DEFAULT 0,
            repeat_days INTEGER NOT NULL DEFAULT 0,
            repeat_hours INTEGER NOT NULL DEFAULT 0,
            repeat_minutes INTEGER NOT NULL DEFAULT 0,
            timezone TEXT,
            created_by INTEGER,
            created_at REAL NOT NULL,
            last_at REAL,
            last_error TEXT
        );
        CREATE INDEX autofeeds_guild ON autofeeds (guild_id, next_at);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._clock = time.time

    async def on_load(self) -> None:
        self.bot.timers.handler("autofeed", self._autofeed_due)
        await self._arm_missing()

    @commands.Cog.listener()
    async def on_guild_available(self, guild: Any) -> None:
        await delivery.restore_held_roles(self.db, guild)

    async def on_guild_restored(self, guild_id: int) -> None:
        await self._skip_past_due(guild_id)
        await self._arm_missing()

    async def _skip_past_due(self, guild_id: int) -> None:
        """A post whose time passed before this backup came back went out back then or is out of date. A repeating one waits for its next time, and a one-time one says what happened."""
        now = self._clock()
        for row in await self.db.all("SELECT * FROM autofeeds WHERE guild_id = ? AND next_at <= ? AND last_error IS NULL", (guild_id, now)):
            try:
                following = next_run(row["next_at"], self._repeat_of(row), now, row["timezone"])
            except (ValueError, OverflowError, TypeError):
                following = None
            if following is None:
                await self.db.run("UPDATE autofeeds SET last_error = ? WHERE id = ?", ("Its time passed before this backup came back. Set it again if it should go out.", row["id"]))
                await self.bot.timers.cancel_key("autofeed", str(row["id"]))
            else:
                await self.db.run("UPDATE autofeeds SET next_at = ? WHERE id = ?", (following, row["id"]))
                await self._arm(row["id"], guild_id, following)

    async def _arm_missing(self) -> None:
        """A restored backup can bring autofeeds back without their timers. One that had already failed for good stays as it is until someone edits it."""
        missing = await self.db.all(
            "SELECT a.id, a.guild_id, a.next_at FROM autofeeds a LEFT JOIN timers t ON t.kind = 'autofeed' AND t.key = CAST(a.id AS TEXT) WHERE t.id IS NULL AND a.last_error IS NULL"
        )
        for row in missing:
            await self._arm(row["id"], row["guild_id"], row["next_at"])

    async def _arm(self, autofeed_id: int, guild_id: int, due: float) -> None:
        await self.bot.timers.schedule("autofeed", due, {"id": autofeed_id}, guild_id=guild_id, key=str(autofeed_id))

    # ---- sending with a mention ----

    async def send_with_mention(self, guild: Any, channel: Any, payload: dict, mention: str, role_id: int | None) -> bool:
        """Post a message with its ping in front. Returns whether the ping could go out. Discord's refusal to post is left to the caller."""
        content = payload.get("content") or ""
        if mention in (EVERYONE, HERE):
            allowed = discord.AllowedMentions(everyone=True, roles=False, users=True)
            pinged = channel.permissions_for(guild.me).mention_everyone
            content = f"@{mention} {content}".strip()
            return await self._send(channel, payload, content, allowed, pinged)
        role = guild.get_role(role_id) if role_id else None
        if mention == SILENT or role is None:
            return await self._send(channel, payload, content, discord.AllowedMentions(everyone=False, roles=False, users=True), mention == SILENT)
        content = f"{role.mention} {content}".strip()
        allowed = discord.AllowedMentions(everyone=False, roles=[role], users=True)
        async with delivery.role_pings(self.db, guild, channel, [role.id]) as pinged:
            return await self._send(channel, payload, content, allowed, pinged)

    @staticmethod
    async def _send(channel: Any, payload: dict, content: str, allowed: discord.AllowedMentions, pinged: bool) -> bool:
        await channel.send(**messages.send_kwargs({**payload, "content": content[:messages.CONTENT_MAX]}, allowed_mentions=allowed))
        return pinged

    # ---- running autofeeds ----

    @staticmethod
    def _repeat_of(row: dict) -> tuple[int, int, int, int]:
        return row["repeat_months"], row["repeat_days"], row["repeat_hours"], row["repeat_minutes"]

    async def _autofeed_due(self, payload: dict, timer: dict) -> None:
        row = await self.db.one("SELECT * FROM autofeeds WHERE id = ?", (payload["id"],))
        if row is None:
            return
        now = self._clock()
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            await self._arm(row["id"], row["guild_id"], now + RETRY_SECONDS)  # a server can be away for a while, so the post waits for it
            return
        # Worked out before posting, so a schedule with no next time posts once rather than each time the timer tries again.
        following = next_run(row["next_at"], self._repeat_of(row), now, row["timezone"])
        error_text = await self._post_autofeed(guild, row, now)
        if following is None and any(self._repeat_of(row)):
            error_text = error_text or BAD_REPEAT
        if following is not None:
            await self.db.run("UPDATE autofeeds SET next_at = ?, last_at = ?, last_error = ? WHERE id = ?", (following, now, error_text, row["id"]))
            await self._arm(row["id"], row["guild_id"], following)
        elif error_text:
            await self.db.run("UPDATE autofeeds SET last_at = ?, last_error = ? WHERE id = ?", (now, error_text, row["id"]))  # kept, so the page can show what went wrong
        else:
            await self.db.run("DELETE FROM autofeeds WHERE id = ?", (row["id"],))

    async def _post_autofeed(self, guild: Any, row: dict, now: float) -> str | None:
        """Post it, and return why not when that's something only a person can fix. Anything else raises, so the timer tries again."""
        channel = await delivery.find_channel(guild, row["channel_id"])
        if channel is None:
            return "The channel no longer exists."
        if not delivery.holds_messages(channel):
            return delivery.NOT_POSTABLE
        scope = Scope(guild=guild, channel=channel, extra={"unix": str(int(now))})
        payload = await render_message(json.loads(row["payload"]), scope, guild_id=row["guild_id"])
        try:
            pinged = await self.send_with_mention(guild, channel, payload, row["mention"], row["role_id"])
        except (discord.Forbidden, discord.NotFound) as error:
            log.info("Autofeed %s couldn't post in %s: %s", row["id"], guild.id, error)
            return "The bot can't post in that channel."
        except discord.HTTPException as error:
            if 400 <= error.status < 500:  # the message itself was refused, so trying it again can't help
                log.info("Discord refused autofeed %s in %s: %s", row["id"], guild.id, error)
                return f"Discord refused the message: {error.text or error.status}"
            raise
        if pinged:
            return None
        return ROLE_GONE if row["mention"] == "role" and guild.get_role(row["role_id"]) is None else NOT_PINGED

    # ---- commands: /feeds ----

    async def _refuse_hidden(self, ctx: commands.Context, channel_id: int) -> bool:
        """A row in a channel this person can't see stays out of their reach, as it does on the dashboard. A channel that's gone has nothing left to hide."""
        channel = ctx.guild.get_channel_or_thread(channel_id)
        refusal = access.view_refusal(ctx.author, channel) if channel is not None else None
        if refusal:
            await ctx.fail(refusal)
            return True
        return False

    @staticmethod
    def _visible_rows(ctx: commands.Context, rows: list[dict]) -> tuple[list[dict], int]:
        """The rows this person can reach, and how many they can't."""
        chosen = []
        for row in rows:
            channel = ctx.guild.get_channel_or_thread(row["channel_id"])
            if channel is None or access.can_view(ctx.author, channel):
                chosen.append(row)
        return chosen, len(rows) - len(chosen)

    @staticmethod
    def _hidden_note(hidden: int) -> str:
        return f" {hidden} in channels you can't see stay." if hidden else ""

    def _nothing_to_clear(self, what: str, hidden: int) -> str:
        return f"There are no {what} to delete.{self._hidden_note(hidden)}"

    async def _find_feed(self, ctx: commands.Context, name: str) -> dict | None:
        row = await self.db.one("SELECT * FROM feeds WHERE guild_id = ? AND name = ?", (ctx.guild.id, name))
        if row is None:
            await ctx.fail(f"There's no feed called `{discord.utils.escape_markdown(name)}`.")
        return row

    @commands.hybrid_group(name="feeds", fallback="list", description="List this server's feeds")
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @requires("manage_guild")
    async def feeds(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT * FROM feeds WHERE guild_id = ? ORDER BY name", (ctx.guild.id,))
        lines = [f"`{row['name']}`: <@&{row['role_id']}> in <#{row['channel_id']}>" for row in rows]
        await ctx.send(embed=discord.Embed(title="Feeds", description=delivery.clipped(lines) or "There are no feeds yet. Make one with `/feeds create`.", color=0x00A2F8), allowed_mentions=discord.AllowedMentions.none())

    @feeds.command(name="create", description="Make a feed in this channel that pings a role")
    @requires("manage_guild")
    async def feeds_create(self, ctx: commands.Context, name: str, role: discord.Role) -> None:
        name = name.strip()
        if not name or len(name) > 50 or "\n" in name:
            await ctx.fail("A feed's name is one line of at most 50 characters.")
            return
        if role.is_default():
            await ctx.fail("Use `/feeds autofeeds everyone` for @everyone. A feed needs its own role.")
            return
        refusal = access.post_refusal(ctx.author, ctx.channel) or _ping_refusal(ctx.author, ctx.channel, "role", role.id)
        if refusal:
            await ctx.fail(refusal)
            return
        if await self.db.scalar("SELECT 1 FROM feeds WHERE guild_id = ? AND name = ?", (ctx.guild.id, name)):
            await ctx.fail(f"There's already a feed called `{discord.utils.escape_markdown(name)}`.")
            return
        if await self.db.scalar("SELECT COUNT(*) FROM feeds WHERE guild_id = ?", (ctx.guild.id,)) >= MAX_FEEDS:
            await ctx.fail(f"A server can hold at most {MAX_FEEDS} feeds.")
            return
        await self.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)", (ctx.guild.id, name, role.id, ctx.channel.id, ctx.author.id, self._clock()))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "feed.create", detail=name)
        await ctx.ok(f"Feed `{discord.utils.escape_markdown(name)}` pings {role.mention} in {ctx.channel.mention}.")

    @feeds.command(name="announce", description="Post to a feed and ping its role")
    @requires("manage_guild")
    async def feeds_announce(self, ctx: commands.Context, name: str, *, content: str) -> None:
        row = await self._find_feed(ctx, name)
        if row is None:
            return
        channel = ctx.guild.get_channel_or_thread(row["channel_id"])
        if channel is None:
            await ctx.fail("That feed's channel no longer exists. Point it somewhere with `/feeds move`.")
            return
        # The channel and the role were chosen earlier, maybe by someone else, and this person's text goes out there now.
        refusal = access.post_refusal(ctx.author, channel) or _ping_refusal(ctx.author, channel, "role", row["role_id"])
        if refusal:
            await ctx.fail(refusal)
            return
        await ctx.defer()
        try:
            pinged = await self.send_with_mention(ctx.guild, channel, {"content": content, "embeds": [], "buttons": []}, "role", row["role_id"])
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused the announcement: {error.text or error.status}")
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "feed.announce", detail=row["name"])
        if ctx.guild.get_role(row["role_id"]) is None:
            await ctx.ok(f"Announced in {channel.mention}, {FEED_ROLE_GONE}")
        elif pinged:
            await ctx.ok(f"Announced in {channel.mention}.")
        else:
            await ctx.ok(f"Announced in {channel.mention}, but the role wasn't pinged. The bot needs Mention Everyone there, or Manage Roles and a place above the role.")

    @feeds.command(name="delete", aliases=["del", "remove"], description="Delete a feed")
    @requires("manage_guild")
    async def feeds_delete(self, ctx: commands.Context, name: str) -> None:
        row = await self._find_feed(ctx, name)
        if row is None or await self._refuse_hidden(ctx, row["channel_id"]):
            return
        await self.db.run("DELETE FROM feeds WHERE guild_id = ? AND name = ?", (ctx.guild.id, name))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "feed.delete", detail=row["name"])
        await ctx.ok(f"Deleted feed `{discord.utils.escape_markdown(row['name'])}`.")

    @feeds.command(name="clear", description="Delete every feed in this server")
    @requires("manage_guild")
    async def feeds_clear(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT name, channel_id FROM feeds WHERE guild_id = ?", (ctx.guild.id,))
        chosen, hidden = self._visible_rows(ctx, rows)
        if not chosen:
            await ctx.fail(self._nothing_to_clear("feeds", hidden))
            return

        async def clear() -> str:
            for row in chosen:
                await self.db.run("DELETE FROM feeds WHERE guild_id = ? AND name = ?", (ctx.guild.id, row["name"]))
            await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "feed.clear", detail=str(len(chosen)))
            return f"Deleted {len(chosen)} feeds.{self._hidden_note(hidden)}"
        await ctx.send(f"Delete all {len(chosen)} feeds?{self._hidden_note(hidden)}", view=ConfirmClear(ctx.author.id, clear), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @feeds.command(name="move", description="Move a feed to another channel")
    @requires("manage_guild")
    async def feeds_move(self, ctx: commands.Context, name: str, channel: discord.TextChannel) -> None:
        row = await self._find_feed(ctx, name)
        if row is None or await self._refuse_hidden(ctx, row["channel_id"]):
            return
        refusal = access.post_refusal(ctx.author, channel) or _ping_refusal(ctx.author, channel, "role", row["role_id"])
        if refusal:
            await ctx.fail(refusal)
            return
        await self.db.run("UPDATE feeds SET channel_id = ? WHERE guild_id = ? AND name = ?", (channel.id, ctx.guild.id, name))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "feed.move", detail=row["name"])
        await ctx.ok(f"Feed `{discord.utils.escape_markdown(row['name'])}` now posts in {channel.mention}.")

    @feeds_announce.autocomplete("name")
    @feeds_delete.autocomplete("name")
    @feeds_move.autocomplete("name")
    async def _feed_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = await self.db.all("SELECT name FROM feeds WHERE guild_id = ? AND name LIKE ? ORDER BY name LIMIT 25", (interaction.guild_id, f"{current}%"))
        return [app_commands.Choice(name=row["name"], value=row["name"]) for row in rows]

    # ---- commands: /feeds autofeeds ----

    @feeds.group(name="autofeeds", fallback="list", description="List this server's scheduled announcements")
    @requires("manage_guild")
    async def autofeeds(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT * FROM autofeeds WHERE guild_id = ? ORDER BY next_at", (ctx.guild.id,))
        lines = []
        for row in rows:
            channel = ctx.guild.get_channel_or_thread(row["channel_id"])
            hidden = channel is not None and not access.can_view(ctx.author, channel)
            text_ = "(in a channel you can't see)" if hidden else (json.loads(row["payload"]).get("content") or "(embed)").replace("\n", " ")
            repeats = repeat_text(self._repeat_of(row))
            lines.append(f"**{row['id']}** <#{row['channel_id']}> {mention_label(row)} <t:{int(row['next_at'])}:R>{'' if repeats == 'never' else f', every {repeats}'}: {discord.utils.escape_markdown(text_[:60])}")
        await ctx.send(embed=discord.Embed(title="Autofeeds", description=delivery.clipped(lines) or "There are no autofeeds yet.", color=0x00A2F8), allowed_mentions=discord.AllowedMentions.none())

    async def _command_create(self, ctx: commands.Context, mention: str, role_id: int | None, when: str, message: str) -> None:
        refusal = access.post_refusal(ctx.author, ctx.channel) or _ping_refusal(ctx.author, ctx.channel, mention, role_id)
        if refusal:
            await ctx.fail(refusal)
            return
        now = self._clock()
        due = parse_when(when, now)
        if due is None:
            await ctx.fail(f"`{discord.utils.escape_markdown(when)}` isn't a time. Try `18h` or `2026-12-24 18:00` in UTC. The dashboard takes your local time.")
            return
        if due < now + LEAD_SECONDS or due > now + LONGEST_LEAD:
            await ctx.fail("Schedule it at least a minute ahead and at most five years ahead.")
            return
        try:
            payload = messages.clean_payload({"content": message})
        except Invalid as error:
            await ctx.fail(str(error))
            return
        if await self.db.scalar("SELECT COUNT(*) FROM autofeeds WHERE guild_id = ?", (ctx.guild.id,)) >= MAX_AUTOFEEDS:
            await ctx.fail(f"A server can hold at most {MAX_AUTOFEEDS} autofeeds.")
            return
        autofeed_id = await self._insert(ctx.guild.id, ctx.channel.id, mention, role_id, payload, due, (0, 0, 0, 0), None, ctx.author.id)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autofeed.create", detail=str(autofeed_id))
        await ctx.ok(f"Autofeed {autofeed_id} posts in {ctx.channel.mention} <t:{int(due)}:R>.")

    @autofeeds.command(name="create", description="Schedule an announcement in this channel that pings a role")
    @requires("manage_guild")
    async def autofeeds_create(self, ctx: commands.Context, role: discord.Role, when: str, *, message: str) -> None:
        if role.is_default():
            await self._command_create(ctx, EVERYONE, None, when, message)
        else:
            await self._command_create(ctx, "role", role.id, when, message)

    @autofeeds.command(name="everyone", description="Schedule an announcement in this channel that pings @everyone")
    @requires("manage_guild")
    async def autofeeds_everyone(self, ctx: commands.Context, when: str, *, message: str) -> None:
        await self._command_create(ctx, EVERYONE, None, when, message)

    @autofeeds.command(name="here", description="Schedule an announcement in this channel that pings @here")
    @requires("manage_guild")
    async def autofeeds_here(self, ctx: commands.Context, when: str, *, message: str) -> None:
        await self._command_create(ctx, HERE, None, when, message)

    @autofeeds.command(name="silent", description="Schedule an announcement in this channel that pings nobody")
    @requires("manage_guild")
    async def autofeeds_silent(self, ctx: commands.Context, when: str, *, message: str) -> None:
        await self._command_create(ctx, SILENT, None, when, message)

    async def _find_autofeed(self, ctx: commands.Context, autofeed_id: int) -> dict | None:
        row = await self.db.one("SELECT * FROM autofeeds WHERE id = ? AND guild_id = ?", (autofeed_id, ctx.guild.id))
        if row is None:
            await ctx.fail(f"There's no autofeed {autofeed_id} in this server.")
            return None
        return None if await self._refuse_hidden(ctx, row["channel_id"]) else row

    @autofeeds.command(name="silence", description="Stop an autofeed from pinging anyone")
    @requires("manage_guild")
    async def autofeeds_silence(self, ctx: commands.Context, id: int) -> None:
        if await self._find_autofeed(ctx, id) is None:
            return
        await self.db.run("UPDATE autofeeds SET mention = ?, role_id = NULL WHERE id = ? AND guild_id = ?", (SILENT, id, ctx.guild.id))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autofeed.silence", detail=str(id))
        await ctx.ok(f"Autofeed {id} no longer pings anyone.")

    @autofeeds.command(name="repeat", description="Repeat an autofeed at this interval until it's deleted, or use off to stop")
    @requires("manage_guild")
    async def autofeeds_repeat(self, ctx: commands.Context, id: int, when: str) -> None:
        row = await self._find_autofeed(ctx, id)
        if row is None:
            return
        if when.strip().lower() in ("off", "stop", "never", "0"):
            repeat = (0, 0, 0, 0)
        else:
            seconds = parse_duration(when)
            if seconds is None:
                await ctx.fail(f"`{discord.utils.escape_markdown(when)}` isn't a length of time. Try `1 day` or `12h`.")
                return
            if seconds < SHORTEST_REPEAT:
                await ctx.fail("An autofeed can repeat at most once an hour.")
                return
            if seconds > LONGEST_LEAD:
                await ctx.fail("An autofeed can wait at most five years between posts.")
                return
            repeat = (0, seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60)
        await self.db.run(
            "UPDATE autofeeds SET repeat_months = ?, repeat_days = ?, repeat_hours = ?, repeat_minutes = ? WHERE id = ? AND guild_id = ?",
            (*repeat, id, ctx.guild.id),
        )
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autofeed.repeat", detail=str(id))
        if not any(repeat):
            await ctx.ok(f"Autofeed {id} now runs once.")
            return
        # A one-time post that failed has no timer left, so repeating it has to arm one again.
        late = await self._rearm(row)
        await ctx.ok(f"Autofeed {id} now repeats every {repeat_text(repeat)}.{late}")

    @autofeeds.command(name="delete", aliases=["del", "remove"], description="Delete an autofeed")
    @requires("manage_guild")
    async def autofeeds_delete(self, ctx: commands.Context, id: int) -> None:
        if await self._find_autofeed(ctx, id) is None:
            return
        await self._remove_autofeed(ctx.guild.id, id)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autofeed.delete", detail=str(id))
        await ctx.ok(f"Deleted autofeed {id}.")

    @autofeeds.command(name="clear", description="Delete every autofeed in this server")
    @requires("manage_guild")
    async def autofeeds_clear(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT id, channel_id FROM autofeeds WHERE guild_id = ?", (ctx.guild.id,))
        chosen, hidden = self._visible_rows(ctx, rows)
        if not chosen:
            await ctx.fail(self._nothing_to_clear("autofeeds", hidden))
            return

        async def clear() -> str:
            for row in chosen:
                await self._remove_autofeed(ctx.guild.id, row["id"])
            await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autofeed.clear", detail=str(len(chosen)))
            return f"Deleted {len(chosen)} autofeeds.{self._hidden_note(hidden)}"
        await ctx.send(f"Delete all {len(chosen)} autofeeds?{self._hidden_note(hidden)}", view=ConfirmClear(ctx.author.id, clear), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @autofeeds.command(name="move", description="Move an autofeed to another channel")
    @requires("manage_guild")
    async def autofeeds_move(self, ctx: commands.Context, id: int, channel: discord.TextChannel) -> None:
        row = await self._find_autofeed(ctx, id)
        if row is None:
            return
        refusal = access.post_refusal(ctx.author, channel) or _ping_refusal(ctx.author, channel, row["mention"], row["role_id"])
        if refusal:
            await ctx.fail(refusal)
            return
        await self.db.run("UPDATE autofeeds SET channel_id = ? WHERE id = ? AND guild_id = ?", (channel.id, id, ctx.guild.id))
        late = await self._rearm(row)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "autofeed.move", detail=str(id))
        await ctx.ok(f"Autofeed {id} now posts in {channel.mention}.{late}")

    async def _rearm(self, row: dict) -> str:
        """Clears a failure and puts the timer back, since one that failed for good has none. Returns what the person should know when the time has already passed."""
        await self.db.run("UPDATE autofeeds SET last_error = NULL WHERE id = ?", (row["id"],))
        now = self._clock()
        await self._arm(row["id"], row["guild_id"], max(row["next_at"], now))
        return f" Its time, <t:{int(row['next_at'])}:f>, has passed, so it posts now." if row["next_at"] <= now else ""

    # ---- storage ----

    async def _insert(self, guild_id: int, channel_id: int, mention: str, role_id: int | None, payload: dict, due: float, repeat: tuple[int, int, int, int], zone: str | None, actor_id: int | None) -> int:
        async with self.db.transaction():
            autofeed_id = await self.db.insert(
                "INSERT INTO autofeeds (guild_id, channel_id, mention, role_id, payload, next_at, repeat_months, repeat_days, repeat_hours, repeat_minutes, timezone, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, mention, role_id, json.dumps(payload), due, *repeat, zone, actor_id, self._clock()),
            )
            await self._arm(autofeed_id, guild_id, due)
        return autofeed_id

    async def _remove_autofeed(self, guild_id: int, autofeed_id: int) -> None:
        await self.db.run("DELETE FROM autofeeds WHERE id = ? AND guild_id = ?", (autofeed_id, guild_id))
        await self.bot.timers.cancel_key("autofeed", str(autofeed_id))

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/autofeeds", self._api_list)
        router.post("/autofeeds", self._api_create)
        router.put("/autofeeds/{autofeed_id:\\d+}", self._api_edit)
        router.delete("/autofeeds/{autofeed_id:\\d+}", self._api_delete)
        router.post("/autofeeds/{autofeed_id:\\d+}/test", self._api_test)
        router.post("/feeds", self._api_feed_create)
        router.put("/feeds/{name}", self._api_feed_edit)
        router.delete("/feeds/{name}", self._api_feed_delete)

    @staticmethod
    def _row_for_page(req: Any, row: dict) -> dict:
        if not req.can_see(row["channel_id"]):  # the row counts toward the limit, so it's listed, with nothing from it
            return {
                "id": row["id"], "channel_id": row["channel_id"], "mention": SILENT, "role_id": None, "message": {"content": "", "embeds": [], "buttons": []},
                "next_at": row["next_at"], "repeat": [0, 0, 0, 0], "timezone": None, "last_at": None, "last_error": None, "hidden": True,
            }
        return {
            "id": row["id"], "channel_id": row["channel_id"], "mention": row["mention"], "role_id": row["role_id"], "message": json.loads(row["payload"]),
            "next_at": row["next_at"], "repeat": [row["repeat_months"], row["repeat_days"], row["repeat_hours"], row["repeat_minutes"]],
            "timezone": row["timezone"], "last_at": row["last_at"], "last_error": row["last_error"],
        }

    async def _api_list(self, req: Any) -> dict:
        autofeeds = await req.db.all("SELECT * FROM autofeeds WHERE guild_id = ? ORDER BY next_at", (req.guild_id,))
        feeds = await req.db.all("SELECT name, role_id, channel_id FROM feeds WHERE guild_id = ? ORDER BY name", (req.guild_id,))
        for feed in feeds:
            if not req.can_see(feed["channel_id"]):
                feed["hidden"] = True
        return {"autofeeds": [self._row_for_page(req, row) for row in autofeeds], "feeds": feeds, "autofeed_limit": MAX_AUTOFEEDS, "feed_limit": MAX_FEEDS}

    def _form_role(self, req: Any, value: Any) -> int:
        try:
            role_id = snowflake(value)
        except Invalid as error:
            raise ValidationError({"role_id": "Pick a role."}) from error
        info = req.ops.describe_guild(req.guild_id) or {}
        if not any(role["id"] == role_id for role in info.get("roles", [])):
            raise ValidationError({"role_id": "That role isn't in this server."})
        return role_id

    def _check_new_ping(self, req: Any, checked: dict, row: dict | None = None) -> None:
        """@everyone, @here or a role members can't ping, saved now, goes out later in the bot's name, so whoever saves it must be able to send it themselves. A row that already pings there keeps doing so through an edit."""
        same_place = row is not None and row["channel_id"] == checked["channel_id"]
        if checked["mention"] in (EVERYONE, HERE):
            # A post that pings at all lets its text ping too, so @everyone written into an @here post is a new ping.
            if not (same_place and row["mention"] == checked["mention"]) or messages.ping_words(checked["payload"], everyone_only=True) - messages.ping_words(json.loads(row["payload"]), everyone_only=True):
                req.require_ping(checked["channel_id"], "mention")
        elif checked["role_id"] and not (same_place and row["role_id"] == checked["role_id"]) and delivery.needs_ping_right(req.bot.get_guild(req.guild_id), checked["role_id"]):
            req.require_ping(checked["channel_id"], "mention")

    def _form_autofeed(self, req: Any, form: Any, *, now: float, keep_due: float | None = None, keep_channel: int | None = None) -> dict:
        """The checked fields of the create and edit forms. An edit that keeps the time it already has may keep a time now passed, and one that keeps its channel may keep one the person can't post in."""
        if not isinstance(form, dict):
            raise ApiError(400, "Expected a set of fields.")
        errors: dict[str, str] = {}
        out: dict[str, Any] = {}
        try:
            out["channel_id"] = delivery.form_channel(req, form.get("channel_id"), keeping=keep_channel)
        except ValidationError as error:
            errors.update(error.fields)
        mention = form.get("mention", SILENT)
        out["role_id"] = None
        if mention in SPECIAL_MENTIONS:
            out["mention"] = mention
        else:
            try:
                out["role_id"] = self._form_role(req, mention)
                out["mention"] = "role"
            except ValidationError:
                errors["mention"] = "That role isn't in this server."
        try:
            out["payload"] = messages.clean_payload(form.get("message"))
            if not messages.has_content(out["payload"]):
                errors["message"] = "Write a message or build an embed."
        except Invalid as error:
            errors["message"] = str(error)
        try:
            due = number(0, 2 ** 40)(form.get("when"))
            if due != keep_due and (due < now + LEAD_SECONDS or due > now + LONGEST_LEAD):
                errors["when"] = "Pick a time at least a minute ahead and at most five years ahead."
            out["due"] = due
        except Invalid:
            errors["when"] = "Pick a date and time."
        repeat = form.get("repeat", [0, 0, 0, 0])
        try:
            if not isinstance(repeat, list) or len(repeat) != 4:
                raise Invalid("Repeat needs months, days, hours and minutes.")
            out["repeat"] = tuple(integer(0, limit)(value or 0) for value, limit in zip(repeat, REPEAT_LIMITS))
            if any(out["repeat"]) and repeat_seconds(out["repeat"]) < SHORTEST_REPEAT:
                errors["repeat"] = "An autofeed can repeat at most once an hour."
        except Invalid as error:
            errors["repeat"] = str(error)
        out["zone"] = clean_zone(form.get("timezone"))
        if errors:
            raise ValidationError(errors)
        return out

    async def _api_create(self, req: Any) -> dict:
        checked = self._form_autofeed(req, await req.json(), now=self._clock())
        self._check_new_ping(req, checked)
        if await req.db.scalar("SELECT COUNT(*) FROM autofeeds WHERE guild_id = ?", (req.guild_id,)) >= MAX_AUTOFEEDS:
            raise ApiError(400, f"A server can hold at most {MAX_AUTOFEEDS} autofeeds.")
        autofeed_id = await self._insert(req.guild_id, checked["channel_id"], checked["mention"], checked["role_id"], checked["payload"], checked["due"], checked["repeat"], checked["zone"], req.actor.id)
        await req.record("autofeed.create", detail=str(autofeed_id))
        return {"id": autofeed_id}

    async def _autofeed_row(self, req: Any) -> dict:
        row = await req.db.one("SELECT * FROM autofeeds WHERE id = ? AND guild_id = ?", (req.int_param("autofeed_id"), req.guild_id))
        if row is None:
            raise ApiError(404, "That autofeed doesn't exist.")
        if not req.can_see(row["channel_id"]):
            raise ApiError(403, "You can't see that channel.")
        return row

    async def _api_edit(self, req: Any) -> dict:
        row = await self._autofeed_row(req)
        checked = self._form_autofeed(req, await req.json(), now=self._clock(), keep_due=row["next_at"], keep_channel=row["channel_id"])
        self._check_new_ping(req, checked, row)
        await req.db.run(
            "UPDATE autofeeds SET channel_id = ?, mention = ?, role_id = ?, payload = ?, next_at = ?, repeat_months = ?, repeat_days = ?, repeat_hours = ?, repeat_minutes = ?, timezone = ?, last_error = NULL "
            "WHERE id = ? AND guild_id = ?",
            (checked["channel_id"], checked["mention"], checked["role_id"], json.dumps(checked["payload"]), checked["due"], *checked["repeat"], checked["zone"], row["id"], req.guild_id),
        )
        # A one-time post that failed has no timer left, so saving it arms one again. A time already passed comes due at once.
        await self._arm(row["id"], req.guild_id, max(checked["due"], self._clock()))
        await req.record("autofeed.edit", detail=str(row["id"]))
        return {"id": row["id"]}

    async def _api_delete(self, req: Any) -> dict:
        row = await self._autofeed_row(req)
        await self._remove_autofeed(req.guild_id, row["id"])
        await req.record("autofeed.delete", detail=str(row["id"]))
        return {"ok": True}

    async def _api_test(self, req: Any) -> dict:
        """Post it once as it stands, without its ping and without touching the schedule."""
        row = await self._autofeed_row(req)
        place = req.ops.channel(req.guild_id, row["channel_id"])
        if place is None:
            raise ApiError(400, "That channel no longer exists. Pick another one.")
        if not req.can_post(row["channel_id"]):
            raise ApiError(403, "You can't post in that channel.")
        info = req.ops.describe_guild(req.guild_id) or {}
        guild = req.bot.get_guild(req.guild_id)
        channel = guild.get_channel_or_thread(row["channel_id"]) if guild else None
        # The tags fill in as they do on the real post. The demo has no server to read, so it fills in the names.
        scope = Scope(guild=guild, channel=channel, extra={"unix": str(int(self._clock()))}) if channel else Scope(extra={"unix": str(int(self._clock())), "server": info.get("name", ""), "channel": place["name"]})
        payload = await render_message(json.loads(row["payload"]), scope, guild_id=row["guild_id"])
        sent = await req.ops.send_message(row["channel_id"], payload)
        await req.record("autofeed.test", detail=str(row["id"]))
        return sent

    async def _feed_row(self, req: Any, name: str) -> dict:
        row = await req.db.one("SELECT * FROM feeds WHERE guild_id = ? AND name = ?", (req.guild_id, name))
        if row is None:
            raise ApiError(404, "That feed doesn't exist.")
        if not req.can_see(row["channel_id"]):
            raise ApiError(403, "You can't see that channel.")
        return row

    async def _api_feed_create(self, req: Any) -> dict:
        form = await req.json()
        if not isinstance(form, dict):
            raise ApiError(400, "Expected a set of fields.")
        try:
            name = text(50, minimum=1)(form.get("name"))
        except Invalid as error:
            raise ValidationError({"name": str(error)}) from error
        if "\n" in name or "/" in name:
            raise ValidationError({"name": "A feed's name is one line, with no slashes."})
        role_id = self._form_role(req, form.get("role_id"))
        channel_id = delivery.form_channel(req, form.get("channel_id"))
        if delivery.needs_ping_right(req.bot.get_guild(req.guild_id), role_id):
            req.require_ping(channel_id, "role_id")
        if await req.db.scalar("SELECT 1 FROM feeds WHERE guild_id = ? AND name = ?", (req.guild_id, name)):
            raise ValidationError({"name": "There's already a feed with that name."})
        if await req.db.scalar("SELECT COUNT(*) FROM feeds WHERE guild_id = ?", (req.guild_id,)) >= MAX_FEEDS:
            raise ApiError(400, f"A server can hold at most {MAX_FEEDS} feeds.")
        await req.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)", (req.guild_id, name, role_id, channel_id, req.actor.id, self._clock()))
        await req.record("feed.create", detail=name)
        return {"name": name}

    async def _api_feed_edit(self, req: Any) -> dict:
        name = req.param("name")
        form = await req.json()
        if not isinstance(form, dict):
            raise ApiError(400, "Expected a set of fields.")
        row = await self._feed_row(req, name)
        role_id = self._form_role(req, form.get("role_id"))
        channel_id = delivery.form_channel(req, form.get("channel_id"), keeping=row["channel_id"])
        if (channel_id != row["channel_id"] or role_id != row["role_id"]) and delivery.needs_ping_right(req.bot.get_guild(req.guild_id), role_id):
            req.require_ping(channel_id, "role_id")
        await req.db.run("UPDATE feeds SET role_id = ?, channel_id = ? WHERE guild_id = ? AND name = ?", (role_id, channel_id, req.guild_id, name))
        await req.record("feed.edit", detail=name)
        return {"name": name}

    async def _api_feed_delete(self, req: Any) -> dict:
        name = req.param("name")
        await self._feed_row(req, name)
        await req.db.run("DELETE FROM feeds WHERE guild_id = ? AND name = ?", (req.guild_id, name))
        await req.record("feed.delete", detail=name)
        return {"ok": True}

    async def seed_demo(self, guild_id: int) -> None:
        """A sample autofeed and feed for the demo dashboard."""
        info = self.bot.ops.describe_guild(guild_id) or {}
        channel = next((row["id"] for row in info.get("channels", []) if row["name"] == "announcements"), None)
        role = next((row["id"] for row in info.get("roles", []) if row["name"] == "Member"), None)
        if channel is None or role is None:
            return
        now = self._clock()
        await self._insert(guild_id, channel, "role", role, messages.clean_payload({"content": "Weekly game night starts in an hour. Bring a friend."}), now + 3 * 86400, (0, 7, 0, 0), None, None)
        await self._insert(guild_id, channel, SILENT, None, messages.clean_payload({"content": "", "embeds": [{"title": "Server rules reminder", "description": "Be kind. Read the rules channel.", "color": 0x00A2F8}]}), now + 86400, (0, 0, 0, 0), None, None)
        await self.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_at) VALUES (?, ?, ?, ?, ?)", (guild_id, "news", role, channel, now))

