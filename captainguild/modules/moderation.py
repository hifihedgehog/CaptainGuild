"""Punishment commands, cases, notes, warnings, lockdown and purge.

Every action on a member becomes a numbered case for that server, whether it came from a command here, from automod, or from a
person or another bot using Discord's own menus. Other features call punish, record_case and history.
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import difflib
import io
import json
import logging
import re
import shlex
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from ..core import access, messages
from ..core.context import unused_words
from ..core.durations import humanize, parse_duration, split_leading_duration
from ..core.members import _looks_whole, find_member, whole_member_list
from ..core.module import Module, requires
from ..core.ops import OpsError
from ..core.people import OneMember, OnePerson
from ..core.perms import DANGEROUS_PERMISSIONS
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.shortcuts import SHORTCUTS, TAG_FIRST_SHORTCUTS
from ..core.tagscript import Scope, render_message
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, choice, integer, optional, snowflake, text, whole_number
from ..web.api import ApiError, safe_json

log = logging.getLogger(__name__)

DAY = 86400
MAX_SECONDS = 5 * 365 * DAY
TIMEOUT_MAX = 28 * DAY
REASON_MAX = 1024
NOTE_MAX = 1800
AUDIT_REASON_MAX = 512
PURGE_MAX = 2000
SQLITE_INT_MAX = 2 ** 63 - 1
EMBEDS_PER_POST = 10
EXPORT_ROWS = 200_000
# The gateway can deliver an audit log entry just after the event it explains.
AUDIT_WAIT = 2.0
EXPECT_SECONDS = 30.0
# What a call to Discord raises when the answer is a refusal, or when no answer came at all.
SEND_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError)
SOFTBAN_RETRY_SECONDS = 60
# An audit entry this much later than its case still explains it.
LATE_AUDIT_SECONDS = 60
EARLIER_REASONS_KEPT = 20
REPOST_MOST = 200
EXPORT_CHUNK = 1000
# A slash command's token dies after 15 minutes, and a purge of old messages deletes about one a second.
INTERACTION_SAFE_SECONDS = 14 * 60
RUNNING_LINES_MAX = 3800
# Discord's bulk ban takes 200 at a time. A pause between calls keeps a big run from tripping its anti-abuse limits.
BULK_BAN_CHUNK = 200
BULK_BAN_PAUSE = 1.0
MASSBAN_MOST = 5000
MASSBAN_LISTED = 20
MASSBAN_SEARCH_DEFAULT = 100
MASSBAN_SEARCH_MAX = 2000
MESSAGE_DAYS_MOST = 7  # the most days of a banned person's messages Discord deletes
MASSBAN_PREVIEW_SECONDS = 180
FILTER_YIELD_EVERY = 2000
RELEASE_INLINE = 25  # letting this many go at Discord's pace still fits the time a page waits for an answer
POSTS_INLINE = 50
# A word this short is near too many channel names to mean any of them, as `for` is near forum.
NEAR_NAME_SHORTEST = 4
# This many joins this fast is a raid, or something staff want to hear about either way. A 43,000-member server sees a few a minute.
JOIN_ALERT_COUNT = 15
JOIN_ALERT_SECONDS = 30.0
JOIN_ALERT_GAP = 600.0

ACTIONS = ("ban", "unban", "tempban", "softban", "kick", "mute", "unmute", "hardmute", "timeout", "untimeout", "warn", "minimute", "unminimute")
TIMED_ACTIONS = ("tempban", "mute", "hardmute", "timeout", "minimute")
# A reversal a timer does repeats the case of a moderator who didn't act again, so it doesn't count for them.
AUTOMATIC_SOURCE = "timer"
# Warnings handed out by the warn threshold itself don't check the threshold again.
THRESHOLD_SOURCE = "threshold"
SOURCE_LABELS = {"automod": "Automod", THRESHOLD_SOURCE: "Automod"}

COLORS = {
    "ban": 0xDD5F53, "tempban": 0xDD5F53, "softban": 0xDD5F53, "unban": 0x53DDA4,
    "mute": 0xF48942, "hardmute": 0xF48942, "minimute": 0xF48942, "timeout": 0xF48942,
    "unmute": 0x42F4A7, "unminimute": 0x42F4A7, "untimeout": 0x42F4A7,
    "kick": 0x4C88FF, "warn": 0xFFFA77,
}
VOIDED_COLOR = 0x6E7881

# Which box on the logged-events list covers each action.
LOGGED_AS = {
    "ban": "ban", "softban": "ban", "tempban": "tempban", "unban": "unban", "mute": "mute", "minimute": "mute", "hardmute": "hardmute",
    "unmute": "unmute", "unminimute": "unmute", "kick": "kick", "warn": "warn", "timeout": "timeout", "untimeout": "untimeout",
}
COUNT_AS = {"warn": "warn", "mute": "mute", "hardmute": "mute", "tempban": "tempban", "ban": "ban", "kick": "kick", "softban": "kick", "timeout": "timeout"}

PERMISSION_NEEDED = {
    "ban": ("ban_members", "Ban Members"), "tempban": ("ban_members", "Ban Members"), "softban": ("ban_members", "Ban Members"), "unban": ("ban_members", "Ban Members"),
    "kick": ("kick_members", "Kick Members"), "mute": ("manage_roles", "Manage Roles"), "hardmute": ("manage_roles", "Manage Roles"), "unmute": ("manage_roles", "Manage Roles"),
    "timeout": ("moderate_members", "Timeout Members"), "untimeout": ("moderate_members", "Timeout Members"),
}
DONE = {
    "ban": "Banned", "tempban": "Banned", "softban": "Softbanned", "unban": "Unbanned", "kick": "Kicked", "mute": "Muted", "hardmute": "Hardmuted",
    "unmute": "Unmuted", "timeout": "Timed out", "untimeout": "Removed the timeout on", "warn": "Warned",
}
DM_PHRASE = {
    "ban": "banned from", "tempban": "banned from", "softban": "softbanned from", "kick": "kicked from", "mute": "muted in",
    "hardmute": "muted in", "timeout": "timed out in", "warn": "warned in",
}
DM_NONE, DM_ACTION, DM_REASON, DM_FULL, DM_CUSTOM = 0, 1, 2, 3, 4

# What a mute takes away in every channel. Slash commands go too, since many of them post in the channel as the bot.
MUTE_DENIED = (
    "send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads", "add_reactions",
    "use_application_commands", "speak", "stream",
)
LOCK_FIELDS = ("send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads", "add_reactions", "use_application_commands", "connect")
VOICE_LOCK_FIELDS = ("connect",)
BOT_LOCK_MARK = "__bot_send_messages"
# In a lockdown's stored `previous`: the allows it took off member roles, and the allows it gave staff roles with what they held before.
ROLE_ALLOWS_MARK = "__roles"
STAFF_ALLOWS_MARK = "__staff"
# A role this share of the server holds is the public, not a private group. A member role sits far above it, and staff and bot roles far below.
PUBLIC_ROLE_SHARE = 0.05
SKIPPED_LISTED = 30
BULK_BAN_NOBODY = 500000
LOCKABLE = discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.ForumChannel | None

_ID_WORD = re.compile(r"<@!?(\d{15,21})>|(\d{15,21})")
_CHANNEL_WORD = re.compile(r"<#\d{15,21}>|\d{15,21}")
_LENGTH_START = re.compile(r"\.?\d")
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
_LINK = re.compile(r"https?://\S+", re.IGNORECASE)
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})
_FILTER_WORD = re.compile(r"([a-z-]+):(.*)", re.DOTALL)
FILTER_NAMES = ("joined", "created", "name", "contains", "channel", "search", "after", "noavatar", "noroles", "raid", "reason")
FILTER_HELP = "Filters: joined:10m, created:1d, name:free*nitro, contains:text, channel:#general, search:500, after:@member, noavatar, noroles, and raid for whoever raid mode holds. Follow them with the reason."
_CSV_RISKY = ("=", "+", "-", "@", "\t", "\r")


NOT_SEEN = "The bot can't see you in that server right now. Try again in a moment."
OWN_RECORD = "You can't change a record about yourself. Ask another moderator."


class ModerationError(Exception):
    """Something the person asking can act on: hierarchy, a missing permission, nothing to undo. The text is shown to them."""


class Someone:
    """Enough of a user for a case, when only an ID and maybe a stored name are left."""

    def __init__(self, user_id: int, name: str | None = None, *, bot: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.bot = bot

    def __str__(self) -> str:
        return self.name or f"ID {self.id}"


def name_of(user: Any) -> str:
    """How a person or bot reads in a case. Older bots still have a four-digit tag, which is how staff tell them apart."""
    if user is None:
        return "Unknown"
    name = getattr(user, "name", None)
    if not name:
        return f"ID {user.id}"
    tag = getattr(user, "discriminator", "0")
    return name if tag in ("0", None) else f"{name}#{tag}"


def clip(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= maximum else cleaned[:maximum - 1] + "\N{HORIZONTAL ELLIPSIS}"


def audit_reason(moderator: Any, reason: str | None, action: str) -> str:
    """What shows in Discord's audit log. Its limit is 512 characters, and moderators like to see who ran the bot."""
    why = reason or f"{action}, no reason given"
    if moderator is None:
        return why[:AUDIT_REASON_MAX]
    return f"{name_of(moderator)} (ID: {moderator.id}): {why}"[:AUDIT_REASON_MAX]


def escape(value: str | None) -> str:
    return discord.utils.escape_markdown(value or "")


def leading_ids(words: str) -> tuple[list[int], str]:
    """The IDs and mentions at the front of a sentence, and whatever follows them."""
    ids: list[int] = []
    parts = words.split()
    used = 0
    for part in parts:
        match = _ID_WORD.fullmatch(part.strip(","))
        if match is None:
            break
        ids.append(int(match.group(1) or match.group(2)))
        used += 1
    unique = list(dict.fromkeys(ids))
    return unique, " ".join(parts[used:])


