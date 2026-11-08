"""Voice moderation, voice access requests and mini mutes.

Temporary voice actions: moderators can server-mute, server-deafen or disconnect someone for a while. A timer reverses a mute or deafen
when it runs out. Discord only lets that happen while the member is in voice, so someone who left keeps their row and is cleared the
moment they reconnect. A rejoin block disconnects the member from any voice channel they join until it lapses, so hopping to another
channel doesn't get around it.

VC veterans are trusted members who act as moderators for voice alone. They can use the temporary actions, but not on staff, bots or
each other. They are capped at the server's default duration, every action they take is posted to the VC veteran log when the server
set one (the report_channel setting, named for what it held before 1.2.3), they may approve access requests but never deny them, they
may grant access outright with that logged too, unless a moderator closed that person's access, and they don't handle reports.

VC reports, from /vc_report and the VC Report menu, are filed through the reports module like any other report: the same post in the
reports channel, the same buttons, and the same review thread. Posts from before 1.2.3 keep their own buttons, which still answer.

Voice access is a role members ask for with a button. The bot screens each request against message and day requirements and sends only
qualifying ones to staff, for a one-time approval that lasts until revoked and comes back if the member leaves and returns.

Mini mutes are a separate tier for Server Veterans, who aren't moderators. They apply a role for a length the server chose, never one
of their own, and can't target staff, other veterans or bots. Unlike a Discord timeout, a role doesn't expire and is lost when a member
leaves, so every mini mute is stored: a timer takes the role back, including after a restart, and rejoining puts it back on. When a
server wants more than one vote, the request becomes a ballot in the log channel. Moderators can veto any open vote and lift any mute.

A mini mute leaves no mark on a member's record. It writes to the mini mute log channel and its own table, and makes no case unless the
server turned on mini_mute_cases.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..core import access
from ..core.durations import parse_duration, split_leading_duration
from ..core.members import find_member
from ..core.messages import component_view
from ..core.module import Module, staff_only
from ..core.ops import MessageGone, OpsError
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.usage import plain_error
from ..core.validate import Field, Schema, boolean, integer, optional, snowflake, whole_number
from ..web.api import ApiError

log = logging.getLogger(__name__)

MUTE, DEAFEN, BLOCK = "mute", "deafen", "block"
MAX_MINUTES = 10080
PAGE_MAX = 100
FLUSH_SECONDS = 30
EXPIRED_ROW_DAYS = 180
RETRY_SECONDS = 60  # how soon a mute that wouldn't come off is tried again
REQUEST_CLAIM_SECONDS = 60  # a request with no post yet may still be posting. After this long, it never will.
LARGEST_PAGE = 10 ** 7
STAFF_PERMISSIONS = ("ban_members", "kick_members", "moderate_members", "mute_members", "move_members")
LOOKUP_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError)  # none of these says whether someone left

REPORT_MUTE_ID = "vcreport:mute"
REPORT_DEAFEN_ID = "vcreport:deafen"
REPORT_REMOVE_ID = "vcreport:remove"
REPORT_DISMISS_ID = "vcreport:dismiss"
ACCESS_REQUEST_ID = "vcaccess:request"
ACCESS_APPROVE_ID = "vcaccess:approve"
ACCESS_DENY_ID = "vcaccess:deny"
VOTE_FOR_ID = "minimute:approve"
VOTE_AGAINST_ID = "minimute:reject"
VOTE_VETO_ID = "minimute:veto"

BLURPLE = discord.Color.blurple()
VOTE_COLORS = {"open": discord.Color.orange(), "applied": discord.Color.red(), "rejected": discord.Color.dark_gray(), "vetoed": discord.Color.dark_gray(), "expired": discord.Color.dark_gray()}


def format_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day{'s' if days != 1 else ''}"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def read_length(value: str) -> int | None:
    """Minutes from a length a person typed. A bare number means minutes."""
    cleaned = (value or "").strip()
    bare = whole_number(cleaned)
    if bare is not None:
        return bare
    seconds = parse_duration(cleaned)
    return None if seconds is None else max(1, math.ceil(seconds / 60))


def split_length(duration: str | None, reason: str | None) -> tuple[int | None, str | None]:
    """A text command can't mark where a length ends, so the words go back together and any length comes off the front."""
    joined = " ".join(part for part in (duration, reason) if part)
    if not joined:
        return None, None
    seconds, rest = split_leading_duration(joined)
    if seconds is not None:
        return max(1, math.ceil(seconds / 60)), rest or None
    first, _, tail = joined.partition(" ")
    bare = whole_number(first)
    if bare is not None:
        return bare, tail.strip() or None
    return None, joined


def audit_text(text: str) -> str:
    """Discord cuts audit log reasons off at 512 characters."""
    return text if len(text) <= 512 else text[:509] + "..."


async def _delete_typed(ctx: commands.Context) -> None:
    """Take down a typed report, which names the person it accuses. A slash command leaves nothing to take down."""
    if ctx.interaction is not None or ctx.message is None:
        return
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


def vote_embed(target_id: int, initiator_id: int, reason: str | None, minutes: int, required: int, approvals: int, rejections: int, expires_at: float, status: str = "open") -> discord.Embed:
    embed = discord.Embed(title="Mini Mute Vote", description=f"<@{initiator_id}> requests a {format_minutes(minutes)} mini mute on <@{target_id}>.", color=VOTE_COLORS.get(status, VOTE_COLORS["open"]))
    embed.add_field(name="Target", value=f"<@{target_id}> (`{target_id}`)", inline=True)
    embed.add_field(name="Requested by", value=f"<@{initiator_id}>", inline=True)
    embed.add_field(name="Reason", value=(reason or "No reason given")[:1024], inline=False)
    embed.add_field(name="Votes to apply", value=f"{approvals} of {required}", inline=True)
    embed.add_field(name="Votes against", value=str(rejections), inline=True)
    if status == "open":
        embed.add_field(name="Closes", value=f"<t:{int(expires_at)}:R>", inline=True)
    else:
        embed.add_field(name="Outcome", value=status.capitalize(), inline=True)
    return embed


class Person:
    """Enough of someone for a case, when all that's left is an ID and a name."""

    def __init__(self, user_id: int, name: str | None) -> None:
        self.id = user_id
        self.name = name
        self.bot = False

    def __str__(self) -> str:
        return self.name or f"ID {self.id}"


class Buttons(discord.ui.View):
    """Buttons that survive a restart. They hold no state, since the message's row in the database says what a click means."""

    def __init__(self, cog: "Voice") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        log.error("Voice button %s failed", getattr(item, "custom_id", item), exc_info=error)
        await self.cog.say(interaction, "Something went wrong with that button. It's been logged.")


