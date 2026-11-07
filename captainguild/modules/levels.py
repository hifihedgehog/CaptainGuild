"""XP, levels, reward roles, rank cards and the leaderboard.

The curve is 5n^2 + 50n + 100 XP to go from level n to the next, so a member reaches level 1 at 100 XP, level 2 at 255 and level 11
at 5,775, the same numbers members know from other bots.

A busy server earns XP far more often than the database should be written, so XP lives in memory and goes out in batches every few
seconds. A level-up is written at once, because it's rare and a crash must not announce it twice.

Reward roles follow a member's level. A level-up changes one person's. A change to the rewards, an import or a restored backup changes
everyone's, so one paced job per server walks the board, since each role change is its own request.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import discord
from aiohttp import web
from discord.ext import commands

from ..core import access, images, messages, rankcard
from ..core.members import find_member, whole_member_list
from ..core.module import Module, requires
from ..core.outside import Outside, OutsideError
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.tagscript import Scope, render_message
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, hex_color, integer, list_of, number, optional, snowflake, snowflakes, whole_number
from ..web.api import ApiError

log = logging.getLogger(__name__)

FLUSH_SECONDS = 5.0
MAX_CACHED = 200_000
EVENTS_KEPT = 200
BACKUPS_KEPT = 7
BOARD_PAGE = 10
VOICE_SECONDS = 60
BACKUP_SECONDS = 86400
SERVER_BACKGROUND = 0  # the user_id that holds a server's default card background
MAX_LEVEL = 1000
MAX_XP = 10 ** 12
MEE6_URL = "https://mee6.xyz/api/plugins/levels/leaderboard/{guild_id}"
MEE6_PAGE = 1000
MEE6_MAX_PAGES = 500
MEE6_TRIES = 6
SYNC_PAUSE = 1.0  # seconds after each member whose roles changed
SYNC_PAGE = 500
CONFIRM_SECONDS = 60
BACKGROUND_DAYS_KEPT = 30  # how long a card background outlasts the member who set it
NOTICE_SECONDS = 86400  # how often staff hear again that level-ups can't be posted
LIST_MORE = " and {} more, see the dashboard"
DEFAULT_MESSAGE = {"content": "Congratulations {mention}, you reached level {lvl}.", "embeds": [], "buttons": []}


def xp_to_next(level: int) -> int:
    """XP needed to go from this level to the next."""
    return 5 * level * level + 50 * level + 100


def xp_for_level(level: int) -> int:
    """Total XP at which a member reaches this level: the sum of xp_to_next for every level below it."""
    if level <= 0:
        return 0
    return 5 * (level - 1) * level * (2 * level - 1) // 6 + 25 * (level - 1) * level + 100 * level


def level_for_xp(xp: int) -> int:
    level = int((max(xp, 0) * 0.6) ** (1 / 3))
    while level > 0 and xp_for_level(level) > xp:
        level -= 1
    while xp_for_level(level + 1) <= xp:
        level += 1
    return level


def destination(value: Any) -> Any:
    """Where level-up messages go: nowhere, the channel it happened in, a DM, or one chosen channel."""
    if value in ("none", "same", "dm"):
        return value
    return snowflake(value)


def _reward(value: Any) -> dict:
    if not isinstance(value, dict):
        raise Invalid("Each reward needs a level and a role.")
    return {"level": integer(1, MAX_LEVEL)(value.get("level")), "role_id": snowflake(value.get("role_id"))}


def rewards(value: Any) -> list[dict]:
    rows = list_of(_reward, 200, unique=False)(value)
    levels = [row["level"] for row in rows]
    for level in levels:
        if levels.count(level) > 1:
            raise Invalid(f"Level {level} already has a reward. Remove it first.")
    return sorted(rows, key=lambda row: row["level"])


def parse_rate(text_: str) -> tuple[int, int] | None:
    """How often XP can be earned, typed as 2/60, 2 per 60 or 2 60. A single number means one award per that many seconds."""
    match = re.fullmatch(r"\s*(\d{1,3})\s*(?:/|per|\s)\s*(\d{1,4})\s*(?:s|sec|secs|seconds)?\s*", text_, re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.fullmatch(r"\s*(\d{1,4})\s*(?:s|sec|secs|seconds)?\s*", text_, re.IGNORECASE)
    if match:
        return 1, int(match.group(1))
    return None


def fit_list(parts: list[str], room: int) -> str:
    """As many entries as fit in a message, then how many didn't. Rewards and blacklists can hold more than Discord lets one message show."""
    tail_room = len(LIST_MORE.format(len(parts)))
    used = 0
    for index, part in enumerate(parts):
        used += len(part) + 2
        if used > room - tail_room and (index < len(parts) - 1 or used > room):
            return ", ".join(parts[:index]) + LIST_MORE.format(len(parts) - index)
    return ", ".join(parts)


class ConfirmView(discord.ui.View):
    """Confirm or Cancel on something that changes everyone, for the person who asked."""

    def __init__(self, author_id: int, label: str) -> None:
        super().__init__(timeout=CONFIRM_SECONDS)
        self.author_id = author_id
        self.answer: bool | None = None
        self.confirm.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.answer = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.answer = False
        await interaction.response.edit_message(content="Canceled. Nothing changed.", view=None)
        self.stop()


@dataclass
class _Entry:
    xp: int = 0
    messages: int = 0
    voice_minutes: int = 0
    name: str | None = None