def split_quoted(words: str) -> list[str]:
    """Words, with quotes around anything that has spaces. A backslash stays as typed, since a pattern needs it. Unbalanced quotes split on spaces alone."""
    lexer = shlex.shlex(words, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    try:
        return list(lexer)
    except ValueError:
        return words.split()


def parse_ban_filters(words: str) -> tuple[dict[str, str], str | None]:
    """The `joined:10m noroles` words at the front of a mass ban, and the reason after them. Raises ModerationError on a word that looks like a filter and isn't one, because dropping a filter would ban more people."""
    parts = split_quoted(words)
    filters: dict[str, str] = {}
    used = 0
    for part in parts:
        found = _FILTER_WORD.fullmatch(part)
        name, value = (found.group(1).lower(), found.group(2)) if found else (part.lower(), "")
        # `raid` has to come first or take a colon, because it's also a word a reason starts with.
        if not found and not (name in ("noavatar", "noroles") or (name == "raid" and not used)):
            break
        if found and name not in FILTER_NAMES:
            raise ModerationError(f"`{name}:` isn't a filter. {FILTER_HELP}")
        if name == "reason":
            used += 1
            return filters, " ".join([value, *parts[used:]]).strip() or None
        filters[name] = value
        used += 1
    return filters, " ".join(parts[used:]) or None


def name_fits(pattern: str, *names: Any) -> bool:
    """Whether a name holds this text, where `*` stands for anything and case doesn't matter. Each piece is a plain search, so no pattern can run long on a big server."""
    pieces = [piece for piece in pattern.casefold().split("*") if piece]
    for name in names:
        if not name:
            continue
        folded, position = str(name).casefold(), 0
        for piece in pieces:
            position = folded.find(piece, position)
            if position < 0:
                break
            position += len(piece)
        else:
            return True
    return False


async def send_private(ctx: Any, content: str | None = None, **kwargs: Any) -> None:
    """A record for staff. A slash reply only the person asking sees, and a typed command answers in the channel, so where everyone can read the channel it goes to their DMs."""
    if ctx.interaction is not None or not _everyone_reads(ctx.channel):
        await ctx.send(content, ephemeral=True, **kwargs)
        return
    try:
        await ctx.author.send(content, **kwargs)
    except SEND_ERRORS:
        await ctx.fail("Everyone can read this channel and I couldn't DM you, so that stayed private. Use the slash command or a staff channel.")
        return
    await ctx.ok("Everyone can read this channel, so I sent that to your DMs.")


def _everyone_reads(channel: Any) -> bool:
    try:
        return bool(channel.permissions_for(channel.guild.default_role).view_channel)
    except discord.ClientException:
        return True


def meant_a_channel(word: str | None, rest: str | None) -> bool:
    """Whether a word that didn't turn into a channel was meant as one: a channel mention or ID, or anything but a length with a length right after it, as in `gneral 2h`. `for 2h` is a length."""
    if not word or parse_duration(word) is not None or word.lower() == "for":
        return False
    return bool(_CHANNEL_WORD.fullmatch(word)) or split_leading_duration(rest or "")[0] is not None


def csv_cell(value: Any) -> Any:
    """Spreadsheets run a cell that starts with these as a formula, and a reason can come from another bot or a member's name."""
    if isinstance(value, str) and value.startswith(_CSV_RISKY):
        return "'" + value
    return value


def _action_settings(mode: int, *, seconds: tuple[int, int, int] | None = None, days: int | None = None) -> Schema:
    """seconds is (default, minimum, maximum). A default of 0 on a mute means it lasts until someone lifts it."""
    fields: dict[str, Any] = {
        "mode": Field(mode, choice(DM_NONE, DM_ACTION, DM_REASON, DM_FULL, DM_CUSTOM)),
        "message": Field(messages.empty_payload(), messages.clean_payload),
    }
    if seconds is not None:
        fields["seconds"] = Field(seconds[0], integer(seconds[1], seconds[2]))
    if days is not None:
        fields["days"] = Field(days, integer(0, 7))
    return Schema(fields)


SETTINGS = Schema({
    "modlog_channel": Field(None, optional(snowflake)),
    "mute_role": Field(None, optional(snowflake)),
    # Lets numbering carry on from an earlier modlog instead of starting again at 1.
    "case_start": Field(1, integer(1, 1_000_000_000)),
    "logged": Schema({name: Field(True, boolean) for name in ("ban", "mute", "warn", "tempban", "hardmute", "kick", "unmute", "unban", "timeout", "untimeout")}),
    "timeout": _action_settings(DM_REASON, seconds=(3600, 60, TIMEOUT_MAX)),
    "mute": _action_settings(DM_REASON, seconds=(0, 0, MAX_SECONDS)),
    "kick": _action_settings(DM_NONE),
    "ban": _action_settings(DM_NONE, days=2),
    "warn": _action_settings(DM_FULL),
    "hardmute": _action_settings(DM_REASON, seconds=(0, 0, MAX_SECONDS)),
    "tempban": _action_settings(DM_NONE, seconds=(DAY, 60, MAX_SECONDS), days=2),
    "softban": _action_settings(DM_NONE, days=1),
})


# The order of the logged-events boxes in the old setup's bitmask.
EXPORT_EVENT_ORDER = ("ban", "mute", "warn", "tempban", "hardmute", "kick", "unmute", "unban", "timeout", "untimeout")


def _known_id(value: Any, known: set[int]) -> int | None:
    """An ID from a pasted export, only if it belongs to this server."""
    try:
        found = snowflake(value)
    except Invalid:
        return None
    return found if found in known else None


def settings_from_export(data: dict, channel_ids: set[int], role_ids: set[int]) -> dict:
    """A settings form from the old setup's moderation export. A channel or role that isn't in this server is left out."""
    form: dict[str, Any] = {}
    modlogs = data.get("modlogs") if isinstance(data.get("modlogs"), dict) else {}
    channel = _known_id(modlogs.get("channel_id"), channel_ids)
    if channel:
        form["modlog_channel"] = channel
    mask = modlogs.get("logging_bitmask")
    if isinstance(mask, int) and not isinstance(mask, bool):
        form["logged"] = {name: bool(mask & 1 << index) for index, name in enumerate(EXPORT_EVENT_ORDER)}
    role = _known_id(data.get("muterole"), role_ids)
    if role:
        form["mute_role"] = role
    overrides = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    for action, row in overrides.items():
        section = SETTINGS.fields.get(action)
        if not isinstance(section, Schema) or not isinstance(row, dict):
            continue
        part: dict[str, Any] = {}
        if row.get("mode") is not None:
            part["mode"] = row["mode"]
        try:
            part["message"] = messages.from_legacy(row.get("message"), row.get("embed"))
        except Invalid:
            pass
        if "seconds" in section.fields and row.get("default_duration") is not None:
            part["seconds"] = row["default_duration"]
        if "days" in section.fields and row.get("default_time") is not None:
            part["days"] = row["default_time"]
        form[action] = {key: value for key, value in part.items() if _fits(section.fields[key], value)}
    return form


def _fits(field: Field, value: Any) -> bool:
    try:
        field.check(value)
    except Invalid:
        return False
    return True


def _purge_links(message: Any) -> bool:
    return bool(_LINK.search(message.content or ""))


def _purge_emoji(message: Any) -> bool:
    return bool(_CUSTOM_EMOJI.search(message.content or ""))


def _purge_mentions(message: Any) -> bool:
    return bool(message.mentions or message.role_mentions or message.mention_everyone)


def _purge_images(message: Any) -> bool:
    return bool(message.attachments or message.embeds)


class MassBanPreview(discord.ui.View):
    """The buttons under a mass ban's preview. The list stays what the preview showed, so a press can't ban someone who wasn't on it. Each person is checked again when the ban runs, since a role can change in the minutes between."""

    def __init__(self, cog: "Moderation", author: Any, matched: list[Any], reason: str | None, days: int | None = None) -> None:
        super().__init__(timeout=MASSBAN_PREVIEW_SECONDS)
        self.cog = cog
        self.author = author
        self.matched = matched
        self.reason = reason
        self.days = days
        self.message: Any = None

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content="That preview ran out, and nobody was banned.", view=None)
            except SEND_ERRORS as error:
                log.info("Could not close a mass ban preview: %s", error)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
        return False

    @discord.ui.button(label="Ban them", style=discord.ButtonStyle.danger)
    async def ban(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content=f"Banning {len(self.matched)}. This can take a minute.", view=None)
        await interaction.followup.send(await self.cog.run_massban(interaction.guild, self.author, self.matched, self.reason, self.days), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Canceled. Nobody was banned.", view=None)


class Moderation(Module):
    key = "moderation"
    category = "Moderation"
    schema = SETTINGS
    migrations = [
        """
        CREATE TABLE cases (
            guild_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            action TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            moderator_id INTEGER,
            moderator_name TEXT,
            moderator_bot INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            seconds INTEGER,
            expires_at REAL,
            source TEXT NOT NULL,
            extra TEXT,
            created_at REAL NOT NULL,
            channel_id INTEGER,
            message_id INTEGER,
            voided_at REAL,
            voided_by INTEGER,
            voided_by_name TEXT,
            void_reason TEXT,
            PRIMARY KEY (guild_id, number)
        );
        CREATE INDEX cases_user ON cases (guild_id, user_id, number);
        CREATE INDEX cases_moderator ON cases (guild_id, moderator_id, number);
        CREATE INDEX cases_action ON cases (guild_id, action, number);
        CREATE INDEX cases_message ON cases (guild_id, message_id);
        CREATE TABLE notes (
            guild_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            author_id INTEGER,
            author_name TEXT,
            text TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, number)
        );
        CREATE INDEX notes_user ON notes (guild_id, user_id, number);
        CREATE TABLE active_mutes (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            role_id INTEGER,
            moderator_id INTEGER,
            moderator_name TEXT,
            removed_roles TEXT NOT NULL DEFAULT '[]',
            started_at REAL NOT NULL,
            expires_at REAL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE lockdowns (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            previous TEXT NOT NULL,
            moderator_id INTEGER,
            moderator_name TEXT,
            reason TEXT,
            created_at REAL NOT NULL,
            expires_at REAL,
            PRIMARY KEY (guild_id, channel_id)
        );
        """,
        # When the bot last saw the mute role on this person. Someone without it who joined before then never left, so staff took it off.
        "ALTER TABLE active_mutes ADD COLUMN confirmed_at REAL;",
        """
        CREATE TABLE raid_modes (
            guild_id INTEGER PRIMARY KEY,
            moderator_id INTEGER,
            moderator_name TEXT,
            reason TEXT,
            started_at REAL NOT NULL,
            expires_at REAL
        );
        CREATE TABLE raid_holds (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            role_id INTEGER NOT NULL,
            held_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """,
    ]
    role_timers = ("moderation.unmute", "moderation.unban")

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._expected: dict[tuple[int, int, str], float] = {}
        self._reconciling = False
        self._seen_kicks: dict[tuple[int, int], float] = {}
        self._mute_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._banned_at: dict[tuple[int, int], float] = {}
        self._used_mute_roles: dict[int, set[int]] = {}
        self._told_role_moved: dict[int, int] = {}
        self._joins: dict[int, deque[float]] = {}
        self._join_alerted: dict[int, float] = {}
        self._background: set[asyncio.Task] = set()

    def _in_background(self, work: Any) -> asyncio.Task:
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _mute_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        """One member's mute, one change at a time. A command, a timer and a rejoin can land together, and each reads the row, calls Discord, then writes the row. Bans take turns on it too."""
        key = (guild_id, user_id)
        lock = self._mute_locks.get(key)
        if lock is None:
            if len(self._mute_locks) > 2000:
                self._mute_locks = {k: held for k, held in self._mute_locks.items() if held.locked()}
            lock = self._mute_locks[key] = asyncio.Lock()
        return lock

    async def on_load(self) -> None:
        self.bot.timers.handler("moderation.unmute", self._mute_due)
        self.bot.timers.handler("moderation.unban", self._tempban_due)
        self.bot.timers.handler("moderation.unlock", self._lockdown_due)
        self.bot.timers.handler("moderation.raid_end", self._raid_due)
        for row in await self.db.all("SELECT DISTINCT guild_id, role_id FROM active_mutes WHERE role_id IS NOT NULL"):
            self._used_mute_roles.setdefault(row["guild_id"], set()).add(row["role_id"])

    def transient_roles(self, guild_id: int) -> set[int]:
        """The mute role, and any earlier one a running mute still uses. A hardmute leaves someone holding only that role, and the roles it took wait in its row."""
        role_id = self.cfg(guild_id)["mute_role"]
        return {*self._used_mute_roles.get(guild_id, set()), *([role_id] if role_id else [])}

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        return {"mute_role": {settings["mute_role"]} if settings.get("mute_role") else set()}

    def power_roles(self, settings: dict) -> dict[str, set[int]]:
        return {"mute_role": {settings["mute_role"]} if settings.get("mute_role") else set()}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {"modlog_channel": {settings["modlog_channel"]} if settings.get("modlog_channel") else set()}

    async def on_guild_restored(self, guild_id: int) -> None:
        rows = await self.db.all("SELECT DISTINCT role_id FROM active_mutes WHERE guild_id = ? AND role_id IS NOT NULL", (guild_id,))
        self._used_mute_roles[guild_id] = {row["role_id"] for row in rows}

    async def after_restore(self, guild_id: int) -> None:
        """Past-due timers stay out of a restore, which leaves mutes and temporary bans past their end with nothing left to lift them."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return  # a file restore runs before the gateway, and the first READY does this check
        await self.reconcile(guild)
        await self._report_dropped_tempbans(guild)

    async def _report_dropped_tempbans(self, guild: Any) -> None:
        rows = await self.db.all(
            "SELECT user_id, user_name FROM cases AS c WHERE guild_id = ? AND action = 'tempban' AND voided_at IS NULL AND expires_at <= ? AND number = ("
            "SELECT MAX(number) FROM cases AS d WHERE d.guild_id = c.guild_id AND d.user_id = c.user_id AND d.action IN ('ban', 'tempban', 'softban', 'unban') AND d.voided_at IS NULL)",
            (guild.id, time.time()),
        )
        waiting = {timer["payload"].get("user_id") for timer in await self.bot.timers.list_kind("moderation.unban", guild.id)}
        names = [f"{row['user_name']} ({row['user_id']})" for row in rows if row["user_id"] not in waiting]
        if names:
            await self._tell_staff(guild, "Temporary bans past their end", f"A restore left these temporary bans past their end with nothing to lift them. Anyone here who is still banned stays banned until someone runs `/unban`: {', '.join(names[:40])}.")

    # ---- cases: what other features call ----

    async def record_case(self, guild: Any, action: str, user: Any, moderator: Any, *, reason: str | None = None, seconds: int | None = None,
                          source: str = "command", extra: dict | None = None, post: bool = True) -> dict:
        """Store a case with the server's next number and post it to the modlog when that event is on. Returns the row."""
        if action not in ACTIONS:
            raise ValueError(f"Not a case action: {action}")
        now = time.time()
        reason = clip(reason, REASON_MAX)
        seconds = int(seconds) if seconds else None
        expires_at = now + seconds if seconds and action in TIMED_ACTIONS else None
        moderator_id = moderator.id if moderator is not None else None
        moderator_name = name_of(moderator) if moderator is not None else None
        moderator_bot = 1 if getattr(moderator, "bot", False) else 0
        user_name = name_of(user)
        async with self.db.transaction():
            highest = await self.db.scalar("SELECT MAX(number) FROM cases WHERE guild_id = ?", (guild.id,))
            number = max((highest or 0) + 1, self.cfg(guild.id)["case_start"])
            await self.db.run(
                "INSERT INTO cases (guild_id, number, action, user_id, user_name, moderator_id, moderator_name, moderator_bot, reason, seconds, expires_at, source, extra, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild.id, number, action, user.id, user_name, moderator_id, moderator_name, moderator_bot, reason, seconds, expires_at, source,
                 json.dumps(extra) if extra else None, now),
            )
        row = {
            "guild_id": guild.id, "number": number, "action": action, "user_id": user.id, "user_name": user_name, "moderator_id": moderator_id,
            "moderator_name": moderator_name, "moderator_bot": moderator_bot, "reason": reason, "seconds": seconds, "expires_at": expires_at, "source": source,
            "extra": extra or {}, "created_at": now, "channel_id": None, "message_id": None, "voided_at": None, "voided_by": None, "voided_by_name": None,
            "void_reason": None, "voided": False,
        }
        if post and not await self._post_cases(guild, [row]):
            row["modlog_failed"] = True
        if action == "warn" and source != THRESHOLD_SOURCE:
            automod = self.bot.module("automod")
            if automod is not None:
                await automod.warning_added(guild, user)
        return row

    async def history(self, guild_id: int, user_id: int) -> dict:
        """A member's standing record. Voided cases are left out, since they no longer count against anyone."""
        rows = await self.db.all("SELECT * FROM cases WHERE guild_id = ? AND user_id = ? AND voided_at IS NULL ORDER BY number DESC", (guild_id, user_id))
        counts = {"warn": 0, "mute": 0, "tempban": 0, "ban": 0, "kick": 0, "timeout": 0}
        for row in rows:
            counted = COUNT_AS.get(row["action"])
            if counted:
                counts[counted] += 1
        return {"cases": [self._decode(row) for row in rows], "notes": await self._notes_of(guild_id, user_id), "counts": counts}

    async def punish(self, guild: Any, user: Any, action: str, *, moderator: Any, reason: str | None = None, seconds: int | None = None, purge_days: int = 0,
                     dm: bool = True, source: str = "command") -> dict:
        """Do the Discord action, send the DM per settings, record the case and schedule the reversal. Raises ModerationError.

        seconds of None means the server's default for that action. A mute with 0 seconds lasts until someone lifts it.
        """
        if action not in PERMISSION_NEEDED and action != "warn":
            raise ModerationError(f"`{action}` isn't something this command can do.")
        target = guild.get_member(user.id) or user
        if not hasattr(target, "top_role"):
            target = (await self._members_by_id(guild, [target.id])).get(target.id, target)
        self.check_target(guild, target, moderator, action)
        await self._check_hardmuted(guild, target, moderator)
        member = target if hasattr(target, "top_role") else None
        seconds = self._duration_for(guild, action, seconds)
        reason = clip(reason, REASON_MAX)
        audit = audit_reason(moderator, reason, action)
        extra: dict[str, Any] = {}
        sent: bool | None = None
        recorded = action
        dm_sent: list[Any] = []

        if action in ("ban", "tempban", "softban"):
            # One at a time, so two moderators banning at once can't both pass the check and make two cases.
            async with self._mute_lock(guild.id, target.id):
                await self._check_not_banned(guild, target, member, action)
                # A DM has to go first here, because afterward Discord refuses it: they no longer share a server.
                if dm and member is not None:
                    sent = await self._send_dm(guild, member, action, moderator, reason, seconds, keep=dm_sent)
                try:
                    extra.update(await self._do_ban(guild, target, action, audit, purge_days, seconds, moderator))
                except Exception:
                    await self.take_back(dm_sent)
                    raise
            if extra.get("softban_unban_failed"):
                recorded = "ban"  # that's what stands until the retry lands
        elif action == "kick":
            self._require_member(member, target)
            if dm:
                sent = await self._send_dm(guild, member, action, moderator, reason, None, keep=dm_sent)
            try:
                async with self._acting(guild, member, "kick"):
                    await member.kick(reason=audit)
            except Exception:
                await self.take_back(dm_sent)
                raise
        elif action == "unban":
            await self._do_unban(guild, target, audit)
        elif action == "unmute" and member is None:
            await self._unmute_someone_gone(guild, target)
        elif action == "warn":
            self._require_member(member, target)
        else:
            self._require_member(member, target)
            if action in ("mute", "hardmute"):
                extra.update(await self._do_mute(guild, member, action, audit, seconds, moderator))
            elif action == "unmute":
                await self._do_unmute(guild, member, audit)
            elif action == "timeout":
                await self._do_timeout(guild, member, seconds, audit)
            elif action == "untimeout":
                await self._do_untimeout(guild, member, audit)
        if extra.get("longer_mute_stands"):
            reason, seconds, dm = clip(f"{reason or action}. A longer mute was already running, so that one stands.", REASON_MAX), None, False

        if dm and action in ("mute", "hardmute", "timeout", "warn"):
            sent = await self._send_dm(guild, member, action, moderator, reason, seconds)
        if sent is not None:
            extra["dm"] = sent
        row = await self.record_case(guild, recorded, target, moderator, reason=reason, seconds=seconds, source=source, extra=extra or None)
        row["dm"] = sent
        return row

    @staticmethod
    async def take_back(sent: list[Any]) -> None:
        """The action a DM announced didn't happen, so the DM goes too."""
        for message in sent:
            try:
                await message.delete()
            except SEND_ERRORS:
                pass

    def check_target(self, guild: Any, target: Any, moderator: Any, action: str) -> None:
        """Refuse what the role hierarchy or Discord wouldn't allow, before anything has happened."""
        me = guild.me
        if target.id == guild.owner_id:
            raise ModerationError("Nobody can act on the server owner.")
        if target.id == me.id:
            raise ModerationError("I can't do that to myself.")
        if moderator is not None and target.id == moderator.id:
            raise ModerationError("You can't do that to yourself.")
        needed = PERMISSION_NEEDED.get(action)
        if needed and not getattr(me.guild_permissions, needed[0]):
            raise ModerationError(f"I need the {needed[1]} permission to do that.")
        top_role = getattr(target, "top_role", None)
        if top_role is None:
            return
        name = escape(name_of(target))
        mod_top = getattr(moderator, "top_role", None)
        if mod_top is not None and moderator.id != guild.owner_id and top_role >= mod_top:
            raise ModerationError(f"{name}'s top role is at or above yours, so you can't act on them.")
        if needed and top_role >= me.top_role:
            raise ModerationError(f"{name}'s top role is at or above mine, so I can't act on them.")

    async def _check_hardmuted(self, guild: Any, target: Any, moderator: Any) -> None:
        """A hardmute leaves someone with only the mute role, which says nothing about who they are. The roles it took still count, and lifting it would hand them back."""
        mod_top = getattr(moderator, "top_role", None)
        if mod_top is None or moderator.id == guild.owner_id:
            return
        row = await self._active_mute(guild.id, target.id)
        taken = [guild.get_role(role_id) for role_id in json.loads(row["removed_roles"])] if row else []
        if any(role is not None and role >= mod_top for role in taken):
            raise ModerationError(f"{escape(name_of(target))} held a role at or above yours before the hardmute, so you can't act on them.")

    async def _members_by_id(self, guild: Any, ids: list[int]) -> dict[int, Any]:
        """Which of these people are in the server. Right after a restart the member list can still be arriving, and someone missing from it has no roles to check, so Discord gets asked."""
        found = {user_id: member for user_id in ids if (member := guild.get_member(user_id)) is not None}
        missing = [user_id for user_id in ids if user_id not in found]
        if not missing or getattr(guild, "chunked", True):
            return found
        try:
            for start in range(0, len(missing), 100):
                for member in await guild.query_members(user_ids=missing[start:start + 100], limit=100):
                    found[member.id] = member
        except TimeoutError as error:
            raise ModerationError("Discord didn't say whether they're in the server, so nothing changed. Try again in a moment.") from error
        return found

    def _require_member(self, member: Any, target: Any) -> None:
        if member is None:
            raise ModerationError(f"{escape(name_of(target))} isn't in this server.")

    def _duration_for(self, guild: Any, action: str, seconds: int | None) -> int | None:
        if action not in ("tempban", "mute", "hardmute", "timeout"):
            return None
        if seconds is None:
            seconds = self.cfg(guild.id)[action]["seconds"]
        if seconds < 0 or seconds > MAX_SECONDS:
            raise ModerationError("That length of time is out of range. The most is five years.")
        if action == "timeout" and not 0 < seconds <= TIMEOUT_MAX:
            raise ModerationError("A timeout lasts from one second up to Discord's limit of 28 days.")
        if action == "tempban" and seconds <= 0:
            raise ModerationError("A temporary ban needs a length of time, and this server's default is empty.")
        return seconds

    @asynccontextmanager
    async def _acting(self, guild: Any, user: Any, *events: str) -> AsyncIterator[None]:
        """Marks these events as the bot's own so the listeners below don't record them a second time, and turns refusals into words."""
        self._expect(guild.id, user.id, *events)
        try:
            yield
        except discord.Forbidden as error:
            self._forget(guild.id, user.id, *events)
            raise ModerationError("Discord refused that. Check that my role sits above theirs and holds the permission.") from error
        except discord.HTTPException as error:
            self._forget(guild.id, user.id, *events)
            raise ModerationError(f"Discord refused that: {error.text or error.status}") from error

    def _expect(self, guild_id: int, user_id: int, *events: str) -> None:
        now = time.monotonic()
        if len(self._expected) > 5000:
            self._expected = {key: until for key, until in self._expected.items() if until > now}
        for event in events:
            self._expected[(guild_id, user_id, event)] = now + EXPECT_SECONDS

    def _forget(self, guild_id: int, user_id: int, *events: str) -> None:
        for event in events:
            self._expected.pop((guild_id, user_id, event), None)

    def _take_expected(self, guild_id: int, user_id: int, event: str) -> bool:
        until = self._expected.pop((guild_id, user_id, event), None)
        return until is not None and until > time.monotonic()

    # ---- banning ----

    async def _check_not_banned(self, guild: Any, target: Any, member: Any, action: str) -> None:
        # Someone banned a moment ago can still be in the member list, and a second ban would be a second case for one ban.
        banned_just_now = time.monotonic() - self._banned_at.get((guild.id, target.id), float("-inf")) < EXPECT_SECONDS
        if member is not None and not banned_just_now:
            return
        # A temporary ban can be replaced by another length or a permanent one. A softban ends in an unban, which would lift it.
        if action != "softban" and await self.bot.timers.get_key("moderation.unban", f"{guild.id}:{target.id}"):
            return
        try:
            await guild.fetch_ban(target)
        except discord.NotFound:
            return
        except discord.HTTPException:
            return  # the ban itself will say what's wrong
        raise ModerationError(f"{escape(name_of(target))} is already banned.")

    def _note_ban(self, guild_id: int, user_id: int) -> None:
        if len(self._banned_at) > 5000:
            self._banned_at.clear()
        self._banned_at[(guild_id, user_id)] = time.monotonic()

    async def _do_ban(self, guild: Any, target: Any, action: str, audit: str, purge_days: int, seconds: int | None, moderator: Any) -> dict:
        purge = max(0, min(int(purge_days or 0), 7)) * DAY
        async with self._acting(guild, target, "ban"):
            await guild.ban(target, reason=audit, delete_message_seconds=purge)
        if action != "softban":
            self._note_ban(guild.id, target.id)
        if action == "softban":
            self._expect(guild.id, target.id, "unban")
            try:
                await guild.unban(target, reason=audit)
            except SEND_ERRORS as error:
                # The ban landed and the unban didn't. That's a ban until the timer gets it through, and the record has to say so.
                log.warning("Could not finish a softban of %s in %s: %s", target.id, guild.id, error)
                self._forget(guild.id, target.id, "unban")
                self._note_ban(guild.id, target.id)
                await self._schedule_unban(guild, target, moderator, time.time() + SOFTBAN_RETRY_SECONDS, "softban")
                return {"softban_unban_failed": True}
        elif action == "tempban":
            await self._schedule_unban(guild, target, moderator, time.time() + seconds, "tempban")
        elif action == "ban":
            await self.bot.timers.cancel_key("moderation.unban", f"{guild.id}:{target.id}")
        return {}

    async def _schedule_unban(self, guild: Any, target: Any, moderator: Any, due_at: float, kind: str) -> None:
        await self.bot.timers.schedule("moderation.unban", due_at, {
            "guild_id": guild.id, "user_id": target.id, "user_name": name_of(target), "moderator_id": moderator.id if moderator else None,
            "moderator_name": name_of(moderator) if moderator else None, "started_at": time.time(), "kind": kind,
        }, guild_id=guild.id, key=f"{guild.id}:{target.id}")

    async def _do_unban(self, guild: Any, target: Any, audit: str) -> None:
        self._expect(guild.id, target.id, "unban")
        try:
            await guild.unban(target, reason=audit)
        except discord.NotFound as error:
            self._forget(guild.id, target.id, "unban")
            raise ModerationError(f"{escape(name_of(target))} isn't banned.") from error
        except discord.Forbidden as error:
            self._forget(guild.id, target.id, "unban")
            raise ModerationError("Discord refused that. I need the Ban Members permission.") from error
        except discord.HTTPException as error:
            self._forget(guild.id, target.id, "unban")
            raise ModerationError(f"Discord refused that: {error.text or error.status}") from error
        await self.bot.timers.cancel_key("moderation.unban", f"{guild.id}:{target.id}")

    async def _timer_guild(self, kind: str, payload: dict, row: dict) -> Any:
        """The timer's server, or None after asking to look again shortly. A server that's unavailable, or that the bot isn't in right now, doesn't end the work."""
        guild = self.bot.get_guild(payload["guild_id"])
        if guild is None or getattr(guild, "unavailable", False):
            await self.bot.timers.schedule(kind, time.time() + 30, payload, guild_id=row.get("guild_id"), key=row.get("key"))
            return None
        return guild

    async def _tempban_due(self, payload: dict, row: dict) -> None:
        guild = await self._timer_guild("moderation.unban", payload, row)
        if guild is None:
            return
        user = Someone(payload["user_id"], payload.get("user_name"))
        # A restore can bring this timer back after a moderator made the ban permanent, whether or not it's past due.
        newest = await self.db.one(
            "SELECT action, extra FROM cases WHERE guild_id = ? AND user_id = ? AND action IN ('ban', 'tempban', 'unban') AND voided_at IS NULL ORDER BY number DESC LIMIT 1",
            (guild.id, user.id),
        )
        if newest is not None and newest["action"] == "ban" and not json.loads(newest["extra"] or "{}").get("softban_unban_failed"):
            return
        reason = self._automatic_reason("unban", payload.get("kind", "tempban"), payload.get("started_at"), payload.get("moderator_id"), payload.get("moderator_name"))
        self._expect(guild.id, user.id, "unban")
        try:
            await guild.unban(discord.Object(user.id), reason=reason[:AUDIT_REASON_MAX])
        except discord.NotFound:
            self._forget(guild.id, user.id, "unban")
            return
        except discord.HTTPException as error:
            self._forget(guild.id, user.id, "unban")
            if isinstance(error, discord.Forbidden):
                await self._tell_stuck(guild, row, f"{escape(user.name or str(user.id))} ({user.id}) is still banned past the end of a temporary ban.", "I need the Ban Members permission.")
            raise
        moderator = Someone(payload["moderator_id"], payload.get("moderator_name")) if payload.get("moderator_id") else None
        await self.record_case(guild, "unban", user, moderator, reason=reason, source=AUTOMATIC_SOURCE)

    async def _tell_staff(self, guild: Any, title: str, body: str) -> None:
        """Something staff have to act on: to the modlog, or the server log when the modlog can't take it."""
        channel_id = self.cfg(guild.id)["modlog_channel"]
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        if channel is not None:
            try:
                await channel.send(embed=discord.Embed(title=title, description=body[:messages.DESCRIPTION_MAX], color=0xF48942), allowed_mentions=discord.AllowedMentions.none())
                return
            except SEND_ERRORS as error:
                log.warning("Could not tell staff in %s: %s", guild.id, error)
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, title, body)

    async def _tell_mute_role_moved(self, guild: Any, role: Any) -> None:
        """The mute stays on record, and it can't go back on until the bot sits above the role. Staff hear once, however many people come back meanwhile."""
        if self._told_role_moved.get(guild.id) != role.id:
            self._told_role_moved[guild.id] = role.id
            await self._tell_staff(guild, "The mute role moved above the bot", f"{escape(role.name)} sits at or above my top role, so I can't put it back on people who are still muted when they return. Move my role above it.")

    async def _tell_stuck(self, guild: Any, row: dict, what: str, fix: str) -> None:
        """A timer that can't finish retries for good, and on its first failure staff hear what to fix."""
        if not row.get("tries"):
            await self._tell_staff(guild, "A timed action is stuck", f"{what} {fix} The bot keeps trying.")

    def _automatic_reason(self, verb: str, kind: str, started_at: float | None, moderator_id: int | None, moderator_name: str | None) -> str:
        ago = f"made {humanize(time.time() - started_at)} ago" if started_at else "that ran out"
        who = f" by {moderator_name} (ID: {moderator_id})" if moderator_id else ""
        return f"Automatic {verb} from {kind} {ago}{who}"

    # ---- muting ----

    def _mute_role_problem(self, guild: Any, role: Any) -> str | None:
        """Why this role can't be the mute role, in a sentence, or None."""
        if role.is_default() or role.managed:
            return f"{role.name} can't be handed out by a bot."
        if role >= guild.me.top_role:
            return f"{role.name} sits at or above my top role, so I can't hand it out. Move my role above it."
        if {name for name, on in role.permissions if on} & set(DANGEROUS_PERMISSIONS):
            return f"{role.name} holds moderator permissions, and a mute must not hand those out."
        return None

    def _mute_role(self, guild: Any) -> Any:
        role_id = self.cfg(guild.id)["mute_role"]
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            raise ModerationError("This server has no mute role. Make one with `/muterole create`, or pick one with `/muterole set`.")
        problem = self._mute_role_problem(guild, role)
        if problem:
            raise ModerationError(problem)
        return role

    async def _do_mute(self, guild: Any, member: Any, action: str, audit: str, seconds: int | None, moderator: Any) -> dict:
        role = self._mute_role(guild)
        async with self._mute_lock(guild.id, member.id):
            return await self._apply_mute(guild, member, role, action, audit, seconds, moderator)

    async def _apply_mute(self, guild: Any, member: Any, role: Any, action: str, audit: str, seconds: int | None, moderator: Any) -> dict:
        current = await self._active_mute(guild.id, member.id)
        removed: list[int] = json.loads(current["removed_roles"]) if current else []
        taken: list[int] = []
        # Automod and the warn threshold can add to a mute and never cut one down. Only a moderator's own choice replaces a longer one.
        stands = moderator is None and current is not None and self._outlasts(current, seconds)
        events = () if role in member.roles else ("mute",)
        async with self._acting(guild, member, *events):
            if action == "hardmute":
                takeable = [r for r in member.roles if not r.is_default() and r != role and not r.managed and r < guild.me.top_role]
                stays = [r for r in member.roles if not r.is_default() and r != role and r not in takeable]
                await member.edit(roles=[role, *stays], reason=audit)
                taken = [r.id for r in takeable]
            elif role not in member.roles:
                await member.add_roles(role, reason=audit)
        self._used_mute_roles.setdefault(guild.id, set()).add(role.id)
        if stands:
            if taken:
                await self.db.run("UPDATE active_mutes SET removed_roles = ? WHERE guild_id = ? AND user_id = ?", (json.dumps(sorted({*removed, *taken})), guild.id, member.id))
            return {"longer_mute_stands": True, **({"roles_removed": taken} if taken else {})}
        now = time.time()
        expires_at = now + seconds if seconds else None
        kind = "hardmute" if action == "hardmute" or (current and current["kind"] == "hardmute") else "mute"
        await self.db.run(
            "INSERT INTO active_mutes (guild_id, user_id, kind, role_id, moderator_id, moderator_name, removed_roles, started_at, expires_at, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET kind = excluded.kind, role_id = excluded.role_id, moderator_id = excluded.moderator_id, moderator_name = excluded.moderator_name, "
            "removed_roles = excluded.removed_roles, started_at = excluded.started_at, expires_at = excluded.expires_at, confirmed_at = excluded.confirmed_at",
            (guild.id, member.id, kind, role.id, moderator.id if moderator else None, name_of(moderator) if moderator else None, json.dumps(sorted({*removed, *taken})), now, expires_at, now),
        )
        key = f"{guild.id}:{member.id}"
        if seconds:
            await self.bot.timers.schedule("moderation.unmute", expires_at, {"guild_id": guild.id, "user_id": member.id}, guild_id=guild.id, key=key)
        else:
            await self.bot.timers.cancel_key("moderation.unmute", key)
        return {"roles_removed": taken} if taken else {}

    @staticmethod
    def _outlasts(current: dict, seconds: int | None) -> bool:
        """Whether the mute running now lasts longer than one of this length would. No length means no end."""
        if not seconds:
            return False
        ends = current["expires_at"]
        return ends is None or ends > time.time() + seconds

    async def _active_mute(self, guild_id: int, user_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM active_mutes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))

    # ---- another feature on the same role ----
    # A server can use its mute role for mini mutes too. The role then comes off only when no mute of either kind still holds it,
    # and the role changes that feature makes aren't mutes of their own to record.

    async def mute_running(self, guild_id: int, user_id: int) -> bool:
        row = await self._active_mute(guild_id, user_id)
        return row is not None and (row["expires_at"] is None or row["expires_at"] > time.time())

    @asynccontextmanager
    async def adding_mute_role(self, guild: Any, member: Any, role: Any) -> AsyncIterator[None]:
        """Another feature puts this server's mute role on someone. If that fails, the listener goes back to watching for a real change."""
        adding = role not in member.roles
        if adding:
            self._expect(guild.id, member.id, "mute")
        try:
            yield
        except discord.HTTPException:
            if adding:
                self._forget(guild.id, member.id, "mute")
            raise

    async def release_mute_role(self, guild: Any, member: Any, role: Any, audit: str) -> bool:
        """Another feature is done with this server's mute role. It comes off unless a mute is still running. True if nothing holds it now."""
        async with self._mute_lock(guild.id, member.id):
            if await self.mute_running(guild.id, member.id):
                return False
            if role in member.roles:
                self._expect(guild.id, member.id, "unmute")
                try:
                    await member.remove_roles(role, reason=audit)
                except discord.HTTPException:
                    self._forget(guild.id, member.id, "unmute")
                    raise
            return True

    async def _end_mini_mute(self, guild_id: int, user_id: int, role_id: int | None) -> None:
        """The shared role came off by someone's choice, so a mini mute on it is over and mustn't come back."""
        voice = self.bot.module("voice")
        if voice is not None and role_id is not None and voice.cfg(guild_id)["mini_mute_role"] == role_id:
            await voice.forget_mini_mute(guild_id, user_id)

    async def _do_unmute(self, guild: Any, member: Any, audit: str) -> None:
        async with self._mute_lock(guild.id, member.id):
            row = await self._active_mute(guild.id, member.id)
            role_id = self.cfg(guild.id)["mute_role"]
            role = guild.get_role(role_id) if role_id else None
            if row is None and (role is None or role not in member.roles):
                raise ModerationError(f"{escape(name_of(member))} isn't muted.")
            await self._lift_mute(guild, member, row, role, audit)
            await self._end_mini_mute(guild.id, member.id, role_id)
            await self.db.run("DELETE FROM raid_holds WHERE guild_id = ? AND user_id = ?", (guild.id, member.id))

    async def _unmute_someone_gone(self, guild: Any, target: Any) -> None:
        """They left while muted, so there's no role to take off, and the mute waits for them to come back. A wrong one comes off here. Roles a hardmute took were stripped already, and there's nobody to hand them to."""
        async with self._mute_lock(guild.id, target.id):
            if await self._active_mute(guild.id, target.id) is None:
                raise ModerationError(f"{escape(name_of(target))} isn't muted.")
            await self.db.run("DELETE FROM active_mutes WHERE guild_id = ? AND user_id = ?", (guild.id, target.id))
            await self.bot.timers.cancel_key("moderation.unmute", f"{guild.id}:{target.id}")
            await self._end_mini_mute(guild.id, target.id, self.cfg(guild.id)["mute_role"])

    async def _lift_mute(self, guild: Any, member: Any, row: dict | None, role: Any, audit: str, *, keep: Any = None) -> None:
        """Take the mute role off, give back what a hardmute took and forget the mute. `keep` is a role something else still needs on this person."""
        wanted = json.loads(row["removed_roles"]) if row else []
        back = [r for r in (guild.get_role(role_id) for role_id in wanted) if r is not None and not r.managed and r < guild.me.top_role]
        # The server may have picked a new mute role since this mute began, and the role it actually used has to come off.
        used = guild.get_role(row["role_id"]) if row and row.get("role_id") else None
        taking = [r for r in {role, used} if r is not None and r in member.roles and r != keep]
        async with self._acting(guild, member, *(("unmute",) if role in taking else ())):
            if back:
                keep = [r for r in member.roles if not r.is_default() and r not in taking]
                await member.edit(roles=[*keep, *[r for r in back if r not in keep]], reason=audit)
            elif taking:
                await member.remove_roles(*taking, reason=audit)
        await self.db.run("DELETE FROM active_mutes WHERE guild_id = ? AND user_id = ?", (guild.id, member.id))
        await self.bot.timers.cancel_key("moderation.unmute", f"{guild.id}:{member.id}")

    async def _mute_due(self, payload: dict, row: dict) -> None:
        guild = await self._timer_guild("moderation.unmute", payload, row)
        if guild is None:
            return
        user_id = payload["user_id"]
        async with self._mute_lock(guild.id, user_id):
            mute = await self._active_mute(guild.id, user_id)
            # Gone already, or a newer mute replaced this timer while it was starting.
            if mute is None or mute["expires_at"] is None or mute["expires_at"] > row["due_at"] + 1:
                return
            reason = self._automatic_reason("unmute", mute["kind"], mute["started_at"], mute["moderator_id"], mute["moderator_name"])
            role_id = self.cfg(guild.id)["mute_role"]
            # Right after a restart the list can be half filled, and someone not on it still holds the role. No answer raises, so the timer tries again.
            member = (await self._members_by_id(guild, [user_id])).get(user_id)
            if member is None:
                # They left, and nothing happened that a case should record. A hardmute's roles wait in the row until they come back, and a plain mute has nothing left to do.
                if json.loads(mute["removed_roles"]):
                    await self.db.run("UPDATE active_mutes SET expires_at = ? WHERE guild_id = ? AND user_id = ?", (time.time(), guild.id, user_id))
                else:
                    await self.db.run("DELETE FROM active_mutes WHERE guild_id = ? AND user_id = ?", (guild.id, user_id))
                return
            role = guild.get_role(role_id) if role_id else None
            voice = self.bot.module("voice")
            held = role is not None and voice is not None and await voice.mini_mute_holds(guild.id, user_id, role.id)
            try:
                await self._lift_mute(guild, member, mute, role, reason[:AUDIT_REASON_MAX], keep=role if held else None)
            except ModerationError:
                await self._tell_stuck(guild, row, f"{escape(name_of(member))} ({user_id}) is still muted past the end of their mute.", "I need the Manage Roles permission, and my role has to sit above the mute role and any role the mute took.")
                raise
        moderator = Someone(mute["moderator_id"], mute["moderator_name"]) if mute["moderator_id"] else None
        await self.record_case(guild, "unmute", member, moderator, reason=reason, source=AUTOMATIC_SOURCE)

    async def _reapply_mute(self, member: Any) -> None:
        """Someone can't shed a mute by leaving and coming back."""
        async with self._mute_lock(member.guild.id, member.id):
            await self._reapply_mute_locked(member)

    async def _reapply_mute_locked(self, member: Any) -> None:
        guild = member.guild
        row = await self._active_mute(guild.id, member.id)
        if row is None:
            return
        role_id = self.cfg(guild.id)["mute_role"]
        role = guild.get_role(role_id) if role_id else None
        if row["expires_at"] and row["expires_at"] <= time.time():
            try:
                await self._lift_mute(guild, member, row, role, "The mute ended while they were away")
            except ModerationError as error:
                log.warning("Could not give roles back to %s in %s: %s", member.id, guild.id, error)
            return
        if role is None:
            return
        if role >= guild.me.top_role:
            await self._tell_mute_role_moved(guild, role)
            return
        if role not in member.roles:
            self._expect(guild.id, member.id, "mute")
            try:
                await member.add_roles(role, reason="Still muted from before they left")
            except discord.HTTPException as error:
                self._forget(guild.id, member.id, "mute")
                log.warning("Could not mute %s again in %s: %s", member.id, guild.id, error)
                return
        await self.db.run("UPDATE active_mutes SET confirmed_at = ? WHERE guild_id = ? AND user_id = ?", (time.time(), guild.id, member.id))

    async def reconcile(self, guild: Any) -> None:
        """After downtime: anyone who came back while the bot was away gets their mute again, and a mute staff took off by hand stays off. Timers handle what ran out."""
        rows = await self.db.all("SELECT * FROM active_mutes WHERE guild_id = ?", (guild.id,))
        now = time.time()
        for row in rows:
            member = guild.get_member(row["user_id"])
            if member is None or getattr(member, "pending", False):
                continue
            if row["expires_at"] and row["expires_at"] <= now:
                await self._settle_past_end(guild, member, row)
                continue
            if self._unmuted_by_hand(guild, member, row):
                await self._end_mute_taken_off(guild, member, row["role_id"])
                continue
            await self._reapply_mute(member)

    async def _settle_past_end(self, guild: Any, member: Any, row: dict) -> None:
        """A mute past its end normally has a timer on the way. A restore leaves past-due timers out, so one with no timer ends here. Someone still holding the role comes out of the mute, and so does someone who came back after it ended, whose roles waited in the row. Anyone else has been out of it since, and staff may have taken the roles it held back away on purpose, so those stay off."""
        if await self.bot.timers.get_key("moderation.unmute", f"{guild.id}:{member.id}"):
            return
        async with self._mute_lock(guild.id, member.id):
            role_id = self.cfg(guild.id)["mute_role"]
            joined = getattr(member, "joined_at", None)
            came_back = joined is not None and joined.timestamp() > row["expires_at"]
            if not came_back and not any(role.id in (row["role_id"], role_id) for role in member.roles):
                await self.db.run("DELETE FROM active_mutes WHERE guild_id = ? AND user_id = ?", (guild.id, member.id))
                return
            reason = self._automatic_reason("unmute", row["kind"], row["started_at"], row["moderator_id"], row["moderator_name"])
            try:
                await self._lift_mute(guild, member, row, guild.get_role(role_id) if role_id else None, reason[:AUDIT_REASON_MAX])
            except ModerationError as error:
                log.warning("Could not end a mute past its end on %s in %s: %s", member.id, guild.id, error)
                return
        moderator = Someone(row["moderator_id"], row["moderator_name"]) if row["moderator_id"] else None
        await self.record_case(guild, "unmute", member, moderator, reason=reason, source=AUTOMATIC_SOURCE)

    def _unmuted_by_hand(self, guild: Any, member: Any, row: dict) -> bool:
        """They joined before the bot last saw the mute on them, so they never left since, and the role is gone anyway. Someone took it off while the bot was away. Someone who joined later is back after leaving, and the mute goes on again."""
        roles = {row["role_id"], self.cfg(guild.id)["mute_role"]}
        joined = getattr(member, "joined_at", None)
        return joined is not None and joined.timestamp() < (row["confirmed_at"] or row["started_at"]) and not any(role.id in roles for role in member.roles)

    async def _end_mute_taken_off(self, guild: Any, member: Any, role_id: int | None) -> None:
        """Forget the mute, give back what a hardmute took, and record it, as when this happens with the bot running."""
        reason = "The mute role came off while the bot was offline"
        async with self._mute_lock(guild.id, member.id):
            row = await self._active_mute(guild.id, member.id)
            if row is None:
                return
            try:
                await self._lift_mute(guild, member, row, guild.get_role(role_id) if role_id else None, reason)
            except ModerationError as error:  # the row stays, so the next start tries again
                log.warning("Could not finish lifting the mute on %s in %s: %s", member.id, guild.id, error)
                return
            await self._end_mini_mute(guild.id, member.id, role_id)
        await self.record_case(guild, "unmute", member, None, reason=reason, source="external")

    async def _do_timeout(self, guild: Any, member: Any, seconds: int, audit: str) -> None:
        if getattr(member.guild_permissions, "administrator", False):
            raise ModerationError("Discord doesn't allow timing out someone with Administrator.")
        until = discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
        async with self._acting(guild, member, "timeout"):
            await member.timeout(until, reason=audit)

    async def _do_untimeout(self, guild: Any, member: Any, audit: str) -> None:
        if not member.is_timed_out():
            raise ModerationError(f"{escape(name_of(member))} isn't timed out.")
        async with self._acting(guild, member, "untimeout"):
            await member.timeout(None, reason=audit)

    # ---- direct messages ----

    def _dm_text(self, guild: Any, action: str, mode: int, moderator: Any, reason: str | None, seconds: int | None) -> str:
        length = f" for {humanize(seconds)}" if seconds else ""
        line = f"You were {DM_PHRASE[action]} {guild.name}{length}."
        if mode >= DM_REASON and reason:
            line += f" Reason: '{reason}'."
        if mode == DM_FULL and moderator is not None:
            line += f" Moderator: {name_of(moderator)} (id: {moderator.id})."
        return line[:messages.CONTENT_MAX]

    async def _send_dm(self, guild: Any, member: Any, action: str, moderator: Any, reason: str | None, seconds: int | None, *, keep: list[Any] | None = None) -> bool | None:
        """True if it went out, False if Discord refused or didn't answer, None if this server sends nothing for this action. `keep` collects the message, for an action that might yet fail."""
        settings = self.cfg(guild.id).get(action)
        if settings is None or settings["mode"] == DM_NONE or getattr(member, "bot", False):
            return None
        mode = settings["mode"]
        none = discord.AllowedMentions.none()
        try:
            if mode == DM_CUSTOM and messages.has_content(settings["message"]):
                scope = Scope(guild=guild, member=member, target=moderator, extra={
                    "reason": reason or "N/A",
                    "moderator": name_of(moderator) if moderator is not None else SOURCE_LABELS["automod"],
                    "offender": name_of(member),
                    "duration": humanize(seconds) if seconds else "",
                })
                message = await member.send(**messages.send_kwargs(await render_message(settings["message"], scope), allowed_mentions=none))
            else:
                message = await member.send(self._dm_text(guild, action, DM_REASON if mode == DM_CUSTOM else mode, moderator, reason, seconds), allowed_mentions=none)
        except SEND_ERRORS:
            return False
        if keep is not None:
            keep.append(message)
        return True

    # ---- the modlog post ----

    def _reason_hint(self, guild_id: int, number: int) -> str:
        prefixes = self.bot.settings.get(guild_id, "core")["prefixes"]
        command = f"{prefixes[0]}reason {number} <text>" if prefixes else f"/modlog reason {number} <text>"
        return f"None given. Add one with `{command}`"

    def _moderator_label(self, row: dict) -> str:
        return row["moderator_name"] or SOURCE_LABELS.get(row["source"]) or "Unknown"

    def case_embed(self, guild_id: int, row: dict) -> discord.Embed:
        voided = row.get("voided_at") is not None
        lines = [f"**Offender:** {escape(row['user_name'])}  <@{row['user_id']}>"]
        if row["seconds"]:
            lines.append(f"**Duration:** {humanize(row['seconds'])}")
        lines.append(f"**Reason:** {row['reason'] or self._reason_hint(guild_id, row['number'])}")
        lines.append(f"**Responsible moderator:** {escape(self._moderator_label(row))}")
        if voided:
            why = f": {row['void_reason']}" if row.get("void_reason") else ""
            lines.append(f"**Voided by:** {escape(row.get('voided_by_name') or 'Unknown')}{why}")
        embed = discord.Embed(
            title=f"{row['action']} | case {row['number']}" + (" (voided)" if voided else ""),
            description="\n".join(lines)[:messages.DESCRIPTION_MAX],
            color=VOIDED_COLOR if voided else COLORS.get(row["action"], 0x4286F4),
            timestamp=datetime.datetime.fromtimestamp(row["created_at"], datetime.timezone.utc),
        )
        embed.set_footer(text=f"ID: {row['user_id']}")
        return embed

    async def _post_cases(self, guild: Any, rows: list[dict]) -> bool:
        """Post these to the modlog. False if a post that should have gone didn't. One batch failing doesn't stop the next, and `/modlog repost` picks up what's left."""
        cfg = self.cfg(guild.id)
        channel_id = cfg["modlog_channel"]
        wanted = [row for row in rows if cfg["logged"].get(LOGGED_AS[row["action"]], True)]
        if not channel_id or not wanted:
            return True
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            log.warning("The modlog channel %s in %s is gone", channel_id, guild.id)
            return False
        batch: list[dict] = []
        size = 0
        batches: list[list[dict]] = []
        for row in wanted:
            entry = messages.embed_size(self.case_embed(guild.id, row).to_dict())
            if batch and (len(batch) >= EMBEDS_PER_POST or size + entry > messages.TOTAL_MAX):
                batches.append(batch)
                batch, size = [], 0
            batch.append(row)
            size += entry
        batches.append(batch)
        posted = True
        for group in batches:
            try:
                sent = await channel.send(embeds=[self.case_embed(guild.id, row) for row in group], allowed_mentions=discord.AllowedMentions.none())
            except SEND_ERRORS as error:
                log.warning("Could not post to the modlog %s in %s: %s", channel_id, guild.id, error)
                posted = False
                continue
            await self.db.many(
                "UPDATE cases SET channel_id = ?, message_id = ? WHERE guild_id = ? AND number = ?",
                [(channel.id, sent.id, guild.id, row["number"]) for row in group],
            )
            for row in group:
                row["channel_id"], row["message_id"] = channel.id, sent.id
        return posted

    async def _refresh_post(self, guild_id: int, row: dict) -> bool:
        """Show a changed case in its modlog post. One post can hold several cases, as after a mass ban, so it's rebuilt whole."""
        if not row["message_id"] or not row["channel_id"]:
            return False
        rows = await self.db.all("SELECT * FROM cases WHERE guild_id = ? AND message_id = ? ORDER BY number", (guild_id, row["message_id"]))
        payload = {"content": "", "embeds": [self.case_embed(guild_id, r).to_dict() for r in rows], "buttons": []}
        try:
            await self.bot.ops.edit_message(row["channel_id"], row["message_id"], payload)
        except OpsError as error:
            log.info("Could not update the modlog post for case %s in %s: %s", row["number"], guild_id, error)
            return False
        return True

    # ---- changing cases ----

    def _decode(self, row: dict) -> dict:
        row["extra"] = json.loads(row["extra"]) if row.get("extra") else {}
        row["voided"] = row.get("voided_at") is not None
        return row

    async def get_case(self, guild_id: int, number: int) -> dict | None:
        if number > SQLITE_INT_MAX:
            return None
        row = await self.db.one("SELECT * FROM cases WHERE guild_id = ? AND number = ?", (guild_id, number))
        return self._decode(row) if row else None

    async def set_reason(self, guild_id: int, number: int, reason: str, actor: Any = None) -> tuple[dict, bool] | None:
        """Change a case's reason and its modlog post. Returns the row and whether the post could be updated, or None if there's no such case.

        The old reason stays in the case's `extra`, with who changed it. A case made outside the bot with nobody named takes the name of whoever explains it."""
        row = await self.get_case(guild_id, number)
        if row is None:
            return None
        extra = row["extra"]
        if row["reason"]:
            extra["earlier_reasons"] = [*extra.get("earlier_reasons", []), {"reason": row["reason"], "by": actor.id if actor else None, "by_name": name_of(actor) if actor else None, "at": time.time()}][-EARLIER_REASONS_KEPT:]
        moderator_id, moderator_name = row["moderator_id"], row["moderator_name"]
        if moderator_id is None and row["source"] == "external" and actor is not None:
            moderator_id, moderator_name = actor.id, name_of(actor)
        await self.db.run(
            "UPDATE cases SET reason = ?, extra = ?, moderator_id = ?, moderator_name = ? WHERE guild_id = ? AND number = ?",
            (clip(reason, REASON_MAX), json.dumps(extra) if extra else None, moderator_id, moderator_name, guild_id, number),
        )
        row = await self.get_case(guild_id, number)
        return row, await self._refresh_post(guild_id, row)

    async def void_case(self, guild_id: int, number: int, actor: Any, reason: str | None = None) -> tuple[dict, bool] | None:
        """Take a case off someone's record. The row stays, marked, so the history of what happened isn't lost."""
        if number > SQLITE_INT_MAX:
            return None
        count = await self.db.run(
            "UPDATE cases SET voided_at = ?, voided_by = ?, voided_by_name = ?, void_reason = ? WHERE guild_id = ? AND number = ? AND voided_at IS NULL",
            (time.time(), actor.id if actor else None, name_of(actor) if actor else None, clip(reason, REASON_MAX), guild_id, number),
        )
        if not count:
            return None
        row = await self.get_case(guild_id, number)
        return row, await self._refresh_post(guild_id, row)

    async def restore_case(self, guild_id: int, number: int) -> tuple[dict, bool] | None:
        if number > SQLITE_INT_MAX:
            return None
        count = await self.db.run(
            "UPDATE cases SET voided_at = NULL, voided_by = NULL, voided_by_name = NULL, void_reason = NULL WHERE guild_id = ? AND number = ? AND voided_at IS NOT NULL",
            (guild_id, number),
        )
        if not count:
            return None
        row = await self.get_case(guild_id, number)
        return row, await self._refresh_post(guild_id, row)

    async def warning_count(self, guild_id: int, user_id: int) -> int:
        return await self.db.scalar("SELECT COUNT(*) FROM cases WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND voided_at IS NULL", (guild_id, user_id))

    # ---- notes ----

    async def _notes_of(self, guild_id: int, user_id: int) -> list[dict]:
        return await self.db.all("SELECT * FROM notes WHERE guild_id = ? AND user_id = ? ORDER BY number DESC", (guild_id, user_id))

    async def add_note(self, guild_id: int, user_id: int, author: Any, body: str) -> dict:
        now = time.time()
        async with self.db.transaction():
            highest = await self.db.scalar("SELECT MAX(number) FROM notes WHERE guild_id = ?", (guild_id,))
            number = (highest or 0) + 1
            await self.db.run(
                "INSERT INTO notes (guild_id, number, user_id, author_id, author_name, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, number, user_id, author.id if author else None, name_of(author) if author else None, body, now),
            )
        return {"guild_id": guild_id, "number": number, "user_id": user_id, "author_id": author.id if author else None, "author_name": name_of(author) if author else None, "text": body, "created_at": now}

    async def remove_note(self, guild_id: int, number: int) -> bool:
        if number > SQLITE_INT_MAX:
            return False
        return await self.db.run("DELETE FROM notes WHERE guild_id = ? AND number = ?", (guild_id, number)) > 0

    # ---- things done outside the bot ----

    async def _audit_entry(self, guild: Any, action: discord.AuditLogAction, target_id: int, where: Callable[[Any], bool] | None = None, wait: float | None = None) -> Any:
        return await self.bot.audit.find(guild.id, action, target_id, wait=AUDIT_WAIT if wait is None else wait, where=where)

    def _by_this_bot(self, entry: Any) -> bool:
        me = self.bot.user
        return entry is not None and me is not None and entry.user_id == me.id

    def _moderator_of(self, entry: Any) -> Any:
        if entry is None or entry.user_id is None:
            return None
        return entry.user or self.bot.get_user(entry.user_id) or Someone(entry.user_id)

    def _entry_explains(self, entry: Any) -> tuple[str, ...]:
        """Which actions on a case this audit entry can fill in."""
        if entry.action == discord.AuditLogAction.ban:
            return ("ban",)
        if entry.action == discord.AuditLogAction.unban:
            return ("unban",)
        if entry.action == discord.AuditLogAction.automod_timeout_member:
            return ("timeout",)
        if entry.action == discord.AuditLogAction.member_update and hasattr(getattr(entry.changes, "after", None), "timed_out_until"):
            return ("timeout", "untimeout")
        return ()

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: Any) -> None:
        """The audit entry can arrive after the wait for it ran out, and then the case was recorded with nobody named."""
        actions = self._entry_explains(entry)
        if not actions or self._by_this_bot(entry) or entry.user_id is None:
            return
        guild = entry.guild
        marks = ", ".join("?" for _ in actions)
        row = await self.db.one(
            f"SELECT number FROM cases WHERE guild_id = ? AND user_id = ? AND source = 'external' AND moderator_id IS NULL AND action IN ({marks}) AND created_at >= ? ORDER BY number DESC LIMIT 1",
            (guild.id, entry.target.id, *actions, time.time() - LATE_AUDIT_SECONDS),
        )
        if row is None:
            return
        moderator = self._moderator_of(entry)
        await self.db.run(
            "UPDATE cases SET moderator_id = ?, moderator_name = ?, moderator_bot = ?, reason = COALESCE(reason, ?) WHERE guild_id = ? AND number = ?",
            (moderator.id, name_of(moderator), 1 if getattr(moderator, "bot", False) else 0, clip(entry.reason, REASON_MAX), guild.id, row["number"]),
        )
        await self._refresh_post(guild.id, await self.get_case(guild.id, row["number"]))

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: Any) -> None:
        if self._take_expected(guild.id, user.id, "ban"):
            return
        entry = await self._audit_entry(guild, discord.AuditLogAction.ban, user.id)
        if self._by_this_bot(entry):
            return
        await self.record_case(guild, "ban", user, self._moderator_of(entry), reason=entry.reason if entry else None, source="external")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: Any) -> None:
        await self.bot.timers.cancel_key("moderation.unban", f"{guild.id}:{user.id}")
        if self._take_expected(guild.id, user.id, "unban"):
            return
        entry = await self._audit_entry(guild, discord.AuditLogAction.unban, user.id)
        if self._by_this_bot(entry):
            return
        await self.record_case(guild, "unban", user, self._moderator_of(entry), reason=entry.reason if entry else None, source="external")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        if self._take_expected(guild.id, member.id, "kick"):
            return
        entry = await self._audit_entry(guild, discord.AuditLogAction.kick, member.id)
        if entry is None or self._by_this_bot(entry):
            return
        # Someone kicked, back and gone again within the audit window must not count as kicked twice.
        now = time.time()
        self._seen_kicks = {key: at for key, at in self._seen_kicks.items() if now - at < 60}
        if self._seen_kicks.get((guild.id, member.id)) == entry.at:
            return
        self._seen_kicks[(guild.id, member.id)] = entry.at
        await self.record_case(guild, "kick", member, self._moderator_of(entry), reason=entry.reason, source="external")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._watch_join_rate(member.guild)
        # A member still on the rules screen can't take roles, so the mute waits until they accept.
        if not member.pending:
            await self._reapply_mute(member)
            await self._hold_if_raid(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.pending and not after.pending:
            await self._reapply_mute(after)
            await self._hold_if_raid(after)
        await self._watch_timeout(before, after)
        await self._watch_mute_role(before, after)

    async def _watch_timeout(self, before: Any, after: Any) -> None:
        if before.timed_out_until == after.timed_out_until:
            return
        guild = after.guild
        now = discord.utils.utcnow()
        running = after.timed_out_until is not None and after.timed_out_until > now
        was_running = before.timed_out_until is not None and before.timed_out_until > now
        if not running and not was_running:
            return
        event = "timeout" if running else "untimeout"
        if self._take_expected(guild.id, after.id, event):
            return
        entry = await self._audit_entry(guild, discord.AuditLogAction.member_update, after.id, where=lambda e: hasattr(getattr(e.changes, "after", None), "timed_out_until"))
        if entry is None and running:
            # Discord's own automod files its timeouts under a separate action.
            entry = await self._audit_entry(guild, discord.AuditLogAction.automod_timeout_member, after.id, wait=0)
        if self._by_this_bot(entry):
            return
        seconds = int((after.timed_out_until - now).total_seconds()) if running else None
        await self.record_case(guild, event, after, self._moderator_of(entry), reason=entry.reason if entry else None, seconds=seconds, source="external")

    async def _watch_mute_role(self, before: Any, after: Any) -> None:
        guild = after.guild
        role_id = self.cfg(guild.id)["mute_role"]
        if not role_id:
            return
        had = before.get_role(role_id) is not None
        has = after.get_role(role_id) is not None
        if had == has:
            return
        event = "mute" if has else "unmute"
        if self._take_expected(guild.id, after.id, event):
            return

        def about_this_role(entry: Any) -> bool:
            side = getattr(entry.changes, "after" if has else "before", None)
            return any(getattr(role, "id", None) == role_id for role in getattr(side, "roles", None) or [])

        entry = await self._audit_entry(guild, discord.AuditLogAction.member_role_update, after.id, where=about_this_role)
        if self._by_this_bot(entry):
            return
        moderator = self._moderator_of(entry)
        reason = entry.reason if entry else None
        # Raid mode holds people under this role and that's no mute, so taking it off by hand only lets them go.
        if not has and await self.db.run("DELETE FROM raid_holds WHERE guild_id = ? AND user_id = ?", (guild.id, after.id)) and await self._active_mute(guild.id, after.id) is None:
            return
        async with self._mute_lock(guild.id, after.id):
            if has:
                await self.db.run(
                    "INSERT INTO active_mutes (guild_id, user_id, kind, role_id, moderator_id, moderator_name, removed_roles, started_at, expires_at) VALUES (?, ?, 'mute', ?, ?, ?, '[]', ?, NULL) "
                    "ON CONFLICT (guild_id, user_id) DO NOTHING",
                    (guild.id, after.id, role_id, moderator.id if moderator else None, name_of(moderator) if moderator else None, time.time()),
                )
            else:
                row = await self._active_mute(guild.id, after.id)
                if row is not None:
                    try:
                        await self._lift_mute(guild, after, row, guild.get_role(role_id), reason or "Mute role removed")
                    except ModerationError as error:
                        log.warning("Could not finish lifting the mute on %s in %s: %s", after.id, guild.id, error)
                        waiting = [role.name for role in (guild.get_role(taken) for taken in json.loads(row["removed_roles"])) if role is not None]
                        if waiting:
                            reason = " ".join(part for part in (reason, f"Their other roles couldn't be given back: {', '.join(waiting)}.") if part)
                await self._end_mini_mute(guild.id, after.id, role_id)
        await self.record_case(guild, event, after, moderator, reason=reason, source="external")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: Any) -> None:
        """A new channel takes the mute role's limits right away, so nobody has to remember /muterole update."""
        role_id = self.cfg(channel.guild.id)["mute_role"]
        role = channel.guild.get_role(role_id) if role_id else None
        if role is not None and self._mute_role_problem(channel.guild, role) is None:
            await self._apply_mute_overwrite(channel, role, "A new channel takes the mute role's limits")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Every READY, since one comes after a session that couldn't resume, and what happened during that gap is gone. A READY during a check needs no second one, because the check waits for the new member lists."""
        if self._reconciling:
            return
        self._reconciling = True
        try:
            await self.bot.members_settled.wait()  # someone not on a half-filled list would be passed over
            for guild in self.bot.guilds:
                try:
                    await self.reconcile(guild)
                except Exception:
                    log.exception("Could not check the mutes in %s", guild.id)
        finally:
            self._reconciling = False

    # ---- command helpers ----

    async def _own_record(self, ctx: Any, subject_id: int) -> bool:
        """Below the owner and Administrators, nobody changes a record about themselves. Answers and returns True when that's what this is. The dashboard's `_check_record_change` says the same."""
        if subject_id != ctx.author.id or access.is_admin(ctx.author):
            return False
        await ctx.fail(OWN_RECORD)
        return True

    def _time_and_reason(self, ctx: Any, duration: str | None, reason: str | None) -> tuple[int | None, str | None]:
        """Slash commands fill a duration box. Text commands type it at the front of the reason, as `.mute @member 20h45m being rude`."""
        if ctx.interaction is not None:
            if duration is None:
                return None, reason
            seconds = parse_duration(duration)
            if not seconds:
                raise ModerationError(f"`{duration}` isn't a length of time. Try something like 3h42m or 2 days.")
            return seconds, reason
        typed = " ".join(part for part in (duration, reason) if part)
        seconds, rest = split_leading_duration(typed)
        if seconds:
            return seconds, rest or None
        # A first word that starts like a length has to be one. Reading it as a reason would turn a typo into a mute with no end.
        if _LENGTH_START.match(typed):
            raise ModerationError(f"I couldn't read that length: `{typed.split()[0]}`. Try something like 3h42m or 2 days, or start the reason with a word.")
        words = typed.split()
        # `for 3h spamming` reads the same as `3h spamming`, and `for spamming` is a reason.
        if words and words[0].lower() == "for":
            seconds, rest = split_leading_duration(" ".join(words[1:]))
            if seconds:
                return seconds, rest or None
        # `spamming for 3h`
        for index in range(len(words) - 2, -1, -1):
            if words[index].lower() == "for":
                seconds = parse_duration(" ".join(words[index + 1:]))
                if seconds:
                    return seconds, " ".join(words[:index]) or None
                break
        # A bare `spamming 3h` could be a length or the end of a sentence, as in "printing in 3d". Guessing wrong makes a mute with no end, or a short one that should have lasted.
        for count in (1, 2):
            tail = words[-count:]
            if len(words) > count and _LENGTH_START.match(tail[0]) and parse_duration(" ".join(tail)):
                raise ModerationError(f"Does `{' '.join(tail)}` at the end mean a length of time? Put a length first, as in `3h spamming`. If it's part of the reason, end the reason with a period.")
        return None, typed or None

    def _done_text(self, row: dict) -> str:
        name = escape(row["user_name"])
        length = f" for {humanize(row['seconds'])}" if row["seconds"] else (" with no end" if row["action"] in ("mute", "hardmute") else "")
        line = f"{DONE[row['action']]} {name}{length}. Case {row['number']}."
        if row["extra"].get("softban_unban_failed"):
            line = f"{name} is banned for now. Discord refused the unban, and it tries again in a minute. Case {row['number']}."
        if row.get("dm") is False:
            line += " They couldn't be sent a DM."
        if row.get("modlog_failed"):
            line += " It couldn't post to the modlog."
        return line

    async def _act(self, ctx: Any, user: Any, action: str, *, duration: str | None = None, reason: str | None = None, seconds: int | None = None, purge_days: int = 0, deferred: bool = False) -> None:
        if not deferred:  # Discord refuses a second acknowledgment of one command
            await ctx.defer(ephemeral=True)
        try:
            if action in TIMED_ACTIONS and seconds is None:
                seconds, reason = self._time_and_reason(ctx, duration, reason)
            row = await self.punish(ctx.guild, user, action, moderator=ctx.author, reason=reason, seconds=seconds, purge_days=purge_days)
        except ModerationError as error:
            await ctx.fail(str(error))
            return
        await ctx.ok((self._done_text(row) + self._mute_gaps(ctx.guild, user, row))[:messages.CONTENT_MAX - 10])

    def _mute_gaps(self, guild: Any, user: Any, row: dict) -> str:
        """Discord merges role overwrites with allow over deny, whatever the role positions, so a role's own allow in a channel beats the mute role there. "Muted" shouldn't stand unqualified when that's so."""
        if row["action"] not in ("mute", "hardmute"):
            return ""
        member = guild.get_member(user.id)
        if member is None:
            return ""
        if getattr(member.guild_permissions, "administrator", False):
            return " They hold Administrator, so the mute role doesn't stop them."
        taken = set(row["extra"].get("roles_removed", []))
        roles = [role for role in member.roles if not role.is_default() and role.id not in taken]
        open_channels = [
            channel for channel in guild.text_channels
            if channel.permissions_for(member).view_channel and (channel.overwrites_for(member).send_messages is True or any(channel.overwrites_for(role).send_messages is True for role in roles))
        ]
        if not open_channels:
            return ""
        hint = " A hardmute takes those roles away." if row["action"] == "mute" else ""
        return f" They can still post in {self._channel_list(open_channels)}, because a role they hold or their own overwrite allows it there.{hint}"

    def _purge_days(self, guild: Any, action: str, days: int | None) -> int:
        return self.cfg(guild.id)[action]["days"] if days is None else days

    def _case_line(self, row: dict, *, show_user: bool = True) -> str:
        who = f" {escape(row['user_name'])}" if show_user else ""
        length = f" for {humanize(row['seconds'])}" if row["seconds"] else ""
        return f"**#{row['number']}** {row['action']}{who}{length} by {escape(self._moderator_label(row))}, <t:{int(row['created_at'])}:R>\n{row['reason'] or 'No reason given.'}"

    def _member_link(self, guild_id: int, user_id: int) -> str | None:
        return f"{self.bot.public_url}/dashboard/{guild_id}/cases?member={user_id}" if self.bot.public_url else None

    def _case_list(self, title: str, rows: list[dict], color: int, *, show_user: bool, link: str | None, empty: str) -> discord.Embed:
        embed = discord.Embed(title=title, color=color)
        lines: list[str] = []
        size = 0
        for index, row in enumerate(rows):
            line = self._case_line(row, show_user=show_user)
            if size + len(line) > 3600:
                lines.append(f"...and {len(rows) - index} more.")
                break
            lines.append(line)
            size += len(line) + 1
        embed.description = "\n".join(lines) or empty
        if link:
            embed.add_field(name="Full record", value=f"[Open in the dashboard]({link})")
        return embed

    # ---- punishment commands ----

    @commands.hybrid_command(name="ban", description="Ban someone, whether or not they're in the server")
    @commands.guild_only()
    @requires("ban_members")
    @app_commands.describe(user="Who to ban", days="Days of their messages to delete, 0 to 7", reason="Why")
    async def ban(self, ctx: commands.Context, user: OnePerson, days: int | None = None, *, reason: str | None = None) -> None:
        if days is not None and not 0 <= days <= 7:
            if ctx.interaction is not None:
                await ctx.fail("Message days must be between 0 and 7.")
                return
            # A number that can't be a day count is the start of the reason.
            reason, days = " ".join(part for part in (str(days), reason) if part), None
        await self._act(ctx, user, "ban", reason=reason, purge_days=self._purge_days(ctx.guild, "ban", days))

    @commands.hybrid_command(name="tempban", description="Ban someone for a length of time")
    @commands.guild_only()
    @requires("ban_members")
    @app_commands.describe(user="Who to ban", days="Days of their messages to delete, 0 to 7", duration="How long, such as 3d or 12h", reason="Why")
    async def tempban(self, ctx: commands.Context, user: OnePerson, days: int | None = None, duration: str | None = None, *, reason: str | None = None) -> None:
        if ctx.interaction is not None:
            if days is not None and not 0 <= days <= 7:
                await ctx.fail("Message days must be between 0 and 7.")
                return
        elif days is not None and (not 0 <= days <= 7 or parse_duration(f"{days} {duration or ''}")):
            duration, days = " ".join(part for part in (str(days), duration) if part), None
        await self._act(ctx, user, "tempban", duration=duration, reason=reason, purge_days=self._purge_days(ctx.guild, "tempban", days))

    @commands.hybrid_command(name="softban", description="Ban and unban someone at once, which clears their messages")
    @commands.guild_only()
    @requires("ban_members")
    @app_commands.describe(user="Who to softban", days="Days of their messages to delete, 0 to 7", reason="Why")
    async def softban(self, ctx: commands.Context, user: OnePerson, days: int | None = None, *, reason: str | None = None) -> None:
        if days is not None and not 0 <= days <= 7:
            if ctx.interaction is not None:
                await ctx.fail("Message days must be between 0 and 7.")
                return
            reason, days = " ".join(part for part in (str(days), reason) if part), None
        await self._act(ctx, user, "softban", reason=reason, purge_days=self._purge_days(ctx.guild, "softban", days))

    @commands.hybrid_command(name="unban", description="Lift a ban")
    @commands.guild_only()
    @requires("ban_members")
    @app_commands.describe(user="Their ID, mention or name", reason="Why")
    async def unban(self, ctx: commands.Context, user: str, *, reason: str | None = None) -> None:
        await ctx.defer(ephemeral=True)
        target = await self._find_banned(ctx.guild, user)
        if target is None:
            await ctx.fail(f"{escape(user)} isn't banned here.")
            return
        await self._act(ctx, target, "unban", reason=reason, deferred=True)

    async def _find_banned(self, guild: Any, words: str) -> Any:
        ids, _ = leading_ids(words)
        try:
            if ids:
                return (await guild.fetch_ban(discord.Object(ids[0]))).user
            needle = words.strip().lstrip("@").lower()
            async for entry in guild.bans(limit=None):
                if str(entry.user).lower() == needle or entry.user.name.lower() == needle:
                    return entry.user
        except discord.HTTPException:
            return None
        return None

    @commands.hybrid_command(name="massban", description="Ban a list of people by ID or mention, in or out of the server")
    @commands.guild_only()
    @requires("ban_members")
    @app_commands.describe(users="IDs or mentions, or filters such as joined:10m created:1d name:free*nitro noroles", reason="Why", days="Days of their messages to delete, 0 to 7")
    async def massban(self, ctx: commands.Context, users: str, *, reason: str | None = None, days: int | None = None) -> None:
        """Typed IDs ban at once, since the person listed them. Filters show who they match first, and nothing happens until someone presses Ban. A number from 0 to 7 at the start is how many days of messages go too."""
        if days is not None and not 0 <= days <= MESSAGE_DAYS_MOST:
            await ctx.fail("Message days must be between 0 and 7.")
            return
        await ctx.defer(ephemeral=True)
        words = " ".join(part for part in (users, reason) if part)
        first, _, after = words.partition(" ")
        typed_days = whole_number(first, MESSAGE_DAYS_MOST)
        if days is None and typed_days is not None and after.strip():
            days, words = typed_days, after.strip()
        ids, rest = leading_ids(words)
        if not ids:
            await self._massban_by_filter(ctx, words, days)
            return
        guild = ctx.guild
        reason = clip(rest, REASON_MAX)
        try:
            members = await self._members_by_id(guild, ids)
        except ModerationError as error:
            await ctx.fail(str(error))
            return
        targets: list[Any] = []
        refused: list[str] = []
        for user_id in ids:
            target = members.get(user_id) or self.bot.get_user(user_id) or Someone(user_id)
            try:
                self.check_target(guild, target, ctx.author, "ban")
                await self._check_hardmuted(guild, target, ctx.author)
            except ModerationError as error:
                refused.append(str(error))
                continue
            targets.append(target)
        await ctx.ok(await self._ban_and_count(guild, ctx.author, targets, len(ids), refused, reason, days))

    async def run_massban(self, guild: Any, moderator: Any, matched: list[Any], reason: str | None, days: int | None = None) -> str:
        """What a press of the preview's button does."""
        targets: list[Any] = []
        refused: list[str] = []
        for target in matched:
            try:
                self.check_target(guild, target, moderator, "ban")
                await self._check_hardmuted(guild, target, moderator)
            except ModerationError as error:
                refused.append(str(error))
                continue
            targets.append(target)
        return await self._ban_and_count(guild, moderator, targets, len(matched), refused, clip(reason, REASON_MAX), days)

    async def _ban_and_count(self, guild: Any, moderator: Any, targets: list[Any], total: int, refused: list[str], reason: str | None, days: int | None = None) -> str:
        """Ban them, record each, and say how it went. A big run's modlog posts take minutes at Discord's pace, so those carry on after the answer."""
        banned = await self._bulk_ban(guild, targets, audit_reason(moderator, reason, "massban"), (self.cfg(guild.id)["ban"]["days"] if days is None else days) * DAY)
        rows = [await self.record_case(guild, "ban", target, moderator, reason=reason, post=False) for target in banned]
        posted = True
        if len(rows) > POSTS_INLINE:
            self._in_background(self._post_cases(guild, rows))
        else:
            posted = await self._post_cases(guild, rows)
        await self.bot.activity_log.record(guild.id, self.bot.actor(moderator), "massban", detail=f"{len(rows)} of {total}")
        line = f"Banned {len(rows)} of {total}."
        if rows:
            line += f" Cases {rows[0]['number']} to {rows[-1]['number']}." if len(rows) > 1 else f" Case {rows[0]['number']}."
        failed = len(targets) - len(rows)
        if failed:
            line += f" {failed} didn't go through, because they're already banned or Discord refused."
        if refused:
            line += " Skipped: " + " ".join(refused[:5])
        if not posted:
            line += " They couldn't post to the modlog. `/modlog repost` tries again."
        return line[:messages.CONTENT_MAX - 10]

    async def _massban_by_filter(self, ctx: Any, words: str, days: int | None = None) -> None:
        try:
            filters, reason = parse_ban_filters(words)
            if not filters:
                raise ModerationError(f"List at least one ID or mention to ban, or use a filter. {FILTER_HELP}")
            if not ctx.guild.me.guild_permissions.ban_members:
                raise ModerationError("I need the Ban Members permission to do that.")
            matched, left_out = await self._ban_matches(ctx, filters)
        except ModerationError as error:
            await ctx.fail(str(error))
            return
        if not matched:
            await ctx.ok("Nobody matches that, so nothing happened." + (f" {left_out} who matched are staff or at or above your rank, and those are always left out." if left_out else ""))
            return
        if len(matched) > MASSBAN_MOST:
            await ctx.fail(f"That matches {len(matched)} people, over the limit of {MASSBAN_MOST} for one run. Narrow the filters. Nothing happened.")
            return
        reason = reason or ("Raid" if "raid" in filters else None)
        shown = "\n".join(f"{escape(name_of(target))} ({target.id})" for target in matched[:MASSBAN_LISTED])
        extra = f"\n...and {len(matched) - MASSBAN_LISTED} more, listed in the file." if len(matched) > MASSBAN_LISTED else ""
        note = f"\n{left_out} who matched are staff or at or above your rank, and those are left out." if left_out else ""
        text_ = f"**This bans {len(matched)} {'person' if len(matched) == 1 else 'people'}.** Nothing has happened yet.\nReason: {escape(reason) if reason else 'none given'}\n{shown}{extra}{note}"
        kwargs: dict[str, Any] = {}
        if len(matched) > MASSBAN_LISTED:
            kwargs["file"] = discord.File(io.BytesIO(self._match_list(matched).encode("utf-8")), filename="massban-preview.txt")
        view = MassBanPreview(self, ctx.author, matched, reason, days)
        view.message = await ctx.send(text_[:messages.CONTENT_MAX - 10], view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    @staticmethod
    def _match_list(matched: list[Any]) -> str:
        def when(moment: Any) -> str:
            return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else "unknown"
        return "\n".join(f"{target.id}\tjoined {when(getattr(target, 'joined_at', None))}\tcreated {when(getattr(target, 'created_at', None))}\t{name_of(target)}" for target in matched)

    async def _ban_matches(self, ctx: Any, filters: dict[str, str]) -> tuple[list[Any], int]:
        """Who these filters match, newest join first, and how many more matched who aren't anyone's to ban: staff, and anyone at or above this person's rank."""
        guild = ctx.guild
        checks = await self._member_checks(guild, filters)
        people: dict[int, Any]
        if "contains" in filters or "channel" in filters:
            people = await self._authors_matching(ctx, filters)
        elif "raid" in filters and not checks:
            people = {row["user_id"]: guild.get_member(row["user_id"]) or Someone(row["user_id"], row["user_name"]) for row in await self.db.all("SELECT user_id, user_name FROM raid_holds WHERE guild_id = ?", (guild.id,))}
        else:
            if not await whole_member_list(guild):
                raise ModerationError("I couldn't load the whole member list, so nothing happened. Try again in a moment.")
            people = {member.id: member for member in guild.members}
        moderator = set(DANGEROUS_PERMISSIONS)
        matched: list[Any] = []
        left_out = 0
        for index, person in enumerate(people.values()):
            if index % FILTER_YIELD_EVERY == FILTER_YIELD_EVERY - 1:
                await asyncio.sleep(0)  # a filter that fits most of a big server takes about a second, and everything else would wait for it
            if getattr(person, "bot", False) or not all(check(person) for check in checks):
                continue
            permissions = getattr(person, "guild_permissions", None)
            try:
                if permissions is not None and {name for name, on in permissions if on} & moderator:
                    raise ModerationError("staff")
                self.check_target(guild, person, ctx.author, "ban")
            except ModerationError:
                left_out += 1
                continue
            matched.append(person)
        first = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        matched.sort(key=lambda person: getattr(person, "joined_at", None) or first, reverse=True)
        return matched, left_out

    async def _member_checks(self, guild: Any, filters: dict[str, str]) -> list[Callable[[Any], bool]]:
        now = discord.utils.utcnow()
        checks: list[Callable[[Any], bool]] = []

        def cutoff(name: str) -> datetime.datetime:
            seconds = parse_duration(filters[name])
            if not seconds:
                raise ModerationError(f"`{name}:{filters[name][:30]}` needs a length of time, such as {name}:10m.")
            return now - datetime.timedelta(seconds=seconds)

        if "joined" in filters:
            joined = cutoff("joined")
            # Someone who posted and left has no join date anymore, and in a raid that's the point of leaving.
            checks.append(lambda person: not hasattr(person, "joined_at") or (person.joined_at is not None and person.joined_at > joined))
        if "created" in filters:
            created = cutoff("created")
            checks.append(lambda person: person.created_at > created)
        if "after" in filters:
            ids, _ = leading_ids(filters["after"])
            first = guild.get_member(ids[0]) if ids else None
            if first is None or first.joined_at is None:
                raise ModerationError("`after:` needs a mention or ID of someone in the server.")
            checks.append(lambda person: getattr(person, "joined_at", None) is not None and person.joined_at > first.joined_at)
        if "name" in filters:
            pattern = filters["name"]
            if not pattern.strip("*"):
                raise ModerationError("`name:` needs some text, such as name:free*nitro.")
            checks.append(lambda person: name_fits(pattern, person.name, getattr(person, "global_name", None), getattr(person, "nick", None)))
        if "noavatar" in filters:
            checks.append(lambda person: person.avatar is None)
        if "noroles" in filters:
            checks.append(lambda person: len(getattr(person, "roles", [])) <= 1)
        if "raid" in filters:
            held = {row["user_id"] for row in await self.db.all("SELECT user_id FROM raid_holds WHERE guild_id = ?", (guild.id,))}
            if checks or "contains" in filters or "channel" in filters:
                checks.append(lambda person: person.id in held)
        return checks

    async def _authors_matching(self, ctx: Any, filters: dict[str, str]) -> dict[int, Any]:
        """Who posted matching messages in a channel. That's text a person might not be able to read, so they have to be able to read the channel."""
        guild = ctx.guild
        if not self.bot.reads_message_text:
            raise ModerationError("Discord hasn't given this bot Message Content, so it can't search what members typed. Use the other filters.")
        channel = ctx.channel
        if "channel" in filters:
            found = _CHANNEL_WORD.fullmatch(filters["channel"])
            digits = re.sub(r"\D", "", filters["channel"]) if found else ""
            channel = guild.get_channel_or_thread(int(digits)) if digits else discord.utils.get(guild.channels, name=filters["channel"].lstrip("#"))
            if channel is None:
                raise ModerationError(f"I can't find a channel called `{filters['channel'][:30]}`, so nothing happened.")
        refused = access.read_refusal(ctx.author, channel)
        if refused:
            raise ModerationError(refused)
        limit = MASSBAN_SEARCH_DEFAULT
        if "search" in filters:
            limit = whole_number(filters["search"], MASSBAN_SEARCH_MAX) or 0
            if not limit:
                raise ModerationError(f"`search:` takes a number of messages from 1 to {MASSBAN_SEARCH_MAX}.")
        needle = filters.get("contains", "").casefold()
        people: dict[int, Any] = {}
        try:
            async for message in channel.history(limit=limit):
                if needle in (message.content or "").casefold():
                    people[message.author.id] = guild.get_member(message.author.id) or message.author
        except discord.HTTPException as error:
            raise ModerationError(f"Discord refused that: {error.text or error.status}") from error
        return people

    async def _ban_one_by_one(self, guild: Any, targets: list[Any], audit: str, purge_seconds: int) -> set[int]:
        done: set[int] = set()
        for target in targets:
            try:
                await self._check_not_banned(guild, target, None, "ban")
                await guild.ban(discord.Object(target.id), reason=audit, delete_message_seconds=purge_seconds)
            except (ModerationError, discord.HTTPException):
                continue
            done.add(target.id)
        return done

    async def _bulk_ban(self, guild: Any, targets: list[Any], audit: str, purge_seconds: int) -> list[Any]:
        """Discord's bulk route takes 200 at a time, needs Manage Server as well and refuses anyone already banned.
        One at a time works without that permission, and it's how a temporary ban turns permanent."""
        temporary = {row["payload"]["user_id"] for row in await self.bot.timers.list_kind("moderation.unban", guild.id)}
        banned: list[Any] = []
        by_id = {target.id: target for target in targets}
        for start in range(0, len(targets), BULK_BAN_CHUNK):
            if start:
                await asyncio.sleep(BULK_BAN_PAUSE)
            chunk = targets[start:start + BULK_BAN_CHUNK]
            for target in chunk:
                self._expect(guild.id, target.id, "ban")
            others = [t for t in chunk if t.id not in temporary]
            done = await self._ban_one_by_one(guild, [t for t in chunk if t.id in temporary], audit, purge_seconds)
            try:
                if others:
                    result = await guild.bulk_ban([discord.Object(t.id) for t in others], reason=audit, delete_message_seconds=purge_seconds)
                    done |= {entry.id for entry in result.banned}
            except discord.HTTPException as error:
                # This code means nobody on the list could be banned, as when they all already are. Banning them one at a time would record cases for nothing.
                if error.code != BULK_BAN_NOBODY:
                    done |= await self._ban_one_by_one(guild, others, audit, purge_seconds)
            for target in chunk:
                if target.id in done:
                    banned.append(by_id[target.id])
                    self._note_ban(guild.id, target.id)
                    if target.id in temporary:
                        await self.bot.timers.cancel_key("moderation.unban", f"{guild.id}:{target.id}")
                else:
                    self._forget(guild.id, target.id, "ban")
        return banned

    @commands.hybrid_command(name="kick", description="Kick a member")
    @commands.guild_only()
    @requires("kick_members")
    @app_commands.describe(member="Who to kick", reason="Why")
    async def kick(self, ctx: commands.Context, member: OneMember, *, reason: str | None = None) -> None:
        await self._act(ctx, member, "kick", reason=reason)

    @commands.hybrid_command(name="mute", description="Mute a member with the mute role, for a time or until unmuted")
    @commands.guild_only()
    @requires("manage_roles")
    @app_commands.describe(member="Who to mute", duration="How long, such as 20h45m. Leave out for the server's default", reason="Why")
    async def mute(self, ctx: commands.Context, member: OneMember, duration: str | None = None, *, reason: str | None = None) -> None:
        await self._act(ctx, member, "mute", duration=duration, reason=reason)

    @commands.hybrid_command(name="hardmute", description="Mute a member and take their other roles until it ends")
    @commands.guild_only()
    @requires("manage_roles")
    @app_commands.describe(member="Who to hardmute", duration="How long, such as 20h45m. Leave out for the server's default", reason="Why")
    async def hardmute(self, ctx: commands.Context, member: OneMember, duration: str | None = None, *, reason: str | None = None) -> None:
        await self._act(ctx, member, "hardmute", duration=duration, reason=reason)

    @commands.hybrid_command(name="unmute", description="Unmute a member")
    @commands.guild_only()
    @requires("manage_roles")
    @app_commands.describe(member="Who to unmute. Someone who left can be named by ID", reason="Why")
    async def unmute(self, ctx: commands.Context, member: OnePerson, *, reason: str | None = None) -> None:
        await self._act(ctx, member, "unmute", reason=reason)

    @commands.hybrid_command(name="timeout", description="Time a member out with Discord's own timeout")
    @commands.guild_only()
    @requires("moderate_members")
    @app_commands.describe(member="Who to time out", duration="How long, up to 28 days. Leave out for the server's default", reason="Why")
    async def timeout(self, ctx: commands.Context, member: OneMember, duration: str | None = None, *, reason: str | None = None) -> None:
        await self._act(ctx, member, "timeout", duration=duration, reason=reason)

    @commands.hybrid_command(name="untimeout", aliases=["removetimeout"], description="Remove a member's timeout")
    @commands.guild_only()
    @requires("moderate_members")
    @app_commands.describe(member="Whose timeout to remove", reason="Why")
    async def untimeout(self, ctx: commands.Context, member: OneMember, *, reason: str | None = None) -> None:
        await self._act(ctx, member, "untimeout", reason=reason)

    @commands.hybrid_command(name="setnick", description="Change a member's nickname, or clear it")
    @commands.guild_only()
    @requires("manage_nicknames")
    @app_commands.describe(member="Whose nickname to change", name="The new nickname. Leave out to clear it")
    async def setnick(self, ctx: commands.Context, member: OneMember, *, name: str | None = None) -> None:
        if name is not None and len(name) > 32:
            await ctx.fail("A nickname can be at most 32 characters.")
            return
        if member.id == ctx.guild.owner_id:
            await ctx.fail("Discord doesn't let a bot change the server owner's nickname.")
            return
        author_top = getattr(ctx.author, "top_role", None)
        if ctx.author.id != ctx.guild.owner_id and ctx.author.id != member.id and author_top is not None and member.top_role >= author_top:
            await ctx.fail(f"{escape(name_of(member))}'s top role is at or above yours, so you can't change their nickname.")
            return
        if member.id != ctx.guild.me.id and member.top_role >= ctx.guild.me.top_role:
            await ctx.fail(f"{escape(name_of(member))}'s top role is at or above mine, so I can't change their nickname.")
            return
        try:
            await member.edit(nick=name, reason=audit_reason(ctx.author, None, "setnick"))
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused that: {error.text or error.status}")
            return
        await ctx.ok(f"Set {escape(name_of(member))}'s nickname to {escape(name)}." if name else f"Cleared {escape(name_of(member))}'s nickname.")

    # ---- warnings ----

    @commands.hybrid_command(name="warn", description="Warn a member")
    @commands.guild_only()
    @requires("manage_roles")
    @app_commands.describe(member="Who to warn", reason="Why")
    async def warn(self, ctx: commands.Context, member: OneMember, *, reason: str | None = None) -> None:
        await self._act(ctx, member, "warn", reason=reason)

    @commands.hybrid_command(name="warns", description="List a member's warnings, or the newest in the server")
    @commands.guild_only()
    @requires("manage_roles")
    @app_commands.describe(member="Whose warnings to list. Leave out for the whole server")
    async def warns(self, ctx: commands.Context, *, member: OnePerson = None) -> None:
        guild_id = ctx.guild.id
        if member is None:
            rows = await self.db.all("SELECT * FROM cases WHERE guild_id = ? AND action = 'warn' AND voided_at IS NULL ORDER BY number DESC LIMIT 100", (guild_id,))
            embed = self._case_list("Newest warnings", rows, COLORS["warn"], show_user=True, link=None, empty="This server has no warnings.")
        else:
            rows = await self.db.all("SELECT * FROM cases WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND voided_at IS NULL ORDER BY number DESC", (guild_id, member.id))
            embed = self._case_list(f"Warnings for {name_of(member)}", rows, COLORS["warn"], show_user=False, link=self._member_link(guild_id, member.id), empty="They have no warnings.")
        await send_private(ctx, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="removewarning", aliases=["removewarn"], description="Remove a warning by its case number")
    @commands.guild_only()
    @requires("manage_roles")
    @app_commands.describe(case="The warning's case number")
    async def removewarning(self, ctx: commands.Context, case: int) -> None:
        row = await self.get_case(ctx.guild.id, case)
        if row is None or row["action"] != "warn" or row["voided"]:
            await ctx.fail(f"Case {case} isn't a current warning.")
            return
        if await self._own_record(ctx, row["user_id"]):
            return
        await self.void_case(ctx.guild.id, case, ctx.author, "Warning removed")
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "case.void", detail=f"Case {case}")
        await ctx.ok(f"Removed the warning in case {case}.")

    @commands.hybrid_command(name="clearwarnings", aliases=["clearwarn", "clearwarns"], description="Remove all of a member's warnings")
    @commands.guild_only()
    @requires("manage_roles")
    @app_commands.describe(member="Whose warnings to remove")
    async def clearwarns(self, ctx: commands.Context, *, member: OnePerson) -> None:
        await ctx.defer(ephemeral=True)
        if await self._own_record(ctx, member.id):
            return
        rows = await self.db.all("SELECT number FROM cases WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND voided_at IS NULL", (ctx.guild.id, member.id))
        for row in rows:
            await self.void_case(ctx.guild.id, row["number"], ctx.author, "Warnings cleared")
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "case.void", detail=f"{len(rows)} warnings of {member.id}")
        await ctx.ok(f"Removed {len(rows)} warning{'' if len(rows) == 1 else 's'} from {escape(name_of(member))}.")

    # ---- notes ----

    @commands.hybrid_group(name="notes", description="Private notes about members that only staff can read")
    @commands.guild_only()
    @requires("manage_guild")
    async def notes(self, ctx: commands.Context, *, member: OnePerson = None) -> None:
        if member is None:
            await ctx.fail("Name a member to see their notes.")
            return
        await self._show_notes(ctx, member)

    async def _show_notes(self, ctx: Any, member: Any) -> None:
        rows = await self._notes_of(ctx.guild.id, member.id)
        lines = [f"**#{row['number']}** by {escape(row['author_name'] or 'Unknown')}, <t:{int(row['created_at'])}:R>\n{row['text']}" for row in rows]
        body = "\n".join(lines)
        if len(body) > messages.DESCRIPTION_MAX:
            body = body[:messages.DESCRIPTION_MAX - 20] + "\n...and more."
        embed = discord.Embed(title=f"Notes for {name_of(member)}", description=body or "They have no notes.", color=0x4286F4)
        await send_private(ctx, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _set_note(self, ctx: Any, member: Any, note: str) -> None:
        if len(note) > NOTE_MAX:
            await ctx.fail(f"A note can be at most {NOTE_MAX} characters.")
            return
        if await self._own_record(ctx, member.id):
            return
        row = await self.add_note(ctx.guild.id, member.id, ctx.author, note)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "note.add", detail=f"Note {row['number']}")
        await ctx.ok(f"Saved note #{row['number']} on {escape(name_of(member))}.")

    async def _remove_note(self, ctx: Any, note_id: int) -> None:
        note = await self.db.one("SELECT user_id FROM notes WHERE guild_id = ? AND number = ?", (ctx.guild.id, note_id)) if note_id <= SQLITE_INT_MAX else None
        if note is not None and await self._own_record(ctx, note["user_id"]):
            return
        if await self.remove_note(ctx.guild.id, note_id):
            await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "note.remove", detail=f"Note {note_id}")
            await ctx.ok(f"Removed note #{note_id}.")
        else:
            await ctx.fail(f"There is no note #{note_id}.")

    async def _clear_notes(self, ctx: Any, member: Any) -> None:
        if await self._own_record(ctx, member.id):
            return
        count = await self.db.run("DELETE FROM notes WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "note.remove", detail=f"{count} notes of {member.id}")
        await ctx.ok(f"Removed {count} note{'' if count == 1 else 's'} from {escape(name_of(member))}.")

    @notes.command(name="setnote", description="Add a note about a member")
    @requires("manage_guild")
    @app_commands.describe(member="Who the note is about", note="The note")
    async def notes_setnote(self, ctx: commands.Context, member: OnePerson, *, note: str) -> None:
        await self._set_note(ctx, member, note)

    @notes.command(name="view", description="Show a member's notes")
    @requires("manage_guild")
    @app_commands.describe(member="Whose notes to show")
    async def notes_view(self, ctx: commands.Context, *, member: OnePerson) -> None:
        await self._show_notes(ctx, member)

    @notes.command(name="removenote", description="Remove a note by its number")
    @requires("manage_guild")
    @app_commands.describe(note_id="The note's number")
    async def notes_removenote(self, ctx: commands.Context, note_id: int) -> None:
        await self._remove_note(ctx, note_id)

    @notes.command(name="clearnotes", description="Remove all of a member's notes")
    @requires("manage_guild")
    @app_commands.describe(member="Whose notes to remove")
    async def notes_clearnotes(self, ctx: commands.Context, *, member: OnePerson) -> None:
        await self._clear_notes(ctx, member)

    # Staff type these three without the group name today. Text only, since slash commands have the group.
    @commands.command(name="setnote", extras={"rules_of": "notes setnote"}, description="Add a note about a member")
    @commands.guild_only()
    @requires("manage_guild")
    async def setnote_text(self, ctx: commands.Context, member: OnePerson, *, note: str) -> None:
        await self._set_note(ctx, member, note)

    @commands.command(name="removenote", extras={"rules_of": "notes removenote"}, description="Remove a note by its number")
    @commands.guild_only()
    @requires("manage_guild")
    async def removenote_text(self, ctx: commands.Context, note_id: int) -> None:
        await self._remove_note(ctx, note_id)

    @commands.command(name="clearnotes", extras={"rules_of": "notes clearnotes"}, description="Remove all of a member's notes")
    @commands.guild_only()
    @requires("manage_guild")
    async def clearnotes_text(self, ctx: commands.Context, *, member: OnePerson) -> None:
        await self._clear_notes(ctx, member)

    # ---- modlog ----

    @commands.hybrid_group(name="modlog", description="Set up the modlog and read its cases")
    @commands.guild_only()
    @requires("manage_guild")
    async def modlog(self, ctx: commands.Context, *, member: OnePerson = None) -> None:
        if member is not None:
            await self._show_cases(ctx, member)
            return
        channel_id = self.cfg(ctx.guild.id)["modlog_channel"]
        await ctx.send(f"The modlog is {f'<#{channel_id}>' if channel_id else 'not set'}. Name a member to see their cases.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _show_cases(self, ctx: Any, member: Any) -> None:
        rows = await self.db.all("SELECT * FROM cases WHERE guild_id = ? AND user_id = ? AND voided_at IS NULL ORDER BY number DESC", (ctx.guild.id, member.id))
        embed = self._case_list(f"Cases for {name_of(member)}", rows, 0x4286F4, show_user=False, link=self._member_link(ctx.guild.id, member.id), empty="They have no cases.")
        await send_private(ctx, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @modlog.command(name="create", description="Make a private channel for the modlog")
    @requires("manage_guild")
    @app_commands.describe(name="The channel's name")
    async def modlog_create(self, ctx: commands.Context, *, name: str = "mod-log") -> None:
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
        }
        try:
            channel = await guild.create_text_channel(name, overwrites=overwrites, reason=audit_reason(ctx.author, None, "modlog create"))
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused to make the channel: {error.text or error.status}")
            return
        await self.bot.settings.save(guild.id, self.key, {"modlog_channel": channel.id}, actor=self.bot.actor(ctx.author), action="modlog.create")
        await ctx.ok(f"Made {channel.mention}, and cases now post there.")

    @modlog.command(name="set", description="Post cases in this channel")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel for cases")
    async def modlog_set(self, ctx: commands.Context, *, channel: discord.TextChannel) -> None:
        mine = channel.permissions_for(ctx.guild.me)
        if not mine.send_messages or not mine.embed_links:
            await ctx.fail(f"I need View Channel, Send Messages and Embed Links in {channel.mention} to post cases there.")
            return
        refused = access.post_refusal(ctx.author, channel)
        if refused:
            await ctx.fail(refused)
            return
        await self.bot.settings.save(ctx.guild.id, self.key, {"modlog_channel": channel.id}, actor=self.bot.actor(ctx.author), action="modlog.set")
        await ctx.ok(f"Cases now post in {channel.mention}.")

    @modlog.command(name="repost", description="Post the cases that never reached the modlog")
    @requires("manage_guild")
    async def modlog_repost(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        rows = await self.db.all("SELECT * FROM (SELECT * FROM cases WHERE guild_id = ? AND message_id IS NULL AND voided_at IS NULL ORDER BY number DESC LIMIT ?) ORDER BY number", (ctx.guild.id, REPOST_MOST))
        rows = [self._decode(row) for row in rows]
        if not rows:
            await ctx.ok("Every case already has a post.")
            return
        worked = await self._post_cases(ctx.guild, rows)
        count = sum(1 for row in rows if row["message_id"])
        if not count and worked:
            await ctx.ok("Every case already has a post, or its event is switched off.")
            return
        await ctx.ok(f"Posted {count} case{'' if count == 1 else 's'}." + ("" if worked else " Some still couldn't post. Check that I can post and embed links in the modlog channel."))

    @modlog.command(name="clear", description="Stop posting cases")
    @requires("manage_guild")
    async def modlog_clear(self, ctx: commands.Context) -> None:
        await self.bot.settings.save(ctx.guild.id, self.key, {"modlog_channel": None}, actor=self.bot.actor(ctx.author), action="modlog.clear")
        await ctx.ok("Cases are still recorded, and no longer post anywhere.")

    @modlog.command(name="from", description="List a member's cases and who handled them")
    @requires("manage_guild")
    @app_commands.describe(member="Whose cases to list")
    async def modlog_from(self, ctx: commands.Context, *, member: OnePerson) -> None:
        await self._show_cases(ctx, member)

    async def _change_reason(self, ctx: Any, case: int, reason: str) -> None:
        await ctx.defer(ephemeral=True)
        row = await self.get_case(ctx.guild.id, case)
        if row is not None and await self._own_record(ctx, row["user_id"]):
            return
        result = await self.set_reason(ctx.guild.id, case, reason, ctx.author)
        if result is None:
            await ctx.fail(f"There is no case {case}.")
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "case.reason", detail=f"Case {case}")
        await ctx.ok(f"Updated the reason on case {case}." + ("" if result[1] or not result[0]["message_id"] else " Its modlog post couldn't be changed."))

    @modlog.command(name="reason", description="Set a case's reason, here and on its modlog post")
    @requires("manage_guild")
    @app_commands.describe(case="The case number", reason="The new reason")
    async def modlog_reason(self, ctx: commands.Context, case: int, *, reason: str) -> None:
        await self._change_reason(ctx, case, reason)

    @commands.command(name="reason", extras={"rules_of": "modlog reason"}, description="Set a case's reason, here and on its modlog post")
    @commands.guild_only()
    @requires("manage_guild")
    async def reason_text(self, ctx: commands.Context, case: int, *, reason: str) -> None:
        await self._change_reason(ctx, case, reason)

    async def _highscores(self, guild_id: int, *, since: float = 0, bots: bool = False, limit: int = 100) -> list[dict]:
        bot_filter = "" if bots else "AND moderator_bot = 0"
        return await self.db.all(
            "SELECT moderator_id, MAX(moderator_name) AS moderator_name, MAX(moderator_bot) AS moderator_bot, COUNT(*) AS total, "
            "SUM(action = 'warn') AS warn, SUM(action IN ('mute', 'hardmute', 'minimute')) AS mute, SUM(action = 'timeout') AS timeout, SUM(action = 'kick') AS kick, "
            "SUM(action IN ('ban', 'tempban', 'softban')) AS ban, SUM(action IN ('unmute', 'unminimute', 'unban', 'untimeout')) AS lifted, MAX(created_at) AS last_at "
            f"FROM cases WHERE guild_id = ? AND voided_at IS NULL AND moderator_id IS NOT NULL AND source != '{AUTOMATIC_SOURCE}' AND created_at >= ? {bot_filter} "
            "GROUP BY moderator_id ORDER BY total DESC LIMIT ?",
            (guild_id, since, limit),
        )

    @modlog.command(name="highscores", description="Rank moderators by how many actions they've taken")
    @requires("manage_guild")
    async def modlog_highscores(self, ctx: commands.Context) -> None:
        rows = await self._highscores(ctx.guild.id, limit=20)
        lines = [f"{index}. {escape(row['moderator_name'])}: {row['total']}" for index, row in enumerate(rows, 1)]
        embed = discord.Embed(title="Moderators by actions taken", description="\n".join(lines) or "No actions yet.", color=0x4286F4)
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @modlog.command(name="export", description="Get a text file of the cases for one or two members")
    @requires("manage_guild")
    @app_commands.describe(member="Whose cases to export", other="A second member")
    async def modlog_export(self, ctx: commands.Context, member: OnePerson, other: OnePerson = None) -> None:
        people = [member, *([other] if other else [])]
        marks = ",".join("?" * len(people))
        rows = await self.db.all(f"SELECT * FROM cases WHERE guild_id = ? AND user_id IN ({marks}) ORDER BY number", (ctx.guild.id, *[p.id for p in people]))
        lines = []
        for row in rows:
            when = datetime.datetime.fromtimestamp(row["created_at"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            length = f" for {humanize(row['seconds'])}" if row["seconds"] else ""
            voided = " (voided)" if row["voided_at"] else ""
            lines.append(f"Case {row['number']}{voided}: {row['action']}{length}, {when}\nOffender: {row['user_name']} ({row['user_id']})\nModerator: {self._moderator_label(row)}\nReason: {row['reason'] or 'None given'}\n")
        if not lines:
            await ctx.fail("No cases to export.")
            return
        data = io.BytesIO("\n".join(lines).encode("utf-8"))
        await send_private(ctx, f"{len(rows)} case{'' if len(rows) == 1 else 's'}.", file=discord.File(data, filename=f"modlog-{member.id}.txt"), allowed_mentions=discord.AllowedMentions.none())

    # ---- mute role ----

    def _mute_overwrite(self, channel: Any, role: Any, left_out: set[str] | None = None) -> Any:
        """The channel's overwrite for the mute role with the mute's limits added, or None when it already has them. Discord refuses a deny for a bit the bot doesn't hold in the server, and one such bit would fail the whole write, so those go in `left_out`."""
        overwrite = channel.overwrites_for(role)
        held = channel.guild.me.guild_permissions
        changed = False
        for name in MUTE_DENIED:
            if getattr(overwrite, name) is False:
                continue
            if not getattr(held, name):
                if left_out is not None:
                    left_out.add(name)
                continue
            setattr(overwrite, name, False)
            changed = True
        return overwrite if changed else None

    async def _apply_mute_overwrite(self, channel: Any, role: Any, reason: str, left_out: set[str] | None = None) -> bool | None:
        """True if changed, None if it was already right or the bot can't manage this channel's permissions, False if Discord refused."""
        if not channel.permissions_for(channel.guild.me).manage_roles:
            return None
        overwrite = self._mute_overwrite(channel, role, left_out)
        if overwrite is None:
            return None
        try:
            await channel.set_permissions(role, overwrite=overwrite, reason=reason)
        except discord.HTTPException as error:
            log.info("Could not set the mute role's limits in %s: %s", channel.id, error)
            return False
        return True

    def _mute_limits_note(self, guild: Any, left_out: set[str]) -> str:
        """What the reply adds: limits the bot couldn't set, and channels where a role's own allow beats the mute role."""
        line = f" I don't hold {', '.join(sorted(left_out))} in this server, so the role doesn't limit those. Give me those permissions and run this again." if left_out else ""
        allowed = self._channels_with_send_allow(guild)
        if allowed:
            line += f" A role holds its own Send Messages allow in {self._channel_list(allowed)}, which beats the mute role for anyone holding it."
        return line

    def _channels_with_send_allow(self, guild: Any) -> list[Any]:
        """Channels where a role without moderator permissions holds its own Send Messages allow."""
        moderator = set(DANGEROUS_PERMISSIONS)
        out = []
        for channel in guild.channels:
            if channel.type == discord.ChannelType.category:
                continue
            for target, overwrite in (getattr(channel, "overwrites", {}) or {}).items():
                if overwrite.send_messages is True and self._is_role(target) and not target.managed and not {name for name, on in target.permissions if on} & moderator:
                    out.append(channel)
                    break
        return out

    @staticmethod
    def _is_role(target: Any) -> bool:
        return hasattr(target, "permissions") and not hasattr(target, "top_role")

    async def apply_mute_everywhere(self, guild: Any, role: Any, reason: str, left_out: set[str] | None = None) -> tuple[int, int]:
        changed = failed = 0
        for channel in guild.channels:
            result = await self._apply_mute_overwrite(channel, role, reason, left_out)
            if result is True:
                changed += 1
            elif result is False:
                failed += 1
        return changed, failed

    @commands.hybrid_group(name="muterole", description="Choose, make or update the role that mutes members")
    @commands.guild_only()
    @requires("manage_roles")
    @app_commands.default_permissions(manage_roles=True)
    async def muterole(self, ctx: commands.Context, *, role: discord.Role | None = None) -> None:
        if role is not None:
            await self._set_mute_role(ctx, role)
            return
        role_id = self.cfg(ctx.guild.id)["mute_role"]
        await ctx.send(f"The mute role is {f'<@&{role_id}>' if role_id else 'not set'}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _set_mute_role(self, ctx: Any, role: Any) -> None:
        problem = self._mute_role_problem(ctx.guild, role)
        if problem:
            await ctx.fail(problem)
            return
        # Every later mute hands this role out, and the dashboard refuses the same thing.
        if not access.is_admin(ctx.author) and role >= ctx.author.top_role:
            await ctx.fail(f"{role.name} sits at or above your highest role, so you can't set it up to be handed out.")
            return
        await self.bot.settings.save(ctx.guild.id, self.key, {"mute_role": role.id}, actor=self.bot.actor(ctx.author), action="muterole.set")
        await ctx.ok(f"{role.mention} is now the mute role. Use `/muterole update` to apply its limits in every channel.")

    @muterole.command(name="create", description="Make a mute role and set its limits in every channel")
    @requires("manage_guild", "manage_channels", "manage_roles")
    @app_commands.describe(name="The role's name")
    async def muterole_create(self, ctx: commands.Context, *, name: str = "Muted") -> None:
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        audit = audit_reason(ctx.author, None, "muterole create")
        try:
            role = await guild.create_role(name=name, permissions=discord.Permissions.none(), reason=audit)
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused to make the role: {error.text or error.status}")
            return
        await self.bot.settings.save(guild.id, self.key, {"mute_role": role.id}, actor=self.bot.actor(ctx.author), action="muterole.create")
        left_out: set[str] = set()
        changed, failed = await self.apply_mute_everywhere(guild, role, audit, left_out)
        await ctx.ok((f"Made {role.mention} and set its limits in {changed} channels." + (f" {failed} channels refused." if failed else "") + self._mute_limits_note(guild, left_out))[:messages.CONTENT_MAX - 10])

    @muterole.command(name="set", description="Use a role you already have as the mute role")
    @requires("manage_roles")
    @app_commands.describe(role="The role")
    async def muterole_set(self, ctx: commands.Context, *, role: discord.Role) -> None:
        await self._set_mute_role(ctx, role)

    @muterole.command(name="update", description="Set the mute role's limits in every channel again")
    @requires("manage_guild", "manage_channels", "manage_roles")
    async def muterole_update(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        role_id = self.cfg(ctx.guild.id)["mute_role"]
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is None:
            await ctx.fail("This server has no mute role yet.")
            return
        left_out: set[str] = set()
        changed, failed = await self.apply_mute_everywhere(ctx.guild, role, audit_reason(ctx.author, None, "muterole update"), left_out)
        await ctx.ok((f"Updated {changed} channels." + (f" {failed} channels refused." if failed else "") + self._mute_limits_note(ctx.guild, left_out))[:messages.CONTENT_MAX - 10])

    # ---- lockdown ----

    def _public_roles(self, guild: Any) -> list[Any]:
        """Roles enough of the server's people hold to count as the public. A role for bots or one with moderator permissions never does, however small the server."""
        people = [member for member in guild.members if not member.bot]
        holders = Counter(role.id for member in people for role in member.roles)
        floor = max(1.0, len(people) * PUBLIC_ROLE_SHARE)
        moderator = set(DANGEROUS_PERMISSIONS)
        return [role for role in guild.roles if not role.is_default() and not role.managed and holders[role.id] >= floor and not {name for name, on in role.permissions if on} & moderator]

    @staticmethod
    def _role_sees(channel: Any, role: Any, everyone: Any) -> bool:
        """What someone holding only this role gets, in Discord's order: the role's overwrite, then @everyone's, then the server-wide permissions."""
        for overwrite in (channel.overwrites_for(role), channel.overwrites_for(everyone)):
            if overwrite.view_channel is not None:
                return overwrite.view_channel
        return role.permissions.view_channel or everyone.permissions.view_channel

    def _lockable_channels(self, guild: Any, public: list[Any]) -> list[Any]:
        """Channels the public can see, whether through @everyone or a role most of them hold. Denying @everyone in a hidden staff channel would silence staff who only hold Send Messages as a server permission."""
        everyone = guild.default_role
        out = []
        for channel in guild.channels:
            if channel.type == discord.ChannelType.category:
                continue
            seen = channel.overwrites_for(everyone).view_channel
            if seen is True or (seen is None and everyone.permissions.view_channel) or any(self._role_sees(channel, role, everyone) for role in public):
                out.append(channel)
        return out

    @staticmethod
    def _channel_list(channels: list[Any]) -> str:
        shown = ", ".join(channel.mention for channel in channels[:SKIPPED_LISTED])
        return shown + (f" and {len(channels) - SKIPPED_LISTED} more" if len(channels) > SKIPPED_LISTED else "")

    def _staff_role_ids(self, guild_id: int) -> set[int]:
        """The roles this server names as its staff: the bot manager role and the dashboard's staff list."""
        manager = self.bot.settings.get(guild_id, COMMANDS_MODULE)["mod_role"]
        return {*self.bot.settings.get(guild_id, "core")["dashboard_staff_roles"], *([manager] if manager else [])}

    @staticmethod
    def _roles_people_hold(guild: Any) -> set[int]:
        return {role.id for member in guild.members if not member.bot for role in member.roles}

    def _role_allows_to_clear(self, guild: Any, channel: Any, fields: list[str], staff: set[int], humans: set[int]) -> list[tuple[Any, list[str]]]:
        """Discord merges role overwrites with allow over deny, so a member role's own allow beats the lockdown's deny on @everyone. Staff, moderator and bot roles keep theirs."""
        moderator = set(DANGEROUS_PERMISSIONS)
        found = []
        for target, overwrite in (getattr(channel, "overwrites", {}) or {}).items():
            if not self._is_role(target) or target.is_default() or target.managed or target.id in staff or target.id not in humans:
                continue
            if {name for name, on in target.permissions if on} & moderator:
                continue
            held = [name for name in fields if getattr(overwrite, name) is True]
            if held:
                found.append((target, held))
        return found

    async def _lock_channel(self, guild: Any, channel: Any, moderator: Any, reason: str | None, seconds: int | None, *, humans: set[int] | None = None) -> None:
        """Deny @everyone first, since nothing else should change when that's refused. The stored row then knows how to undo the rest, so one more refusal only costs that one change."""
        everyone = guild.default_role
        me = guild.me
        audit = audit_reason(moderator, reason, "lockdown")
        row = await self.db.one("SELECT previous FROM lockdowns WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id))
        overwrite = channel.overwrites_for(everyone)
        previous = json.loads(row["previous"]) if row else {}
        # Discord refuses a deny for a bit the bot doesn't hold in the server, and one such bit would fail the whole write. Voice bits mean nothing in other channels.
        voice = channel.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice)
        fields = [name for name in LOCK_FIELDS if (voice or name not in VOICE_LOCK_FIELDS) and (name in previous or getattr(me.guild_permissions, name))]
        if "send_messages" not in fields:
            raise ModerationError("I need the Send Messages permission to lock a channel.")
        for name in fields:
            previous.setdefault(name, getattr(overwrite, name))
            setattr(overwrite, name, False)
        staff = self._staff_role_ids(guild.id)
        cleared = self._role_allows_to_clear(guild, channel, fields, staff, self._roles_people_hold(guild) if humans is None else humans)
        role_allows = previous.setdefault(ROLE_ALLOWS_MARK, {})
        for role, held in cleared:
            role_allows[str(role.id)] = sorted({*role_allows.get(str(role.id), []), *held})
        given = self._staff_allows(guild, channel, fields, previous, staff)
        staff_allows = previous.setdefault(STAFF_ALLOWS_MARK, {})
        for role, grants in given:
            staff_allows.setdefault(str(role.id), {}).update(grants)
        mine = None
        # Without Administrator the bot would lock itself out with everyone else, and couldn't post here until this ends.
        if not me.guild_permissions.administrator and BOT_LOCK_MARK not in previous:
            mine = channel.overwrites_for(me)
            previous[BOT_LOCK_MARK] = mine.send_messages
            mine.send_messages = True
        await channel.set_permissions(everyone, overwrite=overwrite, reason=audit)
        expires_at = time.time() + seconds if seconds else None
        await self.db.run(
            "INSERT INTO lockdowns (guild_id, channel_id, previous, moderator_id, moderator_name, reason, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, channel_id) DO UPDATE SET previous = excluded.previous, moderator_id = excluded.moderator_id, moderator_name = excluded.moderator_name, reason = excluded.reason, "
            "created_at = excluded.created_at, expires_at = excluded.expires_at",
            (guild.id, channel.id, json.dumps(previous), moderator.id if moderator else None, name_of(moderator) if moderator else None, reason, time.time(), expires_at),
        )
        key = f"{guild.id}:{channel.id}"
        if seconds:
            await self.bot.timers.schedule("moderation.unlock", expires_at, {"guild_id": guild.id, "channel_id": channel.id}, guild_id=guild.id, key=key)
        else:
            await self.bot.timers.cancel_key("moderation.unlock", key)
        await self._lock_other_overwrites(channel, cleared, given, mine, me, audit)

    def _staff_allows(self, guild: Any, channel: Any, fields: list[str], previous: dict, staff: set[int]) -> list[tuple[Any, dict[str, Any]]]:
        """What each staff role needs so the deny on @everyone doesn't silence it: only what it could do here before. A role denied here, and a channel @everyone was already shut out of, stay as they are."""
        everyone = guild.default_role
        out = []
        for role_id in staff:
            role = guild.get_role(role_id)
            if role is None or role.is_default():
                continue
            overwrite = channel.overwrites_for(role)
            grants = {name: None for name in fields if getattr(overwrite, name) is None and previous.get(name) is not False and (getattr(role.permissions, name) or getattr(everyone.permissions, name))}
            if grants:
                out.append((role, grants))
        return out

    async def _lock_other_overwrites(self, channel: Any, cleared: list[tuple[Any, list[str]]], given: list[tuple[Any, dict]], mine: Any, me: Any, audit: str) -> None:
        changes: list[tuple[Any, Any]] = []
        for role, held in cleared:
            overwrite = channel.overwrites_for(role)
            for name in held:
                setattr(overwrite, name, None)
            changes.append((role, None if overwrite.is_empty() else overwrite))
        for role, grants in given:
            overwrite = channel.overwrites_for(role)
            for name in grants:
                setattr(overwrite, name, True)
            changes.append((role, overwrite))
        if mine is not None:
            changes.append((me, mine))
        for target, overwrite in changes:
            try:
                await channel.set_permissions(target, overwrite=overwrite, reason=audit)
            except discord.HTTPException as error:
                log.warning("Could not change %s's overwrite in %s during a lockdown: %s", target.id, channel.id, error)

    async def _unlock_channel(self, guild: Any, channel: Any, audit: str) -> bool:
        """Put back what the lockdown changed, leaving any other overwrite someone set meanwhile. False if it wasn't locked."""
        everyone = guild.default_role
        row = await self.db.one("SELECT previous FROM lockdowns WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id))
        overwrite = channel.overwrites_for(everyone)
        if row is None:
            if overwrite.send_messages is not False:
                return False
            previous: dict[str, Any] = {name: None for name in LOCK_FIELDS}
        else:
            previous = json.loads(row["previous"])
        # A row written before a field joined the list never changed that field, so it isn't this row's to clear.
        for name in LOCK_FIELDS:
            if name in previous:
                setattr(overwrite, name, previous[name])
        await channel.set_permissions(everyone, overwrite=None if overwrite.is_empty() else overwrite, reason=audit)
        for role_id, held in previous.get(ROLE_ALLOWS_MARK, {}).items():
            role = guild.get_role(int(role_id))
            if role is not None:
                back = channel.overwrites_for(role)
                for name in held:
                    setattr(back, name, True)
                await channel.set_permissions(role, overwrite=back, reason=audit)
        for role_id, grants in previous.get(STAFF_ALLOWS_MARK, {}).items():
            role = guild.get_role(int(role_id))
            if role is not None:
                back = channel.overwrites_for(role)
                for name, value in grants.items():
                    setattr(back, name, value)
                await channel.set_permissions(role, overwrite=None if back.is_empty() else back, reason=audit)
        if BOT_LOCK_MARK in previous:
            mine = channel.overwrites_for(guild.me)
            mine.send_messages = previous[BOT_LOCK_MARK]
            await channel.set_permissions(guild.me, overwrite=None if mine.is_empty() else mine, reason=audit)
        await self.db.run("DELETE FROM lockdowns WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id))
        await self.bot.timers.cancel_key("moderation.unlock", f"{guild.id}:{channel.id}")
        return True

    async def _lockdown_due(self, payload: dict, row: dict) -> None:
        guild = await self._timer_guild("moderation.unlock", payload, row)
        if guild is None:
            return
        channel = guild.get_channel(payload["channel_id"])
        if channel is None:
            await self.db.run("DELETE FROM lockdowns WHERE guild_id = ? AND channel_id = ?", (guild.id, payload["channel_id"]))
            return
        try:
            await self._unlock_channel(guild, channel, "The lockdown's time ran out")
        except discord.Forbidden:
            await self._tell_stuck(guild, row, f"{channel.mention} is still locked past the end of its lockdown.", "I need Manage Roles and View Channel in that channel.")
            raise

    def _role_exceptions(self, channel: Any) -> str:
        """Who keeps talking through a lockdown, so staff know. Staff and moderator roles hold an allow, and a member role holds one only when clearing it was refused."""
        names = self._roles_still_posting(channel)
        if names:
            return f" These roles can still post: {', '.join(escape(n) for n in names[:10])}."
        return " No role can post here now except Administrators. Staff roles picked on the dashboard keep posting through a lockdown."

    def _roles_still_posting(self, channel: Any) -> list[str]:
        overwrites = getattr(channel, "overwrites", {}) or {}
        return [target.name for target, overwrite in overwrites.items() if overwrite.send_messages is True and self._is_role(target)]

    @staticmethod
    def _lockable_here(ctx: Any, channel: Any) -> Any:
        """A thread has no permissions of its own, so a command run in one locks its parent."""
        return channel or getattr(ctx.channel, "parent", None) or ctx.channel

    async def _lock_one_command(self, ctx: Any, channel: Any, duration: str | None, reason: str | None) -> None:
        await ctx.defer(ephemeral=True)
        if channel is None and ctx.interaction is None and meant_a_channel(duration, reason):
            await ctx.fail(f"I can't find a channel called `{duration}`, so nothing was locked.")
            return
        near = self._near_channel(ctx.guild, duration) if channel is None and ctx.interaction is None else None
        if near is not None:
            await ctx.fail(f"Did you mean {near.mention}? Nothing was locked. If `{duration}` was the reason, word it differently.")
            return
        target = self._lockable_here(ctx, channel)
        refused = access.view_refusal(ctx.author, target)
        if refused:
            await ctx.fail(refused)
            return
        try:
            seconds, why = self._time_and_reason(ctx, duration, reason)
            await self._lock_channel(ctx.guild, target, ctx.author, clip(why, REASON_MAX), seconds)
        except ModerationError as error:
            await ctx.fail(str(error))
            return
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused that: {error.text or error.status}")
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "lockdown.channel", detail=target.name)
        length = f" for {humanize(seconds)}" if seconds else ""
        await ctx.ok(f"Locked {target.mention}{length}.{self._role_exceptions(target)}")

    @staticmethod
    def _near_channel(guild: Any, word: str | None) -> Any:
        """The channel a word nearly names, as `gneral` nearly names general. A word near no channel is the start of a reason, so `.lockdown raid` still works."""
        if not word or len(word) < NEAR_NAME_SHORTEST or parse_duration(word) is not None:
            return None
        channels = {channel.name.lower(): channel for channel in guild.channels if channel.type != discord.ChannelType.category}
        match = difflib.get_close_matches(word.lstrip("#").lower(), list(channels), n=1, cutoff=0.75)
        return channels[match[0]] if match else None

    @commands.hybrid_group(name="lockdown", description="Stop @everyone posting in a channel or the whole server")
    @commands.guild_only()
    @requires("manage_channels")
    async def lockdown(self, ctx: commands.Context, channel: LOCKABLE = None, duration: str | None = None, *, reason: str | None = None) -> None:
        await self._lock_one_command(ctx, channel, duration, reason)

    @lockdown.command(name="channel", description="Stop @everyone posting in a channel")
    @requires("manage_channels")
    @app_commands.describe(channel="The channel. Leave out for this one", duration="How long, such as 2h. Leave out for no end", reason="Why")
    async def lockdown_channel(self, ctx: commands.Context, channel: LOCKABLE = None, duration: str | None = None, *, reason: str | None = None) -> None:
        await self._lock_one_command(ctx, channel, duration, reason)

    @lockdown.command(name="server", description="Stop @everyone posting in every public channel")
    @requires("manage_channels")
    @app_commands.describe(duration="How long, such as 2h. Leave out for no end", reason="Why")
    async def lockdown_server(self, ctx: commands.Context, duration: str | None = None, *, reason: str | None = None) -> None:
        await ctx.defer(ephemeral=True)
        try:
            seconds, why = self._time_and_reason(ctx, duration, reason)
        except ModerationError as error:
            await ctx.fail(str(error))
            return
        public = self._public_roles(ctx.guild)
        lockable = self._lockable_channels(ctx.guild, public)
        humans = self._roles_people_hold(ctx.guild)
        locked = failed = 0
        for channel in lockable:
            try:
                await self._lock_channel(ctx.guild, channel, ctx.author, clip(why, REASON_MAX), seconds, humans=humans)
                locked += 1
            except (discord.HTTPException, ModerationError):
                failed += 1
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "lockdown.server", detail=f"{locked} channels")
        length = f" for {humanize(seconds)}" if seconds else ""
        line = f"Locked {locked} channels{length}." + (f" {failed} refused." if failed else "")
        still_open = [channel for channel in lockable if any(channel.overwrites_for(role).send_messages is True for role in public)]
        if still_open:
            line += f" A role members hold still has its own Send Messages allow in {self._channel_list(still_open)}, so they can still post there."
        keeping = sorted({name for channel in lockable for name in self._roles_still_posting(channel)})
        if keeping:
            line += f" These roles keep posting: {', '.join(escape(name) for name in keeping[:10])}."
        locked_ids = {channel.id for channel in lockable}
        skipped = [channel for channel in ctx.guild.channels if channel.type != discord.ChannelType.category and channel.id not in locked_ids]
        if skipped:
            line += f" Left alone, since only staff or small groups can see them: {self._channel_list(skipped)}."
        await ctx.ok(line[:messages.CONTENT_MAX - 10])

    @lockdown.command(name="setup", description="Remove Send Messages from every role except @everyone")
    @requires("manage_roles")
    @app_commands.describe(confirm="True to make the change. Leave out to see what would change")
    async def lockdown_setup(self, ctx: commands.Context, confirm: bool = False) -> None:
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        # On a server where members post through a role, taking Send Messages from every role would silence everyone.
        if not guild.default_role.permissions.send_messages:
            await ctx.fail("@everyone doesn't hold Send Messages here, so taking it from the other roles would stop everyone posting. Give it to @everyone first.")
            return
        # Discord wouldn't let this person edit a role at or above their own, and the bot doesn't do it for them either.
        is_owner = ctx.author.id == guild.owner_id
        moderator = set(DANGEROUS_PERMISSIONS)
        roles = [r for r in guild.roles if not r.is_default() and not r.managed and r < guild.me.top_role and (is_owner or r < ctx.author.top_role) and r.permissions.send_messages and not {name for name, on in r.permissions if on} & moderator]
        if not roles:
            await ctx.ok("No role below both of us holds Send Messages as a server permission, so there's nothing to change.")
            return
        names = ", ".join(escape(r.name) for r in roles[:30])
        if not confirm:
            allowed = self._channels_with_send_allow(guild)
            allows = f" These channels give a role its own Send Messages allow, which this leaves alone: {self._channel_list(allowed)}. A lockdown clears those for member roles while it runs." if allowed else ""
            await ctx.ok(f"This removes Send Messages from {len(roles)} roles: {names}. Members keep it through @everyone, and roles with moderator permissions aren't touched.{allows} Run it again with confirm to go ahead."[:messages.CONTENT_MAX - 10])
            return
        changed = failed = 0
        audit = audit_reason(ctx.author, None, "lockdown setup")
        done: list[str] = []
        for role in roles:
            permissions = discord.Permissions(**{name: on for name, on in role.permissions})
            permissions.send_messages = False
            try:
                await role.edit(permissions=permissions, reason=audit)
                changed += 1
                done.append(str(role.id))
            except discord.HTTPException:
                failed += 1
        await self.bot.activity_log.record(guild.id, self.bot.actor(ctx.author), "lockdown.setup", detail=f"{changed} roles: {', '.join(done)}")
        await ctx.ok(f"Removed Send Messages from {changed} roles." + (f" {failed} refused." if failed else ""))

    async def _unlock_one_command(self, ctx: Any, channel: Any) -> None:
        await ctx.defer(ephemeral=True)
        unread = unused_words(ctx)
        if unread:
            await ctx.fail(f"I can't find a channel called `{unread.replace('`', '')[:60]}`, so nothing was unlocked.")
            return
        target = self._lockable_here(ctx, channel)
        refused = access.view_refusal(ctx.author, target)
        if refused:
            await ctx.fail(refused)
            return
        try:
            done = await self._unlock_channel(ctx.guild, target, audit_reason(ctx.author, None, "unlockdown"))
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused that: {error.text or error.status}")
            return
        if not done:
            await ctx.fail(f"{target.mention} isn't locked.")
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "lockdown.end", detail=target.name)
        await ctx.ok(f"Unlocked {target.mention}.")

    @lockdown.command(name="list", description="List the locked channels and when they open")
    @requires("manage_channels")
    async def lockdown_list(self, ctx: commands.Context) -> None:
        running = await self.running(ctx.guild.id)
        lines = [
            f"<#{row['channel_id']}> {self._until(row['expires_at'])}, by {escape(row['moderator_name'] or 'Unknown')}" + (f": {escape(row['reason'])}" if row["reason"] else "")
            for row in running["lockdowns"] if access.can_view(ctx.author, ctx.guild.get_channel(row["channel_id"]) or ctx.channel)
        ]
        await self._send_running(ctx, "Locked channels", lines, "No channel is locked.")

    @staticmethod
    def _until(expires_at: float | None) -> str:
        return f"ends <t:{int(expires_at)}:R>" if expires_at else "no end"

    async def _send_running(self, ctx: Any, title: str, lines: list[str], empty: str) -> None:
        shown: list[str] = []
        size = 0
        for index, line in enumerate(lines):
            if size + len(line) > RUNNING_LINES_MAX:
                shown.append(f"...and {len(lines) - index} more. The Running tab on the dashboard's Cases page lists them all.")
                break
            shown.append(line)
            size += len(line) + 1
        await send_private(ctx, embed=discord.Embed(title=title, description="\n".join(shown) or empty, color=0x4286F4), allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="mutes", description="List running mutes and when they end")
    @commands.guild_only()
    @requires("manage_roles")
    async def mutes(self, ctx: commands.Context) -> None:
        running = await self.running(ctx.guild.id)
        lines = [f"{escape(row['name'] or 'Unknown')} ({row['user_id']}) {row['kind']}, {self._until(row['expires_at'])}, by {escape(row['moderator_name'] or 'Automod')}" for row in running["mutes"]]
        await self._send_running(ctx, "Running mutes", lines, "Nobody is muted.")

    @commands.hybrid_command(name="tempbans", description="List temporary bans and when they end")
    @commands.guild_only()
    @requires("ban_members")
    async def tempbans(self, ctx: commands.Context) -> None:
        running = await self.running(ctx.guild.id)
        lines = [f"{escape(row['name'] or 'Unknown')} ({row['user_id']}), {self._until(row['expires_at'])}, by {escape(row['moderator_name'] or 'Automod')}" for row in running["bans"]]
        await self._send_running(ctx, "Temporary bans", lines, "Nobody is on a temporary ban.")

    async def running(self, guild_id: int) -> dict:
        """What's in force right now and when it ends. The commands above and the dashboard's Running tab both read this."""
        now = time.time()
        mutes = await self.db.all(
            "SELECT user_id, kind, moderator_name, started_at, expires_at, (SELECT user_name FROM cases AS c WHERE c.guild_id = m.guild_id AND c.user_id = m.user_id ORDER BY number DESC LIMIT 1) AS known_name "
            "FROM active_mutes AS m WHERE guild_id = ? AND (expires_at IS NULL OR expires_at > ?) ORDER BY started_at DESC",
            (guild_id, now),
        )
        guild = self.bot.get_guild(guild_id)
        for row in mutes:
            member = guild.get_member(row["user_id"]) if guild else None
            row["name"], row["here"] = (name_of(member), True) if member is not None else (row.pop("known_name"), False)
            row.pop("known_name", None)
        bans = [
            {"user_id": timer["payload"]["user_id"], "name": timer["payload"].get("user_name"), "moderator_name": timer["payload"].get("moderator_name"), "started_at": timer["payload"].get("started_at"), "expires_at": timer["due_at"], "kind": timer["payload"].get("kind", "tempban")}
            for timer in await self.bot.timers.list_kind("moderation.unban", guild_id)
        ]
        lockdowns = await self.db.all("SELECT channel_id, moderator_name, reason, created_at, expires_at FROM lockdowns WHERE guild_id = ? ORDER BY created_at DESC", (guild_id,))
        return {"mutes": mutes, "bans": bans, "lockdowns": lockdowns, "raid": await self.db.one("SELECT moderator_name, reason, started_at, expires_at FROM raid_modes WHERE guild_id = ?", (guild_id,)),
                "held": await self.db.scalar("SELECT COUNT(*) FROM raid_holds WHERE guild_id = ?", (guild_id,))}

    # ---- raid mode ----

    async def raid_on(self, guild: Any, actor: Any, seconds: int | None, reason: str | None) -> None:
        """From now until it ends, everyone who joins is held under the mute role, which already has its limits in every channel. Raises ModerationError when there's no role to use."""
        self._mute_role(guild)
        now = time.time()
        expires_at = now + seconds if seconds else None
        await self.db.run(
            "INSERT INTO raid_modes (guild_id, moderator_id, moderator_name, reason, started_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET moderator_id = excluded.moderator_id, moderator_name = excluded.moderator_name, reason = excluded.reason, started_at = excluded.started_at, expires_at = excluded.expires_at",
            (guild.id, actor.id, name_of(actor), reason, now, expires_at),
        )
        key = str(guild.id)
        if seconds:
            await self.bot.timers.schedule("moderation.raid_end", expires_at, {"guild_id": guild.id}, guild_id=guild.id, key=key)
        else:
            await self.bot.timers.cancel_key("moderation.raid_end", key)
        await self.bot.activity_log.record(guild.id, self.bot.actor(actor), "raid.on", detail=reason)
        length = f" for {humanize(seconds)}" if seconds else ""
        await self._tell_staff(guild, "Raid mode is on", f"{escape(name_of(actor))} turned on raid mode{length}. New members are held under the mute role until someone runs `/raid release`." + (f" Reason: {reason}" if reason else ""))

    async def raid_off(self, guild: Any, actor: Any) -> int:
        """New joiners go through again. Whoever is held stays held, since who they are is what staff need to look at. Returns how many."""
        await self.db.run("DELETE FROM raid_modes WHERE guild_id = ?", (guild.id,))
        await self.bot.timers.cancel_key("moderation.raid_end", str(guild.id))
        held = await self.db.scalar("SELECT COUNT(*) FROM raid_holds WHERE guild_id = ?", (guild.id,))
        if actor is not None:
            await self.bot.activity_log.record(guild.id, self.bot.actor(actor), "raid.off", detail=f"{held} held")
        return held

    async def _raid_due(self, payload: dict, row: dict) -> None:
        guild = await self._timer_guild("moderation.raid_end", payload, row)
        if guild is None:
            return
        # A restore keeps raid mode as it stands and can still bring back the timer of a raid in the file. A raid with an end has its own timer, which a file's can't replace, so one with no end, or none at all, has nothing for this to end.
        mode = await self.db.one("SELECT expires_at FROM raid_modes WHERE guild_id = ?", (guild.id,))
        if mode is None or mode["expires_at"] is None:
            return
        held = await self.raid_off(guild, None)
        await self._tell_staff(guild, "Raid mode ended", f"Raid mode's time ran out, and new members go through again. {self._held_text(held)}")

    @staticmethod
    def _held_text(held: int) -> str:
        return f"{held} {'person is' if held == 1 else 'people are'} still held. `/raid release` lets them in, and `/massban raid` shows who they are and bans them." if held else "Nobody is held."

    async def _hold_if_raid(self, member: Any) -> None:
        """Hold someone who joins during raid mode, or who left while held and came back."""
        guild = member.guild
        if member.bot:
            return
        held = await self.db.one("SELECT 1 FROM raid_holds WHERE guild_id = ? AND user_id = ?", (guild.id, member.id))
        if held is None and await self.db.one("SELECT 1 FROM raid_modes WHERE guild_id = ?", (guild.id,)) is None:
            return
        try:
            role = self._mute_role(guild)
            if role not in member.roles:
                self._expect(guild.id, member.id, "mute")
                await member.add_roles(role, reason="Raid mode holds new members")
        except (ModerationError, discord.HTTPException) as error:
            self._forget(guild.id, member.id, "mute")
            log.warning("Could not hold %s in %s for raid mode: %s", member.id, guild.id, error)
            return
        await self.db.run(
            "INSERT INTO raid_holds (guild_id, user_id, user_name, role_id, held_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (guild_id, user_id) DO NOTHING",
            (guild.id, member.id, name_of(member), role.id, time.time()),
        )

    async def release_held(self, guild: Any, audit: str) -> tuple[int, int]:
        """Let everyone held go, at Discord's pace. A mute of their own keeps its role. Returns how many were let go and how many Discord refused, and those stay held for the next try."""
        released = failed = 0
        for row in await self.db.all("SELECT user_id, role_id FROM raid_holds WHERE guild_id = ?", (guild.id,)):
            member = guild.get_member(row["user_id"])
            role = guild.get_role(row["role_id"])
            try:
                if member is None and role is not None and not _looks_whole(guild):
                    member = await find_member(guild, row["user_id"])  # a short list says nothing about whether they left, and dropping the row would leave them muted with no record
                if member is not None and role is not None:
                    await self.release_mute_role(guild, member, role, audit)
            except discord.HTTPException as error:
                log.warning("Could not let %s go in %s: %s", row["user_id"], guild.id, error)
                failed += 1
                continue
            await self.db.run("DELETE FROM raid_holds WHERE guild_id = ? AND user_id = ?", (guild.id, row["user_id"]))
            released += 1
        return released, failed

    async def _release_and_say(self, guild: Any, actor: Any, audit: str) -> None:
        """A release too big for a page to wait on. Whoever isn't let go stays held, so running it again finishes the job."""
        try:
            released, failed = await self.release_held(guild, audit)
        except Exception:
            log.exception("Letting raid mode's held members go failed in %s", guild.id)
            await self._tell_staff(guild, "Raid release stopped", "Letting held members go stopped partway because of an error. Whoever is left is still held. Run `/raid release` again.")
            return
        await self.bot.activity_log.record(guild.id, self.bot.actor(actor), "raid.release", detail=f"{released} released")
        await self._tell_staff(guild, "Raid release finished", f"{released} {'person was' if released == 1 else 'people were'} let go." + (f" Discord refused {failed}, and they're still held." if failed else ""))

    async def _watch_join_rate(self, guild: Any) -> None:
        """Tell staff when people arrive fast enough to look like a raid, so they know the tools are there. It only tells, and raid mode already holding them needs no notice."""
        now = time.monotonic()
        joins = self._joins.setdefault(guild.id, deque(maxlen=JOIN_ALERT_COUNT))
        joins.append(now)
        if len(joins) < JOIN_ALERT_COUNT or now - joins[0] > JOIN_ALERT_SECONDS or now - self._join_alerted.get(guild.id, float("-inf")) < JOIN_ALERT_GAP:
            return
        if await self.db.one("SELECT 1 FROM raid_modes WHERE guild_id = ?", (guild.id,)) is not None:
            return
        self._join_alerted[guild.id] = now
        await self._tell_staff(guild, "Members are joining fast", f"{JOIN_ALERT_COUNT} people joined within {int(now - joins[0]) + 1} seconds. `/raid on` holds new members under the mute role, and `/massban joined:10m` shows who just came in.")

    @commands.hybrid_group(name="raid", fallback="status", description="See raid mode, which holds new members under the mute role")
    @commands.guild_only()
    @requires("manage_roles")
    async def raid(self, ctx: commands.Context) -> None:
        running = await self.running(ctx.guild.id)
        mode = running["raid"]
        line = f"Raid mode has been on since <t:{int(mode['started_at'])}:R>, {self._until(mode['expires_at'])}, by {escape(mode['moderator_name'] or 'Unknown')}." if mode else "Raid mode is off."
        await ctx.send(f"{line} {self._held_text(running['held'])}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @raid.command(name="on", description="Hold everyone who joins from now on under the mute role")
    @requires("manage_roles")
    @app_commands.describe(duration="How long, such as 2h. Leave out for no end", reason="Why")
    async def raid_on_command(self, ctx: commands.Context, duration: str | None = None, *, reason: str | None = None) -> None:
        await ctx.defer(ephemeral=True)
        try:
            seconds, why = self._time_and_reason(ctx, duration, reason)
            await self.raid_on(ctx.guild, ctx.author, seconds, clip(why, REASON_MAX))
        except ModerationError as error:
            await ctx.fail(str(error))
            return
        await ctx.ok(f"Raid mode is on{f' for {humanize(seconds)}' if seconds else ' with no end'}. New members are held under the mute role. `/raid off` ends it.")

    @raid.command(name="off", description="Let new members through again")
    @requires("manage_roles")
    async def raid_off_command(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        await ctx.ok(f"Raid mode is off. {self._held_text(await self.raid_off(ctx.guild, ctx.author))}")

    @raid.command(name="release", description="Let everyone raid mode is holding go")
    @requires("manage_roles")
    async def raid_release(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        released, failed = await self.release_held(ctx.guild, audit_reason(ctx.author, None, "raid release"))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "raid.release", detail=f"{released} released")
        await ctx.ok(f"Let {released} {'person' if released == 1 else 'people'} go." + (f" Discord refused {failed}, and they're still held." if failed else ""))

    @commands.hybrid_group(name="unlockdown", ignore_extra=True, description="Let @everyone post again")
    @commands.guild_only()
    @requires("manage_channels")
    async def unlockdown(self, ctx: commands.Context, channel: LOCKABLE = None) -> None:
        await self._unlock_one_command(ctx, channel)

    @unlockdown.command(name="channel", ignore_extra=True, description="Let @everyone post in a channel again")
    @requires("manage_channels")
    @app_commands.describe(channel="The channel. Leave out for this one")
    async def unlockdown_channel(self, ctx: commands.Context, channel: LOCKABLE = None) -> None:
        await self._unlock_one_command(ctx, channel)

    @unlockdown.command(name="server", description="End every lockdown in the server")
    @requires("manage_channels")
    async def unlockdown_server(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        rows = await self.db.all("SELECT channel_id FROM lockdowns WHERE guild_id = ?", (guild.id,))
        unlocked = failed = 0
        audit = audit_reason(ctx.author, None, "unlockdown server")
        for row in rows:
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                await self.db.run("DELETE FROM lockdowns WHERE guild_id = ? AND channel_id = ?", (guild.id, row["channel_id"]))
                continue
            try:
                await self._unlock_channel(guild, channel, audit)
                unlocked += 1
            except discord.HTTPException:
                failed += 1
        await self.bot.activity_log.record(guild.id, self.bot.actor(ctx.author), "lockdown.end", detail=f"{unlocked} channels")
        await ctx.ok(f"Unlocked {unlocked} channels." + (f" {failed} refused." if failed else ""))

    # ---- purge ----

    async def _purge(self, ctx: Any, limit: int, check: Callable[[Any], bool], *, spare_pinned: bool = True) -> None:
        await ctx.defer(ephemeral=True)
        if not 1 <= limit <= PURGE_MAX:
            await ctx.fail(f"Pick a count from 1 to {PURGE_MAX}.")
            return
        channel = ctx.channel
        mine = channel.permissions_for(ctx.guild.me)
        can_delete_others = mine.manage_messages
        if not mine.read_message_history or (not can_delete_others and spare_pinned):
            await ctx.fail("I need Manage Messages and Read Message History in this channel.")
            return

        def wanted(message: Any) -> bool:
            if spare_pinned and message.pinned:
                return False
            return bool(check(message))

        # A text command's own message stays out of the count, and goes afterward.
        before = ctx.message if ctx.interaction is None else None
        try:
            deleted = await channel.purge(limit=limit, check=wanted, before=before, bulk=can_delete_others, reason=audit_reason(ctx.author, None, "purge"))
        except discord.Forbidden:
            await ctx.fail("Discord refused that. I need Manage Messages and Read Message History in this channel.")
            return
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused that: {error.text or error.status}")
            return
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        # Old messages go one by one, so a big purge can outlast the slash command's token, and then only the channel can hear how it went.
        started = getattr(ctx.interaction, "created_at", None)
        if started is not None and (discord.utils.utcnow() - started).total_seconds() > INTERACTION_SAFE_SECONDS:
            await channel.send(f"{ctx.author.mention} {self._purge_report(deleted)}", delete_after=30, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[ctx.author]))
            return
        await ctx.ok(self._purge_report(deleted), delete_after=10)

    def _purge_report(self, deleted: list[Any]) -> str:
        line = f"Removed {len(deleted)} message{'' if len(deleted) == 1 else 's'}."
        authors = Counter(name_of(message.author) for message in deleted)
        if authors:
            line += "\n" + "\n".join(f"**{escape(name)}**: {count}" for name, count in authors.most_common(15))
        return line[:messages.CONTENT_MAX - 10]

    @commands.hybrid_group(name="purge", description="Delete messages in this channel. Pinned messages stay")
    @commands.guild_only()
    @requires("manage_guild")
    async def purge(self, ctx: commands.Context, *, target: str = "") -> None:
        """Typed as `.purge 50` or `.purge @member 50`."""
        count: int | None = None
        who: int | None = None
        for word in target.split():
            ids, _ = leading_ids(word)
            if ids:
                who = ids[0]
            elif whole_number(word) is not None:
                count = whole_number(word)
            else:
                count = who = None
                break
        # A bare `.purge` deleting the channel's last 100 messages would be too easy to type by accident.
        if count is None and who is None:
            await ctx.fail("Give a number of messages, and a member if you like, or use a subcommand such as `purge contains`.")
            return
        await self._purge(ctx, 100 if count is None else count, (lambda m: m.author.id == who) if who else (lambda m: True))

    @purge.command(name="bot", description="Delete bot messages, and messages that start with a prefix")
    @requires("manage_guild")
    @app_commands.describe(search="How many messages to look through. Defaults to 100.", prefix="Also delete messages that start with this")
    async def purge_bot(self, ctx: commands.Context, search: int | None = None, *, prefix: str | None = None) -> None:
        await self._purge(ctx, 100 if search is None else search, lambda m: m.author.bot or bool(prefix and (m.content or "").startswith(prefix)))

    @purge.command(name="contains", description="Delete messages holding this text")
    @requires("manage_guild")
    @app_commands.describe(substring="The text to look for", search="How many messages to look through")
    async def purge_contains(self, ctx: commands.Context, *, substring: str, search: int = 100) -> None:
        if ctx.interaction is None:  # typed, the count can come before or after the text
            first, _, rest = substring.partition(" ")
            head, _, last = substring.rpartition(" ")
            if rest and whole_number(first) is not None:
                search, substring = whole_number(first), rest.strip()
            elif head and whole_number(last) is not None:
                search, substring = whole_number(last), head.strip()
            substring = substring.strip("\"")
        if len(substring) < 2:
            await ctx.fail("Give at least two characters to look for.")
            return
        needle = substring.lower()
        await self._purge(ctx, search, lambda m: needle in (m.content or "").lower())

    @purge.command(name="user", description="Delete messages from one member")
    @requires("manage_guild")
    @app_commands.describe(member="Whose messages to delete", search="How many messages to look through")
    async def purge_user(self, ctx: commands.Context, member: OnePerson, search: int = 100) -> None:
        await self._purge(ctx, search, lambda m: m.author.id == member.id)

    @purge.command(name="all", description="Delete this many messages")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages")
    async def purge_all(self, ctx: commands.Context, count: int) -> None:
        await self._purge(ctx, count, lambda m: True)

    @purge.command(name="embeds", description="Delete messages with embeds")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def purge_embeds(self, ctx: commands.Context, count: int = 100) -> None:
        await self._purge(ctx, count, lambda m: bool(m.embeds))

    @purge.command(name="emoji", description="Delete messages with custom emoji")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def purge_emoji(self, ctx: commands.Context, count: int = 100) -> None:
        await self._purge(ctx, count, _purge_emoji)

    @purge.command(name="files", description="Delete messages with attachments")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def purge_files(self, ctx: commands.Context, count: int = 100) -> None:
        await self._purge(ctx, count, lambda m: bool(m.attachments))

    @purge.command(name="images", description="Delete messages with attachments or embeds")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def purge_images(self, ctx: commands.Context, count: int = 100) -> None:
        await self._purge(ctx, count, _purge_images)

    @purge.command(name="links", description="Delete messages with links")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def purge_links(self, ctx: commands.Context, count: int = 100) -> None:
        await self._purge(ctx, count, _purge_links)

    @purge.command(name="mentions", aliases=["pings"], description="Delete messages that ping someone")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def purge_mentions(self, ctx: commands.Context, count: int = 100) -> None:
        await self._purge(ctx, count, _purge_mentions)

    @purge.command(name="human", aliases=["humans"], description="Delete messages except those from bots")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def purge_human(self, ctx: commands.Context, count: int = 100) -> None:
        await self._purge(ctx, count, lambda m: not m.author.bot)

    @purge.command(name="reactions", description="Remove all reactions from recent messages")
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def purge_reactions(self, ctx: commands.Context, count: int = 100) -> None:
        await ctx.defer(ephemeral=True)
        if not 1 <= count <= PURGE_MAX:
            await ctx.fail(f"Pick a count from 1 to {PURGE_MAX}.")
            return
        total = cleared = 0
        before = ctx.message if ctx.interaction is None else None
        try:
            async for message in ctx.channel.history(limit=count, before=before):
                if message.reactions:
                    total += sum(reaction.count for reaction in message.reactions)
                    await message.clear_reactions()
                    cleared += 1
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused that: {error.text or error.status}")
            return
        await ctx.ok(f"Removed {total} reactions from {cleared} messages.", delete_after=10)

    @commands.hybrid_command(name="cleanup", description="Delete my recent messages and commands aimed at me. Pinned ones go too")
    @commands.guild_only()
    @requires("manage_guild")
    @app_commands.describe(count="How many messages to look through")
    async def cleanup(self, ctx: commands.Context, count: int = 100) -> None:
        me = ctx.guild.me.id
        prefixes = tuple(self.bot.settings.get(ctx.guild.id, "core")["prefixes"])
        can_delete_others = ctx.channel.permissions_for(ctx.guild.me).manage_messages
        tags = self.bot.module("tags")
        names = set(SHORTCUTS) | set(TAG_FIRST_SHORTCUTS) | (await tags.all_names(ctx.guild.id) if tags is not None else set())

        def mine(message: Any) -> bool:
            if message.author.id == me:
                return True
            return can_delete_others and self._runs_a_command(message.content or "", prefixes, names)

        await self._purge(ctx, min(count, 1000), mine, spare_pinned=False)

    def _runs_a_command(self, content: str, prefixes: tuple[str, ...], names: set[str]) -> bool:
        """Whether this text runs a command or a tag. A prefix alone isn't enough: with `.` as the prefix, that would take "..." and ".3mf files" from anyone."""
        for prefix in prefixes:
            if content.startswith(prefix):
                words = content[len(prefix):].split(maxsplit=1)
                if words and (self.bot.get_command(words[0]) is not None or words[0].lower() in names):
                    return True
        return False

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/cases", self.api_cases, access="staff")
        router.get("/cases/filters", self.api_case_filters, access="staff")
        router.get("/cases/export", self.api_case_export, access="staff")
        router.get("/cases/leaderboard", self.api_leaderboard, access="staff")
        router.get("/cases/member/{user_id:\\d+}", self.api_member, access="staff")
        router.post("/cases/member/{user_id:\\d+}/notes", self.api_add_note, access="staff")
        router.delete("/cases/notes/{number:\\d+}", self.api_remove_note, access="staff")
        router.get("/cases/{number:\\d+}", self.api_case, access="staff")
        router.patch("/cases/{number:\\d+}", self.api_edit_case, access="staff")
        router.post("/cases/{number:\\d+}/void", self.api_void_case, access="staff")
        router.post("/cases/{number:\\d+}/restore", self.api_restore_case, access="staff")
        router.post("/moderation/import", self.api_import)
        router.post("/moderation/mute-role/apply", self.api_apply_mute_role)
        router.get("/moderation/running", self.api_running, access="staff")
        router.post("/moderation/running/mutes/{user_id:\\d+}/lift", self.api_lift_mute, access="staff")
        router.post("/moderation/running/bans/{user_id:\\d+}/lift", self.api_lift_ban, access="staff")
        router.post("/moderation/running/lockdowns/{channel_id:\\d+}/lift", self.api_lift_lockdown, access="staff")
        router.post("/moderation/raid", self.api_raid, access="staff")

    def _case_filter(self, guild_id: int, query: Any) -> tuple[str, list]:
        where = ["guild_id = ?"]
        args: list[Any] = [guild_id]

        def person(value: str, id_column: str, name_column: str) -> None:
            value = value.strip()
            if not value:
                return
            found = whole_number(value)
            if found is not None and len(value) >= 15:
                where.append(f"{id_column} = ?")
                args.append(found)
            else:
                where.append(f"{name_column} LIKE ? ESCAPE '\\'")
                args.append(f"%{value.translate(_LIKE_ESCAPE)}%")

        actions = [name for name in (query.get("action") or "").split(",") if name in ACTIONS]
        if actions:
            where.append(f"action IN ({','.join('?' * len(actions))})")
            args.extend(actions)
        person(query.get("user") or "", "user_id", "user_name")
        person(query.get("moderator") or "", "moderator_id", "moderator_name")
        source = query.get("source")
        if source:
            where.append("source = ?")
            args.append(source)
        number = whole_number(query.get("number") or "")
        if number is not None:
            where.append("number = ?")
            args.append(number)
        reason_part = (query.get("reason") or "").strip()
        if reason_part:
            where.append("reason LIKE ? ESCAPE '\\'")
            args.append(f"%{reason_part.translate(_LIKE_ESCAPE)}%")
        state = query.get("state")
        if state == "voided":
            where.append("voided_at IS NOT NULL")
        elif state == "active":
            where.append("voided_at IS NULL")
        search = (query.get("q") or "").strip()
        if search:
            like = f"%{search.translate(_LIKE_ESCAPE)}%"
            clause = "(reason LIKE ? ESCAPE '\\' OR user_name LIKE ? ESCAPE '\\' OR moderator_name LIKE ? ESCAPE '\\'"
            args.extend([like, like, like])
            found = whole_number(search)
            if found is not None:
                clause += " OR number = ? OR user_id = ? OR moderator_id = ?"
                args.extend([found] * 3)
            where.append(clause + ")")
        return " AND ".join(where), args

    async def api_import(self, req: Any) -> dict:
        """Take in settings exported from the old setup, so moving doesn't mean retyping them."""
        body = await req.json()
        code = body.get("code") if isinstance(body, dict) else None
        try:
            data = json.loads(code) if isinstance(code, str) else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "That isn't valid JSON.")
        info = req.ops.describe_guild(req.guild_id)
        if info is None:
            raise ApiError(404, "The bot isn't in that server.")
        channel_ids = {row["id"] for row in info["channels"]}
        form = settings_from_export(data, channel_ids, {row["id"] for row in info["roles"]})
        saved = await req.save_settings(self.key, form, action="moderation.import")
        reports = req.settings.schemas.get("reports")
        report_channel = _known_id((data.get("modlogs") or {}).get("report_channel"), channel_ids) if isinstance(data.get("modlogs"), dict) else None
        # This only fills an empty setting. A reports channel already chosen is the one staff watch.
        if reports is not None and report_channel and "report_channel" in reports.fields and not req.settings.get(req.guild_id, "reports")["report_channel"]:
            try:
                await req.save_settings("reports", {"report_channel": report_channel}, action="reports.import")
            except ValidationError as error:  # the file's moderation settings are in by now, and this one stays for someone who can post there
                log.info("Left out a report channel from an import in %s: %s", req.guild_id, error.fields)
        return saved

    def _dashboard_moderator(self, req: Any, command: str) -> tuple[Any, Any]:
        """The server and the person, once they could run this command themselves. Manage Server alone doesn't reach what these routes do."""
        guild = self.bot.get_guild(req.guild_id)
        if guild is None:
            raise ApiError(404, "The bot isn't in that server.")
        member = guild.get_member(req.user.id)
        if member is None:
            raise ApiError(403, NOT_SEEN)
        if not req.is_admin:
            self._check_command(req, command)
        return guild, member

    async def api_apply_mute_role(self, req: Any) -> dict:
        """Picking a role on the page saves it, and this sets its limits in every channel, so mutes do something from then on. It can take a while on a big server, so it carries on after the answer."""
        req.require_permission("manage_roles")
        req.require_permission("manage_channels")
        guild = self.bot.get_guild(req.guild_id)
        if guild is None:  # the demo has no server behind it
            info = req.ops.describe_guild(req.guild_id)
            return {"channels": len(info["channels"]) if info else 0}
        try:
            role = self._mute_role(guild)
        except ModerationError as error:
            raise ApiError(400, str(error)) from error
        self._in_background(self.apply_mute_everywhere(guild, role, "The mute role was picked on the dashboard"))
        await req.record("muterole.apply", detail=role.name)
        return {"channels": len(guild.channels)}

    async def api_running(self, req: Any) -> dict:
        running = await self.running(req.guild_id)
        running["lockdowns"] = [row for row in running["lockdowns"] if req.can_see(row["channel_id"])]
        return running

    async def _lift_reason(self, req: Any) -> str | None:
        form = await req.json()
        why = form.get("reason")
        return text(REASON_MAX)(why) if why else None

    async def api_lift_mute(self, req: Any) -> dict:
        guild, member = self._dashboard_moderator(req, "unmute")
        user_id = req.int_param("user_id")
        reason = await self._lift_reason(req)
        name = await self.db.scalar("SELECT user_name FROM cases WHERE guild_id = ? AND user_id = ? ORDER BY number DESC LIMIT 1", (guild.id, user_id))
        try:
            row = await self.punish(guild, guild.get_member(user_id) or Someone(user_id, name), "unmute", moderator=member, reason=reason)
        except ModerationError as error:
            raise ApiError(400, str(error)) from error
        return {"case": row["number"]}

    async def api_lift_ban(self, req: Any) -> dict:
        guild, member = self._dashboard_moderator(req, "unban")
        user_id = req.int_param("user_id")
        reason = await self._lift_reason(req)
        timer = await self.bot.timers.get_key("moderation.unban", f"{guild.id}:{user_id}")
        if timer is None:
            raise ApiError(404, "That temporary ban isn't running anymore.")
        try:
            row = await self.punish(guild, Someone(user_id, timer["payload"].get("user_name")), "unban", moderator=member, reason=reason)
        except ModerationError as error:
            raise ApiError(400, str(error)) from error
        return {"case": row["number"]}

    async def api_lift_lockdown(self, req: Any) -> dict:
        guild, member = self._dashboard_moderator(req, "unlockdown")
        channel_id = req.int_param("channel_id")
        if not req.can_see(channel_id):
            raise ApiError(403, "You can't see that channel.")
        channel = guild.get_channel(channel_id)
        if channel is None:
            await self.db.run("DELETE FROM lockdowns WHERE guild_id = ? AND channel_id = ?", (guild.id, channel_id))
            return {"ok": True}
        try:
            done = await self._unlock_channel(guild, channel, audit_reason(member, None, "unlockdown"))
        except discord.HTTPException as error:
            raise ApiError(502, f"Discord refused that: {error.text or error.status}") from error
        if not done:
            raise ApiError(404, "That channel isn't locked.")
        await req.record("lockdown.end", detail=channel.name)
        return {"ok": True}

    async def api_raid(self, req: Any) -> dict:
        form = await req.json()
        action = choice("on", "off", "release")(form.get("action"))
        guild, member = self._dashboard_moderator(req, f"raid {action}")
        if action == "on":
            seconds = integer(0, MAX_SECONDS)(form.get("seconds") or 0)
            reason = text(REASON_MAX)(form["reason"]) if form.get("reason") else None
            try:
                await self.raid_on(guild, member, seconds or None, reason)
            except ModerationError as error:
                raise ApiError(400, str(error)) from error
        elif action == "off":
            await self.raid_off(guild, member)
        else:
            audit = audit_reason(member, None, "raid release")
            held = await self.db.scalar("SELECT COUNT(*) FROM raid_holds WHERE guild_id = ?", (guild.id,))
            if held > RELEASE_INLINE:
                self._in_background(self._release_and_say(guild, member, audit))
                return {"releasing": held}
            released, failed = await self.release_held(guild, audit)
            await req.record("raid.release", detail=f"{released} released")
            return {"released": released, "failed": failed}
        return {"ok": True}

    async def api_cases(self, req: Any) -> dict:
        where, args = self._case_filter(req.guild_id, req.query)
        try:
            page = max(1, int(req.query.get("page", 1)))
            size = int(req.query.get("size", 25))
        except ValueError:
            page, size = 1, 25
        size = size if size in (10, 25, 50, 100) else 25
        page = min(page, 1_000_000)
        total = await req.db.scalar(f"SELECT COUNT(*) FROM cases WHERE {where}", args)
        rows = await req.db.all(f"SELECT * FROM cases WHERE {where} ORDER BY number DESC LIMIT ? OFFSET ?", (*args, size, (page - 1) * size))
        return {"rows": [self._decode(row) for row in rows], "total": total, "page": page, "size": size}

    async def api_case_filters(self, req: Any) -> dict:
        moderators = await req.db.all(
            "SELECT moderator_id AS id, MAX(moderator_name) AS name, COUNT(*) AS cases FROM cases WHERE guild_id = ? AND moderator_id IS NOT NULL GROUP BY moderator_id ORDER BY cases DESC LIMIT 200",
            (req.guild_id,),
        )
        return {"actions": list(ACTIONS), "moderators": moderators}

    async def api_case(self, req: Any) -> dict:
        row = await self.get_case(req.guild_id, req.int_param("number"))
        if row is None:
            raise ApiError(404, "There is no such case.")
        return row

    def _check_record_change(self, req: Any, command: str, subject_id: int | None) -> None:
        """Staff can open these records, and changing one takes what the matching command takes. Below the owner and Administrators, nobody changes a record about themselves."""
        if req.is_admin:
            return
        if subject_id == req.user.id:
            raise ApiError(403, OWN_RECORD)
        if req.access != "staff":
            return
        self._check_command(req, command)

    def _check_command(self, req: Any, command: str) -> None:
        """What running this command takes, for a dashboard route that does what the command does."""
        guild = self.bot.get_guild(req.guild_id)
        member = guild.get_member(req.user.id) if guild else None
        found = self.bot.get_command(command)
        if member is None or found is None:
            raise ApiError(403, NOT_SEEN)
        # The dashboard isn't a channel, so a rule about where the command runs doesn't count against it.
        overwrite = self.bot.settings.get(req.guild_id, COMMANDS_MODULE)["overwrites"].get((found.root_parent or found).qualified_name) or {}
        place = SimpleNamespace(id=(overwrite.get("channel_wl") or [0])[0], category_id=None)
        decision = self.bot.command_decision(found, member, place)
        if not decision.allowed:
            raise ApiError(403, decision.reason or "You can't change that here.")

    async def _check_case_change(self, req: Any, number: int, *, voiding: bool = False) -> None:
        row = await self.get_case(req.guild_id, number)
        if row is not None:
            self._check_record_change(req, "removewarning" if voiding and row["action"] == "warn" else "modlog reason", row["user_id"])

    async def api_edit_case(self, req: Any) -> dict:
        form = await req.json()
        reason = text(REASON_MAX, minimum=1)(form.get("reason") if isinstance(form, dict) else None)
        number = req.int_param("number")
        await self._check_case_change(req, number)
        result = await self.set_reason(req.guild_id, number, reason, req.actor)
        if result is None:
            raise ApiError(404, "There is no such case.")
        await req.record("case.reason", detail=f"Case {number}")
        return {**result[0], "post_updated": result[1]}

    async def api_void_case(self, req: Any) -> dict:
        form = await req.json()
        why = form.get("reason") if isinstance(form, dict) else None
        why = text(REASON_MAX)(why) if why else None
        number = req.int_param("number")
        await self._check_case_change(req, number, voiding=True)
        result = await self.void_case(req.guild_id, number, req.actor, why)
        if result is None:
            raise ApiError(404, "There is no such case, or it's already voided.")
        await req.record("case.void", detail=f"Case {number}")
        return {**result[0], "post_updated": result[1]}

    async def api_restore_case(self, req: Any) -> dict:
        number = req.int_param("number")
        await self._check_case_change(req, number, voiding=True)
        result = await self.restore_case(req.guild_id, number)
        if result is None:
            raise ApiError(404, "There is no such case, or it isn't voided.")
        await req.record("case.restore", detail=f"Case {number}")
        return {**result[0], "post_updated": result[1]}

    async def api_member(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        if user_id > SQLITE_INT_MAX:
            raise ApiError(404, "Not found.")
        member = req.ops.member(req.guild_id, user_id)
        rows = await req.db.all("SELECT * FROM cases WHERE guild_id = ? AND user_id = ? ORDER BY number DESC", (req.guild_id, user_id))
        record = await self.history(req.guild_id, user_id)
        name = member["display_name"] if member else (rows[0]["user_name"] if rows else None)
        mute = await self._active_mute(req.guild_id, user_id)
        return {
            "user_id": user_id, "name": name, "member": member, "cases": [self._decode(row) for row in rows], "notes": record["notes"], "counts": record["counts"],
            "muted_until": (mute["expires_at"] or 0) if mute and (not mute["expires_at"] or mute["expires_at"] > time.time()) else None,
        }

    async def api_add_note(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        if user_id > SQLITE_INT_MAX:
            raise ApiError(404, "Not found.")
        form = await req.json()
        body = text(NOTE_MAX, minimum=1)(form.get("text") if isinstance(form, dict) else None)
        self._check_record_change(req, "notes setnote", user_id)
        row = await self.add_note(req.guild_id, user_id, req.actor, body)
        await req.record("note.add", detail=f"Note {row['number']}")
        return row

    async def api_remove_note(self, req: Any) -> dict:
        number = req.int_param("number")
        note = await self.db.one("SELECT user_id FROM notes WHERE guild_id = ? AND number = ?", (req.guild_id, number))
        if note is not None:
            self._check_record_change(req, "notes removenote", note["user_id"])
        if not await self.remove_note(req.guild_id, number):
            raise ApiError(404, "There is no such note.")
        await req.record("note.remove", detail=f"Note {number}")
        return {"ok": True}

    async def api_leaderboard(self, req: Any) -> list[dict]:
        try:
            days = int(req.query.get("days", 0))
        except ValueError:
            days = 0
        since = time.time() - days * DAY if 0 < days <= 3650 else 0
        return await self._highscores(req.guild_id, since=since, bots=req.query.get("bots") == "1")

    async def _export_rows(self, guild_id: int, where: str, args: list) -> AsyncIterator[dict]:
        """A thousand rows at a time, keyed on the case number, so a big export doesn't hold them all in memory or stop the bot while it reads."""
        last: int | None = None
        sent = 0
        while sent < EXPORT_ROWS:
            clause, extra = (where, []) if last is None else (f"{where} AND number < ?", [last])
            rows = await self.db.all(f"SELECT * FROM cases WHERE {clause} ORDER BY number DESC LIMIT ?", [*args, *extra, EXPORT_CHUNK])
            if not rows:
                return
            for row in rows:
                yield row
            sent += len(rows)
            last = rows[-1]["number"]

    async def api_case_export(self, req: Any) -> web.StreamResponse:
        where, args = self._case_filter(req.guild_id, req.query)
        kind = "json" if req.query.get("format") == "json" else "csv"
        total = await req.db.scalar(f"SELECT COUNT(*) FROM cases WHERE {where}", args)
        await req.record("case.export", detail=f"{min(total, EXPORT_ROWS)} cases as {kind}")
        # Prepared by hand because this streams, so the middleware's headers would arrive too late.
        response = web.StreamResponse(headers={
            "Content-Type": "text/csv; charset=utf-8" if kind == "csv" else "application/json; charset=utf-8",
            "Content-Disposition": f'attachment; filename="cases-{req.guild_id}.{kind}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })
        await response.prepare(req.request)
        try:
            await (self._write_cases_json if kind == "json" else self._write_cases_csv)(response, req.guild_id, where, args)
            await response.write_eof()
        except ConnectionResetError:
            log.debug("A case export stopped because the browser went away")  # closing the tab mid-download isn't an error
        return response

    async def _write_cases_json(self, response: web.StreamResponse, guild_id: int, where: str, args: list) -> None:
        await response.write(b"[")
        first = True
        async for row in self._export_rows(guild_id, where, args):
            await response.write((b"" if first else b",\n") + json.dumps(safe_json(self._decode(row)), ensure_ascii=False).encode("utf-8"))
            first = False
        await response.write(b"]\n")

    async def _write_cases_csv(self, response: web.StreamResponse, guild_id: int, where: str, args: list) -> None:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["case", "action", "user_id", "user_name", "moderator_id", "moderator_name", "reason", "seconds", "expires_at", "source", "created_at", "voided_at", "voided_by_name", "void_reason"])
        async for row in self._export_rows(guild_id, where, args):
            writer.writerow([csv_cell(value) for value in (
                row["number"], row["action"], str(row["user_id"]), row["user_name"], str(row["moderator_id"]) if row["moderator_id"] else "", row["moderator_name"], row["reason"],
                row["seconds"], _iso(row["expires_at"]), row["source"], _iso(row["created_at"]), _iso(row["voided_at"]), row["voided_by_name"], row["void_reason"],
            )])
            if out.tell() > 64 * 1024:
                await response.write(out.getvalue().encode("utf-8"))
                out.seek(0)
                out.truncate()
        await response.write(out.getvalue().encode("utf-8"))

    # ---- demo ----

    async def seed_demo(self, guild_id: int) -> None:
        """Sample cases, so the demo's Cases page has something to show. Only demo.py calls this."""
        if await self.db.scalar("SELECT COUNT(*) FROM cases WHERE guild_id = ?", (guild_id,)):
            return
        staff = [(guild_id + 2, "demo_admin"), (guild_id + 50, "helpful_mod"), (guild_id + 51, "night_shift")]
        people = [(guild_id + 3, "sample_member"), (guild_id + 60, "loud_larry"), (guild_id + 61, "spam_account_42"), (guild_id + 62, "left_the_server")]
        actions = ["warn", "mute", "ban", "timeout", "kick", "warn", "unmute", "tempban", "warn", "hardmute", "unban", "softban"]
        reasons = ["Spam in general", "Posting invites", None, "Rude to other members", "Scam links", "Off-topic after a warning"]
        now = time.time()
        for index in range(60):
            moderator, member, action = staff[index % len(staff)], people[index % len(people)], actions[index % len(actions)]
            seconds = 3600 * (index % 5 + 1) if action in ("mute", "tempban", "timeout", "hardmute") else None
            await self.db.run(
                "INSERT INTO cases (guild_id, number, action, user_id, user_name, moderator_id, moderator_name, reason, seconds, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'command', ?)",
                (guild_id, index + 1, action, member[0], member[1], moderator[0], moderator[1], reasons[index % len(reasons)], seconds, now - (60 - index) * 7200),
            )
        await self.add_note(guild_id, people[0][0], Someone(staff[1][0], staff[1][1]), "Asked to read the rules. Keep an eye on this one.")


def _iso(seconds: float | None) -> str:
    if not seconds:
        return ""
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