class ReportButtons(Buttons):
    @discord.ui.button(label="Temp Mute", style=discord.ButtonStyle.danger, custom_id=REPORT_MUTE_ID)
    async def temp_mute(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_report(interaction, MUTE)

    @discord.ui.button(label="Temp Deafen", style=discord.ButtonStyle.danger, custom_id=REPORT_DEAFEN_ID)
    async def temp_deafen(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_report(interaction, DEAFEN)

    @discord.ui.button(label="Temp Remove", style=discord.ButtonStyle.danger, custom_id=REPORT_REMOVE_ID)
    async def temp_remove(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_report(interaction, BLOCK)

    @discord.ui.button(label="Dismiss Report", style=discord.ButtonStyle.secondary, custom_id=REPORT_DISMISS_ID)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_report(interaction, "dismiss")


class RequestButton(Buttons):
    @discord.ui.button(label="Request Voice Access", style=discord.ButtonStyle.primary, custom_id=ACCESS_REQUEST_ID)
    async def request(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.cog.submit_request(interaction.user), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class ApprovalButtons(Buttons):
    @discord.ui.button(label="Approve VC Access", style=discord.ButtonStyle.success, custom_id=ACCESS_APPROVE_ID)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_decision(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id=ACCESS_DENY_ID)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_decision(interaction, False)


class VoteButtons(Buttons):
    @discord.ui.button(label="Vote to Mute", style=discord.ButtonStyle.danger, custom_id=VOTE_FOR_ID)
    async def vote_for(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_vote(interaction, True)

    @discord.ui.button(label="Vote Against", style=discord.ButtonStyle.secondary, custom_id=VOTE_AGAINST_ID)
    async def vote_against(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_vote(interaction, False)

    @discord.ui.button(label="Veto (moderator)", style=discord.ButtonStyle.primary, custom_id=VOTE_VETO_ID)
    async def veto(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_veto(interaction)


class ActionsPanel(discord.ui.View):
    """What Voice Actions opens: a private panel for one member. A right-click takes no arguments, so these use the server's default length, and disconnect is a plain one with no rejoin block."""

    def __init__(self, cog: "Voice", target: Any) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.target = target

    async def _act(self, interaction: discord.Interaction, action: str, label: str) -> None:
        await interaction.response.defer()
        audit = f"{label} by {interaction.user} ({interaction.user.id}) from the Voice Actions menu"
        ok, text = await self.cog.perform(interaction.user, self.target, action, None, audit, default_if_none=action != BLOCK)
        await interaction.edit_original_response(content=text if ok else f"\N{CROSS MARK} {text}", view=self, allowed_mentions=discord.AllowedMentions.none())
        if ok:  # the panel is private, and staff in the channel see this the way they see the commands
            await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Temp Mute", style=discord.ButtonStyle.danger)
    async def temp_mute(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._act(interaction, MUTE, "VC temp mute")

    @discord.ui.button(label="Temp Deafen", style=discord.ButtonStyle.danger)
    async def temp_deafen(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._act(interaction, DEAFEN, "VC temp deafen")

    @discord.ui.button(label="Disconnect", style=discord.ButtonStyle.secondary)
    async def disconnect(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._act(interaction, BLOCK, "VC disconnect")


class Form(discord.ui.Modal):
    """A form with one text box, answered instead of leaving the person with Discord's generic failure."""

    def __init__(self, title: str, label: str, placeholder: str, *, required: bool, submit: Any) -> None:
        super().__init__(title=title)
        self.submit = submit
        self.box = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph, placeholder=placeholder, required=required, max_length=1000)
        self.add_item(self.box)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.submit(interaction, self.box.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Voice form %s failed", self.title, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong with that form. It's been logged.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong with that form. It's been logged.", ephemeral=True)
        except discord.HTTPException:
            pass


class Voice(Module):
    key = "voice"
    category = "Moderation"
    child_links = {"mini_mute_ballots": {"vote_message_id": "mini_mute_votes.vote_message_id"}}
    schema = Schema({
        "moderator_role": Field(None, optional(snowflake)),
        "report_channel": Field(None, optional(snowflake)),
        "temp_minutes": Field(10, integer(1, MAX_MINUTES)),
        "veteran_role": Field(None, optional(snowflake)),
        "access_role": Field(None, optional(snowflake)),
        "approval_channel": Field(None, optional(snowflake)),
        "min_messages": Field(100, integer(0, 1000000)),
        "min_days": Field(7, integer(0, 3650)),
        "server_veteran_role": Field(None, optional(snowflake)),
        "mini_mute_role": Field(None, optional(snowflake)),
        "mini_mute_minutes": Field(10, integer(1, 1440)),
        "mini_mute_log_channel": Field(None, optional(snowflake)),
        "mini_mute_votes_required": Field(1, integer(1, 10)),
        "mini_mute_vote_window": Field(30, integer(1, 1440)),
        "mini_mute_cases": Field(False, boolean),
    })
    role_timers = ("mini_mute", "voice_action")
    migrations = [
        """
        CREATE TABLE voice_actions (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            expires_at REAL NOT NULL,
            moderator_id INTEGER,
            reason TEXT,
            PRIMARY KEY (guild_id, user_id, action)
        );
        CREATE TABLE voice_reports (
            report_message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER,
            reporter_id INTEGER NOT NULL,
            reporter_name TEXT,
            target_id INTEGER NOT NULL,
            target_name TEXT,
            voice_channel_id INTEGER,
            reason TEXT,
            created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            handled_by_name TEXT
        );
        CREATE INDEX voice_reports_recent ON voice_reports (guild_id, created_at DESC);
        CREATE TABLE voice_access (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            request_message_id INTEGER,
            requested_at REAL,
            decided_by INTEGER,
            decided_by_name TEXT,
            decided_at REAL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE INDEX voice_access_by_status ON voice_access (guild_id, status, requested_at DESC);
        CREATE INDEX voice_access_by_message ON voice_access (guild_id, request_message_id);
        CREATE TABLE voice_message_counts (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE mini_mutes (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            expires_at REAL NOT NULL,
            applied_by INTEGER,
            applied_by_name TEXT,
            reason TEXT,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE mini_mute_votes (
            vote_message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            target_name TEXT,
            initiator_id INTEGER NOT NULL,
            initiator_name TEXT,
            reason TEXT,
            minutes INTEGER NOT NULL,
            required INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        );
        CREATE INDEX mini_mute_votes_open ON mini_mute_votes (guild_id, target_id, status);
        CREATE TABLE mini_mute_ballots (
            vote_message_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            voter_id INTEGER NOT NULL,
            approve INTEGER NOT NULL,
            PRIMARY KEY (vote_message_id, voter_id)
        );
        """,
        """
        ALTER TABLE mini_mutes ADD COLUMN role_id INTEGER;
        ALTER TABLE voice_access ADD COLUMN request_channel_id INTEGER;
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._own_removals: dict[tuple[int, int], float] = {}  # who the bot just took a mini mute role off, so the listener doesn't take it for someone else's doing
        self._pending_counts: Counter = Counter()  # messages counted since the last write, so a busy server doesn't write per message
        self._active: set[tuple[int, int]] = set()  # who has a voice action row, so ordinary voice traffic costs no query
        self._rejoined: set[tuple[int, int]] = set()  # who came back with a mute or deafen still running, until their next connect
        self._before_restore: dict[int, tuple[list[dict], list[dict]]] = {}
        self._audit_wait = 3.0  # Discord can send a role change a moment before its audit log entry
        self._out_of_reach: set[tuple[int, int]] = set()  # roles staff were told about
        self._started = False
        self._menus = [
            app_commands.ContextMenu(name="VC Report", callback=self.menu_report),
            app_commands.ContextMenu(name="Mini Mute", callback=self.menu_mini_mute),
            app_commands.ContextMenu(name="Voice Actions", callback=self.menu_actions),
        ]

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("access_role", "mini_mute_role")}

    def staff_roles(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("moderator_role", "veteran_role", "server_veteran_role")}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("report_channel", "approval_channel", "mini_mute_log_channel")}

    def transient_roles(self, guild_id: int) -> set[int]:
        role_id = self.cfg(guild_id)["mini_mute_role"]
        return {role_id} if role_id else set()

    async def on_load(self) -> None:
        for view in (ReportButtons, RequestButton, ApprovalButtons, VoteButtons):
            self.bot.add_view(view(self))
        for menu in self._menus:
            self.bot.tree.add_command(menu)
        self.bot.timers.handler("voice_action", self._action_due)
        self.bot.timers.handler("mini_mute", self._mini_mute_due)
        self.bot.timers.handler("mini_mute_vote", self._vote_due)
        await self._load_active()

    async def before_restore(self, guild_id: int) -> None:
        """What runs now may not be in the backup, and its timer stays scheduled with nothing left to do. A mini mute past its end counts only while its timer is still trying, and someone waiting to be unmuted at their next connect has no timer."""
        mutes = await self.db.all(
            "SELECT * FROM mini_mutes m WHERE m.guild_id = ? AND (m.expires_at > ? OR EXISTS (SELECT 1 FROM timers t WHERE t.kind = 'mini_mute' AND t.key = m.guild_id || ':' || m.user_id))",
            (guild_id, time.time()),
        )
        self._before_restore[guild_id] = (mutes, await self.db.all("SELECT * FROM voice_actions WHERE guild_id = ?", (guild_id,)))

    async def on_guild_restored(self, guild_id: int) -> None:
        running = self._before_restore.pop(guild_id, None)
        if running is not None:  # My data calls this too, only to refresh what memory holds
            await self._settle_restored(guild_id, *running)
        await self._load_active()

    async def after_restore(self, guild_id: int) -> None:
        self._before_restore.pop(guild_id, None)

    async def _settle_restored(self, guild_id: int, mutes: list[dict], actions: list[dict]) -> None:
        """A restored row past its end ran back then, and running it now could undo what staff have done since. What was running before the restore goes back, so nobody stays muted for good."""
        now = time.time()
        await self.db.run("DELETE FROM mini_mutes WHERE guild_id = ? AND expires_at <= ?", (guild_id, now))
        await self.db.run("DELETE FROM voice_actions WHERE guild_id = ? AND expires_at <= ?", (guild_id, now))
        await self.db.many(
            "INSERT INTO mini_mutes (guild_id, user_id, user_name, expires_at, applied_by, applied_by_name, reason, role_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, user_id) DO NOTHING",
            [(row["guild_id"], row["user_id"], row["user_name"], row["expires_at"], row["applied_by"], row["applied_by_name"], row["reason"], row["role_id"]) for row in mutes],
        )
        await self.db.many(
            "INSERT INTO voice_actions (guild_id, user_id, action, expires_at, moderator_id, reason) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, user_id, action) DO NOTHING",
            [(row["guild_id"], row["user_id"], row["action"], row["expires_at"], row["moderator_id"], row["reason"]) for row in actions],
        )
        await self._rearm_timers(guild_id)

    async def _load_active(self) -> None:
        rows = await self.db.all("SELECT DISTINCT guild_id, user_id FROM voice_actions")
        self._active = {(row["guild_id"], row["user_id"]) for row in rows}

    async def cog_unload(self) -> None:
        writing = self._flush_loop.get_task()
        self._flush_loop.cancel()
        if writing is not None:  # a write this cancels puts its batch back, and that has to happen before the last write looks
            await asyncio.gather(writing, return_exceptions=True)
        await self.flush_counts()
        for menu in self._menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._started:
            return
        self._started = True
        cutoff = time.time() - EXPIRED_ROW_DAYS * 86400
        # The counts come first, so nothing a stored row does can stop them for the life of the process.
        if not self._flush_loop.is_running():
            self._flush_loop.start()
        # A block is done once it lapses. A mute or deafen stays stored however old it gets, because only their next connect can clear it.
        await self.db.run("DELETE FROM voice_actions WHERE action = ? AND expires_at < ?", (BLOCK, cutoff))
        await self._rearm_timers()
        await self.bot.members_settled.wait()  # someone not on a half-filled list would be passed over
        await self._enforce_blocks()

    async def _enforce_blocks(self) -> None:
        """Someone who joined voice while the bot was down sends no voice event until they next change state, so a running block reaches them here."""
        for row in await self.db.all("SELECT guild_id, user_id FROM voice_actions WHERE action = ? AND expires_at > ?", (BLOCK, time.time())):
            guild = self.bot.get_guild(row["guild_id"])
            member = guild.get_member(row["user_id"]) if guild is not None else None
            if member is None or member.voice is None:
                continue
            try:
                await member.move_to(None, reason="Temporarily removed from voice by moderators")
            except discord.HTTPException as error:
                log.warning("Could not enforce a rejoin block on %s: %s", member.id, error)

    async def _rearm_timers(self, guild_id: int | None = None) -> None:
        """A row can outlive its timer, as after a restored backup. Anything still stored gets a timer again, so nobody stays muted for good."""
        now = time.time()
        where, args = ("WHERE guild_id = ?", (guild_id,)) if guild_id is not None else ("", ())
        for row in await self.db.all(f"SELECT guild_id, user_id, expires_at FROM mini_mutes {where}", args):
            try:
                await self._schedule_mini_mute(row["guild_id"], row["user_id"], max(row["expires_at"], now + 5))
            except TypeError:  # one bad row must not leave another server's mutes with no timer
                log.warning("Skipped a mini mute with no readable end time in %s", row["guild_id"])
        for row in await self.db.all(f"SELECT guild_id, user_id, action, expires_at FROM voice_actions {where}", args):
            try:
                await self._schedule_action(row["guild_id"], row["user_id"], row["action"], max(row["expires_at"], now + 5))
            except TypeError:
                log.warning("Skipped a voice action with no readable end time in %s", row["guild_id"])

    # ---- who is who ----

    def _moderator_roles(self, guild_id: int) -> set[int]:
        roles = {self.cfg(guild_id)["moderator_role"], self.bot.settings.get(guild_id, COMMANDS_MODULE).get("mod_role")}
        reports = self.bot.module("reports")
        if reports is not None:
            roles.add(reports.cfg(guild_id)["moderator_role"])
        roles.discard(None)
        roles.discard(guild_id)  # @everyone holds no power, even from a value stored before that was refused
        return roles

    def is_moderator(self, member: Any) -> bool:
        permissions = getattr(member, "guild_permissions", None)
        if permissions is None:
            return False
        if permissions.administrator or permissions.manage_guild:
            return True
        return bool(self._moderator_roles(member.guild.id) & {role.id for role in member.roles})

    def _has_role(self, member: Any, setting: str) -> bool:
        role_id = self.cfg(member.guild.id)[setting]
        return bool(role_id) and role_id != member.guild.id and any(role.id == role_id for role in getattr(member, "roles", []))

    def is_vc_veteran(self, member: Any) -> bool:
        return self._has_role(member, "veteran_role")

    def is_server_veteran(self, member: Any) -> bool:
        return self._has_role(member, "server_veteran_role")

    def _looks_like_staff(self, member: Any) -> bool:
        """Who a veteran can't touch. It's wider than is_moderator: a role with Ban Members counts as staff here though neither page names it, and it grants nothing."""
        permissions = getattr(member, "guild_permissions", None)
        return self.is_moderator(member) or (permissions is not None and any(getattr(permissions, name) for name in STAFF_PERMISSIONS))

    def _veteran_refusal(self, target: Any) -> str | None:
        if target.bot:
            return "VC veterans can't act on bots."
        if self._looks_like_staff(target) or self.is_vc_veteran(target):
            return "VC veterans can't act on moderators or fellow veterans."
        return None

    @staticmethod
    def _role_refusal(error: discord.HTTPException) -> str:
        if isinstance(error, discord.Forbidden):
            return "I can't give the voice access role. Check Manage Roles and where my role sits."
        return f"Discord refused the voice access role: {error.text or error.status}. Try again."

    @staticmethod
    def _hierarchy_refusal(actor: Any, target: Any) -> str | None:
        guild = actor.guild
        if target.id == guild.owner_id:
            return "Nobody can act on the server owner."
        if actor.id != guild.owner_id and target.top_role > actor.top_role:
            return f"{discord.utils.escape_markdown(target.display_name)}'s top role is above yours, so you can't act on them."
        return None

    @staticmethod
    async def say(interaction: discord.Interaction, text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ---- temporary voice actions ----

    async def _schedule_action(self, guild_id: int, user_id: int, action: str, due: float) -> None:
        await self.bot.timers.schedule("voice_action", due, {"guild_id": guild_id, "user_id": user_id, "action": action}, guild_id=guild_id, key=f"{guild_id}:{user_id}:{action}")

    async def _record_action(self, guild_id: int, user_id: int, action: str, minutes: int, moderator_id: int | None, reason: str | None) -> None:
        # One row per person and action: applying it again replaces the old timer, so an earlier expiry can't cut the new length short.
        expires_at = time.time() + minutes * 60
        await self.db.run(
            "INSERT INTO voice_actions (guild_id, user_id, action, expires_at, moderator_id, reason) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id, action) DO UPDATE SET expires_at = excluded.expires_at, moderator_id = excluded.moderator_id, reason = excluded.reason",
            (guild_id, user_id, action, expires_at, moderator_id, reason),
        )
        self._active.add((guild_id, user_id))
        await self._schedule_action(guild_id, user_id, action, expires_at)

    async def _drop_actions(self, guild_id: int, user_id: int, action: str | None = None) -> None:
        if action:
            await self.db.run("DELETE FROM voice_actions WHERE guild_id = ? AND user_id = ? AND action = ?", (guild_id, user_id, action))
        else:
            await self.db.run("DELETE FROM voice_actions WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        left = await self.db.scalar("SELECT COUNT(*) FROM voice_actions WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        if not left:
            self._active.discard((guild_id, user_id))

    @staticmethod
    def _out_of_voice(member: Any, action: str, minutes: int | None) -> str | None:
        """A block can wait for them to join. Everything else needs them in voice now."""
        if (member.voice is not None and member.voice.channel is not None) or (action == BLOCK and minutes is not None):
            return None
        return f"{discord.utils.escape_markdown(member.display_name)} isn't connected to a voice channel."

    async def apply_temp_action(self, member: Any, action: str, minutes: int | None, moderator: Any, audit: str, reason: str | None = None) -> tuple[bool, str]:
        """Do one action and answer with what happened. `audit` goes to Discord's audit log, and `reason` is what a person gave, for the dashboard. For a block, no minutes means a plain disconnect with no rejoin block."""
        in_voice = member.voice is not None and member.voice.channel is not None
        refusal = self._out_of_voice(member, action, minutes)
        if refusal:
            return False, refusal
        try:
            if action == MUTE:
                await member.edit(mute=True, reason=audit_text(audit))
            elif action == DEAFEN:
                await member.edit(deafen=True, reason=audit_text(audit))
            elif action == BLOCK and in_voice:
                await member.move_to(None, reason=audit_text(audit))
        except discord.Forbidden:
            return False, "I lack the permission for that. I need Mute Members, Deafen Members and Move Members."
        except discord.HTTPException as error:
            return False, f"Discord refused: {error.text or error.status}"
        if action == MUTE:
            await self._record_action(member.guild.id, member.id, MUTE, minutes, moderator.id, reason)
            return True, f"Server-muted {member.mention} for {format_minutes(minutes)}."
        if action == DEAFEN:
            await self._record_action(member.guild.id, member.id, DEAFEN, minutes, moderator.id, reason)
            return True, f"Server-deafened {member.mention} for {format_minutes(minutes)}."
        if minutes:
            await self._record_action(member.guild.id, member.id, BLOCK, minutes, moderator.id, reason)
            if in_voice:
                return True, f"Disconnected {member.mention} from voice and blocked rejoining any voice channel for {format_minutes(minutes)}."
            return True, f"{member.mention} isn't in voice right now, but is blocked from joining voice for {format_minutes(minutes)}."
        return True, f"Disconnected {member.mention} from voice."

    def _action_refusal(self, actor: Any, target: Any) -> str | None:
        """The tier's limits on who may act on whom."""
        if self.is_moderator(actor):
            return self._hierarchy_refusal(actor, target)
        if not self.is_vc_veteran(actor):
            return "Only moderators and VC veterans can do that."
        return self._veteran_refusal(target)

    async def perform(self, actor: Any, target: Any, action: str, minutes: int | None, audit: str, *, reason: str | None = None, default_if_none: bool = True) -> tuple[bool, str]:
        """The shared body of the voice commands and the panel: the default length, the tier's limits, the action, and the veteran log."""
        cfg = self.cfg(actor.guild.id)
        default = cfg["temp_minutes"]
        if minutes is None and default_if_none:
            minutes = default
        refusal = self._action_refusal(actor, target)
        if refusal:
            return False, refusal
        is_moderator = self.is_moderator(actor)
        note = ""
        if not is_moderator and minutes is not None and minutes > default:
            minutes = default
            note = f" VC veteran actions are capped at {format_minutes(default)}."
        ok, text = await self.apply_temp_action(target, action, minutes, actor, audit, reason)
        if ok and not is_moderator:
            self._log_veteran_action(actor, target, text)
        return ok, text + note if ok else text

    def _log_veteran_action(self, actor: Any, target: Any, text: str) -> None:
        """Every action a VC veteran takes goes to the VC veteran log, so the team can spot abuse of the trust. With no log set, nothing is posted and the action stands."""
        embed = discord.Embed(title="VC Veteran Action", description=text, color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Veteran", value=f"{actor.mention} (`{actor.id}`)", inline=True)
        embed.add_field(name="Target", value=f"{target.mention} (`{target.id}`)", inline=True)
        self.bot.logs.post(self.cfg(actor.guild.id)["report_channel"], embed)

    async def _action_due(self, payload: dict, row: dict) -> None:
        guild_id, user_id, action = payload["guild_id"], payload["user_id"], payload["action"]
        stored = await self.db.one("SELECT expires_at FROM voice_actions WHERE guild_id = ? AND user_id = ? AND action = ?", (guild_id, user_id, action))
        if stored is None or stored["expires_at"] > time.time():
            return  # released, or applied again with a later end
        if action == BLOCK:
            await self._drop_actions(guild_id, user_id, BLOCK)
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await self._drop_actions(guild_id, user_id, action)
            return
        member = guild.get_member(user_id)
        if member is None or member.voice is None:
            return  # server mute and deafen can only be cleared in voice, so the row waits for their next connect
        try:
            if action == MUTE and member.voice.mute:
                await member.edit(mute=False, reason="Temporary VC mute expired")
            elif action == DEAFEN and member.voice.deaf:
                await member.edit(deafen=False, reason="Temporary VC deafen expired")
        except discord.HTTPException as error:
            # They're still muted, so this keeps trying for as long as it takes. A role moved or a permission fixed hours later still frees them.
            log.warning("Could not end voice action %s for %s in %s, trying again: %s", action, user_id, guild_id, error)
            await self._schedule_action(guild_id, user_id, action, time.time() + RETRY_SECONDS)
            return
        await self._drop_actions(guild_id, user_id, action)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        if member.bot or after.channel is None or (member.guild.id, member.id) not in self._active:
            return
        now = time.time()
        rows = await self.db.all("SELECT action, expires_at FROM voice_actions WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
        rejoined = (member.guild.id, member.id) in self._rejoined
        self._rejoined.discard((member.guild.id, member.id))
        for row in rows:
            try:
                if row["action"] == BLOCK:
                    if row["expires_at"] > now:
                        await member.move_to(None, reason="Temporarily removed from voice by moderators")
                    else:
                        await self._drop_actions(member.guild.id, member.id, BLOCK)
                elif row["expires_at"] <= now:
                    if row["action"] == MUTE and after.mute:
                        await member.edit(mute=False, reason="Temporary VC mute expired")
                    elif row["action"] == DEAFEN and after.deaf:
                        await member.edit(deafen=False, reason="Temporary VC deafen expired")
                    await self._drop_actions(member.guild.id, member.id, row["action"])
                elif rejoined:  # leaving the server sheds a server mute, and this is the first moment it can go back on
                    if row["action"] == MUTE and not after.mute:
                        await member.edit(mute=True, reason="Temporary VC mute is still running. Put back after rejoining.")
                    elif row["action"] == DEAFEN and not after.deaf:
                        await member.edit(deafen=True, reason="Temporary VC deafen is still running. Put back after rejoining.")
            except discord.HTTPException as error:
                log.warning("Could not enforce voice action %s for %s: %s", row["action"], member.id, error)

    async def _vc_command(self, ctx: commands.Context, user: Any, action: str, duration: str | None, reason: str | None, label: str, *, default_if_none: bool = True) -> None:
        if ctx.interaction is not None:
            minutes = None
            if duration:
                minutes = read_length(duration)
                if minutes is None:
                    await ctx.fail(f"`{duration}` isn't a length of time. Try something like 10m or 2h.")
                    return
        else:
            minutes, reason = split_length(duration, reason)
        if minutes is not None and not 1 <= minutes <= MAX_MINUTES:
            await ctx.fail("The length must be between one minute and seven days.")
            return
        # What can be told now is told now, in private. Once deferred in public, the answer is public.
        refusal = self._action_refusal(ctx.author, user) or self._out_of_voice(user, action, minutes)
        if refusal:
            await ctx.fail(refusal)
            return
        await self._defer_public(ctx)
        audit = f"{label} by {ctx.author} ({ctx.author.id})" + (f": {reason}" if reason else "")
        ok, text = await self.perform(ctx.author, user, action, minutes, audit, reason=reason, default_if_none=default_if_none)
        if ok:  # staff in the channel see what was done, and only the person asking sees why it wasn't
            await ctx.ok(text, ephemeral=False)
        else:
            await ctx.fail(text)

    @staticmethod
    async def _defer_public(ctx: commands.Context) -> None:
        """Buys time for an answer everyone sees. It stays quiet when the server muted or moved this command's output, since the answer won't land here."""
        decision = ctx.decision
        await ctx.defer(ephemeral=bool(decision and (decision.muted or decision.redirect or decision.restricted_channel)))

    # ---- commands: /vc ----

    @commands.hybrid_group(name="vc", fallback="progress", description="See your progress toward voice channel access")
    @commands.guild_only()
    async def vc(self, ctx: commands.Context) -> None:
        await ctx.send(await self._progress_text(ctx.author), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="vc_report", description="Report someone misbehaving in a voice channel to the moderators")
    @commands.guild_only()
    @app_commands.describe(user="The person you are reporting", reason="What happened")
    async def vc_report(self, ctx: commands.Context, user: discord.Member, *, reason: str) -> None:
        reports = self.bot.module("reports")
        if reports is None:  # every full copy of the bot loads it
            await _delete_typed(ctx)
            await ctx.fail("Reports aren't available on this bot.", **({"delete_after": 10} if ctx.interaction is None else {}))
            return
        if ctx.interaction is None:  # typed in the open, so it comes down at once and the answer is private, as with /report
            await reports.typed_voice_report(ctx, user, reason)
            return
        refusal = reports.voice_refusal(ctx.author, user)
        if refusal:
            await ctx.fail(refusal)
            return
        await reports.submit_voice_report(ctx.interaction, user, reason)

    @vc_report.error
    async def _vc_report_refused(self, ctx: commands.Context, error: Exception) -> None:
        """A typed report that fails before the command's body would stay up in public, naming the person it accuses."""
        if ctx.interaction is not None or not isinstance(error, (commands.CheckFailure, commands.UserInputError, commands.CommandOnCooldown, commands.DisabledCommand)):
            return
        ctx.error_answered = True
        await _delete_typed(ctx)
        await ctx.fail(plain_error(ctx, error), delete_after=10)

    @commands.hybrid_command(name="vc_mute", description="Server-mute someone for a while")
    @commands.guild_only()
    @app_commands.describe(user="Who to mute", minutes="How long, in minutes or such as 2h. Leave out for the default.", reason="Reason for the audit log")
    @staff_only("Moderators and Veterans")
    async def vc_mute(self, ctx: commands.Context, user: discord.Member, minutes: str | None = None, *, reason: str | None = None) -> None:
        await self._vc_command(ctx, user, MUTE, minutes, reason, "VC temp mute")

    @commands.hybrid_command(name="vc_deafen", description="Server-deafen someone for a while")
    @commands.guild_only()
    @app_commands.describe(user="Who to deafen", minutes="How long, in minutes or such as 2h. Leave out for the default.", reason="Reason for the audit log")
    @staff_only("Moderators and Veterans")
    async def vc_deafen(self, ctx: commands.Context, user: discord.Member, minutes: str | None = None, *, reason: str | None = None) -> None:
        await self._vc_command(ctx, user, DEAFEN, minutes, reason, "VC temp deafen")

    @commands.hybrid_command(name="vc_disconnect", description="Disconnect someone from voice, and optionally block rejoining for a while")
    @commands.guild_only()
    @app_commands.describe(user="Who to disconnect", block_minutes="Also disconnect them if they rejoin within this long, in minutes or such as 2h", reason="Reason for the audit log")
    @staff_only("Moderators and Veterans")
    async def vc_disconnect(self, ctx: commands.Context, user: discord.Member, block_minutes: str | None = None, *, reason: str | None = None) -> None:
        await self._vc_command(ctx, user, BLOCK, block_minutes, reason, "VC disconnect", default_if_none=False)

    @commands.hybrid_command(name="vc_release", description="Lift someone's temporary voice mute, deafen or rejoin block")
    @commands.guild_only()
    @app_commands.describe(user="Who to release")
    @staff_only("Moderators")
    async def vc_release(self, ctx: commands.Context, user: discord.Member) -> None:
        if not self.is_moderator(ctx.author):
            await ctx.fail("Only moderators can release someone.")
            return
        rows = await self.db.all("SELECT action FROM voice_actions WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, user.id))
        if not rows:
            await ctx.fail(f"{discord.utils.escape_markdown(user.display_name)} has no temporary voice actions.")
            return
        await self._defer_public(ctx)
        actions = {row["action"] for row in rows}
        changes = {}
        if MUTE in actions:
            changes["mute"] = False
        if DEAFEN in actions:
            changes["deafen"] = False
        cleared_now = False
        if changes and user.voice is not None:
            try:
                await user.edit(reason=audit_text(f"Voice actions released by {ctx.author} ({ctx.author.id})"), **changes)
                cleared_now = True
            except discord.HTTPException as error:
                await ctx.fail(f"Couldn't clear the mute or deafen: {error.text or error.status}")
                return
        await self._finish_release(ctx.guild.id, user.id, actions, cleared_now or not changes)
        released = ", ".join(sorted(actions)).replace(BLOCK, "rejoin block")
        note = "" if cleared_now or not changes else " They aren't in voice right now, so the mute or deafen clears the moment they reconnect."
        await ctx.ok(f"Released {user.mention} from: {released}.{note}", ephemeral=False)

    async def _finish_release(self, guild_id: int, user_id: int, actions: set[str], cleared: bool) -> None:
        for action in actions:
            await self.bot.timers.cancel_key("voice_action", f"{guild_id}:{user_id}:{action}")
        if cleared:
            await self._drop_actions(guild_id, user_id)
        else:
            # Not in voice, so these become expired rows and on_voice_state_update clears them at the next connect. A block has nothing to clear.
            await self.db.run("UPDATE voice_actions SET expires_at = ? WHERE guild_id = ? AND user_id = ?", (time.time() - 1, guild_id, user_id))
            await self._drop_actions(guild_id, user_id, BLOCK)

    @vc.command(name="button", description="Post the button members press to ask for voice access")
    @app_commands.describe(channel="Where to post it. Defaults to this channel.")
    @staff_only("Manage Server")
    async def vc_button(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        # Checked here and not with requires, which would label the whole vc command on the Commands page.
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.fail("You need Manage Server to post the request button.")
            return
        target = channel or ctx.channel
        refusal = access.post_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        try:
            await target.send(**self._button_post(ctx.guild.id))
        except discord.Forbidden:
            await ctx.fail(f"I can't post in {target.mention}.")
            return
        await ctx.ok(f"The request button is posted in {target.mention}. Post it again after changing the requirements, so its text stays right.")

    def _button_post(self, guild_id: int) -> dict:
        return {"embed": discord.Embed.from_dict(self._button_embed(guild_id)), "view": component_view({}, self._button_controls()), "allowed_mentions": discord.AllowedMentions.none()}

    def _button_embed(self, guild_id: int) -> dict:
        cfg = self.cfg(guild_id)
        return {
            "title": "Voice Channel Access",
            "description": (
                "Press the button below to ask for access to the voice channels.\n\n"
                f"Requirements: at least **{cfg['min_messages']} messages** and **{cfg['min_days']} days** on the server. "
                "Requests that meet the requirements go to the moderators for a one-time approval, which lasts until it's revoked."
            ),
            "color": BLURPLE.value,
        }

    @staticmethod
    def _button_controls() -> list[dict]:
        return [{"type": "button", "custom_id": ACCESS_REQUEST_ID, "label": "Request Voice Access", "style": "primary"}]

    # ---- commands: voice access ----

    @commands.hybrid_command(name="vc_access_grant", description="Give someone voice access now, skipping the requirements")
    @commands.guild_only()
    @app_commands.describe(user="Who to give access")
    @staff_only("Moderators and Veterans")
    async def vc_access_grant(self, ctx: commands.Context, user: discord.Member) -> None:
        is_moderator = self.is_moderator(ctx.author)
        if not is_moderator and not self.is_vc_veteran(ctx.author):
            await ctx.fail("Only moderators and VC veterans can grant voice access.")
            return
        role, refusal = self._access_role(ctx.guild)
        refusal = refusal or self._rank_refusal(ctx.author, role)
        if refusal:
            await ctx.fail(refusal)
            return
        if not is_moderator:
            refusal = await self._veteran_grant_refusal(ctx.guild, user.id)
            if refusal:
                await ctx.fail(refusal)
                return
        await self._defer_public(ctx)
        tag = "" if is_moderator else " (VC veteran)"
        try:
            await user.add_roles(role, reason=audit_text(f"VC access granted by {ctx.author} ({ctx.author.id}){tag}"))
        except discord.HTTPException as error:
            await ctx.fail(self._role_refusal(error))
            return
        await self._close_pending_request(ctx.guild.id, user.id, f"Granted by {ctx.author.display_name}{tag}")
        await self._set_access(ctx.guild.id, user.id, str(user), "approved", ctx.author.id, str(ctx.author))
        if not is_moderator:  # a veteran's grant skips the requirements, so moderators get to see it
            embed = discord.Embed(title="Voice Access Granted", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            embed.add_field(name="Member", value=f"{user.mention} (`{user.id}`)", inline=True)
            embed.add_field(name="Granted by", value=f"{ctx.author.mention} (VC veteran, requirements skipped)", inline=True)
            self.bot.logs.post(self.cfg(ctx.guild.id)["approval_channel"], embed)
        await ctx.ok(f"Granted voice access to {user.mention}. It lasts until it's revoked.", ephemeral=False)

    async def _veteran_grant_refusal(self, guild: Any, user_id: int) -> str | None:
        """A denial or a revoke stays with full moderators, and a veteran's grant is logged, so with nowhere to log it doesn't happen."""
        channel_id = self.cfg(guild.id)["approval_channel"]
        if not channel_id or guild.get_channel(channel_id) is None:
            return "This server hasn't set an approval channel, so veteran grants are off. Tell an admin."
        status = await self.db.scalar("SELECT status FROM voice_access WHERE guild_id = ? AND user_id = ?", (guild.id, user_id))
        if status in ("denied", "revoked"):
            return "A moderator closed this one. Ask a moderator to grant it."
        return None

    @commands.hybrid_command(name="vc_access_revoke", description="Take away someone's voice access")
    @commands.guild_only()
    @app_commands.describe(user="Who to revoke", reason="Reason for the audit log and staff log")
    @staff_only("Moderators")
    async def vc_access_revoke(self, ctx: commands.Context, user: discord.Member, *, reason: str | None = None) -> None:
        if not self.is_moderator(ctx.author):
            await ctx.fail("Only moderators can revoke voice access.")
            return
        refusal = self._hierarchy_refusal(ctx.author, user)
        if refusal:
            await ctx.fail(refusal)
            return
        await self._defer_public(ctx)
        cfg = self.cfg(ctx.guild.id)
        role = ctx.guild.get_role(cfg["access_role"]) if cfg["access_role"] else None
        audit = audit_text(f"VC access revoked by {ctx.author} ({ctx.author.id})" + (f": {reason}" if reason else ""))
        if role is not None and role in user.roles:
            try:
                await user.remove_roles(role, reason=audit)
            except discord.Forbidden:
                await ctx.fail("I can't remove the voice access role, so nothing changed. Check Manage Roles and where my role sits.")
                return
        await self._close_pending_request(ctx.guild.id, user.id, f"Closed: access revoked by {ctx.author.display_name}")
        await self._set_access(ctx.guild.id, user.id, str(user), "revoked", ctx.author.id, str(ctx.author))
        if user.voice is not None:
            try:
                await user.move_to(None, reason=audit)
            except discord.HTTPException as error:
                log.warning("Could not disconnect %s after a revoke: %s", user.id, error)
        self._log_revoke(ctx.guild.id, user.id, ctx.author.mention, reason)
        await ctx.ok(f"Revoked voice access for {user.mention}. They can't ask again. Grant it to restore it.", ephemeral=False)

    def _log_revoke(self, guild_id: int, user_id: int, by: str, reason: str | None) -> None:
        embed = discord.Embed(title="Voice Access Revoked", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=f"<@{user_id}> (`{user_id}`)", inline=True)
        embed.add_field(name="Revoked by", value=by, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason[:1024], inline=False)
        self.bot.logs.post(self.cfg(guild_id)["approval_channel"], embed)

    @commands.hybrid_command(name="vc_access_status", description="Show someone's progress toward voice access")
    @commands.guild_only()
    @app_commands.describe(user="Who to check")
    @staff_only("Moderators")
    async def vc_access_status(self, ctx: commands.Context, user: discord.Member) -> None:
        if not self.is_moderator(ctx.author):
            await ctx.fail("Only moderators can check someone else's voice access.")
            return
        await ctx.send(await self._progress_text(user), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _progress_text(self, member: Any) -> str:
        cfg = self.cfg(member.guild.id)
        role = member.guild.get_role(cfg["access_role"]) if cfg["access_role"] else None
        row = await self.db.one("SELECT status FROM voice_access WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
        count = await self._message_count(member.guild.id, member.id)
        days = self._days_on_server(member)
        has = "role not configured" if role is None else ("yes" if role in member.roles else "no")
        return "\n".join([
            f"**Messages (since tracking began):** {count} / {cfg['min_messages']}",
            f"**Days on server:** {days} / {cfg['min_days']}",
            f"**Approval status:** {row['status'] if row else 'no request yet'}",
            f"**Has access role:** {has}",
        ])

    @staticmethod
    def _days_on_server(member: Any) -> int:
        return (discord.utils.utcnow() - member.joined_at).days if member.joined_at else 0

    # ---- VC reports ----

    @app_commands.guild_only()
    async def menu_report(self, interaction: discord.Interaction, member: discord.Member) -> None:
        reports = self.bot.module("reports")
        refusal = "Reports aren't available on this bot." if reports is None else reports.voice_refusal(interaction.user, member)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        async def submit(answer: discord.Interaction, reason: str) -> None:
            await reports.submit_voice_report(answer, member, reason)
        await interaction.response.send_modal(Form("Report to voice moderators", "What happened?", "Describe what this person is doing in voice", required=True, submit=submit))

    async def press_report(self, interaction: discord.Interaction, action: str) -> None:
        """The buttons on VC report posts from before 1.2.3. Newer VC reports are ordinary reports, with the reports module's buttons."""
        if not self.is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can act on VC reports.", ephemeral=True)
            return
        row = await self.db.one("SELECT * FROM voice_reports WHERE report_message_id = ? AND guild_id = ?", (interaction.message.id, interaction.guild_id))
        if row is None:
            await interaction.response.send_message("This report is no longer tracked.", ephemeral=True)
            return
        if row["status"] == "handled":  # a report someone acted on reads "acted" and keeps its buttons
            await interaction.response.send_message("This report has already been dismissed.", ephemeral=True)
            return
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(title="Voice Channel Report")
        if action == "dismiss":
            await self.db.run("UPDATE voice_reports SET status = 'handled', handled_by_name = ? WHERE report_message_id = ?", (str(interaction.user), row["report_message_id"]))
            embed.color = discord.Color.dark_gray()
            embed.add_field(name="Handled", value=f"Dismissed by {interaction.user.mention}", inline=False)
            await interaction.response.edit_message(embed=embed, view=None)
            return
        await interaction.response.defer()
        try:
            target = await find_member(interaction.guild, row["target_id"])
        except LOOKUP_ERRORS as error:
            log.warning("Could not look up %s for a VC report: %s", row["target_id"], error)
            await self.say(interaction, "Discord didn't answer when I looked that person up. Try again.")
            return
        if target is None:
            await self.say(interaction, "The reported person is no longer in the server.")
            return
        refusal = self._hierarchy_refusal(interaction.user, target)
        if refusal:
            await self.say(interaction, refusal)
            return
        minutes = self.cfg(interaction.guild_id)["temp_minutes"]
        ok, text = await self.apply_temp_action(target, action, minutes, interaction.user, f"VC report action by {interaction.user} ({interaction.user.id})", row["reason"])
        if not ok:
            await self.say(interaction, text)
            return
        await self.db.run("UPDATE voice_reports SET status = 'acted', handled_by_name = ? WHERE report_message_id = ?", (str(interaction.user), row["report_message_id"]))
        if len(embed.fields) < 25:  # Discord's limit, and enough for any report
            embed.add_field(name="Action taken", value=f"{text} (by {interaction.user.mention})"[:1024], inline=False)
        await interaction.edit_original_response(embed=embed)

    # ---- voice access ----

    def _tell_staff_once(self, guild: Any, role: Any, what: str) -> None:
        """A role moved above the bot after setup. The bot skips it and keeps its records, because moving the bot's role fixes it, and staff have no other way to learn why."""
        key = (guild.id, role.id)
        if key in self._out_of_reach:
            return
        self._out_of_reach.add(key)
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, f"The {what} role is out of reach", f"{role.mention} sits at or above the bot's top role, so the bot can't give it or take it off. Move the bot's role above it.")

    def _rank_refusal(self, actor: Any, role: Any) -> str | None:
        """A moderator hands out only what sits below them. A veteran's grant is a duty an Administrator gave that role, and it's logged."""
        if access.is_admin(actor) or not self.is_moderator(actor) or role < actor.top_role:
            return None
        return f"{role.name} sits at or above your highest role, so you can't give it."

    def _access_role(self, guild: Any) -> tuple[Any, str | None]:
        """The role to hand out, or why it can't be. It goes to anyone who gets approved, so it can't be one that carries power."""
        cfg = self.cfg(guild.id)
        role = guild.get_role(cfg["access_role"]) if cfg["access_role"] else None
        if role is None:
            return None, "The voice access role isn't set or no longer exists. Choose it on the Voice controls page."
        if not role.is_default() and role >= guild.me.top_role:
            self._tell_staff_once(guild, role, "voice access")
        if role.is_default() or role.managed or role >= guild.me.top_role:
            return None, "I can't give the voice access role. It must be an ordinary role below mine."
        if not role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE)):
            return None, "The voice access role holds moderation permissions, so the bot won't hand it out. Pick another role, or allow elevated roles on the Commands page."
        return role, None

    async def _message_count(self, guild_id: int, user_id: int) -> int:
        stored = await self.db.scalar("SELECT count FROM voice_message_counts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) or 0
        return stored + self._pending_counts.get((guild_id, user_id), 0)

    async def flush_counts(self) -> None:
        if not self._pending_counts:
            return
        pending, self._pending_counts = self._pending_counts, Counter()
        try:
            await self.db.many(
                "INSERT INTO voice_message_counts (guild_id, user_id, count) VALUES (?, ?, ?) ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + excluded.count",
                [(guild_id, user_id, amount) for (guild_id, user_id), amount in pending.items()],
            )
        except BaseException:  # a shutdown cancels this too, and its last write has to find these still waiting
            self._pending_counts.update(pending)  # they go out with the next write
            raise

    @tasks.loop(seconds=FLUSH_SECONDS)
    async def _flush_loop(self) -> None:
        try:
            await self.flush_counts()
        except Exception:
            log.exception("Could not store voice message counts")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is not None and not message.author.bot:
            self._pending_counts[(message.guild.id, message.author.id)] += 1

    async def submit_request(self, member: Any) -> str:
        """Screen a request from the button. Only requests that meet the requirements reach staff. Returns the reply for the member."""
        if not hasattr(member, "guild"):
            return "This button only works inside a server."
        guild = member.guild
        cfg = self.cfg(guild.id)
        role, refusal = self._access_role(guild)
        channel = guild.get_channel(cfg["approval_channel"]) if cfg["approval_channel"] else None
        if refusal or channel is None:
            return "Voice access requests aren't fully set up on this server yet. Tell an admin."
        if role in member.roles:
            return "You already have voice channel access."
        row = await self.db.one("SELECT status, request_message_id, request_channel_id, requested_at FROM voice_access WHERE guild_id = ? AND user_id = ?", (guild.id, member.id))
        if row is not None and row["status"] == "pending" and await self._request_is_lost(guild, row):
            await self.db.run("DELETE FROM voice_access WHERE guild_id = ? AND user_id = ? AND status = 'pending'", (guild.id, member.id))
            row = None
        if row is not None:
            if row["status"] == "pending":
                return "Your request is already waiting for moderator review."
            if row["status"] == "approved":
                try:
                    await member.add_roles(role, reason="VC access previously approved, restored on request")
                except discord.HTTPException:
                    return "You were already approved, but I couldn't restore the role. Contact the moderators."
                return "You were already approved, so I restored your access role."
            return "Your voice access was denied or revoked earlier. Contact the moderators if you think this should change."
        count = await self._message_count(guild.id, member.id)
        days = self._days_on_server(member)
        missing = []
        if count < cfg["min_messages"]:
            missing.append(f"messages: {count} of {cfg['min_messages']}")
        if days < cfg["min_days"]:
            missing.append(f"days on the server: {days} of {cfg['min_days']}")
        if missing:
            return "You don't meet the requirements yet (" + ", ".join(missing) + "). Keep participating and try again later."
        # Claim the pending slot first, so a double click can't post twice.
        claimed = await self.db.run(
            "INSERT INTO voice_access (guild_id, user_id, user_name, status, requested_at) VALUES (?, ?, ?, 'pending', ?) ON CONFLICT (guild_id, user_id) DO NOTHING",
            (guild.id, member.id, str(member), time.time()),
        )
        if not claimed:
            return "Your request is already waiting for moderator review."
        embed = discord.Embed(title="Voice Access Request", description=f"{member.mention} requests voice channel access.", color=BLURPLE, timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Messages", value=f"{count} (requires {cfg['min_messages']})", inline=True)
        embed.add_field(name="Time on server", value=f"{days} days (requires {cfg['min_days']})", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            posted = await channel.send(embed=embed, view=component_view({}, self._approval_controls()), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.warning("Could not post a voice access request in %s: %s", channel.id, error)
            # The claim goes back, so the member can try again once staff fix the channel.
            await self.db.run("DELETE FROM voice_access WHERE guild_id = ? AND user_id = ? AND status = 'pending'", (guild.id, member.id))
            return "I couldn't reach the staff approval channel. Tell a moderator."
        await self.db.run("UPDATE voice_access SET request_message_id = ?, request_channel_id = ? WHERE guild_id = ? AND user_id = ?", (posted.id, channel.id, guild.id, member.id))
        return "Your request went to the moderators. You'll get the role once it's approved."

    async def _request_is_lost(self, guild: Any, row: dict) -> bool:
        """Staff can delete the post or its channel, and a crash can land between the claim and the post. Nobody can answer such a request, so the member may ask again."""
        if not row["request_message_id"]:
            return time.time() - (row["requested_at"] or 0.0) > REQUEST_CLAIM_SECONDS  # younger than this, the first click may still be posting
        channel_id = row["request_channel_id"] or self.cfg(guild.id)["approval_channel"]
        if not channel_id or guild.get_channel(channel_id) is None:
            return True
        try:
            await self.bot.ops.fetch_message(channel_id, row["request_message_id"])
        except MessageGone:
            return True
        except OpsError as error:
            log.info("Could not check a voice access request in %s: %s", channel_id, error)  # this says nothing about whether it's gone
        return False

    @staticmethod
    def _approval_controls() -> list[dict]:
        return [
            {"type": "button", "custom_id": ACCESS_APPROVE_ID, "label": "Approve VC Access", "style": "success"},
            {"type": "button", "custom_id": ACCESS_DENY_ID, "label": "Deny", "style": "danger"},
        ]

    async def press_decision(self, interaction: discord.Interaction, approve: bool) -> None:
        is_moderator = self.is_moderator(interaction.user)
        if approve and not is_moderator and not self.is_vc_veteran(interaction.user):
            await interaction.response.send_message("Only moderators and VC veterans can approve voice access requests.", ephemeral=True)
            return
        if not approve and not is_moderator:  # a denial blocks asking again for good, so it stays with full moderators
            await interaction.response.send_message("Only moderators can deny voice access requests.", ephemeral=True)
            return
        row = await self.db.one("SELECT user_id, status FROM voice_access WHERE guild_id = ? AND request_message_id = ?", (interaction.guild_id, interaction.message.id))
        if row is None:
            await interaction.response.send_message("This request is no longer tracked.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message(f"This request was already resolved ({row['status']}).", ephemeral=True)
            return
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(title="Voice Access Request")
        user_id = row["user_id"]
        await interaction.response.defer()
        member = role = None
        if approve:
            try:
                member = await find_member(interaction.guild, user_id)
            except LOOKUP_ERRORS as error:
                log.warning("Could not look up %s for a voice access request: %s", user_id, error)
                await self.say(interaction, "Discord didn't answer when I looked that person up. Try again.")
                return
            if member is None:
                await self.say(interaction, "That person is no longer in the server. The request stays pending in case they return.")
                return
            role, refusal = self._access_role(interaction.guild)
            refusal = refusal or self._rank_refusal(interaction.user, role)
            if refusal:
                await self.say(interaction, refusal)
                return
        # One decision wins. Claiming it first means a role call waiting on Discord can't overwrite a denial that lands meanwhile.
        status = "approved" if approve else "denied"
        claimed = await self.db.run(
            "UPDATE voice_access SET status = ?, decided_by = ?, decided_by_name = ?, decided_at = ? WHERE guild_id = ? AND user_id = ? AND status = 'pending'",
            (status, interaction.user.id, str(interaction.user), time.time(), interaction.guild_id, user_id),
        )
        if not claimed:
            fresh = await self.db.scalar("SELECT status FROM voice_access WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, user_id))
            await self.say(interaction, f"This request was already resolved ({fresh}).")
            return
        if approve:
            try:
                await member.add_roles(role, reason=audit_text(f"VC access approved by {interaction.user} ({interaction.user.id})"))
            except discord.HTTPException as error:
                log.warning("Could not give the voice access role to %s: %s", user_id, error)
                await self.db.run("UPDATE voice_access SET status = 'pending', decided_by = NULL, decided_by_name = NULL, decided_at = NULL WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, user_id))
                await self.say(interaction, self._role_refusal(error))
                return
            embed.color = discord.Color.green()
            embed.set_footer(text=f"Approved by {interaction.user.display_name}{'' if is_moderator else ' (VC veteran)'}. Lasts until revoked.")
        else:
            embed.color = discord.Color.red()
            embed.set_footer(text=f"Denied by {interaction.user.display_name}")
        await interaction.edit_original_response(embed=embed, view=None)

    async def _set_access(self, guild_id: int, user_id: int, user_name: str, status: str, decided_by: int, decided_by_name: str) -> None:
        now = time.time()
        await self.db.run(
            "INSERT INTO voice_access (guild_id, user_id, user_name, status, requested_at, decided_by, decided_by_name, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET user_name = excluded.user_name, status = excluded.status, decided_by = excluded.decided_by, decided_by_name = excluded.decided_by_name, decided_at = excluded.decided_at",
            (guild_id, user_id, user_name, status, now, decided_by, decided_by_name, now),
        )

    async def _close_pending_request(self, guild_id: int, user_id: int, note: str) -> None:
        """Take the buttons off a request that's still open, since a decision elsewhere has answered it. Fine if the message is gone."""
        row = await self.db.one("SELECT request_message_id, request_channel_id, status FROM voice_access WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        channel_id = (row and row["request_channel_id"]) or self.cfg(guild_id)["approval_channel"]  # the post stays where it was made when the setting moves
        if row is None or row["status"] != "pending" or not row["request_message_id"] or not channel_id:
            return
        ops = self.bot.ops
        try:
            message = await ops.fetch_message(channel_id, row["request_message_id"])
            payload = dict(message["payload"])
            if payload.get("embeds"):
                payload["embeds"] = [{**payload["embeds"][0], "footer": {"text": note}, "color": discord.Color.dark_gray().value}, *payload["embeds"][1:]]
            await ops.edit_message(channel_id, row["request_message_id"], payload, components=[])
        except OpsError as error:
            log.info("Could not close the request for %s: %s", user_id, error)

    async def _restore_on_join(self, member: Any) -> None:
        await self._restore_access(member)
        await self._restore_mini_mute(member)
        await self._note_running_actions(member)

    async def _note_running_actions(self, member: Any) -> None:
        """A server mute can only go back on in voice, so someone who left and came back is marked for their next connect."""
        key = (member.guild.id, member.id)
        if key in self._active and await self.db.scalar(
            "SELECT 1 FROM voice_actions WHERE guild_id = ? AND user_id = ? AND action IN (?, ?) AND expires_at > ?", (*key, MUTE, DEAFEN, time.time()),
        ):
            self._rejoined.add(key)

    @commands.Cog.listener()
    async def on_member_join(self, member: Any) -> None:
        # A member still on the rules screen can't take roles, so they get theirs when they accept.
        if not member.bot and not member.pending:
            await self._restore_on_join(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: Any, after: Any) -> None:
        if before.pending and not after.pending and not after.bot:
            await self._restore_on_join(after)
        removed = {role.id for role in before.roles} - {role.id for role in after.roles}
        if removed:
            await self._roles_removed(after, removed)

    async def _roles_removed(self, member: Any, removed: set[int]) -> None:
        if self.cfg(member.guild.id)["access_role"] in removed:
            await self._access_role_removed(member, self.cfg(member.guild.id)["access_role"])
        row = await self.db.one("SELECT role_id FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
        held = (row["role_id"] if row else None) or self.cfg(member.guild.id)["mini_mute_role"]
        if held in removed and self._shared_with(member.guild.id, held) is None:
            await self._mini_role_removed(member)

    async def _access_role_removed(self, member: Any, role_id: int) -> None:
        """A person taking the role off by hand has revoked it, so pressing the button or rejoining doesn't bring it back. A bot's change doesn't count, as a hardmute strips this role and puts it back later. With no audit entry to read, nothing is assumed."""
        status = await self.db.scalar("SELECT status FROM voice_access WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
        if status != "approved":
            return

        def took_it_off(entry: Any) -> bool:
            return any(getattr(role, "id", None) == role_id for role in getattr(entry.changes.before, "roles", None) or [])

        entry = await self.bot.audit.find(member.guild.id, discord.AuditLogAction.member_role_update, member.id, wait=self._audit_wait, where=took_it_off)
        if entry is None or entry.user is None or entry.user.bot:
            return
        await self._set_access(member.guild.id, member.id, str(member), "revoked", entry.user.id, str(entry.user))
        self._log_revoke(member.guild.id, member.id, entry.user.mention, entry.reason)

    async def _mini_role_removed(self, member: Any) -> None:
        key = (member.guild.id, member.id)
        until = self._own_removals.pop(key, None)
        if until is not None and until > time.monotonic():
            return
        row = await self.db.one("SELECT user_id FROM mini_mutes WHERE guild_id = ? AND user_id = ?", key)
        if row is not None:
            await self.forget_mini_mute(*key)
            await self._record_case(member.guild, "unminimute", member, None, "Mute role removed")

    async def _restore_access(self, member: Any) -> None:
        cfg = self.cfg(member.guild.id)
        if not cfg["access_role"]:
            return
        row = await self.db.one("SELECT status FROM voice_access WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
        if row is None or row["status"] != "approved":
            return
        role, refusal = self._access_role(member.guild)
        if refusal:
            return
        try:
            await member.add_roles(role, reason="VC access approval lasts until revoked. Restored on rejoin.")
        except discord.HTTPException as error:
            log.warning("Could not restore the voice access role for %s: %s", member.id, error)

    # ---- mini mutes ----

    def _mini_mute_role(self, guild: Any) -> Any:
        role_id = self.cfg(guild.id)["mini_mute_role"]
        return guild.get_role(role_id) if role_id else None

    def _held_role(self, guild: Any, row: dict | None) -> Any:
        """The role a running mini mute put on, which stays its role if the server picks a new one meanwhile. A row from before the bot kept this reads the setting."""
        role_id = row["role_id"] if row else None
        return guild.get_role(role_id) if role_id else self._mini_mute_role(guild)

    def _shared_with(self, guild_id: int, role_id: int | None) -> Any:
        """The moderation feature, when this server's mini mutes and regular mutes use one role."""
        moderation = self.bot.module("moderation")
        if moderation is not None and role_id is not None and moderation.cfg(guild_id)["mute_role"] == role_id:
            return moderation
        return None

    async def _release_role(self, guild: Any, member: Any, role: Any, audit: str) -> bool:
        """A mini mute is done with its role. True if the role is off now. False if a regular mute on the same role still holds it."""
        shared = self._shared_with(guild.id, role.id)
        if shared is not None:
            return await shared.release_mute_role(guild, member, role, audit)
        if role in member.roles:
            self._own_role_change(guild.id, member.id)
            await member.remove_roles(role, reason=audit)
        return True

    def _own_role_change(self, guild_id: int, user_id: int) -> None:
        now = time.monotonic()
        if len(self._own_removals) > 5000:
            self._own_removals = {key: until for key, until in self._own_removals.items() if until > now}
        self._own_removals[(guild_id, user_id)] = now + 30

    async def mini_mute_holds(self, guild_id: int, user_id: int, role_id: int) -> bool:
        """Whether a mini mute still running needs this role on this person."""
        row = await self.db.one("SELECT expires_at, role_id FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return row is not None and (row["role_id"] or self.cfg(guild_id)["mini_mute_role"]) == role_id and row["expires_at"] > time.time()

    async def forget_mini_mute(self, guild_id: int, user_id: int) -> None:
        """Someone ended it another way, so nothing is left to take off and it mustn't come back after a rejoin."""
        await self.db.run("DELETE FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await self.bot.timers.cancel_key("mini_mute", f"{guild_id}:{user_id}")

    async def _schedule_mini_mute(self, guild_id: int, user_id: int, due: float) -> None:
        await self.bot.timers.schedule("mini_mute", due, {"guild_id": guild_id, "user_id": user_id}, guild_id=guild_id, key=f"{guild_id}:{user_id}")

    async def _record_case(self, guild: Any, action: str, user: Any, moderator: Any, reason: str | None, seconds: int | None = None) -> None:
        """A mini mute leaves no mark on a member's record, unless this server turned on mini_mute_cases. A problem there must not undo the mute itself."""
        recorder = getattr(self.bot.module("moderation"), "record_case", None)
        if recorder is None or guild is None or not self.cfg(guild.id)["mini_mute_cases"]:
            return
        try:
            await recorder(guild, action, user, moderator, reason=reason, seconds=seconds, source="mini mute")
        except Exception:
            log.exception("Could not record a %s case in %s", action, guild.id)

    async def apply_mini_mute(self, target: Any, minutes: int, reason: str | None, audit: str, applied_by: Any) -> tuple[bool, str | None, float | None]:
        """Apply the role and record when to take it back. Returns (ok, why not, when it lifts)."""
        guild = target.guild
        role = self._mini_mute_role(guild)
        if role is None:
            return False, "No mini mute role is set. Choose one on the Voice controls page.", None
        if role.is_default() or role >= guild.me.top_role:
            return False, f"The mute role ({role.name}) sits at or above my top role, so I can't apply it.", None
        if not role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE)):
            return False, "The mini mute role holds moderation permissions, so I won't apply it. Pick another role.", None
        expires_at = time.time() + minutes * 60
        try:
            shared = self._shared_with(guild.id, role.id)
            if shared is not None:
                async with shared.adding_mute_role(guild, target, role):
                    await target.add_roles(role, reason=audit_text(audit))
            else:
                await target.add_roles(role, reason=audit_text(audit))
        except discord.Forbidden:
            return False, "I can't apply the mute role. I need Manage Roles, and my role must sit above the mute role.", None
        except discord.HTTPException as error:
            return False, f"Discord refused: {error.text or error.status}", None
        await self.db.run(
            "INSERT INTO mini_mutes (guild_id, user_id, user_name, expires_at, applied_by, applied_by_name, reason, role_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET user_name = excluded.user_name, expires_at = excluded.expires_at, applied_by = excluded.applied_by, applied_by_name = excluded.applied_by_name, reason = excluded.reason, role_id = excluded.role_id",
            (guild.id, target.id, str(target), expires_at, applied_by.id, str(applied_by), reason, role.id),
        )
        await self._schedule_mini_mute(guild.id, target.id, expires_at)
        await self._record_case(guild, "minimute", target, applied_by, reason, minutes * 60)
        return True, None, expires_at

    async def _mini_mute_due(self, payload: dict, row: dict) -> None:
        guild_id, user_id = payload["guild_id"], payload["user_id"]
        stored = await self.db.one("SELECT expires_at, user_name, role_id FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        if stored is None or stored["expires_at"] > time.time():
            return  # lifted already, or applied again with a later end
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            role = self._held_role(guild, stored)
            try:
                member = await find_member(guild, user_id)
            except LOOKUP_ERRORS as error:
                # A gap in the member list right after a start isn't someone leaving, so the row stays.
                log.warning("Could not look up %s to lift a mini mute in %s, trying again: %s", user_id, guild_id, error)
                await self._schedule_mini_mute(guild_id, user_id, time.time() + RETRY_SECONDS)
                return
            if role is not None and member is not None:
                try:
                    await self._release_role(guild, member, role, "Mini mute expired")
                except discord.HTTPException as error:
                    # They're still muted, so this keeps trying for as long as it takes, with the row still here.
                    log.warning("Could not lift the mini mute on %s in %s, trying again: %s", user_id, guild_id, error)
                    if role >= guild.me.top_role:
                        self._tell_staff_once(guild, role, "mini mute")
                    await self._schedule_mini_mute(guild_id, user_id, time.time() + RETRY_SECONDS)
                    return
            await self._record_case(guild, "unminimute", member or Person(user_id, stored["user_name"]), None, "Mini mute expired")
        await self.db.run("DELETE FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))

    async def _restore_mini_mute(self, member: Any) -> None:
        """Leaving and rejoining strips roles, so an unexpired mini mute goes back on."""
        row = await self.db.one("SELECT expires_at, role_id FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
        if row is None:
            return
        if row["expires_at"] <= time.time():
            await self.db.run("DELETE FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
            return
        role = self._held_role(member.guild, row)
        if role is None:
            return
        shared = self._shared_with(member.guild.id, role.id)
        if shared is not None and await shared.mute_running(member.guild.id, member.id):
            return  # the regular mute puts the same role back
        try:
            if shared is not None:
                async with shared.adding_mute_role(member.guild, member, role):
                    await member.add_roles(role, reason="Mini mute is still running. Put back after rejoining.")
            else:
                await member.add_roles(role, reason="Mini mute is still running. Put back after rejoining.")
        except discord.HTTPException as error:
            log.warning("Could not put the mini mute back on %s: %s", member.id, error)

    def _mini_mute_refusal(self, actor: Any, target: Any) -> str | None:
        """Server Veterans may only act on ordinary members."""
        if target.id == actor.id:
            return "You can't mini mute yourself."
        if target.bot:
            return "Bots can't be mini muted."
        if self._looks_like_staff(target):
            return "Server Veterans can't mini mute moderators."
        if self.is_server_veteran(target) or self.is_vc_veteran(target):
            return "Server Veterans can't mini mute fellow veterans. Ask a moderator."
        return None

    def _mini_mute_gate(self, actor: Any, target: Any) -> str | None:
        """Who may ask for a mini mute on whom. Moderators follow the role hierarchy, and everyone else the veteran limits."""
        if self.is_moderator(actor):
            if target.bot:
                return "Bots can't be mini muted."
            return self._hierarchy_refusal(actor, target)
        if not self.is_server_veteran(actor):
            return "Only Server Veterans and moderators can mini mute."
        return self._mini_mute_refusal(actor, target)

    async def request_mini_mute(self, actor: Any, target: Any, reason: str | None) -> str:
        """Apply a mini mute or open a ballot, and return the reply for the person asking. Shared by the command and the menu."""
        guild = actor.guild
        cfg = self.cfg(guild.id)
        refusal = self._mini_mute_gate(actor, target)
        if refusal:
            return refusal
        role = self._mini_mute_role(guild)
        if role is None:
            return "No mini mute role is set. Choose one on the Voice controls page."
        name = discord.utils.escape_markdown(target.display_name)
        # No stacking onto a mute already running, so an expiring mini mute can never cut short a longer one someone else applied.
        if role in target.roles:
            existing = await self.db.one("SELECT expires_at FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (guild.id, target.id))
            lifts = f" It lifts <t:{int(existing['expires_at'])}:R>." if existing else ""
            return f"{name} is already muted.{lifts}"
        minutes, required = cfg["mini_mute_minutes"], cfg["mini_mute_votes_required"]
        is_moderator = self.is_moderator(actor)
        # Moderators skip the ballot, and a server that wants one vote applies at once.
        if is_moderator or required <= 1:
            audit = f"Mini mute by {actor} ({actor.id})" + (f": {reason}" if reason else "")
            ok, why, expires_at = await self.apply_mini_mute(target, minutes, reason, audit, actor)
            if not ok:
                return why
            embed = discord.Embed(title="Mini Mute Applied", color=discord.Color.red(), timestamp=discord.utils.utcnow())
            embed.add_field(name="Target", value=f"{target.mention} (`{target.id}`)", inline=True)
            embed.add_field(name="By", value=f"{actor.mention}{'' if is_moderator else ' (Server Veteran)'}", inline=True)
            embed.add_field(name="Duration", value=format_minutes(minutes), inline=True)
            embed.add_field(name="Lifts", value=f"<t:{int(expires_at)}:R>", inline=True)
            embed.add_field(name="Reason", value=(reason or "No reason given")[:1024], inline=False)
            self.bot.logs.post(cfg["mini_mute_log_channel"], embed)
            return f"{target.mention} is muted for {format_minutes(minutes)}, lifting <t:{int(expires_at)}:R>."
        return await self._open_vote(actor, target, reason, minutes, required)

    async def _open_vote(self, actor: Any, target: Any, reason: str | None, minutes: int, required: int) -> str:
        guild = actor.guild
        cfg = self.cfg(guild.id)
        channel = guild.get_channel(cfg["mini_mute_log_channel"]) if cfg["mini_mute_log_channel"] else None
        if channel is None:
            return "Mini mute votes need a log channel, and none is set. Tell an admin."
        name = discord.utils.escape_markdown(target.display_name)
        existing = await self.db.one("SELECT vote_message_id FROM mini_mute_votes WHERE guild_id = ? AND target_id = ? AND status = 'open'", (guild.id, target.id))
        if existing:
            return f"A mini mute vote for {name} is already open in {channel.mention}. Vote there instead."
        expires_at = time.time() + cfg["mini_mute_vote_window"] * 60
        veterans = guild.get_role(cfg["server_veteran_role"]) if cfg["server_veteran_role"] else None
        try:
            posted = await channel.send(
                content=veterans.mention if veterans else None, embed=vote_embed(target.id, actor.id, reason, minutes, required, 1, 0, expires_at),
                view=component_view({}, self._vote_controls()),
                allowed_mentions=discord.AllowedMentions(roles=[veterans], users=False, everyone=False) if veterans else discord.AllowedMentions.none(),
            )
        except discord.HTTPException as error:
            log.warning("Could not post a mini mute vote in %s: %s", channel.id, error)
            if isinstance(error, discord.Forbidden):
                return "I can't post in the mini mute log channel. Tell an admin."
            return f"Discord refused the vote: {error.text or error.status}. Try again."
        async with self.db.transaction():
            await self.db.run(
                "INSERT INTO mini_mute_votes (vote_message_id, guild_id, channel_id, target_id, target_name, initiator_id, initiator_name, reason, minutes, required, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (posted.id, guild.id, channel.id, target.id, str(target), actor.id, str(actor), reason, minutes, required, expires_at),
            )
            await self.db.run("INSERT INTO mini_mute_ballots (vote_message_id, guild_id, voter_id, approve) VALUES (?, ?, ?, 1)", (posted.id, guild.id, actor.id))
        await self.bot.timers.schedule("mini_mute_vote", expires_at, {"vote_id": posted.id}, guild_id=guild.id, key=str(posted.id))
        return f"Your mini mute request for {target.mention} needs {required - 1} more veteran {'vote' if required == 2 else 'votes'}. It's posted in {channel.mention} and closes in {format_minutes(cfg['mini_mute_vote_window'])}."

    @staticmethod
    def _vote_controls() -> list[dict]:
        return [
            {"type": "button", "custom_id": VOTE_FOR_ID, "label": "Vote to Mute", "style": "danger"},
            {"type": "button", "custom_id": VOTE_AGAINST_ID, "label": "Vote Against", "style": "secondary"},
            {"type": "button", "custom_id": VOTE_VETO_ID, "label": "Veto (moderator)", "style": "primary"},
        ]

    async def _ballots(self, vote_id: int) -> tuple[int, int]:
        row = await self.db.one("SELECT SUM(approve) AS yes, SUM(1 - approve) AS no FROM mini_mute_ballots WHERE vote_message_id = ? AND guild_id = (SELECT guild_id FROM mini_mute_votes WHERE vote_message_id = ?)", (vote_id, vote_id))
        return (row["yes"] or 0), (row["no"] or 0)

    async def _claim_close(self, vote_id: int, status: str) -> bool:
        """Two clicks reaching quorum together must not both apply the mute, so closing is one atomic change that only the first can make."""
        return await self.db.run("UPDATE mini_mute_votes SET status = ? WHERE vote_message_id = ? AND status = 'open'", (status, vote_id)) > 0

    async def press_vote(self, interaction: discord.Interaction, approve: bool) -> None:
        if not self.is_moderator(interaction.user) and not self.is_server_veteran(interaction.user):
            await interaction.response.send_message("Only Server Veterans and moderators can vote on mini mutes.", ephemeral=True)
            return
        vote_id = interaction.message.id
        row = await self.db.one("SELECT * FROM mini_mute_votes WHERE vote_message_id = ? AND guild_id = ?", (vote_id, interaction.guild_id))
        if row is None:
            await interaction.response.send_message("This vote is no longer tracked.", ephemeral=True)
            return
        if row["status"] != "open":
            await interaction.response.send_message(f"This vote is already closed ({row['status']}).", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            target = await find_member(interaction.guild, row["target_id"])
        except LOOKUP_ERRORS as error:
            log.warning("Could not look up %s for a mini mute vote: %s", row["target_id"], error)
            await self.say(interaction, "Discord didn't answer when I looked that person up. Try again.")
            return
        if target is None:
            await self.db.run("UPDATE mini_mute_votes SET status = 'expired' WHERE vote_message_id = ?", (vote_id,))
            await self.bot.timers.cancel_key("mini_mute_vote", str(vote_id))
            await interaction.edit_original_response(view=None)
            await self.say(interaction, "That member has left the server, so this vote is closed.")
            return
        counted = await self.db.run(
            "INSERT INTO mini_mute_ballots (vote_message_id, guild_id, voter_id, approve) VALUES (?, ?, ?, ?) ON CONFLICT (vote_message_id, voter_id) DO NOTHING",
            (vote_id, interaction.guild_id, interaction.user.id, 1 if approve else 0),
        )
        if not counted:
            await self.say(interaction, "You already voted on this one.")
            return
        approvals, rejections = await self._ballots(vote_id)
        required = row["required"]
        outcome = "open"
        note = "Your vote is recorded."
        if approvals >= required or rejections >= required:
            outcome = "applied" if approvals >= required else "rejected"
            if not await self._claim_close(vote_id, outcome):
                await self.say(interaction, "Another vote just closed this request.")
                return
            await self.bot.timers.cancel_key("mini_mute_vote", str(vote_id))
            if outcome == "applied":
                role = self._mini_mute_role(interaction.guild)
                name = discord.utils.escape_markdown(target.display_name)
                if role is not None and role in target.roles:
                    # Someone muted them while the ballot was open, so that mute stays as it is, with its own clock.
                    note = f"Quorum reached, but {name} is already muted. The existing mute stays as it is."
                else:
                    audit = f"Mini mute passed by {approvals} Server Veterans" + (f": {row['reason']}" if row["reason"] else "")
                    applied_by = interaction.guild.get_member(row["initiator_id"]) or Person(row["initiator_id"], row["initiator_name"])
                    ok, why, _ = await self.apply_mini_mute(target, row["minutes"], row["reason"], audit, applied_by)
                    if not ok:
                        # The mute didn't land, so the vote goes back to open with its timer, and this click's ballot stays counted.
                        await self.db.run("UPDATE mini_mute_votes SET status = 'open' WHERE vote_message_id = ?", (vote_id,))
                        await self.bot.timers.schedule("mini_mute_vote", row["expires_at"], {"vote_id": vote_id}, guild_id=interaction.guild_id, key=str(vote_id))
                        await self.say(interaction, why)
                        return
                    note = f"Quorum reached. {name} is muted for {format_minutes(row['minutes'])}."
            else:
                note = "The request was voted down."
        embed = vote_embed(row["target_id"], row["initiator_id"], row["reason"], row["minutes"], required, approvals, rejections, row["expires_at"], outcome)
        await interaction.edit_original_response(embed=embed, view=None if outcome != "open" else component_view({}, self._vote_controls()))
        await self.say(interaction, note)

    async def press_veto(self, interaction: discord.Interaction) -> None:
        if not self.is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can veto a mini mute vote.", ephemeral=True)
            return
        vote_id = interaction.message.id
        row = await self.db.one("SELECT * FROM mini_mute_votes WHERE vote_message_id = ? AND guild_id = ?", (vote_id, interaction.guild_id))
        if row is None:
            await interaction.response.send_message("This vote is no longer tracked.", ephemeral=True)
            return
        if not await self._claim_close(vote_id, "vetoed"):
            fresh = await self.db.scalar("SELECT status FROM mini_mute_votes WHERE vote_message_id = ?", (vote_id,))
            await interaction.response.send_message(f"This vote is already closed ({fresh}).", ephemeral=True)
            return
        await self.bot.timers.cancel_key("mini_mute_vote", str(vote_id))
        approvals, rejections = await self._ballots(vote_id)
        embed = vote_embed(row["target_id"], row["initiator_id"], row["reason"], row["minutes"], row["required"], approvals, rejections, row["expires_at"], "vetoed")
        embed.set_footer(text=f"Vetoed by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=None)

    async def _vote_due(self, payload: dict, row: dict) -> None:
        vote_id = payload["vote_id"]
        vote = await self.db.one("SELECT * FROM mini_mute_votes WHERE vote_message_id = ?", (vote_id,))
        if vote is None or not await self._claim_close(vote_id, "expired"):
            return
        approvals, rejections = await self._ballots(vote_id)
        embed = vote_embed(vote["target_id"], vote["initiator_id"], vote["reason"], vote["minutes"], vote["required"], approvals, rejections, vote["expires_at"], "expired")
        ops = self.bot.ops
        try:
            await ops.edit_message(vote["channel_id"], vote_id, {"content": "", "embeds": [embed.to_dict()], "buttons": []}, components=[])
        except OpsError as error:
            log.info("Could not close the vote message %s: %s", vote_id, error)

    @commands.hybrid_command(name="mini_mute", description="Briefly mute a member (Server Veterans and moderators)")
    @commands.guild_only()
    @app_commands.describe(user="Who to mute", reason="Why this member should be muted")
    @staff_only("Moderators and Veterans")
    async def mini_mute(self, ctx: commands.Context, user: discord.Member, *, reason: str | None = None) -> None:
        await ctx.defer(ephemeral=True)
        answer = await self.request_mini_mute(ctx.author, user, reason)
        await ctx.send(answer, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="mini_mute_lift", description="Take a member's mini mute off early (moderators)")
    @commands.guild_only()
    @app_commands.describe(user="Who to unmute", reason="Reason for the log")
    @staff_only("Moderators")
    async def mini_mute_lift(self, ctx: commands.Context, user: discord.Member, *, reason: str | None = None) -> None:
        if not self.is_moderator(ctx.author):
            await ctx.fail("Only moderators can lift a mini mute.")
            return
        active = await self.db.one("SELECT user_id, role_id FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, user.id))
        role = self._held_role(ctx.guild, active)
        name = discord.utils.escape_markdown(user.display_name)
        if active is None:
            if role is None or role not in user.roles:
                await ctx.fail(f"{name} isn't mini muted.")
                return
            shared = self._shared_with(ctx.guild.id, role.id)
            if shared is not None and await shared.mute_running(ctx.guild.id, user.id):
                await ctx.fail("That's a regular mute. `/unmute` ends it.")
                return
        await self._defer_public(ctx)
        released = True
        if role is not None:
            try:
                released = await self._release_role(ctx.guild, user, role, audit_text(f"Mini mute lifted by {ctx.author} ({ctx.author.id})"))
            except discord.HTTPException as error:
                await ctx.fail(f"I couldn't remove the mute role: {error.text or error.status}")
                return
        await self._finish_lift(ctx.guild, ctx.guild.id, user, user.id, ctx.author, ctx.author.mention, reason)
        if released:
            await ctx.ok(f"Cleared the mute on {user.mention}.", ephemeral=False)
        else:
            await ctx.ok(f"Lifted the mini mute on {user.mention}. They're still muted, because a regular mute is running on the same role. `/unmute` ends that.", ephemeral=False)

    async def _finish_lift(self, guild: Any, guild_id: int, user: Any, user_id: int, actor: Any, actor_mention: str, reason: str | None) -> None:
        await self.db.run("DELETE FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await self.bot.timers.cancel_key("mini_mute", f"{guild_id}:{user_id}")
        embed = discord.Embed(title="Mini Mute Lifted", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Target", value=f"<@{user_id}> (`{user_id}`)", inline=True)
        embed.add_field(name="Lifted by", value=actor_mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason[:1024], inline=False)
        self.bot.logs.post(self.cfg(guild_id)["mini_mute_log_channel"], embed)
        await self._record_case(guild, "unminimute", user, actor, reason or "Lifted early")

    @app_commands.guild_only()
    async def menu_mini_mute(self, interaction: discord.Interaction, member: discord.Member) -> None:
        refusal = self._mini_mute_gate(interaction.user, member)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        async def submit(answer: discord.Interaction, reason: str) -> None:
            await answer.response.defer(ephemeral=True)
            await answer.followup.send(await self.request_mini_mute(answer.user, member, reason or None), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_modal(Form("Mini mute this member", "Reason", "Why does this member need a short mute?", required=False, submit=submit))

    @app_commands.guild_only()
    async def menu_actions(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not self.is_moderator(interaction.user) and not self.is_vc_veteran(interaction.user):
            await interaction.response.send_message("Only moderators and VC veterans can use this.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Voice actions for {member.mention}. Mute and deafen last {format_minutes(self.cfg(interaction.guild_id)['temp_minutes'])}.",
            view=ActionsPanel(self, member), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/voice/actions", self.api_actions)
        router.post(r"/voice/actions/{user_id:\d+}/release", self.api_release)
        router.get("/voice/access", self.api_access)
        router.post(r"/voice/access/{user_id:\d+}/grant", self.api_grant)
        router.post(r"/voice/access/{user_id:\d+}/revoke", self.api_revoke)
        router.get("/voice/mini-mutes", self.api_mini_mutes)
        router.post(r"/voice/mini-mutes/{user_id:\d+}/lift", self.api_lift)
        router.get("/voice/reports", self.api_reports)
        router.post("/voice/button", self.api_post_button)

    @staticmethod
    def _name_for(req: Any, user_id: int, stored: str | None = None) -> str:
        member = req.ops.member(req.guild_id, user_id)
        return member["display_name"] if member else (stored or str(user_id))

    @staticmethod
    def _page_args(req: Any) -> tuple[int, int]:
        try:
            page = min(max(1, int(req.query.get("page", "1"))), LARGEST_PAGE)
            size = int(req.query.get("size", "10"))
        except ValueError as error:
            raise ApiError(400, "Page and size must be whole numbers.") from error
        return page, min(max(size, 1), PAGE_MAX)

    async def api_actions(self, req: Any) -> list[dict]:
        rows = await self.db.all("SELECT user_id, action, expires_at, moderator_id, reason FROM voice_actions WHERE guild_id = ? ORDER BY expires_at DESC LIMIT 500", (req.guild_id,))
        for row in rows:
            row["name"] = self._name_for(req, row["user_id"])
            row["moderator_name"] = self._name_for(req, row["moderator_id"]) if row["moderator_id"] else None
        return rows

    async def api_release(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        rows = await self.db.all("SELECT action FROM voice_actions WHERE guild_id = ? AND user_id = ?", (req.guild_id, user_id))
        if not rows:
            raise ApiError(404, "That person has no temporary voice actions.")
        actions = {row["action"] for row in rows}
        changes: dict[str, Any] = {}
        if MUTE in actions:
            changes["mute"] = False
        if DEAFEN in actions:
            changes["deafen"] = False
        cleared = not changes or await req.ops.edit_member_voice(req.guild_id, user_id, reason=audit_text(f"Voice actions released by {req.actor.name} on the dashboard"), **changes)
        await self._finish_release(req.guild_id, user_id, actions, cleared)
        await req.record("voice.release", detail=str(user_id))
        return {"cleared_now": cleared}

    async def api_access(self, req: Any) -> dict:
        page, size = self._page_args(req)
        where = ["guild_id = ?"]
        args: list[Any] = [req.guild_id]
        status = req.query.get("status", "")
        if status:
            if status not in ("pending", "approved", "denied", "revoked"):
                raise ApiError(400, "That isn't an access status.")
            where.append("status = ?")
            args.append(status)
        member = req.query.get("member", "").strip()
        if member:
            user_id = whole_number(member)
            if user_id is not None:
                where.append("user_id = ?")
                args.append(user_id)
            else:
                escaped = member.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where.append("user_name LIKE ? ESCAPE '\\'")
                args.append(f"%{escaped}%")
        clause = " AND ".join(where)
        total = await self.db.scalar(f"SELECT COUNT(*) FROM voice_access WHERE {clause}", args)
        rows = await self.db.all(f"SELECT * FROM voice_access WHERE {clause} ORDER BY COALESCE(decided_at, requested_at) DESC LIMIT ? OFFSET ?", [*args, size, (page - 1) * size])
        for row in rows:
            row["name"] = self._name_for(req, row["user_id"], row["user_name"])
            row["count"] = await self._message_count(req.guild_id, row["user_id"])
        return {"rows": rows, "total": total, "page": page, "size": size}

    async def api_grant(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        role_id = self.cfg(req.guild_id)["access_role"]
        if not role_id:
            raise ApiError(400, "Choose the voice access role in the settings first.")
        member = req.ops.member(req.guild_id, user_id)
        if member is None:
            raise ApiError(404, "That person isn't in this server.")
        info = req.ops.describe_guild(req.guild_id) or {"roles": [], "me": {"top_role_position": 0}}
        role = next((r for r in info["roles"] if r["id"] == role_id), None)
        if role is None or role["default"] or role["managed"] or role["position"] >= info["me"]["top_role_position"]:
            raise ApiError(400, "The bot can't give the voice access role. It must be an ordinary role below the bot's own.")
        if not role_is_safe(role["permissions"], req.settings.get(req.guild_id, COMMANDS_MODULE)):
            raise ApiError(400, "The voice access role holds moderation permissions, so the bot won't hand it out. Pick another role, or allow elevated roles on the Commands page.")
        guild = self.bot.get_guild(req.guild_id)
        actor = guild.get_member(req.user.id) if guild else None
        refusal = self._rank_refusal(actor, guild.get_role(role_id)) if actor is not None and guild.get_role(role_id) is not None else None
        if refusal:
            raise ApiError(403, refusal)
        await req.ops.add_member_role(req.guild_id, user_id, role_id, reason=audit_text(f"VC access granted by {req.actor.name} on the dashboard"))
        await self._close_pending_request(req.guild_id, user_id, f"Granted by {req.actor.name}")
        await self._set_access(req.guild_id, user_id, member["name"], "approved", req.actor.id, req.actor.name)
        await req.record("voice.access.grant", detail=str(user_id))
        return {"ok": True}

    async def api_revoke(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        form = await req.json()
        reason = form.get("reason") if isinstance(form, dict) else None
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise ApiError(400, "A reason can be at most 500 characters.", {"reason": "A reason can be at most 500 characters."})
        role_id = self.cfg(req.guild_id)["access_role"]
        member = req.ops.member(req.guild_id, user_id)
        audit = audit_text(f"VC access revoked by {req.actor.name} on the dashboard" + (f": {reason}" if reason else ""))
        if role_id and member is not None:
            await req.ops.remove_member_role(req.guild_id, user_id, role_id, reason=audit)
        await self._close_pending_request(req.guild_id, user_id, f"Closed: access revoked by {req.actor.name}")
        stored = await self.db.scalar("SELECT user_name FROM voice_access WHERE guild_id = ? AND user_id = ?", (req.guild_id, user_id))
        await self._set_access(req.guild_id, user_id, member["name"] if member else (stored or str(user_id)), "revoked", req.actor.id, req.actor.name)
        try:
            await req.ops.edit_member_voice(req.guild_id, user_id, disconnect=True, reason=audit)
        except OpsError as error:  # the revoke itself has happened, so it still gets logged like the command's does
            log.warning("Could not disconnect %s after a revoke: %s", user_id, error)
        self._log_revoke(req.guild_id, user_id, discord.utils.escape_markdown(req.actor.name), reason)
        await req.record("voice.access.revoke", detail=str(user_id))
        return {"ok": True}

    async def api_mini_mutes(self, req: Any) -> dict:
        mutes = await self.db.all("SELECT user_id, user_name, expires_at, applied_by, applied_by_name, reason FROM mini_mutes WHERE guild_id = ? ORDER BY expires_at LIMIT 500", (req.guild_id,))
        votes = await self.db.all(
            "SELECT v.vote_message_id, v.channel_id, v.target_id, v.target_name, v.initiator_id, v.initiator_name, v.reason, v.minutes, v.required, v.expires_at, v.status, "
            "(SELECT COALESCE(SUM(approve), 0) FROM mini_mute_ballots b WHERE b.vote_message_id = v.vote_message_id AND b.guild_id = v.guild_id) AS approvals, "
            "(SELECT COALESCE(SUM(1 - approve), 0) FROM mini_mute_ballots b WHERE b.vote_message_id = v.vote_message_id AND b.guild_id = v.guild_id) AS rejections "
            "FROM mini_mute_votes v WHERE v.guild_id = ? ORDER BY v.vote_message_id DESC LIMIT 50",
            (req.guild_id,),
        )
        for row in mutes:
            row["name"] = self._name_for(req, row["user_id"], row["user_name"])
        for row in votes:
            row["name"] = self._name_for(req, row["target_id"], row["target_name"])
            row["url"] = f"https://discord.com/channels/{req.guild_id}/{row['channel_id']}/{row['vote_message_id']}"
        return {"mutes": mutes, "votes": votes}

    async def api_lift(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        row = await self.db.one("SELECT user_name, role_id FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (req.guild_id, user_id))
        if row is None:
            raise ApiError(404, "That person isn't mini muted.")
        role_id = row["role_id"] or self.cfg(req.guild_id)["mini_mute_role"]
        member = req.ops.member(req.guild_id, user_id)
        guild = self.bot.get_guild(req.guild_id)  # only the moderation module's case and a shared role need it, and the demo has none
        audit = audit_text(f"Mini mute lifted by {req.actor.name} on the dashboard")
        cached = guild.get_member(user_id) if guild else None
        role = guild.get_role(role_id) if guild and role_id else None
        released = True
        if cached is not None and role is not None:
            try:
                released = await self._release_role(guild, cached, role, audit)
            except discord.HTTPException as error:
                raise OpsError(f"Discord refused: {error.text or error.status}") from error
        elif role_id and member is not None:
            await req.ops.remove_member_role(req.guild_id, user_id, role_id, reason=audit)
        user = (guild.get_member(user_id) if guild else None) or Person(user_id, row["user_name"])
        await self._finish_lift(guild, req.guild_id, user, user_id, Person(req.actor.id, req.actor.name), discord.utils.escape_markdown(req.actor.name), None)
        await req.record("voice.minimute.lift", detail=str(user_id))
        return {"ok": True, "released": released}

    async def api_reports(self, req: Any) -> dict:
        """VC reports, newest first: those filed as reports, and the older ones from before 1.2.3 with their own buttons."""
        page, size = self._page_args(req)
        older = (
            "SELECT channel_id AS post_channel_id, report_message_id AS post_message_id, reporter_id, reporter_name, target_id, target_name, voice_channel_id, reason, created_at, "
            "status, NULL AS number FROM voice_reports WHERE guild_id = ?"
        )
        listed, args = older, [req.guild_id]
        if self.bot.module("reports") is not None:  # its table, which a copy without it doesn't have
            listed = (
                "SELECT post_channel_id, post_message_id, reporter_id, reporter_name, target_id, target_name, channel_id AS voice_channel_id, reason, created_at, state AS status, number "
                f"FROM reports WHERE guild_id = ? AND kind = 'voice' UNION ALL {older}"
            )
            args.insert(0, req.guild_id)
        total = await self.db.scalar(f"SELECT COUNT(*) FROM ({listed})", args)
        rows = await self.db.all(f"SELECT * FROM ({listed}) ORDER BY created_at DESC LIMIT ? OFFSET ?", [*args, size, (page - 1) * size])
        for row in rows:
            row["url"] = f"https://discord.com/channels/{req.guild_id}/{row['post_channel_id']}/{row['post_message_id']}" if row["post_channel_id"] and row["post_message_id"] else None
        return {"rows": rows, "total": total, "page": page, "size": size}

    async def api_post_button(self, req: Any) -> dict:
        form = await req.json()
        channel_id = req.channel(form.get("channel_id") if isinstance(form, dict) else None, posting=True)
        sent = await req.ops.send_message(channel_id, {"content": "", "embeds": [self._button_embed(req.guild_id)], "buttons": []}, silent=True, components=self._button_controls())
        await req.record("voice.button", detail=str(channel_id))
        return sent

    async def seed_demo(self, guild_id: int) -> None:
        """Sample rows for the demo dashboard, so the tables can be seen filled in."""
        ops = self.bot.ops
        admin, member = ops.members[0], ops.members[1]
        channels = {c["name"]: c["id"] for c in ops.describe_guild(guild_id)["channels"]}
        now = time.time()
        await self.db.run(
            "INSERT INTO voice_actions (guild_id, user_id, action, expires_at, moderator_id, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, member["id"], MUTE, now + 420, admin["id"], "Shouting over everyone"),
        )
        await self.db.many(
            "INSERT INTO voice_access (guild_id, user_id, user_name, status, requested_at, decided_by, decided_by_name, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (guild_id, member["id"], member["name"], "pending", now - 3000, None, None, None),
                (guild_id, member["id"] + 7000, "former_member", "revoked", now - 900000, admin["id"], admin["name"], now - 40000),
                (guild_id, admin["id"], admin["name"], "approved", now - 2000000, admin["id"], admin["name"], now - 1900000),
            ],
        )
        await self.db.run(
            "INSERT INTO mini_mutes (guild_id, user_id, user_name, expires_at, applied_by, applied_by_name, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, member["id"], member["name"], now + 300, admin["id"], admin["name"], "Spamming the same joke"),
        )
        await self.db.run(
            "INSERT INTO mini_mute_votes (vote_message_id, guild_id, channel_id, target_id, target_name, initiator_id, initiator_name, reason, minutes, required, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (900000000000500002, guild_id, channels["mod-chat"], member["id"], member["name"], admin["id"], admin["name"], "Baiting arguments", 10, 3, now + 1200),
        )
        await self.db.run("INSERT INTO mini_mute_ballots (vote_message_id, guild_id, voter_id, approve) VALUES (?, ?, ?, 1)", (900000000000500002, guild_id, admin["id"]))