class LeaderboardView(discord.ui.View):
    """Page turning for /leaderboard. It's for the person who asked, and it lapses, so nothing needs to survive a restart."""

    def __init__(self, cog: "Levels", guild_id: int, author_id: int, page: int, pages: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.page = page
        self.pages = pages
        self._refresh()

    def _refresh(self) -> None:
        self.previous_page.disabled = self.page <= 1
        self.next_page.disabled = self.page >= self.pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Only the person who asked can turn these pages. Use /leaderboard for your own.", ephemeral=True)
        return False

    async def _show(self, interaction: discord.Interaction, page: int) -> None:
        embed, self.page, self.pages = await self.cog.board_embed(self.guild_id, page)
        self._refresh()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(interaction, self.page - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(interaction, self.page + 1)


class Levels(Module):
    key = "levels"
    category = "Levels"
    schema = Schema({
        "enabled": Field(False, boolean),
        "message": Field(DEFAULT_MESSAGE, messages.clean_payload),
        "destination": Field("none", destination),
        "only_rewards": Field(False, boolean),
        "replace_rewards": Field(False, boolean),
        "voice_xp": Field(False, boolean),
        "rate": Field(1, integer(1, 25)),
        "window": Field(60, integer(1, 600)),
        "custom_xp": Field(None, optional(integer(1, 100))),
        "modulo": Field(1, integer(1, 100)),
        "minimum": Field(0, integer(0, MAX_LEVEL)),
        "blacklist_channels": Field([], snowflakes()),
        "blacklist_roles": Field([], snowflakes()),
        "rewards": Field([], rewards),
        "card_color": Field(0x00A2F8, hex_color),
        "card_accent": Field(0xFFFFFF, hex_color),
        "card_opacity": Field(75.0, number(0, 100)),
    })
    migrations = [
        """
        CREATE TABLE level_members (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0,
            messages INTEGER NOT NULL DEFAULT 0,
            voice_minutes INTEGER NOT NULL DEFAULT 0,
            name TEXT,
            left_at REAL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE INDEX level_members_board ON level_members (guild_id, left_at, xp DESC);
        CREATE TABLE level_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            level INTEGER NOT NULL,
            at REAL NOT NULL
        );
        CREATE INDEX level_events_guild ON level_events (guild_id, id DESC);
        CREATE TABLE level_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            made_at REAL NOT NULL,
            members INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE INDEX level_backups_guild ON level_backups (guild_id, id DESC);
        CREATE TABLE level_backgrounds (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            image TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()
        self._sleep = asyncio.sleep
        self._clock = time.time
        self._entries: dict[tuple[int, int], _Entry] = {}
        self._dirty: set[tuple[int, int]] = set()
        self._earned: set[tuple[int, int]] = set()  # who earned XP as a live member since the last write, which marks them present
        self._windows: dict[tuple[int, int], tuple[float, int]] = {}
        self._flusher: asyncio.Future | None = None
        self._save_lock = asyncio.Lock()
        self._imports: dict[int, dict] = {}
        self._tasks: set[asyncio.Future] = set()
        self._reconciled = False
        self._voice_running = False
        self._restoring: set[int] = set()
        self._syncs: dict[int, dict] = {}
        self._sync_dropped: dict[int, set[int]] = {}
        self._known_rewards: dict[int, tuple] = {}  # each server's rewards as of its last save, since a change to them has to know what went away
        self._out_of_reach: set[tuple[int, int]] = set()
        self._announce_notices: dict[tuple[int, int], float] = {}

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        given: dict[str, set[int]] = {}
        for row in settings["rewards"]:
            given.setdefault(f"rewards:{row['level']}", set()).add(row["role_id"])
        return given

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        target = settings["destination"]
        return {"destination": {target} if isinstance(target, int) else set()}

    async def on_load(self) -> None:
        for guild_id in self.bot.settings.guilds_with(self.key):
            self._known_rewards[guild_id] = self._rewards_of(self.cfg(guild_id))
        self.bot.settings.listen(self.key, self._settings_changed)
        self.bot.timers.handler("levels.voice", self._voice_due)
        self.bot.timers.handler("levels.backup", self._backup_due)
        self.bot.timers.handler("levels.sync", self._sync_due)
        now = self._clock()
        if await self.bot.timers.get_key("levels.voice", "levels.voice") is None:
            await self.bot.timers.schedule("levels.voice", now + VOICE_SECONDS, key="levels.voice")
        if await self.bot.timers.get_key("levels.backup", "levels.backup") is None:
            await self.bot.timers.schedule("levels.backup", now + BACKUP_SECONDS, key="levels.backup")

    async def cog_unload(self) -> None:
        for task in [*self._tasks, self._flusher]:
            if task is not None:
                task.cancel()
        try:
            await self.flush()
        except Exception:
            log.exception("Could not save XP while shutting down")
        await self.outside.close()

    async def before_restore(self, guild_id: int) -> None:
        await self.flush()
        self._restoring.add(guild_id)  # nothing is earned or saved for this server until the restore is over

    async def on_guild_restored(self, guild_id: int) -> None:
        async with self._save_lock:
            self._forget(guild_id)  # what memory holds is older than the tables now, and saving it would undo the restore

    async def after_restore(self, guild_id: int) -> None:
        self._restoring.discard(guild_id)

    def _spawn(self, work: Any) -> asyncio.Future:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- XP in memory, written in batches ----

    async def _entry(self, guild_id: int, user_id: int) -> _Entry:
        key = (guild_id, user_id)
        entry = self._entries.get(key)
        if entry is not None:
            return entry
        row = await self.db.one("SELECT xp, messages, voice_minutes, name FROM level_members WHERE guild_id = ? AND user_id = ?", key)
        entry = self._entries.get(key)  # another message may have loaded this while the read waited
        if entry is None:
            entry = _Entry(**row) if row else _Entry()
            self._entries[key] = entry
            self._trim()
        return entry

    def _trim(self) -> None:
        if len(self._entries) <= MAX_CACHED:
            return
        spare = [key for key in self._entries if key not in self._dirty][:MAX_CACHED // 10]
        for key in spare:
            del self._entries[key]
        now = self._clock()
        for key, (start, _) in list(self._windows.items()):
            if now - start > 3600:
                del self._windows[key]

    def _mark(self, key: tuple[int, int]) -> None:
        self._dirty.add(key)
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.ensure_future(self._flush_later())

    async def _flush_later(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await self.flush()
            except Exception:
                log.exception("Could not save XP. It stays in memory and the next save tries again.")
            if not self._dirty:
                return

    async def flush(self) -> None:
        """Write every changed member now."""
        async with self._save_lock:
            keys = [key for key in self._dirty if key[0] not in self._restoring]
            if not keys:
                return
            self._dirty.difference_update(keys)
            now = self._clock()
            rows = []
            for key in keys:
                entry = self._entries.get(key)
                if entry is not None:
                    rows.append((*key, entry.xp, entry.messages, entry.voice_minutes, entry.name, now, 1 if key in self._earned else 0))
            try:
                # Earning XP in the server marks someone present. A staff member's edit doesn't, so someone who left stays off the board.
                await self.db.many(
                    "INSERT INTO level_members (guild_id, user_id, xp, messages, voice_minutes, name, left_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?) "
                    "ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = excluded.xp, messages = excluded.messages, voice_minutes = excluded.voice_minutes, "
                    "name = COALESCE(excluded.name, level_members.name), left_at = CASE WHEN ? = 1 THEN NULL ELSE level_members.left_at END, updated_at = excluded.updated_at",
                    rows,
                )
            except BaseException:  # a shutdown cancels this too, and its last flush has to find these still waiting
                self._dirty.update(keys)
                raise
            self._earned.difference_update(keys)

    def _forget(self, guild_id: int, user_id: int | None = None) -> None:
        """Drop what memory holds, so the next read comes from the database."""
        for store in (self._entries, self._windows):
            for key in [k for k in store if k[0] == guild_id and (user_id is None or k[1] == user_id)]:
                del store[key]
        self._dirty = {k for k in self._dirty if not (k[0] == guild_id and (user_id is None or k[1] == user_id))}
        self._earned = {k for k in self._earned if not (k[0] == guild_id and (user_id is None or k[1] == user_id))}

    # ---- earning XP ----

    def _is_excluded(self, cfg: dict, channel: Any, member: Any) -> bool:
        blocked = cfg["blacklist_channels"]
        if blocked:
            parent = getattr(channel, "parent_id", None)  # a thread follows its parent, and a channel follows its category
            places = {channel.id, parent, getattr(channel, "category_id", None)}
            if places & set(blocked):
                return True
        blocked_roles = cfg["blacklist_roles"]
        return bool(blocked_roles) and any(role.id in blocked_roles for role in member.roles)

    def _take_window(self, key: tuple[int, int], cfg: dict) -> bool:
        """One award per rate-limit window, decided before anything waits, so a burst of messages can't all pass."""
        now = self._clock()
        start, used = self._windows.get(key, (0.0, 0))
        if now - start >= cfg["window"]:
            start, used = now, 0
        if used >= cfg["rate"]:
            return False
        self._windows[key] = (start, used + 1)
        return True

    @staticmethod
    def _amount(cfg: dict) -> int:
        return cfg["custom_xp"] or random.randint(15, 25)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply) or not hasattr(message.author, "roles"):
            return
        cfg = self.cfg(message.guild.id)
        if not cfg["enabled"] or self._is_excluded(cfg, message.channel, message.author):
            return
        if self._take_window((message.guild.id, message.author.id), cfg):
            await self._award(message.author, message.channel, cfg, message_count=1)

    async def _award(self, member: Any, channel: Any, cfg: dict, *, message_count: int = 0, voice_minutes: int = 0) -> None:
        guild = member.guild
        if guild.id in self._restoring:
            return
        key = (guild.id, member.id)
        entry = await self._entry(*key)
        before = level_for_xp(entry.xp)
        entry.xp += self._amount(cfg)
        entry.messages += message_count
        entry.voice_minutes += voice_minutes
        entry.name = member.display_name
        self._earned.add(key)
        self._mark(key)
        after = level_for_xp(entry.xp)
        if after > before:
            await self._level_up(member, channel, before, after, cfg)

    async def _level_up(self, member: Any, channel: Any, before: int, after: int, cfg: dict) -> None:
        await self.flush()
        event_id = await self.db.insert("INSERT INTO level_events (guild_id, user_id, level, at) VALUES (?, ?, ?, ?)", (member.guild.id, member.id, after, self._clock()))
        if event_id % 50 == 0:
            await self.db.run(
                "DELETE FROM level_events WHERE guild_id = ? AND id NOT IN (SELECT id FROM level_events WHERE guild_id = ? ORDER BY id DESC LIMIT ?)",
                (member.guild.id, member.guild.id, EVENTS_KEPT),
            )
        await self._apply_rewards(member, after, cfg)
        await self._announce(member, channel, before, after, cfg)

    # ---- rewards ----

    def _role_problem(self, guild: Any, role: Any) -> str | None:
        """Why the bot can't hand out this role, or None when it can."""
        if role is None or role.is_default() or role.managed:
            return "That role can't be handed out."
        if guild.me is None or role >= guild.me.top_role:
            return f"{role.mention} sits at or above the bot's top role, so the bot can't give it."
        if not role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE)):
            return f"{role.mention} holds moderator permissions. Turn on elevated roles on the Commands page to allow it."
        return None

    def _tell_staff_once(self, guild: Any, role: Any) -> None:
        """The reward stays on the list and the bot skips it, because moving the bot's role fixes it. Staff have no other way to learn why the role stopped coming."""
        key = (guild.id, role.id)
        if key in self._out_of_reach:
            return
        self._out_of_reach.add(key)
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, "A level reward is out of reach", f"{role.mention} sits at or above the bot's top role, so the bot can't give it. Move the bot's role above it, then run `/level sync`.")

    async def _apply_rewards(self, member: Any, level: int, cfg: dict, *, remove_above: bool = False, dropped: set[int] | frozenset[int] = frozenset()) -> bool:
        """Give what this level has earned. With replace on, only the highest reward stays. `remove_above` is for a level an admin lowered. `dropped` is roles just taken off the list. True if it changed their roles."""
        rows = cfg["rewards"]
        if not rows and not dropped:
            return False
        guild = member.guild
        earned = [row for row in rows if row["level"] <= level]
        usable = []
        for row in earned:
            role = guild.get_role(row["role_id"])
            problem = None if role is None else self._role_problem(guild, role)
            if problem:
                log.info("Level reward skipped in %s: %s", guild.id, problem)
                if guild.me is not None and role >= guild.me.top_role:
                    self._tell_staff_once(guild, role)
            elif role is not None:
                usable.append(row)
        # With replace on, a top reward the bot can't give must not cost someone the reward below it.
        keep = usable[-1:] if cfg["replace_rewards"] else usable
        kept_ids = {row["role_id"] for row in keep}
        drop = [row for row in earned if row["role_id"] not in kept_ids]
        if remove_above:
            drop += [row for row in rows if row["level"] > level and row["role_id"] not in kept_ids]
        add = [guild.get_role(row["role_id"]) for row in keep if member.get_role(row["role_id"]) is None]
        dropping = [row["role_id"] for row in drop] + [role_id for role_id in dropped if role_id not in kept_ids]
        remove = [role for role in (member.get_role(role_id) for role_id in dropping) if role is not None and guild.me is not None and role < guild.me.top_role]
        try:
            if add:
                await member.add_roles(*add, reason=f"Level {level} reward")
            if remove:
                await member.remove_roles(*remove, reason=f"Level {level} rewards changed")
        except discord.HTTPException as error:
            log.warning("Could not change reward roles for %s in %s: %s", member.id, guild.id, error)
        return bool(add or remove)

    @staticmethod
    def _rewards_of(cfg: dict) -> tuple[tuple[tuple[int, int], ...], bool]:
        return tuple((row["level"], row["role_id"]) for row in cfg["rewards"]), cfg["replace_rewards"]

    async def _settings_changed(self, guild_id: int, settings: dict) -> None:
        """Saving a change to the rewards brings everyone's roles in line, and a role taken off the list comes off the people who have it."""
        before = self._known_rewards.get(guild_id, ((), False))
        now = self._rewards_of(settings)
        self._known_rewards[guild_id] = now
        if before == now or not settings["enabled"] or guild_id in self._restoring or self.bot.get_guild(guild_id) is None:
            return
        self.start_role_sync(guild_id, dropped={role_id for _, role_id in before[0]} - {role_id for _, role_id in now[0]})

    def start_role_sync(self, guild_id: int, *, dropped: set[int] | None = None) -> dict:
        """One job per server. Asking again while one runs makes it go round once more, so a later change isn't missed."""
        job = self._syncs.get(guild_id)
        if job and job["state"] == "running":
            job["again"] = True
            self._sync_dropped[guild_id] |= dropped or set()
            return job
        job = {"state": "running", "checked": 0, "total": 0, "changed": 0, "again": False, "started_at": self._clock(), "finished_at": None}
        self._syncs[guild_id] = job
        self._sync_dropped[guild_id] = set(dropped or ())
        self._spawn(self._run_role_sync(guild_id, job))
        return job

    def role_sync_running(self, guild_id: int) -> bool:
        job = self._syncs.get(guild_id)
        return bool(job and job["state"] == "running")

    async def _run_role_sync(self, guild_id: int, job: dict) -> None:
        try:
            while True:
                job["again"] = False
                await self._role_sync_pass(guild_id, job)
                if not job["again"]:
                    break
            job["state"] = "done"
        except Exception:
            log.exception("The reward role sync failed for %s", guild_id)
            job["state"] = "failed"
        finally:
            job["finished_at"] = self._clock()

    async def _role_sync_pass(self, guild_id: int, job: dict) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        await self.flush()
        dropped = self._sync_dropped.get(guild_id, set())
        job["checked"] = 0
        job["total"] = await self.db.scalar("SELECT COUNT(*) FROM level_members WHERE guild_id = ? AND left_at IS NULL", (guild_id,)) or 0
        after = 0
        while True:
            rows = await self.db.all("SELECT user_id, xp FROM level_members WHERE guild_id = ? AND left_at IS NULL AND user_id > ? ORDER BY user_id LIMIT ?", (guild_id, after, SYNC_PAGE))
            if not rows:
                break
            for row in rows:
                after = row["user_id"]
                job["checked"] += 1
                member = guild.get_member(after)
                if member is None or member.bot:
                    continue
                entry = self._entries.get((guild_id, after))
                if await self._apply_rewards(member, level_for_xp(entry.xp if entry else row["xp"]), self.cfg(guild_id), remove_above=True, dropped=dropped):
                    job["changed"] += 1
                    await self._sleep(SYNC_PAUSE)

    async def _sync_after_xp_change(self, guild_id: int) -> int:
        """Everyone's XP was replaced. Returns how many members' levels now reach a reward."""
        rows = self.cfg(guild_id)["rewards"]
        if not rows or self.bot.get_guild(guild_id) is None:
            return 0
        lowest = min(row["level"] for row in rows)
        self.start_role_sync(guild_id)
        return await self.db.scalar("SELECT COUNT(*) FROM level_members WHERE guild_id = ? AND left_at IS NULL AND xp >= ?", (guild_id, xp_for_level(lowest))) or 0

    def _reward_out_of_reach(self, actor: Any, before: int, after: int) -> Any:
        """A reward role this change of level would hand out that the person making it can't give, or None. The role has to stay out for good, since their next level-up would hand it over anyway, so the change itself is refused."""
        if access.is_admin(actor):
            return None
        for row in self.cfg(actor.guild.id)["rewards"]:
            role = actor.guild.get_role(row["role_id"])
            if before < row["level"] <= after and role is not None and role >= actor.top_role and self._role_problem(actor.guild, role) is None:
                return role
        return None

    # ---- announcements ----

    async def _announce(self, member: Any, channel: Any, before: int, after: int, cfg: dict) -> None:
        target = cfg["destination"]
        if target == "none" or after < cfg["minimum"] or after % cfg["modulo"]:
            return
        if cfg["only_rewards"] and not any(before < row["level"] <= after for row in cfg["rewards"]):
            return
        place: Any = channel
        if target == "dm":
            place = member
        elif target != "same":
            place = member.guild.get_channel_or_thread(target)
        if place is None or not hasattr(place, "send"):
            self._announce_failed(member.guild, target, "That channel no longer exists.")
            return
        entry = await self._entry(member.guild.id, member.id)
        scope = Scope(guild=member.guild, member=member, channel=channel, extra={"lvl": str(after), "level": str(after), "xp": str(entry.xp)})
        payload = await render_message(cfg["message"], scope)
        if not messages.has_content(payload):
            return
        try:
            # Only the member leveling up gets pinged, since a nickname can hold another person's mention.
            await place.send(**messages.send_kwargs(payload, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member])))
        except discord.HTTPException as error:
            if target == "dm":
                log.info("Could not send a level-up by DM to %s: %s", member.id, error)  # members close their DMs, and that's theirs to do
            else:
                self._announce_failed(member.guild, getattr(place, "id", 0), f"Discord refused the post: {error.text or error.status}")

    def _announce_failed(self, guild: Any, channel_id: int, why: str) -> None:
        """Level-ups stopping leaves no other sign, so staff hear about it once a day per channel."""
        now = time.monotonic()
        if self._announce_notices.get((guild.id, channel_id), -NOTICE_SECONDS) + NOTICE_SECONDS > now:
            return
        if len(self._announce_notices) > 1000:
            self._announce_notices.clear()
        self._announce_notices[(guild.id, channel_id)] = now
        log.warning("Could not announce a level-up in %s, channel %s: %s", guild.id, channel_id, why)
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, "Level-ups can't be posted", f"Level-up messages can't go to <#{channel_id}>. {why} Pick another place on the Levels page.")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        if self.cfg(channel.guild.id)["destination"] == channel.id:
            await self.bot.settings.save(channel.guild.id, self.key, {"destination": "same"}, action="level.channel_deleted")

    # ---- people coming and going ----

    @commands.Cog.listener()
    async def on_member_remove(self, member: Any) -> None:
        if not self.cfg(member.guild.id)["enabled"] or member.bot:
            return
        key = (member.guild.id, member.id)
        if key in self._dirty:
            await self.flush()
        self._forget(*key)
        await self.db.run("UPDATE level_members SET left_at = ? WHERE guild_id = ? AND user_id = ?", (self._clock(), *key))

    @commands.Cog.listener()
    async def on_member_join(self, member: Any) -> None:
        if self.cfg(member.guild.id)["enabled"] and not member.bot and not member.pending:
            await self._welcome_back(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: Any, after: Any) -> None:
        if before.pending and not after.pending and self.cfg(after.guild.id)["enabled"] and not after.bot:
            await self._welcome_back(after)

    async def _welcome_back(self, member: Any) -> None:
        """Someone returning keeps their level, and their reward roles come back with it."""
        key = (member.guild.id, member.id)
        await self.db.run("UPDATE level_members SET left_at = NULL WHERE guild_id = ? AND user_id = ?", key)
        cfg = self.cfg(member.guild.id)
        if cfg["rewards"]:
            xp = await self.db.scalar("SELECT xp FROM level_members WHERE guild_id = ? AND user_id = ?", key)
            if xp:
                await self._apply_rewards(member, level_for_xp(xp), cfg)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Mark anyone who left while the bot was down, and anyone who came back, so the leaderboard shows the people here."""
        if self._reconciled:
            return
        self._reconciled = True
        await self.bot.members_settled.wait()
        for guild in self.bot.guilds:
            if not self.cfg(guild.id)["enabled"] or not await whole_member_list(guild):
                continue
            present = {member.id for member in guild.members}
            rows = await self.db.all("SELECT user_id, left_at FROM level_members WHERE guild_id = ?", (guild.id,))
            gone = [(self._clock(), guild.id, row["user_id"]) for row in rows if row["user_id"] not in present and row["left_at"] is None]
            back = [(guild.id, row["user_id"]) for row in rows if row["user_id"] in present and row["left_at"] is not None]
            if gone:
                await self.db.many("UPDATE level_members SET left_at = ? WHERE guild_id = ? AND user_id = ?", gone)
            if back:
                await self.db.many("UPDATE level_members SET left_at = NULL WHERE guild_id = ? AND user_id = ?", back)

    # ---- voice ----

    async def _voice_due(self, payload: dict, row: dict) -> None:
        await self.bot.timers.schedule("levels.voice", self._clock() + VOICE_SECONDS, key="levels.voice")
        if not self._voice_running:
            self._spawn(self._voice_round())  # other timers wait while a handler runs, so this one doesn't hold them up

    async def _voice_round(self) -> None:
        self._voice_running = True
        try:
            await self.voice_tick()
        except Exception:
            log.exception("Voice XP round failed")
        finally:
            self._voice_running = False

    async def voice_tick(self) -> None:
        """Each minute in a voice channel with at least one other person earns what a message earns."""
        for guild_id in self.bot.settings.guilds_with(self.key):
            cfg = self.cfg(guild_id)
            guild = self.bot.get_guild(guild_id)
            if guild is None or not cfg["enabled"] or not cfg["voice_xp"]:
                continue
            away = getattr(guild, "afk_channel", None)
            for channel in guild.channels:
                if channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice) or channel == away:
                    continue
                people = [member for member in channel.members if not member.bot]
                if len(people) < 2:
                    continue
                for member in people:
                    state = member.voice
                    if state is not None and (state.self_deaf or state.deaf):
                        continue
                    if not self._is_excluded(cfg, channel, member):
                        await self._award(member, channel, cfg, voice_minutes=1)

    # ---- backups ----

    async def snapshot(self, guild_id: int, reason: str) -> int | None:
        """Keep a copy of everyone's XP. The newest few stay, and each one can be restored."""
        await self.flush()
        rows = await self.db.all("SELECT user_id, xp, messages, voice_minutes, name, left_at FROM level_members WHERE guild_id = ?", (guild_id,))
        if not rows:
            return None
        data = json.dumps([[r["user_id"], r["xp"], r["messages"], r["voice_minutes"], r["name"], r["left_at"]] for r in rows])
        backup_id = await self.db.insert(
            "INSERT INTO level_backups (guild_id, reason, made_at, members, data) VALUES (?, ?, ?, ?, ?)", (guild_id, reason, self._clock(), len(rows), data),
        )
        await self.db.run(
            "DELETE FROM level_backups WHERE guild_id = ? AND id NOT IN (SELECT id FROM level_backups WHERE guild_id = ? ORDER BY id DESC LIMIT ?)",
            (guild_id, guild_id, BACKUPS_KEPT),
        )
        return backup_id

    async def _backup_due(self, payload: dict, row: dict) -> None:
        await self.bot.timers.schedule("levels.backup", self._clock() + BACKUP_SECONDS, key="levels.backup")
        self._spawn(self._backup_round())

    async def _backup_round(self) -> None:
        for guild_id in self.bot.settings.guilds_with(self.key):
            if self.cfg(guild_id)["enabled"]:
                try:
                    await self.snapshot(guild_id, "daily")
                    await self._sweep_backgrounds(guild_id)
                except Exception:
                    log.exception("Daily XP backup failed for %s", guild_id)

    async def _sweep_backgrounds(self, guild_id: int) -> None:
        """A background is about 100 KB, and someone who left a month ago isn't coming back for theirs. The server's own stays."""
        await self.db.run(
            "DELETE FROM level_backgrounds WHERE guild_id = ? AND user_id != ? AND user_id IN (SELECT user_id FROM level_members WHERE guild_id = ? AND left_at < ?)",
            (guild_id, SERVER_BACKGROUND, guild_id, self._clock() - BACKGROUND_DAYS_KEPT * 86400),
        )

    async def forget_person(self, guild_id: int, user_id: int) -> int:
        """Someone deleted their level data, so it comes out of the copies too, or restoring one would bring it back. Returns how many copies held them."""
        changed = 0
        for row in await self.db.all("SELECT id, data FROM level_backups WHERE guild_id = ?", (guild_id,)):
            people = await asyncio.to_thread(json.loads, row["data"])
            kept = [person for person in people if person[0] != user_id]
            if len(kept) != len(people):
                data = await asyncio.to_thread(json.dumps, kept)
                await self.db.run("UPDATE level_backups SET data = ?, members = ? WHERE id = ? AND guild_id = ?", (data, len(kept), row["id"], guild_id))
                changed += 1
        return changed

    async def restore_backup(self, guild_id: int, backup_id: int | None = None) -> dict | None:
        """Put a backup back, the newest when none is named. What it replaces becomes a backup too, so a restore can be undone."""
        if backup_id is None:
            row = await self.db.one("SELECT * FROM level_backups WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (guild_id,))
        else:
            row = await self.db.one("SELECT * FROM level_backups WHERE guild_id = ? AND id = ?", (guild_id, backup_id))
        if row is None:
            return None
        await self.snapshot(guild_id, "before restore")
        await self._replace_members(guild_id, json.loads(row["data"]))
        return {"id": row["id"], "made_at": row["made_at"], "members": row["members"], "role_updates": await self._sync_after_xp_change(guild_id)}

    async def _replace_members(self, guild_id: int, rows: list[list]) -> None:
        now = self._clock()
        async with self._save_lock:
            async with self.db.transaction():
                await self.db.run("DELETE FROM level_members WHERE guild_id = ?", (guild_id,))
                await self.db.many(
                    "INSERT INTO level_members (guild_id, user_id, xp, messages, voice_minutes, name, left_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(guild_id, *row, now) for row in rows],
                )
            self._forget(guild_id)

    async def reset_server(self, guild_id: int) -> int:
        await self.snapshot(guild_id, "before reset")
        async with self._save_lock:
            count = await self.db.run("DELETE FROM level_members WHERE guild_id = ?", (guild_id,))
            self._forget(guild_id)
        return count

    async def reset_member(self, guild_id: int, user_id: int) -> bool:
        async with self._save_lock:
            self._forget(guild_id, user_id)
            count = await self.db.run("DELETE FROM level_members WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return count > 0

    # ---- changing someone's XP by hand ----

    async def set_xp(self, guild_id: int, user_id: int, xp: int, *, name: str | None = None) -> None:
        entry = await self._entry(guild_id, user_id)
        entry.xp = max(0, min(MAX_XP, xp))
        entry.name = name or entry.name
        self._dirty.add((guild_id, user_id))
        await self.flush()

    async def _sync_roles(self, guild: Any, user_id: int) -> None:
        member = guild.get_member(user_id)
        if member is not None:
            entry = await self._entry(guild.id, user_id)
            await self._apply_rewards(member, level_for_xp(entry.xp), self.cfg(guild.id), remove_above=True)

    async def _sync_due(self, payload: dict, row: dict) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        if guild is not None:
            await self._sync_roles(guild, payload["user_id"])

    # ---- reading ----

    async def rank_of(self, guild_id: int, xp: int) -> int:
        count = await self.db.scalar("SELECT COUNT(*) FROM level_members WHERE guild_id = ? AND left_at IS NULL AND xp > ?", (guild_id, xp))
        return (count or 0) + 1

    async def board(self, guild_id: int, *, page: int = 1, size: int = BOARD_PAGE, search: str = "", include_left: bool = False) -> dict:
        await self.flush()
        where = ["guild_id = ?"]
        args: list[Any] = [guild_id]
        if not include_left:
            where.append("left_at IS NULL")
        search = search.strip()
        user_id = whole_number(search)
        if user_id is not None:
            where.append("user_id = ?")
            args.append(user_id)
        elif search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("name LIKE ? ESCAPE '\\'")
            args.append(f"%{escaped}%")
        clause = " AND ".join(where)
        total = await self.db.scalar(f"SELECT COUNT(*) FROM level_members WHERE {clause}", args) or 0
        pages = max(1, (total + size - 1) // size)
        page = max(1, min(page, pages))
        offset = (page - 1) * size
        rows = await self.db.all(
            f"SELECT user_id, xp, messages, voice_minutes, name, left_at FROM level_members WHERE {clause} ORDER BY xp DESC, user_id LIMIT ? OFFSET ?",
            [*args, size, offset],
        )
        for index, row in enumerate(rows):
            row["level"] = level_for_xp(row["xp"])
            row["rank"] = await self.rank_of(guild_id, row["xp"]) if search else offset + index + 1
        return {"rows": rows, "page": page, "pages": pages, "total": total}

    async def board_embed(self, guild_id: int, page: int) -> tuple[discord.Embed, int, int]:
        data = await self.board(guild_id, page=page)
        guild = self.bot.get_guild(guild_id)
        lines = []
        for row in data["rows"]:
            member = guild.get_member(row["user_id"]) if guild else None
            name = member.display_name if member else (row["name"] or f"User {row['user_id']}")
            lines.append(f"**{row['rank']:,}.** {discord.utils.escape_markdown(name)}: level {row['level']:,}, {row['xp']:,} XP")
        embed = discord.Embed(title=f"{guild.name if guild else 'Server'} leaderboard", description="\n".join(lines) or "Nobody has earned XP yet.", color=0x00A2F8)
        embed.set_footer(text=f"Page {data['page']} of {data['pages']}, {data['total']:,} members")
        return embed, data["page"], data["pages"]

    async def _card_background(self, guild_id: int, user_id: int | None) -> bytes | None:
        rows = await self.db.all("SELECT user_id, image FROM level_backgrounds WHERE guild_id = ? AND user_id IN (?, ?)", (guild_id, user_id or SERVER_BACKGROUND, SERVER_BACKGROUND))
        by_owner = {row["user_id"]: row["image"] for row in rows}
        image = by_owner.get(user_id) or by_owner.get(SERVER_BACKGROUND)
        return base64.b64decode(image) if image else None

    async def _store_background(self, guild_id: int, user_id: int, image: bytes | None) -> None:
        """Stored as text in the database. A file's link on Discord expires, and text passes through a JSON backup as it is."""
        if image is None:
            await self.db.run("DELETE FROM level_backgrounds WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            return
        await self.db.run(
            "INSERT INTO level_backgrounds (guild_id, user_id, image, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET image = excluded.image, updated_at = excluded.updated_at",
            (guild_id, user_id, base64.b64encode(image).decode(), self._clock()),
        )

    async def card_for(self, member: Any) -> bytes:
        await self.flush()
        guild = member.guild
        cfg = self.cfg(guild.id)
        entry = await self._entry(guild.id, member.id)
        level = level_for_xp(entry.xp)
        rank = await self.rank_of(guild.id, entry.xp) if entry.xp else None
        avatar = None
        try:
            avatar = await member.display_avatar.replace(size=256).read()
        except (discord.HTTPException, ValueError, AttributeError) as error:
            log.debug("No avatar for the card of %s: %s", member.id, error)  # the card draws their initial instead
        background = await self._card_background(guild.id, member.id)
        return await asyncio.to_thread(
            rankcard.draw, name=member.display_name, level=level, rank=rank, into_level=entry.xp - xp_for_level(level), level_size=xp_to_next(level),
            avatar=avatar, background=background, color=cfg["card_color"], accent=cfg["card_accent"], opacity=cfg["card_opacity"],
        )

    # ---- commands ----

    async def _save(self, ctx: commands.Context, action: str, form: dict) -> bool:
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return False
        return True

    async def _confirm(self, ctx: commands.Context, prompt: str, label: str) -> bool:
        """Something that replaces everyone's XP asks first. A misread menu choice mustn't wipe the board."""
        view = ConfirmView(ctx.author.id, label)
        sent = await ctx.send(prompt, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        if sent is None:
            return False
        await view.wait()
        if view.answer is None:
            try:
                await sent.edit(content="No answer, so nothing changed.", view=None)
            except discord.HTTPException:
                pass
        return bool(view.answer)

    async def _levels_off(self, ctx: commands.Context) -> bool:
        if self.cfg(ctx.guild.id)["enabled"]:
            return False
        await ctx.fail(f"Levels are off in this server. A manager can turn them on at {self.bot.public_url}/dashboard/{ctx.guild.id}/levels")
        return True

    async def _send_card(self, ctx: commands.Context, member: Any) -> None:
        if await self._levels_off(ctx):
            return
        if member.bot:
            await ctx.fail("Bots don't earn XP.")
            return
        await ctx.defer()
        png = await self.card_for(member)
        await ctx.send(file=discord.File(io.BytesIO(png), filename="rank.png"))

    @commands.hybrid_group(name="level", description="Levels, XP and rank cards")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.member)
    async def level(self, ctx: commands.Context, *, member_text: str = "") -> None:
        """Typed with a name, this is that member's card. A word that's neither a command nor a member says so, since showing the asker's card would look like it worked."""
        if not member_text:
            await self._send_card(ctx, ctx.author)
            return
        try:
            member = await commands.MemberConverter().convert(ctx, member_text)
        except commands.BadArgument:
            await ctx.fail(f"`{discord.utils.remove_markdown(member_text)[:80]}` isn't a member or a level command. `/help level` lists the commands.")
            return
        await self._send_card(ctx, member)

    @level.command(name="member", description="Show a member's rank card")
    @commands.cooldown(1, 5, commands.BucketType.member)
    async def level_member(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        await self._send_card(ctx, member or ctx.author)

    @level.command(name="config", description="Show the level settings, or set the card's color, accent or opacity")
    @requires("manage_guild")
    async def level_config(self, ctx: commands.Context, choice: Literal["color", "opacity", "accent"] | None = None, value: str | None = None) -> None:
        if choice is not None:
            if value is None:
                await ctx.fail(f"Give a value for {choice}: six hex digits for a color, or 0 for clear up to 100 for solid.")
                return
            form: dict[str, Any]
            if choice == "opacity":
                try:
                    amount = float(value)
                    form = {"card_opacity": amount * 100 if "." in value and 0 < amount <= 1 else amount}  # 0.5 is half and 1.0 is solid, as a share of the whole
                except ValueError:
                    form = {"card_opacity": value}  # the schema's check turns this into a sentence
            else:
                form = {"card_color" if choice == "color" else "card_accent": value}
            if await self._save(ctx, f"level.config.{choice}", form):
                await ctx.ok(f"The card's {choice} is now `{value}`.")
            return
        cfg = self.cfg(ctx.guild.id)
        target = cfg["destination"]
        where = {"none": "not announced", "same": "the same channel", "dm": "a DM"}.get(target, f"<#{target}>")
        rewards_text = fit_list([f"level {row['level']}: <@&{row['role_id']}>" for row in cfg["rewards"]], 3400) or "none"
        embed = discord.Embed(title="Level settings", color=0x00A2F8, description=(
            f"Levels: {'on' if cfg['enabled'] else 'off'}\n"
            f"XP per message: {cfg['custom_xp'] or '15 to 25'}\n"
            f"Rate: {cfg['rate']} per {cfg['window']} seconds\n"
            f"Voice XP: {'on' if cfg['voice_xp'] else 'off'}\n"
            f"Level-ups go to: {where}\n"
            f"Announce from level {cfg['minimum']}, multiples of {cfg['modulo']}{', rewards only' if cfg['only_rewards'] else ''}\n"
            f"Rewards ({'replace' if cfg['replace_rewards'] else 'stack'}): {rewards_text}\n"
            f"Card: color #{cfg['card_color']:06x}, accent #{cfg['card_accent']:06x}, opacity {cfg['card_opacity']:g}\n"
            f"All settings: {self.bot.public_url}/dashboard/{ctx.guild.id}/levels"
        ))
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @level.command(name="set_message_xp", description="Set how much XP each message earns, or go back to the default 15 to 25")
    @requires("manage_guild")
    async def level_set_message_xp(self, ctx: commands.Context, choice: Literal["set", "default"], xp: commands.Range[int, 1, 100] | None = None) -> None:
        if choice == "default":
            if await self._save(ctx, "level.message_xp", {"custom_xp": None}):
                await ctx.ok("Messages earn 15 to 25 XP again.")
            return
        if xp is None:
            await ctx.fail("Give the XP per message, from 1 to 100.")
            return
        if await self._save(ctx, "level.message_xp", {"custom_xp": xp}):
            await ctx.ok(f"Messages now earn {xp} XP.")

    @level.command(name="messagexp", aliases=["msgxp"], with_app_command=False, description="Set how much XP each message earns. With no number, go back to the default")
    @requires("manage_guild")
    async def level_messagexp(self, ctx: commands.Context, xp: commands.Range[int, 1, 100] | None = None) -> None:
        await self.level_set_message_xp.callback(self, ctx, "default" if xp is None else "set", xp)

    async def _manual_change_refusal(self, ctx: commands.Context, user: Any, new_xp: int | None) -> str | None:
        """What stops someone changing this person's XP by hand. It answers the same as the dashboard."""
        if user.bot:
            return "Bots don't earn XP."
        known = await self.db.scalar("SELECT 1 FROM level_members WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, user.id))
        if not known:
            try:
                here = await find_member(ctx.guild, user.id)
            except discord.HTTPException as error:
                log.warning("Could not look up %s for a level change: %s", user.id, error)
                return "Discord didn't answer when I looked that person up. Try again."
            if here is None:
                return "That person isn't in this server."
        if new_xp is None:
            return None
        entry = await self._entry(ctx.guild.id, user.id)
        role = self._reward_out_of_reach(ctx.author, level_for_xp(entry.xp), level_for_xp(new_xp))
        return f"That level earns {role.mention}, which sits at or above your top role." if role else None

    @level.command(name="add_xp", aliases=["addxp"], description="Add XP to a member")
    @requires("manage_guild")
    async def level_add_xp(self, ctx: commands.Context, xp: commands.Range[int, 1, 1000], member: discord.User) -> None:
        entry = await self._entry(ctx.guild.id, member.id)
        refusal = await self._manual_change_refusal(ctx, member, entry.xp + xp)
        if refusal:
            await ctx.fail(refusal)
            return
        await self.set_xp(ctx.guild.id, member.id, entry.xp + xp, name=getattr(member, "display_name", None))
        await self._sync_roles(ctx.guild, member.id)
        await ctx.ok(f"{member.mention} now has {entry.xp:,} XP, which is level {level_for_xp(entry.xp)}.")

    @level.command(name="set_level", aliases=["setlevel"], description="Set a member's level")
    @requires("manage_guild")
    async def level_set_level(self, ctx: commands.Context, level: commands.Range[int, 0, 1000], member: discord.User) -> None:
        refusal = await self._manual_change_refusal(ctx, member, xp_for_level(level))
        if refusal:
            await ctx.fail(refusal)
            return
        await self.set_xp(ctx.guild.id, member.id, xp_for_level(level), name=getattr(member, "display_name", None))
        await self._sync_roles(ctx.guild, member.id)
        await ctx.ok(f"{member.mention} is now level {level}.")

    @level.group(name="blacklist", fallback="show", description="Show the channels and roles that earn no XP")
    @requires("manage_guild")
    async def level_blacklist(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        channels = fit_list([f"<#{c}>" for c in cfg["blacklist_channels"]], 950) or "none"
        roles = fit_list([f"<@&{r}>" for r in cfg["blacklist_roles"]], 950) or "none"
        await ctx.send(f"No XP in: {channels}\nNo XP for: {roles}", allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _parse_targets(guild: Any, text_: str) -> tuple[list[int], list[int], list[str]]:
        """Channels and roles out of free text, as mentions, IDs or names, so slash and prefix use read the same."""
        channels, roles, unknown = [], [], []
        for word in text_.split():
            found = re.fullmatch(r"<#(\d+)>|<@&(\d+)>|(\d{15,21})", word)
            if found:
                number_ = int(found.group(1) or found.group(2) or found.group(3))
                if guild.get_channel(number_) is not None:
                    channels.append(number_)
                    continue
                if guild.get_role(number_) is not None:
                    roles.append(number_)
                    continue
            else:
                name = word.lstrip("#@&")
                channel = discord.utils.get(guild.channels, name=name)
                role = discord.utils.get(guild.roles, name=name)
                if channel is not None:
                    channels.append(channel.id)
                    continue
                if role is not None:
                    roles.append(role.id)
                    continue
            unknown.append(word)
        return channels, roles, unknown

    async def _change_blacklist(self, ctx: commands.Context, targets: str, *, add: bool) -> None:
        channels, roles, unknown = self._parse_targets(ctx.guild, targets)
        if unknown or not (channels or roles):
            await ctx.fail(f"Couldn't find {', '.join(f'`{word}`' for word in unknown) or 'a channel or role'}. Mention them or use their IDs.")
            return
        cfg = self.cfg(ctx.guild.id)
        if add:
            for channel_id in channels:
                refusal = access.view_refusal(ctx.author, ctx.guild.get_channel(channel_id))
                if refusal:
                    await ctx.fail(refusal)
                    return
            form = {"blacklist_channels": [*cfg["blacklist_channels"], *channels], "blacklist_roles": [*cfg["blacklist_roles"], *roles]}
        else:
            form = {"blacklist_channels": [c for c in cfg["blacklist_channels"] if c not in channels], "blacklist_roles": [r for r in cfg["blacklist_roles"] if r not in roles]}
        if await self._save(ctx, "level.blacklist", form):
            shown = ", ".join([*(f"<#{c}>" for c in channels), *(f"<@&{r}>" for r in roles)])
            await ctx.ok(f"{shown}: {'no XP from now on' if add else 'earning XP again'}.")

    @level_blacklist.command(name="add", description="Stop channels or roles from earning XP. Mention as many as you like.")
    @requires("manage_guild")
    async def level_blacklist_add(self, ctx: commands.Context, *, targets: str) -> None:
        await self._change_blacklist(ctx, targets, add=True)

    @level_blacklist.command(name="remove", description="Let channels or roles earn XP again. Mention as many as you like.")
    @requires("manage_guild")
    async def level_blacklist_remove(self, ctx: commands.Context, *, targets: str) -> None:
        await self._change_blacklist(ctx, targets, add=False)

    async def _read_mee6_page(self, guild_id: int, page: int) -> dict:
        for _ in range(MEE6_TRIES):
            try:
                reply = await self.outside.get(MEE6_URL.format(guild_id=guild_id), params={"limit": MEE6_PAGE, "page": page}, timeout=30.0)
            except OutsideError as error:
                raise ValueError(f"Could not reach MEE6. {error}") from error
            if reply.status == 200:
                try:
                    data = reply.json()
                except OutsideError as error:
                    raise ValueError("MEE6 sent something unreadable. Try again later.") from error
                if not isinstance(data, dict):
                    raise ValueError("MEE6 sent something unreadable. Try again later.")
                return data
            if reply.status == 429:
                await self._sleep(min(60.0, reply.retry_after(5.0)))
                continue
            if reply.status in (401, 403):
                raise ValueError("MEE6 keeps this server's leaderboard private. Turn on the public leaderboard in MEE6's Levels settings, then try again.")
            if reply.status == 404:
                raise ValueError("MEE6 has no leaderboard for this server.")
            raise ValueError(f"MEE6 answered with error {reply.status}. Try again later.")
        raise ValueError("MEE6 kept asking the bot to slow down. Try again in a few minutes.")

    def _start_import(self, guild_id: int) -> dict:
        job = self._imports.get(guild_id)
        if job and job["state"] == "running":
            raise ValueError("An import is already running.")
        job = {"state": "running", "members": 0, "rewards": 0, "rewards_skipped": 0, "role_updates": 0, "error": None, "started_at": self._clock(), "finished_at": None}
        self._imports[guild_id] = job
        return job

    async def import_mee6(self, guild_id: int, actor: Any, job: dict) -> dict:
        """Replace everyone's XP with what MEE6 holds, after keeping a backup. Reward roles join any rewards already set here. Raises ValueError."""
        try:
            players: dict[int, list] = {}
            role_rewards: list = []
            for page in range(MEE6_MAX_PAGES):
                data = await self._read_mee6_page(guild_id, page)
                if page == 0:
                    role_rewards = data.get("role_rewards") or []
                rows = data.get("players") or []
                for row in rows:
                    try:
                        players[int(row["id"])] = [int(row["id"]), max(0, int(row["xp"])), max(0, int(row.get("message_count") or 0)), 0, row.get("username"), None]
                    except (KeyError, TypeError, ValueError):
                        continue
                job["members"] = len(players)
                if len(rows) < MEE6_PAGE:
                    break
            if not players:
                raise ValueError("MEE6's leaderboard for this server is empty.")
            guild = self.bot.get_guild(guild_id)
            if guild is not None and await whole_member_list(guild):  # MEE6's board holds people who left, and they don't belong on this one
                present = {member.id for member in guild.members}
                for user_id, player in players.items():
                    if user_id not in present:
                        player[5] = self._clock()
            await self.snapshot(guild_id, "before import")
            await self._replace_members(guild_id, list(players.values()))
            job["rewards"], job["rewards_skipped"] = await self._take_mee6_rewards(guild_id, role_rewards, actor)
            job["role_updates"] = await self._sync_after_xp_change(guild_id)
            job["state"] = "done"
            job["finished_at"] = self._clock()
            await self.bot.activity_log.record(guild_id, actor, "level.import", detail=f"MEE6, {len(players)} members")
            return job
        except ValueError as error:
            job["state"] = "failed"
            job["error"] = str(error)
            job["finished_at"] = self._clock()
            raise
        except Exception:
            job["state"] = "failed"
            job["error"] = "The import failed. It's been logged."
            job["finished_at"] = self._clock()
            raise

    def _can_take_reward(self, guild: Any, actor: Any, role_id: int) -> bool:
        """An import is a way to set up a reward, so it stops at the same place as `/level reward` and the settings form: below the bot, and below the person, whatever MEE6's panel holds."""
        if guild is None:
            return True  # the demo has no real roles to rank
        role = guild.get_role(role_id)
        if role is None or self._role_problem(guild, role):
            return False
        if actor is None:
            return True
        person = guild.get_member(actor.id)
        return person is not None and (access.is_admin(person) or role < person.top_role)

    async def _take_mee6_rewards(self, guild_id: int, role_rewards: list, actor: Any) -> tuple[int, int]:
        """Returns how many rewards joined the list, and how many were left out for being out of reach."""
        info = self.bot.ops.describe_guild(guild_id) or {}
        known = {role["id"] for role in info.get("roles", [])}
        guild = self.bot.get_guild(guild_id)
        current = list(self.cfg(guild_id)["rewards"])
        taken = {row["level"] for row in current}
        added = skipped = 0
        for row in role_rewards:
            try:
                level, role_id = int(row["rank"]), int(row["role"]["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if role_id in known and level not in taken and 1 <= level <= MAX_LEVEL:  # one reward per level here, so the first at each level is used
                if not self._can_take_reward(guild, actor, role_id):
                    skipped += 1
                    continue
                current.append({"level": level, "role_id": role_id})
                taken.add(level)
                added += 1
        if added:
            await self.bot.settings.save(guild_id, self.key, {"rewards": current}, actor=actor, action="level.import.rewards")
        return added, skipped

    @level.command(name="mee6import", description="Replace everyone's XP with their XP from MEE6's public leaderboard")
    @requires("manage_guild")
    async def level_mee6import(self, ctx: commands.Context) -> None:
        count = await self.db.scalar("SELECT COUNT(*) FROM level_members WHERE guild_id = ?", (ctx.guild.id,)) or 0
        if not await self._confirm(ctx, f"This replaces the XP of all {count:,} members here with what MEE6's public leaderboard holds. The current XP goes into a backup first.", "Import"):
            return
        try:
            job = await self.import_mee6(ctx.guild.id, self.bot.actor(ctx.author), self._start_import(ctx.guild.id))
        except ValueError as error:
            await ctx.fail(str(error))
            return
        left_out = f" {job['rewards_skipped']} reward roles were left out, because they sit above your top role or the bot's, or hold moderator permissions." if job["rewards_skipped"] else ""
        roles = f" Roles are being updated for {job['role_updates']:,} members." if job["role_updates"] else ""
        await ctx.ok(f"Imported {job['members']:,} members and {job['rewards']} reward roles.{left_out}{roles} The old XP is in a backup, so `/level restore` brings it back.")

    @level.command(name="sync", description="Give and take reward roles so everyone's match their level")
    @requires("manage_guild")
    async def level_sync(self, ctx: commands.Context) -> None:
        if self.role_sync_running(ctx.guild.id):
            job = self._syncs[ctx.guild.id]
            await ctx.fail(f"A role update is already running, {job['checked']:,} of {job['total']:,} members checked.")
            return
        self.start_role_sync(ctx.guild.id)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "level.sync")
        await ctx.ok("Updating everyone's reward roles. This runs in the background, at one change a second.")

    @level.command(name="rate", description="Show or change how often XP can be earned, such as 1/60 for one message per minute")
    @requires("manage_guild")
    async def level_rate(self, ctx: commands.Context, *, rate: str | None = None) -> None:
        cfg = self.cfg(ctx.guild.id)
        if rate is None:
            await ctx.send(f"Members earn XP from {cfg['rate']} message(s) per {cfg['window']} seconds.", ephemeral=True)
            return
        parsed = parse_rate(rate)
        if parsed is None:
            await ctx.fail("Write it as messages/seconds, such as `1/60`.")
            return
        if await self._save(ctx, "level.rate", {"rate": parsed[0], "window": parsed[1]}):
            await ctx.ok(f"Members now earn XP from {parsed[0]} message(s) per {parsed[1]} seconds.")

    @level.command(name="reset", description="Reset the XP of one member or the whole server")
    @requires("manage_guild")
    async def level_reset(self, ctx: commands.Context, choice: Literal["member", "server", "all"], member: discord.User | None = None) -> None:
        if choice in ("server", "all"):
            count = await self.db.scalar("SELECT COUNT(*) FROM level_members WHERE guild_id = ?", (ctx.guild.id,)) or 0
            if not await self._confirm(ctx, f"This puts all {count:,} members back to zero XP. A backup is kept, and reward roles stay as they are.", "Reset everyone"):
                return
            count = await self.reset_server(ctx.guild.id)
            await ctx.ok(f"Reset {count:,} members. Reward roles stay as they are. `/level restore` brings the XP back.")
            return
        if member is None:
            await ctx.fail("Name the member to reset.")
            return
        if await self.reset_member(ctx.guild.id, member.id):
            await self._sync_roles(ctx.guild, member.id)
            await ctx.ok(f"Reset {member.mention}.")
        else:
            await ctx.fail(f"{member.mention} has no XP to reset.")

    @level.command(name="restore", description="Bring back everyone's XP from the newest backup")
    @requires("manage_guild")
    async def level_restore(self, ctx: commands.Context) -> None:
        newest = await self.db.one("SELECT reason, made_at, members FROM level_backups WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (ctx.guild.id,))
        if newest is None:
            await ctx.fail("There's no backup yet.")
            return
        if not await self._confirm(ctx, f"This puts everyone's XP back to the backup made <t:{int(newest['made_at'])}:R> ({newest['reason']}, {newest['members']:,} members). Today's XP is kept as a backup too.", "Restore"):
            return
        restored = await self.restore_backup(ctx.guild.id)
        if restored is None:
            await ctx.fail("There's no backup yet.")
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "level.restore", detail=str(restored["members"]))
        roles = f" Roles are being updated for {restored['role_updates']:,} members." if restored["role_updates"] else ""
        await ctx.ok(f"Restored {restored['members']:,} members from the backup made <t:{int(restored['made_at'])}:R>.{roles}")

    @level.command(name="voice_xp", aliases=["voicexp"], description="Turn voice XP on or off")
    @requires("manage_guild")
    async def level_voice_xp(self, ctx: commands.Context) -> None:
        turned_on = not self.cfg(ctx.guild.id)["voice_xp"]
        if await self._save(ctx, "level.voice_xp", {"voice_xp": turned_on}):
            await ctx.ok(f"Voice XP is now {'on' if turned_on else 'off'}.")

    @level.command(name="message", description="Set the level-up message or put back the default. {user}, {lvl} and {mention} work.")
    @requires("manage_guild")
    async def level_message(self, ctx: commands.Context, choice: Literal["set", "reset"], *, message: str | None = None) -> None:
        if choice == "reset":
            if await self._save(ctx, "level.message", {"message": DEFAULT_MESSAGE}):
                await ctx.ok("The level-up message is back to the default.")
            return
        if not message:
            await ctx.fail("Give the message text.")
            return
        current = self.cfg(ctx.guild.id)["message"]
        if await self._save(ctx, "level.message", {"message": {**current, "content": message}}):
            await ctx.ok("The level-up message is set.")

    @level.command(name="toggle", description="Switch DMs for level-ups, announcements, or rewards replacing earlier ones")
    @requires("manage_guild")
    async def level_toggle(self, ctx: commands.Context, choice: Literal["dm", "replace", "announcements"]) -> None:
        cfg = self.cfg(ctx.guild.id)
        if choice == "replace":
            turned_on = not cfg["replace_rewards"]
            if await self._save(ctx, "level.replace", {"replace_rewards": turned_on}):
                await ctx.ok(f"New rewards now {'replace earlier ones' if turned_on else 'stack with earlier ones'}.")
            return
        current = cfg["destination"]
        if choice == "dm":
            target = "same" if current == "dm" else "dm"
        else:
            target = "same" if current == "none" else "none"
        if await self._save(ctx, f"level.{choice}", {"destination": target}):
            await ctx.ok({"dm": "Level-ups now go by DM.", "same": "Level-ups are announced in the channel where they happen.", "none": "Level-ups are no longer announced."}[target])

    @level.command(name="channel", description="Send level-up messages to a channel")
    @requires("manage_guild")
    async def level_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        target = channel or ctx.channel
        refusal = access.post_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        if not target.permissions_for(ctx.guild.me).send_messages:
            await ctx.fail(f"I can't post in {target.mention}.")
            return
        if await self._save(ctx, "level.channel", {"destination": target.id}):
            await ctx.ok(f"Level-ups now go to {target.mention}.")

    @level.command(name="limit", description="Only announce level-ups from this level on")
    @requires("manage_guild")
    async def level_limit(self, ctx: commands.Context, limit: commands.Range[int, 0, 1000]) -> None:
        if await self._save(ctx, "level.limit", {"minimum": limit}):
            await ctx.ok(f"Level-ups are announced from level {limit} on.")

    @level.command(name="mod", description="Only announce levels evenly divisible by this number")
    @requires("manage_guild")
    async def level_mod(self, ctx: commands.Context, mod: commands.Range[int, 1, 100]) -> None:
        if await self._save(ctx, "level.mod", {"modulo": mod}):
            await ctx.ok(f"Only levels divisible by {mod} are announced.")

    @level.command(name="rewardonly", description="Turn on or off announcing only the level-ups that give a reward role")
    @requires("manage_guild")
    async def level_rewardonly(self, ctx: commands.Context) -> None:
        turned_on = not self.cfg(ctx.guild.id)["only_rewards"]
        if await self._save(ctx, "level.rewardonly", {"only_rewards": turned_on}):
            await ctx.ok(f"{'Only level-ups with a reward' if turned_on else 'All level-ups'} are announced now.")

    @level.command(name="log", description="Show the last 25 level-ups")
    @requires("manage_guild")
    async def level_log(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT user_id, level, at FROM level_events WHERE guild_id = ? ORDER BY id DESC LIMIT 25", (ctx.guild.id,))
        text_ = "\n".join(f"<@{row['user_id']}> reached level {row['level']} <t:{int(row['at'])}:R>" for row in rows) or "Nobody has leveled up yet."
        await ctx.send(embed=discord.Embed(title="Recent level-ups", description=text_, color=0x00A2F8), allowed_mentions=discord.AllowedMentions.none())

    @level.command(name="reward", description="Give a role at a level. Leave the role out to remove that level's reward.")
    @requires("manage_guild")
    async def level_reward(self, ctx: commands.Context, level: commands.Range[int, 1, 1000], role: discord.Role | None = None) -> None:
        rows = [row for row in self.cfg(ctx.guild.id)["rewards"] if row["level"] != level]
        if role is None:
            if await self._save(ctx, "level.reward.remove", {"rewards": rows}):
                await ctx.ok(f"Level {level} has no reward now.")
            return
        problem = self._role_problem(ctx.guild, role)
        if problem is None and not access.is_admin(ctx.author) and role >= ctx.author.top_role:
            problem = f"{role.mention} sits at or above your top role."
        if problem:
            await ctx.fail(problem)
            return
        if await self._save(ctx, "level.reward.add", {"rewards": [*rows, {"level": level, "role_id": role.id}]}):
            await ctx.ok(f"Reaching level {level} now gives {role.mention}.")

    async def _read_background(self, ctx: commands.Context, link: str | None, attachment: discord.Attachment | None) -> bytes | None:
        """The prepared image, or None after telling the person why not."""
        try:
            if attachment is not None:
                if attachment.size > images.MAX_BYTES:
                    raise Invalid(f"That file is too large. The limit is {images.MAX_BYTES // 1048576} MB.")
                data = await attachment.read()
            else:
                reply = await self.outside.get(link or "", stranger=True, max_bytes=images.MAX_BYTES, timeout=15.0)
                if reply.status != 200:
                    raise Invalid(f"That link answered with error {reply.status}.")
                data = reply.body
            return await asyncio.to_thread(rankcard.prepare_background, data)
        except (Invalid, OutsideError) as error:
            await ctx.fail(str(error))
        except discord.HTTPException:
            await ctx.fail("Discord wouldn't hand over that file. Try again.")
        return None

    async def _set_background(self, ctx: commands.Context, owner: int, link: str | None, attachment: discord.Attachment | None, whose: str) -> bool:
        if link is None and attachment is None:
            await self._store_background(ctx.guild.id, owner, None)
            await ctx.ok(f"{whose} background is back to the default.")
            return True
        await ctx.defer()
        prepared = await self._read_background(ctx, link, attachment)
        if prepared is None:
            return False
        await self._store_background(ctx.guild.id, owner, prepared)
        await ctx.ok(f"{whose} background is set.")
        return True

    @level.command(name="background", aliases=["bg"], description="Set the background of your rank card. Leave it empty to remove it.")
    @commands.cooldown(1, 30, commands.BucketType.member)
    async def level_background(self, ctx: commands.Context, link: str | None = None, attachment: discord.Attachment | None = None) -> None:
        if await self._levels_off(ctx):
            return
        await self._set_background(ctx, ctx.author.id, link, attachment, "Your")

    @level.command(name="serverbackground", aliases=["serverbg"], description="Set the default rank card background for this server")
    @requires("manage_guild")
    async def level_serverbackground(self, ctx: commands.Context, link: str | None = None, attachment: discord.Attachment | None = None) -> None:
        if await self._set_background(ctx, SERVER_BACKGROUND, link, attachment, "The server's"):
            await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "level.background")

    @commands.hybrid_command(name="leaderboard", description="List members by XP")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.member)
    async def leaderboard(self, ctx: commands.Context, page: commands.Range[int, 1, 100000] = 1) -> None:
        embed, shown, pages = await self.board_embed(ctx.guild.id, page)
        kwargs: dict[str, Any] = {}
        if pages > 1:
            kwargs["view"] = LeaderboardView(self, ctx.guild.id, ctx.author.id, shown, pages)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/levels/leaderboard", self._api_board, access="staff")
        router.get("/levels/events", self._api_events, access="staff")
        router.put("/levels/members/{user_id:\\d+}", self._api_set_member)
        router.delete("/levels/members/{user_id:\\d+}", self._api_reset_member)
        router.post("/levels/reset", self._api_reset_server)
        router.get("/levels/backups", self._api_backups)
        router.post("/levels/backups/{backup_id:\\d+}/restore", self._api_restore)
        router.post("/levels/import", self._api_import)
        router.get("/levels/import", self._api_import_status)
        router.post("/levels/sync", self._api_start_sync)
        router.get("/levels/sync", self._api_sync_status)
        router.get("/levels/background", self._api_background_state)
        router.post("/levels/background", self._api_upload_background)
        router.delete("/levels/background", self._api_clear_background)
        router.get("/levels/card", self._api_card, access="staff")

    @staticmethod
    def _query_int(req: Any, name: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(req.query.get(name, default))))
        except ValueError:
            return default

    async def _api_board(self, req: Any) -> dict:
        data = await self.board(
            req.guild_id, page=self._query_int(req, "page", 1, 1, 10 ** 7), size=self._query_int(req, "size", 10, 5, 100),
            search=req.query.get("q", "")[:100], include_left=req.query.get("left") == "1",
        )
        for row in data["rows"]:
            person = req.ops.member(req.guild_id, row["user_id"])
            row["avatar"] = person["avatar"] if person else None
            row["name"] = person["display_name"] if person else (row["name"] or f"User {row['user_id']}")
        return data

    async def _api_events(self, req: Any) -> list[dict]:
        rows = await req.db.all("SELECT id, user_id, level, at FROM level_events WHERE guild_id = ? ORDER BY id DESC LIMIT 50", (req.guild_id,))
        names = {row["user_id"]: row["name"] for row in await req.db.all(
            "SELECT user_id, name FROM level_members WHERE guild_id = ? AND user_id IN (SELECT user_id FROM level_events WHERE guild_id = ? ORDER BY id DESC LIMIT 50)",
            (req.guild_id, req.guild_id),
        )}
        for row in rows:
            row["name"] = names.get(row["user_id"]) or f"User {row['user_id']}"
        return rows

    async def _api_set_member(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        form = await req.json()
        if not isinstance(form, dict) or ("xp" in form) == ("level" in form):
            raise ApiError(400, "Send either a level or an XP amount.")
        try:
            xp = integer(0, MAX_XP)(form["xp"]) if "xp" in form else xp_for_level(integer(0, MAX_LEVEL)(form["level"]))
        except Invalid as error:
            raise ValidationError({"xp" if "xp" in form else "level": str(error)}) from error
        person = req.ops.member(req.guild_id, user_id)
        known = await req.db.scalar("SELECT 1 FROM level_members WHERE guild_id = ? AND user_id = ?", (req.guild_id, user_id))
        if person is None and not known:
            raise ApiError(404, "That person isn't in this server.")
        guild = self.bot.get_guild(req.guild_id)
        actor = guild.get_member(req.user.id) if guild is not None else None
        if actor is not None:
            entry = await self._entry(req.guild_id, user_id)
            role = self._reward_out_of_reach(actor, level_for_xp(entry.xp), level_for_xp(xp))
            if role is not None:
                raise ApiError(403, f"That level earns {role.name}, which sits at or above your top role.")
        await self.set_xp(req.guild_id, user_id, xp, name=person["display_name"] if person else None)
        await req.bot.timers.schedule("levels.sync", self._clock(), {"guild_id": req.guild_id, "user_id": user_id}, guild_id=req.guild_id, key=f"{req.guild_id}:{user_id}")
        await req.record("level.member.update", detail=f"{user_id}: {xp} XP")
        return {"xp": xp, "level": level_for_xp(xp)}

    async def _api_reset_member(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        if not await self.reset_member(req.guild_id, user_id):
            raise ApiError(404, "That member has no XP here.")
        await req.bot.timers.schedule("levels.sync", self._clock(), {"guild_id": req.guild_id, "user_id": user_id}, guild_id=req.guild_id, key=f"{req.guild_id}:{user_id}")
        await req.record("level.member.reset", detail=str(user_id))
        return {"ok": True}

    async def _api_reset_server(self, req: Any) -> dict:
        count = await self.reset_server(req.guild_id)
        await req.record("level.reset", detail=f"{count} members")
        return {"members": count}

    async def _api_backups(self, req: Any) -> list[dict]:
        return await req.db.all("SELECT id, reason, made_at, members FROM level_backups WHERE guild_id = ? ORDER BY id DESC", (req.guild_id,))

    async def _api_restore(self, req: Any) -> dict:
        restored = await self.restore_backup(req.guild_id, req.int_param("backup_id"))
        if restored is None:
            raise ApiError(404, "That backup doesn't exist.")
        await req.record("level.restore", detail=f"{restored['members']} members")
        return restored

    async def _run_import(self, guild_id: int, actor: Any, job: dict) -> None:
        try:
            await self.import_mee6(guild_id, actor, job)
        except ValueError:
            pass  # the job holds the reason for the page to show
        except Exception:
            log.exception("MEE6 import failed for %s", guild_id)

    async def _api_import(self, req: Any) -> dict:
        try:
            job = self._start_import(req.guild_id)
        except ValueError as error:
            raise ApiError(409, str(error)) from error
        self._spawn(self._run_import(req.guild_id, req.actor, job))
        await req.record("level.import.start")
        return job

    async def _api_import_status(self, req: Any) -> dict:
        return self._imports.get(req.guild_id) or {"state": "idle"}

    async def _api_start_sync(self, req: Any) -> dict:
        if self.role_sync_running(req.guild_id):
            raise ApiError(409, "A role update is already running.")
        job = self.start_role_sync(req.guild_id)
        await req.record("level.sync")
        return job

    async def _api_sync_status(self, req: Any) -> dict:
        return self._syncs.get(req.guild_id) or {"state": "idle"}

    async def _api_background_state(self, req: Any) -> dict:
        at = await req.db.scalar("SELECT updated_at FROM level_backgrounds WHERE guild_id = ? AND user_id = ?", (req.guild_id, SERVER_BACKGROUND))
        return {"set": at is not None, "updated_at": at}

    async def _api_upload_background(self, req: Any) -> dict:
        files, _ = await images.read_form(req.request)
        data = files.get("background")
        if data is None:
            raise ApiError(400, "Pick an image first.")
        prepared = await asyncio.to_thread(rankcard.prepare_background, data)
        await self._store_background(req.guild_id, SERVER_BACKGROUND, prepared)
        await req.record("level.background")
        return {"set": True, "updated_at": self._clock()}

    async def _api_clear_background(self, req: Any) -> dict:
        await self._store_background(req.guild_id, SERVER_BACKGROUND, None)
        await req.record("level.background.clear")
        return {"set": False}

    async def _api_card(self, req: Any) -> web.Response:
        """A sample card with the server's background. The page passes colors it hasn't saved yet, so the preview follows the form."""
        cfg = self.cfg(req.guild_id)
        try:
            color = hex_color(req.query.get("color", cfg["card_color"]))
            accent = hex_color(req.query.get("accent", cfg["card_accent"]))
            strength = number(0, 100)(float(req.query.get("opacity", cfg["card_opacity"])))
        except (Invalid, ValueError) as error:
            raise ApiError(400, "That isn't a usable color or opacity.") from error
        background = await self._card_background(req.guild_id, None)
        png = await asyncio.to_thread(
            rankcard.draw, name=req.user.name, level=12, rank=7, into_level=740, level_size=xp_to_next(12),
            background=background, color=color, accent=accent, opacity=strength,
        )
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})

    async def seed_demo(self, guild_id: int) -> None:
        """Sample members for the demo dashboard."""
        names = ["Ada", "Bram", "Cleo", "Dmitri", "Esme", "Farid", "Greta", "Hiro", "Imani", "Jonas", "Kaia", "Lev", "Mira", "Nils", "Opal", "Priya", "Quinn", "Rosa"]
        now = self._clock()
        rows = [(guild_id, 900000000000200000 + index, xp_for_level(40 - 2 * index) + 900, 3000 - 100 * index, 60 * index, name, now) for index, name in enumerate(names)]
        await self.db.many("INSERT INTO level_members (guild_id, user_id, xp, messages, voice_minutes, name, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        for index in range(3):
            await self.db.insert("INSERT INTO level_events (guild_id, user_id, level, at) VALUES (?, ?, ?, ?)", (guild_id, 900000000000200000 + index, 40 - 2 * index, now - 900 * index))
        await self.snapshot(guild_id, "daily")
