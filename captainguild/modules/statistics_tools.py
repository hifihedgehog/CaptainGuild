"""Statistics: counter channels and stat roles. A mixin the Statistics pack module inherits, so its commands belong to the pack.

Counter channels are voice channels nobody can join, named from text and counters (see core/stats/counters.py). Each one updates on the
clock, as often as its setup says, and only when its name would change. Discord allows two renames of a channel in ten minutes, and the
last two are kept, so a forced update or a restart never asks for a third. Turning a counter off deletes its channel and keeps its setup.
Deleting the channel by hand turns the counter off. A counter can take over an existing channel instead of making one, which keeps the
channel's place and permissions. The bot keeps a member overwrite for itself on each counter channel, so syncing it with a category can't
lock the bot out.

Stat roles give a role to the members who pass a rule's conditions and take it from the ones who stop passing (see core/stats/statroles.py).
A run reads the members as they are when it starts, works out who changes, and hands the changes to one queue per server, which edits
about one member a second and takes turns between the rules running at once. Removing the bot's Manage Roles stops every run in the server.
Each change can send a message: to a channel, by DM, or by DM with a channel for members whose DMs are closed, one per member or one per run.

Both run only while the server has Statistics switched on.
"""
from __future__ import annotations

import asyncio
import collections
import copy
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, delivery, messages
from ..core.members import whole_member_list
from ..core.module import staff_only
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.stats import counters, query, statroles
from ..core.stats.social import Followers
from ..core.stats.timeframe import time_zone, zone
from ..core.tagscript import Scope, render_message
from ..core.validate import Field, Invalid, ValidationError, boolean, snowflake, whole_number
from ..web.api import ApiError

log = logging.getLogger(__name__)

MAX_COUNTER_CHANNELS = 100  # a counter is a channel, and each update can read stored statistics
MAX_STAT_ROLES = 250  # Discord's limit on roles in a server, and each rule manages one
RENAME_SECONDS = 20.0  # longer than this, Discord is holding the rename back, so it waits for the next window
PRESENT_SECONDS = 300.0
SHARES_SECONDS = 3600.0  # a server's status split is read again after this long
SPACING = 1.0  # seconds between role edits in one server
OUTAGE_PAUSE = 60.0
RUNS_AT_ONCE = 2  # rules worked out at once across every server, since each reads the statistics
EVALUATION_SECONDS = 120.0
PREVIEW_SECONDS = 60.0
PREVIEW_NAMES = 100
EVERYONE_NAMES = 1000
PREVIEW_KEPT_SECONDS = 120.0
PREVIEWS_KEPT = 8
RUNS_KEPT = 50
GROUPED_MESSAGES = 10  # a grouped notification posts at most this many messages per run
MEMBERS_MARK = "MEMBERSLISTGOESHERE"  # stands in for a grouped notification's {members} while the room for the list is measured
DM_SPACING = 2.0  # seconds between DMs from one server
DM_LIMIT = 200  # DMs one run sends, after which the rest go to its channel, or go without when it has none
SKIPS_PER_TURN = 500  # members a run passes over in a row before it lets other work in
REPEAT_GAP = 600.0  # a scheduled run never starts within this many seconds of the rule's last start
QUICK_CATEGORY = "Server stats"
TRANSIENT = (OSError, asyncio.TimeoutError)
RENAME_REASON = "Counter channel update"
LOCKED_NOTE = "The bot can't rename this channel. Give it View Channel, Manage Channels and Connect there."
GONE_NOTE = "The channel was deleted, so this counter is off. Turn it on to make a new channel."
NOT_CONNECTED = "The bot isn't connected to this server right now, so nothing can change in Discord."
CANT_MAKE = "The bot couldn't make the channel. It needs Manage Channels, View Channel and Connect."
PAUSED_ROLE_GONE = "Its role was deleted, so the bot switched it off. Pick a role, then switch it back on."
LOST_MANAGE_ROLES = "The bot lost Manage Roles, so the run stopped."
ROLE_TOO_HIGH = "The role sits at or above the bot's highest role, so the run stopped. Move the bot's role above it."
REFUSED = "Discord refused a role change, so the run stopped. Check that the bot has Manage Roles and that its role sits above this one."
ELEVATED = "The role holds permissions the bot won't hand out while elevated roles are off on the Commands page, so the run stopped."
RESTORED = "A backup was restored during this run."
SWITCHED_OFF = "Statistics was switched off, so the run stopped."
FOLLOWS_MISSING = "It follows a stat role that's gone or can't be read, so the run stopped and took nothing. Pick another stat role in its conditions."
STATISTICS_OFF = "Statistics is off in this server, so stat roles don't run. Switch it on in Statistics settings."
RESTORING = "A backup is being restored in this server. Try again once it's done."
APPLYING = "A recount or merge is applying in this server's statistics. Try again when it finishes."
APPLIED_DURING = "A recount or merge started applying while this run worked out who passes, so it stopped. The next run goes at its time."
COUNTERS_WHO = "Manage Server and Manage Channels, or a role picked for counter channels"
STATROLES_WHO = "Manage Server and Manage Roles, or a role picked for stat roles"
# Each area: the Discord permission the bot uses for it, that permission's name, and what it's called.
TOOLS_AREAS = {"counters": ("manage_channels", "Manage Channels", "counter channels"), "statroles": ("manage_roles", "Manage Roles", "stat roles")}
FREQUENCY_NAMES = {6: "6 minutes", 10: "10 minutes", 15: "15 minutes", 20: "20 minutes", 30: "30 minutes", 45: "45 minutes", 60: "1 hour",
                   120: "2 hours", 180: "3 hours", 240: "4 hours", 360: "6 hours", 480: "8 hours", 720: "12 hours", 1440: "1 day"}

TOOLS_FIELDS: dict[str, Any] = {
    # Whether someone below the owner and Administrators may set up a stat role for a role at or above their own highest role. Only an
    # Administrator changes it, and the bot's own highest role still bounds every stat role.
    "statrole_above_own": Field(False, boolean),
}
TOOLS_ADMIN_FIELDS = frozenset({"statrole_above_own"})
TOOLS_ROW_LIMITS = {"stat_counters": MAX_COUNTER_CHANNELS, "stat_roles": MAX_STAT_ROLES, "stat_role_runs": MAX_STAT_ROLES * RUNS_KEPT}
TOOLS_MIGRATIONS: list[Any] = [
    """
    CREATE TABLE stat_counters (
        guild_id INTEGER NOT NULL,
        number INTEGER NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        setup TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        channel_id INTEGER,
        parent_id INTEGER,
        position INTEGER,
        shown TEXT,
        readings TEXT,
        renames TEXT NOT NULL DEFAULT '[]',
        checked_at REAL,
        problem TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (guild_id, number)
    );
    CREATE INDEX stat_counters_channel ON stat_counters (channel_id);
    CREATE TABLE stat_roles (
        guild_id INTEGER NOT NULL,
        number INTEGER NOT NULL,
        name TEXT NOT NULL,
        role_id INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        rule TEXT NOT NULL,
        paused TEXT,
        next_run REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (guild_id, number)
    );
    CREATE INDEX stat_roles_role ON stat_roles (guild_id, role_id);
    CREATE TABLE stat_role_runs (
        guild_id INTEGER NOT NULL,
        rule INTEGER NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        reason TEXT NOT NULL,
        state TEXT NOT NULL,
        matching INTEGER NOT NULL DEFAULT 0,
        to_give INTEGER NOT NULL DEFAULT 0,
        to_take INTEGER NOT NULL DEFAULT 0,
        gave INTEGER NOT NULL DEFAULT 0,
        took INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        PRIMARY KEY (guild_id, rule, started_at)
    );
    """,
]
TOOLS_MESSAGE_MIGRATIONS: list[Any] = []


def _json(value: Any, default: Any) -> Any:
    """A stored JSON cell, or the default when the cell holds something else, as a row copied from elsewhere can."""
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _settled(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()  # a rename left to finish on its own has nobody waiting for its answer


def counter_from_row(row: dict) -> dict | None:
    """A stored counter channel, checked again as it's read, or None when the row can't be used."""
    setup = _json(row.get("setup"), None)
    try:
        cleaned = counters.clean_setup({**(setup or {}), "name": row.get("name") or ""})
    except (Invalid, TypeError, AttributeError):
        return None
    renames = [moment for moment in _json(row.get("renames"), []) if isinstance(moment, (int, float)) and math.isfinite(moment)]
    readings = [item if isinstance(item, dict) else {} for item in _json(row.get("readings"), [])]
    return {
        "guild_id": row["guild_id"], "number": row["number"], "name": cleaned["name"], "setup": cleaned, "enabled": bool(row.get("enabled")),
        "channel_id": whole_number(row.get("channel_id")), "parent_id": whole_number(row.get("parent_id")), "position": whole_number(row.get("position")),
        "shown": row.get("shown") if isinstance(row.get("shown"), str) else None, "readings": readings, "renames": renames,
        "checked_at": _number(row.get("checked_at")), "problem": row.get("problem") if isinstance(row.get("problem"), str) else None,
    }


def rule_from_row(row: dict) -> dict | None:
    """A stored stat role, checked again as it's read, or None when the row can't be used."""
    try:
        rule = statroles.clean_rule(_json(row.get("rule"), None))
    except (Invalid, TypeError, AttributeError, ValueError):
        return None
    role_id = whole_number(row.get("role_id"))
    if not role_id or not isinstance(row.get("name"), str):
        return None
    return {"guild_id": row["guild_id"], "number": row["number"], "name": row["name"], "role_id": role_id, "enabled": bool(row.get("enabled")),
            "rule": rule, "paused": row.get("paused") if isinstance(row.get("paused"), str) else None, "next_run": _number(row.get("next_run"))}


def describe_counter(counter: dict) -> str:
    """One counter in a few words, as lists show it."""
    kind = counter["type"]
    label = counters.LABELS.get(kind, kind)
    if kind == "members":
        label += {"humans": " (people)", "bots": " (bots)", "both": ""}[counter["include"]]
    elif kind == "statuses":
        label = "Members " + ", ".join(counter["statuses"])
    elif kind == "datetime":
        label = f"Date ({counter['format']})"
    elif kind in counters.SOCIAL_TYPES:
        label = f"{label} of {counter['account']}"
    elif kind in ("stat_count", "stat_average", "stat_change", "stat_top"):
        label = f"{label} of {counter['stat']['kind']}"
    if counter.get("goal") is not None:
        label += f", counting down to {counter['goal']:,}"
    return label


def setup_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Run:
    """A stat role run applying its changes."""
    guild_id: int
    number: int
    name: str
    role_id: int
    rule: dict
    reason: str
    started_at: float
    give: collections.deque = field(default_factory=collections.deque)
    take: collections.deque = field(default_factory=collections.deque)
    matching: int = 0
    to_give: int = 0
    to_take: int = 0
    gave: int = 0
    took: int = 0
    failed: int = 0
    dms: int = 0
    notes: list[str] = field(default_factory=list)
    grouped: dict[bool, list[int]] = field(default_factory=lambda: {True: [], False: []})
    stopped: str | None = None

    def json(self) -> dict:
        return {"rule": self.number, "running": True, "matching": self.matching, "to_give": self.to_give, "to_take": self.to_take, "gave": self.gave,
                "took": self.took, "failed": self.failed, "left": len(self.give) + len(self.take), "started_at": self.started_at}

    def note(self, text: str) -> None:
        """A line for the run's history, once however often it happens."""
        if text not in self.notes:
            self.notes.append(text)


@dataclass
class ServerRuns:
    runs: dict[int, Run] = field(default_factory=dict)
    order: collections.deque = field(default_factory=collections.deque)
    worker: asyncio.Task | None = None
    working_out: set[int] = field(default_factory=set)
    again: dict[int, str] = field(default_factory=dict)


def public_channel(guild: Any, channel_id: int) -> bool:
    """Whether everyone in the server can see a channel, which is when its name may appear in a counter's name."""
    channel = guild.get_channel(channel_id) if guild is not None else None
    if channel is None:
        return False
    try:
        return bool(channel.permissions_for(guild.default_role).view_channel)
    except (AttributeError, discord.ClientException):
        return False


def channel_ids(value: Any) -> set[int]:
    """Every channel a counter setup or a rule names in its filters or its notifications."""
    found: set[int] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "channels" and isinstance(inner, list):
                found.update(item for item in inner if isinstance(item, int))
            elif key == "channel_id" and isinstance(inner, int) and not isinstance(inner, bool):
                found.add(inner)
            else:
                found |= channel_ids(inner)
    elif isinstance(value, list):
        for inner in value:
            found |= channel_ids(inner)
    return found


def role_ids(value: Any) -> set[int]:
    """Every role a counter setup or a rule names in its filters."""
    found: set[int] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in ("roles", "roles_allow", "roles_block") and isinstance(inner, list):
                found.update(item for item in inner if isinstance(item, int))
            else:
                found |= role_ids(inner)
    elif isinstance(value, list):
        for inner in value:
            found |= role_ids(inner)
    return found


def discord_words(error: discord.HTTPException) -> str:
    """A plain sentence for a request Discord refused or failed."""
    if isinstance(error, discord.Forbidden):
        return "Discord refused that: the bot is missing a permission it needs there. Check its role and where it sits in the role list."
    if isinstance(error, discord.NotFound):
        return "Discord says that's gone. Reload the page and try again."
    return f"Discord didn't take that just now (error {error.status}). Try again in a moment."


def members_room(rendered: dict) -> int | None:
    """How many characters a grouped notification's member list can take, read from the message made with MEMBERS_MARK in the list's place.
    Every place the list appears in holds all of it, so the tightest one decides. None when the message doesn't list the members."""
    mark = len(MEMBERS_MARK)
    rooms: list[int] = []

    def place(text: Any, limit: int) -> int:
        found = text.count(MEMBERS_MARK) if isinstance(text, str) else 0
        if found:
            rooms.append((limit - (len(text) - found * mark)) // found)
        return found

    place(rendered.get("content"), messages.CONTENT_MAX)
    marks = size = 0
    for embed in rendered.get("embeds") or []:
        marks += place(embed.get("title"), messages.TITLE_MAX) + place(embed.get("description"), messages.DESCRIPTION_MAX)
        marks += place((embed.get("footer") or {}).get("text"), messages.FOOTER_MAX) + place((embed.get("author") or {}).get("name"), messages.AUTHOR_MAX)
        for item in embed.get("fields") or []:
            marks += place(item.get("name"), messages.FIELD_NAME_MAX) + place(item.get("value"), messages.FIELD_VALUE_MAX)
        size += messages.embed_size(embed)
    if marks:
        rooms.append((messages.TOTAL_MAX - (size - marks * mark)) // marks)  # every embed's text together
    return min(rooms) if rooms else None


def grouped_lists(mentions: list[str], room: int) -> list[str]:
    """The member lists for a grouped notification, each within `room` characters, whole mentions only, and at most GROUPED_MESSAGES of
    them. The last ends "and N more" when the rest don't fit."""
    chunks = statroles.group_chunks(mentions, room)
    if len(chunks) <= GROUPED_MESSAGES:
        return [", ".join(chunk) for chunk in chunks]
    head = chunks[:GROUPED_MESSAGES - 1]
    shown = sum(len(chunk) for chunk in head)
    last = statroles.group_chunks(mentions[shown:], room - len(f" and {len(mentions):,} more"))[0]
    more = len(mentions) - shown - len(last)
    return [", ".join(chunk) for chunk in head] + [", ".join(last) + f" and {more:,} more"]


def parse_end(text: str | None, tz: Any, now: float) -> float:
    """When a countdown ends, from a Discord timestamp, a Unix time, YYYY-MM-DD or YYYY-MM-DD HH:MM in the server's zone, or 3d or 12h from now."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise Invalid("Give when the countdown ends, with the ends option.")
    found = re.fullmatch(r"<t:(\d{1,13})(?::[tTdDfFR])?>", cleaned)
    if found or re.fullmatch(r"\d{9,13}", cleaned):
        value = int(found.group(1) if found else cleaned)
        moment = value / 1000 if value > 10 ** 11 else float(value)
    elif match := re.fullmatch(r"(\d{1,4})\s*([dhm])", cleaned.lower()):
        moment = now + int(match.group(1)) * {"d": 86400, "h": 3600, "m": 60}[match.group(2)]
    else:
        try:
            parsed = dt.datetime.fromisoformat(cleaned.replace("T", " "))
        except ValueError as error:
            raise Invalid("Give the end as a Discord timestamp, as YYYY-MM-DD HH:MM, or as 3d or 12h from now.") from error
        moment = (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).timestamp()
    if moment <= now:
        raise Invalid("That time has already passed.")
    return moment


COUNTER_PRESET_ITEMS = (
    ("members", ("Members", {"type": "members", "include": "both"}, "Members: {1}")),
    ("people", ("People, without bots", {"type": "members", "include": "humans"}, "People: {1}")),
    ("bots", ("Bots", {"type": "members", "include": "bots"}, "Bots: {1}")),
    ("online", ("Members online", {"type": "statuses", "statuses": ["online"]}, "Online: {1}")),
    ("not_offline", ("Members not offline", {"type": "statuses", "statuses": ["online", "idle", "dnd"]}, "Online: {1}")),
    ("in_roles", ("Members in roles", {"type": "role_members", "roles": []}, "In role: {1}")),
    ("in_voice", ("Members in voice", {"type": "voice"}, "In voice: {1}")),
    ("channels", ("Channels", {"type": "channels"}, "Channels: {1}")),
    ("roles", ("Roles", {"type": "roles"}, "Roles: {1}")),
    ("date", ("Date", {"type": "datetime", "format": "MMMM D, YYYY"}, "{1}")),
    ("time", ("Time", {"type": "datetime", "format": "h:mma"}, "{1}")),
    ("countdown", ("Countdown", {"type": "countdown", "end": 1.0}, "{1}")),
    ("messages", ("Messages in the last days", {"type": "stat_count", "stat": {"kind": "messages", "period": {"amount": 1, "unit": "days"}}}, "Messages today: {1}")),
    ("voice_hours", ("Voice hours in the last days", {"type": "stat_count", "unit": "hours", "decimals": 1,
                                                       "stat": {"kind": "voice", "period": {"amount": 1, "unit": "days"}}}, "Voice today: {1}h")),
    ("member_change", ("Member change over the last days", {"type": "stat_change", "plus": True, "stat": {"kind": "members", "period": {"amount": 7, "unit": "days"}}},
                       "Members this week: {1}")),
    ("top_member", ("Top member by messages", {"type": "stat_top", "of": "member", "rank": 1, "stat": {"kind": "messages", "period": {"amount": 1, "unit": "days"}}},
                    "Top chatter: {1}")),
    ("invite_joins", ("Members who joined by invite", {"type": "invites"}, "Invited: {1}")),
    ("youtube", ("YouTube subscribers", {"type": "youtube", "account": ""}, "YouTube: {1}")),
    ("instagram", ("Instagram followers", {"type": "instagram", "account": ""}, "Instagram: {1}")),
    ("tiktok", ("TikTok followers", {"type": "tiktok", "account": ""}, "TikTok: {1}")),
)
COUNTER_PRESETS = dict(COUNTER_PRESET_ITEMS)


class StatsTools:
    # ---- start and stop ----

    async def _tools_load(self) -> None:
        self.followers = Followers()
        self._tools_loop: asyncio.Task | None = None
        self._tools_tasks: set[asyncio.Task] = set()
        self._refreshing: set[tuple[int, int]] = set()
        self._present_cache: dict[int, tuple[float, tuple[int, int] | None]] = {}
        self._server_runs: dict[int, ServerRuns] = {}
        self._run_gate = asyncio.Semaphore(RUNS_AT_ONCE)
        self._previews: dict[tuple[int, str], tuple[float, Any, Any]] = {}
        self._tools_told: set[tuple[int, str]] = set()
        self._tools_resumed = False
        self._counter_locks: dict[int, asyncio.Lock] = {}
        self._switch_locks: dict[int, asyncio.Lock] = {}
        self._updating: dict[int, asyncio.Task] = {}  # each server's counter update, one at a time
        self._restoring: set[int] = set()  # servers a backup is being restored into, where nothing starts
        self._restores: dict[int, int] = {}  # how many restores each server has had, so a run working out across one knows
        self._dm_next: dict[int, float] = {}  # when each server may send its next DM
        self._shares: dict[int, tuple[float, dict]] = {}  # each server's status split, with when it was read
        await self._load_held_roles()
        self._tools_zones = {guild_id: self.cfg(guild_id)["timezone"] for guild_id in self.bot.settings.guilds_with(self.key)}
        self.bot.settings.listen(self.key, self._tools_settings_saved)

    async def _tools_unload(self) -> None:
        tasks = [self._tools_loop, *self._tools_tasks, *(state.worker for state in self._server_runs.values())]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await self.followers.close()

    def transient_roles(self, guild_id: int) -> set[int]:
        """A stat role comes and goes with its rule, so it isn't put back on someone who returns, as with role rules kept in sync. A
        permanent one stays theirs, so it is."""
        parent = getattr(super(), "transient_roles", None)
        held = getattr(self, "_held_roles", {})  # empty when loading stopped early, since every member join in every server asks this
        return {*held.get(guild_id, ()), *(parent(guild_id) if parent is not None else ())}

    async def _load_held_roles(self) -> None:
        held: dict[int, set[int]] = {}
        for row in await self.db.all("SELECT * FROM stat_roles WHERE enabled = 1"):
            found = rule_from_row(row)
            if found is not None and not found["rule"]["permanent"]:
                held.setdefault(found["guild_id"], set()).add(found["role_id"])
        self._held_roles = held

    async def _remember_held_roles(self, guild_id: int) -> None:
        held = {found["role_id"] for found in await self._rules(guild_id) if found["enabled"] and not found["rule"]["permanent"]}
        if held:
            self._held_roles[guild_id] = held
        else:
            self._held_roles.pop(guild_id, None)

    def _tools_spawn(self, work: Any) -> asyncio.Task:
        task = asyncio.ensure_future(work)
        self._tools_tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._tools_tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                log.error("Counter or stat role work failed", exc_info=finished.exception())
        task.add_done_callback(done)
        return task

    @commands.Cog.listener("on_ready")
    async def _tools_ready(self) -> None:
        if self._tools_loop is None or self._tools_loop.done():
            self._tools_loop = asyncio.ensure_future(self._tools_run())
        if not self._tools_resumed:
            self._tools_resumed = True
            self._tools_spawn(self._resume_runs())

    async def _tools_run(self) -> None:
        while not self._closing:
            await asyncio.sleep(60 - time.time() % 60 + 1)  # a second past each minute, so a 10-minute counter turns at :00, :10 and so on
            try:
                await self.tools_tick(time.time())
            except Exception:
                log.exception("Counters and stat roles missed a minute. The next one tries again.")

    async def tools_tick(self, now: float) -> None:
        """Once a minute: every counter whose time has come, and every stat role whose schedule has come."""
        if not self.bot.members_settled.is_set():
            return  # a half-loaded member list would count wrong and hand roles out wrong
        await self.counters_due(now)
        await self.statroles_due(now)

    def _zone(self, guild_id: int) -> Any:
        return zone(self.cfg(guild_id)["timezone"])

    async def _tools_settings_saved(self, guild_id: int, settings: dict) -> None:
        """Switching Statistics off stops the server's runs. A new time zone moves every schedule's next run onto the new clock. Any other
        save leaves them, so a run that's due still runs."""
        if not settings.get("enabled"):
            self._stop_runs(guild_id, SWITCHED_OFF)
        if self._tools_zones.get(guild_id, "UTC") == settings["timezone"]:
            return
        self._tools_zones[guild_id] = settings["timezone"]
        tz = zone(settings["timezone"])
        now = time.time()
        for row in await self.db.all("SELECT * FROM stat_roles WHERE guild_id = ? AND enabled = 1", (guild_id,)):
            found = rule_from_row(row)
            if found is not None:
                await self.db.run("UPDATE stat_roles SET next_run = ? WHERE guild_id = ? AND number = ?",
                                  (statroles.next_run(found["rule"]["schedule"], now, tz), guild_id, found["number"]))

    def _tools_notice(self, guild: Any, topic: str, text: str, title: str = "Counters and stat roles need a look") -> None:
        """Staff hear about a problem once, and again only after it was fixed and came back."""
        if (guild.id, topic) in self._tools_told:
            return
        self._tools_told.add((guild.id, topic))
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, title, text)

    def _tools_fixed(self, guild_id: int, topic: str) -> None:
        self._tools_told.discard((guild_id, topic))

    async def before_restore(self, guild_id: int) -> None:
        """A backup is about to replace this server's rows. Its runs stop, and none starts, by schedule or by hand, until after_restore."""
        self._restoring.add(guild_id)
        self._restores[guild_id] = self._restores.get(guild_id, 0) + 1
        self._stop_runs(guild_id, RESTORED)
        parent = getattr(super(), "before_restore", None)
        if parent is not None:
            await parent(guild_id)

    async def on_guild_restored(self, guild_id: int) -> None:
        """A backup's counters and stat roles replace this server's. Runs from before stop, and each rule's next run is worked out again."""
        self._restores[guild_id] = self._restores.get(guild_id, 0) + 1
        self._stop_runs(guild_id, RESTORED)
        await self.db.run("UPDATE stat_roles SET next_run = NULL WHERE guild_id = ?", (guild_id,))
        await self.db.run("UPDATE stat_role_runs SET state = 'stopped', note = ? WHERE guild_id = ? AND state = 'running'", (RESTORED, guild_id))
        await self._remember_held_roles(guild_id)
        parent = getattr(super(), "on_guild_restored", None)
        if parent is not None:
            await parent(guild_id)

    async def after_restore(self, guild_id: int) -> None:
        self._restoring.discard(guild_id)
        parent = getattr(super(), "after_restore", None)
        if parent is not None:
            await parent(guild_id)

    # ---- counter channels: reading and renaming ----

    async def _counter_rows(self, guild_id: int) -> list[dict]:
        rows = await self.db.all("SELECT * FROM stat_counters WHERE guild_id = ? ORDER BY number LIMIT ?", (guild_id, MAX_COUNTER_CHANNELS))
        return [found for found in (counter_from_row(row) for row in rows) if found is not None]

    async def _counter(self, guild_id: int, number: int) -> dict | None:
        row = await self.db.one("SELECT * FROM stat_counters WHERE guild_id = ? AND number = ?", (guild_id, number))
        return counter_from_row(row) if row else None

    async def counters_due(self, now: float) -> None:
        rows = await self.db.all("SELECT * FROM stat_counters WHERE enabled = 1 AND channel_id IS NOT NULL ORDER BY guild_id, number")
        by_guild: dict[int, list[dict]] = collections.defaultdict(list)
        for row in rows:
            by_guild[row["guild_id"]].append(row)
        for guild_id, found in by_guild.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.unavailable or not self.on_in(guild_id) or guild_id in self._restoring or self._tools_applying(guild_id):
                continue  # a recount or merge applying half swaps the counts, so the server's counters wait for the tick after it
            going = self._updating.get(guild_id)
            if going is not None and not going.done():
                continue  # the server's last update is still going, held back by Discord or by follower reads, and it reads each row afresh
            server_zone = self._zone(guild_id)
            due = []
            for row in found[:MAX_COUNTER_CHANNELS]:
                counter = counter_from_row(row)
                if counter is None:
                    continue
                boundary = counters.last_boundary(counter["setup"]["frequency"], now, counters.schedule_zone(counter["setup"], server_zone))
                if counter["checked_at"] is None or counter["checked_at"] < boundary:
                    due.append(counter)
            if due:
                self._updating[guild_id] = self._tools_spawn(self._refresh_many(guild, [counter["number"] for counter in due], now))

    async def _refresh_many(self, guild: Any, numbers: list[int], now: float) -> None:
        """One server's update. Each counter's row is read again just before it's refreshed, so an edit, a switch or an update made since
        the tick isn't undone by what the tick read."""
        found = [counter for counter in [await self._counter(guild.id, number) for number in numbers] if counter is not None]
        place = await self.counter_place(guild, now, [counter["setup"] for counter in found])
        for number in numbers:
            counter = await self._counter(guild.id, number)
            if counter is None or not counter["enabled"] or counter["channel_id"] is None:
                continue
            try:
                await self.refresh_counter(guild, counter, place)
            except Exception:
                log.exception("Counter %s in %s failed to update", number, guild.id)

    async def counter_place(self, guild: Any, now: float, setups: list[dict]) -> counters.Place:
        """What reading these setups' counters needs, read once for all of them. Its `public` set is the server's channels everyone can see,
        which public_place keeps for a counter whose channel everyone can see."""
        presences = bool(self.bot.intents.presences)
        kinds = {item["type"] for setup in setups for item in setup["counters"]}
        present = await self._present(guild.id, now) if guild is not None and not presences and "statuses" in kinds else None
        shares = await self._status_shares(guild.id, now) if guild is not None and not presences and "statuses" in kinds else None
        if kinds & set(counters.STAT_TYPES):
            await self.up_to_date()
        guild_id = guild.id if guild is not None else 0
        everyone_sees = frozenset(channel.id for channel in getattr(guild, "channels", []) if public_channel(guild, channel.id))
        return counters.Place(guild=guild, guild_id=guild_id, now=now, tz=self._zone(guild_id), db=self.message_db, settings=self.cfg(guild_id),
                              presences=presences, present=present, social=self.followers, visible=lambda channel_id: public_channel(guild, channel_id),
                              public=everyone_sees, shares=shares)

    def public_place(self, place: counters.Place, shown_to_everyone: bool) -> counters.Place:
        """The place a counter reads from: the server's public channels alone when everyone can see its channel, and every channel when
        only some members can, since then its name shows only to them."""
        return dataclasses.replace(place, public=place.public if shown_to_everyone else None)

    def _shown_to_everyone(self, guild: Any, channel_id: int | None = None, parent_id: int | None = None) -> bool:
        """Whether everyone in the server sees a counter's channel: the channel itself, or for one still to be made, the category it goes
        in, whose permissions it copies, or the server's own when it goes in none."""
        if guild is None:
            return True
        if channel_id:
            return public_channel(guild, channel_id)
        if parent_id and guild.get_channel(parent_id) is not None:
            return public_channel(guild, parent_id)
        return bool(getattr(guild.default_role.permissions, "view_channel", True))

    async def _status_shares(self, guild_id: int, now: float) -> dict:
        """The server's status split (query.status_shares), read at most once an hour, so every status counter it has costs one read."""
        cached = self._shares.get(guild_id)
        if cached is not None and now - cached[0] < SHARES_SECONDS:
            return cached[1]
        try:
            found = await query.status_shares(self.message_db, guild_id, self._zone(guild_id))
        except Exception:  # a split that can't be read leaves the counters on Discord's present count, so nothing here may stop an update
            log.exception("Could not read the status split of %s", guild_id)
            found = cached[1] if cached is not None else {}
        self._shares[guild_id] = (now, found)
        return found

    async def _present(self, guild_id: int, now: float) -> tuple[int, int] | None:
        """Discord's approximate counts of members and of members present, read at most every five minutes."""
        cached = self._present_cache.get(guild_id)
        if cached is not None and now - cached[0] < PRESENT_SECONDS:
            return cached[1]
        counts = cached[1] if cached else None
        try:
            found = await self.bot.fetch_guild(guild_id, with_counts=True)
            if found.approximate_presence_count is not None:
                counts = (found.approximate_member_count or 0, found.approximate_presence_count)
        except Exception as error:  # a best guess that's missing leaves the counter at its last value, so nothing here may stop an update
            log.info("Could not read how many members are present in %s: %s", guild_id, error)
        self._present_cache[guild_id] = (now, counts)
        return counts

    async def counter_readings(self, setup: dict, place: counters.Place, last: list[dict] | None = None) -> list[counters.Reading]:
        """Each counter's reading, with the value it had before standing in for one that can't be read now."""
        out = []
        last = last or []
        for index, item in enumerate(setup["counters"]):
            try:
                reading = await counters.read(item, place)
            except Exception:
                log.exception("A %s counter in %s couldn't be read", item.get("type"), place.guild_id)
                reading = counters.Reading(note="This counter couldn't be read just now. It keeps its last value.", kept=True)
            before = last[index] if index < len(last) and isinstance(last[index], dict) else {}
            if reading.kept:
                if reading.value is None:
                    reading.value = _number(before.get("value"))
                if reading.words is None and isinstance(before.get("words"), str):
                    reading.words = before["words"]
            out.append(reading)
        return out

    async def refresh_counter(self, guild: Any, counter: dict, place: counters.Place) -> dict:
        """Read the counter and rename its channel when the name changed and Discord's limit allows. Returns what happened, for the page."""
        key = (guild.id, counter["number"])
        if key in self._refreshing:
            return {"busy": True}
        if self._tools_applying(guild.id):
            return {"applying": True}  # the counts are half swapped, so the name waits for the next update
        self._refreshing.add(key)
        try:
            return await self._refresh(guild, counter, place)
        finally:
            self._refreshing.discard(key)

    async def _refresh(self, guild: Any, counter: dict, place: counters.Place) -> dict:
        now = place.now
        channel = guild.get_channel(counter["channel_id"]) if counter["channel_id"] else None
        place = self.public_place(place, channel is None or public_channel(guild, channel.id))
        readings = await self.counter_readings(counter["setup"], place, counter["readings"])
        name = counters.render(counter["setup"], readings)
        if channel is None:
            await self._counter_gone(guild.id, counter["number"], None)
            return {"name": name, "gone": True}
        clock = max(time.time(), now)  # Discord's rename window runs on the real clock, and an update can start well before it reaches this counter
        renames = counters.recent_renames(counter["renames"], clock)
        problem = None
        waiting_until = None
        shown = counter["shown"]
        if channel.name == name:
            shown = name
        else:
            allowed = counters.rename_allowed_at(renames, clock)
            if allowed > clock:
                waiting_until = allowed
            else:
                try:
                    await self._rename(channel, name)
                except discord.NotFound:
                    await self._counter_gone(guild.id, counter["number"], channel)
                    return {"name": name, "gone": True}
                except discord.Forbidden:
                    problem = LOCKED_NOTE
                    self._tools_notice(guild, f"counter:{counter['number']}", f"Counter channel {channel.mention}: {LOCKED_NOTE}")
                except discord.HTTPException as error:
                    if error.status == 429:
                        renames = [clock, clock]
                        waiting_until = clock + counters.RENAME_WINDOW
                    else:
                        problem = f"Discord refused the new name: {error.text or error.status}"
                except asyncio.TimeoutError:
                    renames = [clock, clock]  # Discord is holding renames of this channel back, so the next try waits out the window
                    waiting_until = clock + counters.RENAME_WINDOW
                else:
                    renames = [*renames, max(time.time(), now)][-counters.RENAMES_ALLOWED:]
                    shown = name
                    self._tools_fixed(guild.id, f"counter:{counter['number']}")
        notes = [reading.note for reading in readings if reading.note]
        await self.db.run(
            "UPDATE stat_counters SET readings = ?, shown = ?, renames = ?, checked_at = ?, problem = ? WHERE guild_id = ? AND number = ?",
            (json.dumps([reading.as_json() for reading in readings]), shown, json.dumps(renames), now, problem, guild.id, counter["number"]),
        )
        return {"name": name, "shown": shown, "waiting_until": waiting_until, "problem": problem, "notes": notes}

    async def _rename(self, channel: Any, name: str) -> None:
        """Renames the channel, or raises asyncio.TimeoutError when Discord holds the rename back past RENAME_SECONDS. discord.py waits out
        that hold inside the request, and a request canceled while it waits out Discord's global limit leaves that limit shut for every
        request after it, so one that runs long is left to finish on its own rather than canceled."""
        task = asyncio.ensure_future(channel.edit(name=name, reason=RENAME_REASON))
        task.add_done_callback(_settled)
        done, _ = await asyncio.wait({task}, timeout=RENAME_SECONDS)
        if not done:
            raise asyncio.TimeoutError
        task.result()

    # ---- counter channels: their Discord channel ----

    def _counter_overwrites(self, guild: Any, category: Any) -> dict:
        """The category's overwrites, so a private category stays private, with Connect denied to everyone and the bot's own access on top."""
        overwrites = dict(getattr(category, "overwrites", {}) or {}) if category is not None else {}
        for target, overwrite in list(overwrites.items()):
            if overwrite.connect:  # a role or member the category lets connect would outrank @everyone's deny
                locked = discord.PermissionOverwrite.from_pair(*overwrite.pair())
                locked.update(connect=False)
                overwrites[target] = locked
        everyone = overwrites.get(guild.default_role) or discord.PermissionOverwrite()
        everyone = discord.PermissionOverwrite.from_pair(*everyone.pair())
        everyone.update(connect=False)
        overwrites[guild.default_role] = everyone
        overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        return overwrites

    async def _make_channel(self, guild: Any, name: str, parent_id: int | None, position: int | None) -> Any:
        category = guild.get_channel(parent_id) if parent_id else None
        if category is not None and getattr(category, "type", None) != discord.ChannelType.category:
            category = None
        kwargs: dict[str, Any] = {"overwrites": self._counter_overwrites(guild, category), "reason": "Counter channel"}
        if category is not None:
            kwargs["category"] = category
        if position is not None:
            kwargs["position"] = position
        return await guild.create_voice_channel(name, **kwargs)

    async def _keep_own_access(self, guild: Any, channel: Any) -> str | None:
        """Gives the bot its own overwrite on a channel it took over. Returns why it couldn't, when it couldn't."""
        me = guild.me
        current = channel.overwrites_for(me)
        if current.view_channel and current.connect and current.manage_channels:
            return None
        if not channel.permissions_for(me).manage_roles:
            return ("The bot can rename this channel, but it can't add its own permissions to it. Give it Manage Permissions there, so syncing "
                    "the channel with a category can't lock it out.")
        current.update(view_channel=True, connect=True, manage_channels=True)
        try:
            await channel.set_permissions(me, overwrite=current, reason="Counter channel: the bot keeps its own access")
        except (discord.HTTPException, *TRANSIENT) as error:
            return f"The bot couldn't add its own permissions to this channel: {getattr(error, 'text', '') or error}"
        return None

    def _switch_lock(self, guild_id: int) -> asyncio.Lock:
        """A server's counter switches go one at a time, so two requests can't make two channels for one counter or hand one channel to two."""
        return self._switch_locks.setdefault(guild_id, asyncio.Lock())

    async def counter_on(self, guild: Any, counter: dict, *, channel_id: int | None = None, parent_id: int | None = None) -> tuple[bool, str | None]:
        """Make the counter's channel, or take over an existing one. Returns (whether it's on, a problem to show)."""
        async with self._switch_lock(counter["guild_id"]):
            fresh = await self._counter(counter["guild_id"], counter["number"])
            if fresh is None:
                return False, "That counter channel isn't there anymore."
            if fresh["enabled"]:
                return True, None
            return await self._counter_on(guild, fresh, channel_id=channel_id, parent_id=parent_id)

    async def _counter_on(self, guild: Any, counter: dict, *, channel_id: int | None, parent_id: int | None) -> tuple[bool, str | None]:
        now = time.time()
        place = await self.counter_place(guild, now, [counter["setup"]])
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel is None or getattr(channel, "type", None) not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                return False, "Pick a voice channel in this server to take over."
            taken = await self.db.one("SELECT number FROM stat_counters WHERE guild_id = ? AND channel_id = ? AND number != ?", (guild.id, channel_id, counter["number"]))
            if taken:
                return False, f"Counter {taken['number']} already uses that channel."
            seeded = counters.values_from_name(counter["setup"], channel.name) or []
            last = [{"value": value} if value is not None else {} for value in seeded]
            readings = await self.counter_readings(counter["setup"], self.public_place(place, self._shown_to_everyone(guild, channel_id=channel.id)), last or counter["readings"])
            problem = await self._keep_own_access(guild, channel)
            await self.db.run(
                "UPDATE stat_counters SET enabled = 1, channel_id = ?, parent_id = ?, position = ?, shown = ?, readings = ?, checked_at = NULL, problem = ?, "
                "updated_at = ? WHERE guild_id = ? AND number = ?",
                (channel.id, getattr(channel, "category_id", None), getattr(channel, "position", None), channel.name,
                 json.dumps([reading.as_json() for reading in readings]), problem, now, guild.id, counter["number"]),
            )
            fresh = await self._counter(guild.id, counter["number"])
            result = await self.refresh_counter(guild, fresh, place) if fresh else {}
            return True, problem or result.get("problem")
        parent = parent_id if parent_id is not None else counter["parent_id"]
        readings = await self.counter_readings(counter["setup"], self.public_place(place, self._shown_to_everyone(guild, parent_id=parent)), counter["readings"])
        name = counters.render(counter["setup"], readings)
        try:
            channel = await self._make_channel(guild, name, parent, None if parent_id is not None else counter["position"])
        except discord.Forbidden:
            return False, CANT_MAKE
        except (discord.HTTPException, *TRANSIENT) as error:
            return False, f"Discord couldn't make the channel: {getattr(error, 'text', '') or error}"
        await self.db.run(
            "UPDATE stat_counters SET enabled = 1, channel_id = ?, parent_id = ?, position = ?, shown = ?, readings = ?, renames = '[]', checked_at = ?, "
            "problem = NULL, updated_at = ? WHERE guild_id = ? AND number = ?",
            (channel.id, getattr(channel, "category_id", None), getattr(channel, "position", None), name, json.dumps([reading.as_json() for reading in readings]),
             now, now, guild.id, counter["number"]),
        )
        return True, None

    async def counter_off(self, guild: Any, counter: dict, *, keep_channel: bool = False) -> str | None:
        """Switch a counter off and delete its channel, keeping its setup. Returns a problem to show."""
        async with self._switch_lock(counter["guild_id"]):
            fresh = await self._counter(counter["guild_id"], counter["number"])
            if fresh is None or not fresh["enabled"]:
                return None
            return await self._counter_off(guild, fresh, keep_channel=keep_channel)

    async def _counter_off(self, guild: Any, counter: dict, *, keep_channel: bool) -> str | None:
        channel = guild.get_channel(counter["channel_id"]) if guild is not None and counter["channel_id"] else None
        parent = getattr(channel, "category_id", None) if channel is not None else counter["parent_id"]
        position = getattr(channel, "position", None) if channel is not None else counter["position"]
        await self.db.run("UPDATE stat_counters SET enabled = 0, channel_id = NULL, parent_id = ?, position = ?, problem = NULL, updated_at = ? WHERE guild_id = ? AND number = ?",
                          (parent, position, time.time(), counter["guild_id"], counter["number"]))
        if channel is None or keep_channel:
            return None
        try:
            await channel.delete(reason="Counter channel switched off")
        except discord.NotFound:
            return None
        except (discord.HTTPException, *TRANSIENT) as error:
            return f"The counter is off, and its channel stays, since the bot couldn't delete it: {getattr(error, 'text', '') or error}"
        return None

    async def _counter_gone(self, guild_id: int, number: int, channel: Any) -> None:
        await self.db.run("UPDATE stat_counters SET enabled = 0, channel_id = NULL, parent_id = COALESCE(?, parent_id), position = COALESCE(?, position), problem = ?, "
                          "updated_at = ? WHERE guild_id = ? AND number = ?",
                          (getattr(channel, "category_id", None), getattr(channel, "position", None), GONE_NOTE, time.time(), guild_id, number))

    @commands.Cog.listener("on_guild_channel_delete")
    async def _counter_channel_deleted(self, channel: Any) -> None:
        """Deleting a counter's channel by hand switches the counter off and keeps its setup."""
        for row in await self.db.all("SELECT number FROM stat_counters WHERE guild_id = ? AND channel_id = ?", (channel.guild.id, channel.id)):
            await self._counter_gone(channel.guild.id, row["number"], channel)

    @commands.Cog.listener("on_guild_channel_update")
    async def _counter_channel_changed(self, before: Any, after: Any) -> None:
        """A counter's channel moved or had its permissions changed: the bot notes where it is, and puts its own access back when it can."""
        rows = await self.db.all("SELECT number FROM stat_counters WHERE guild_id = ? AND channel_id = ? AND enabled = 1", (after.guild.id, after.id))
        if not rows:
            return
        guild = after.guild
        problem = None
        allowed = after.permissions_for(guild.me)
        if not (allowed.view_channel and allowed.manage_channels and allowed.connect):
            problem = await self._keep_own_access(guild, after)
            allowed = after.permissions_for(guild.me)
            if not (allowed.view_channel and allowed.manage_channels and allowed.connect):
                problem = problem or LOCKED_NOTE
                self._tools_notice(guild, f"counter:{rows[0]['number']}", f"Counter channel {after.mention}: {problem}")
        for row in rows:
            await self.db.run("UPDATE stat_counters SET parent_id = ?, position = ?, problem = COALESCE(?, problem) WHERE guild_id = ? AND number = ?",
                              (getattr(after, "category_id", None), getattr(after, "position", None), problem, guild.id, row["number"]))

    # ---- counter channels: setups ----

    def _counter_lock(self, guild_id: int) -> asyncio.Lock:
        """New counters and stat roles in a server take their numbers one at a time."""
        return self._counter_locks.setdefault(guild_id, asyncio.Lock())

    async def add_counter(self, guild_id: int, setup: dict, *, guild: Any = None, enabled: bool = False, channel_id: int | None = None,
                          parent_id: int | None = None) -> tuple[int, str | None]:
        """Store a new setup and, when it's to be on, make or take over its channel. Returns (its number, a problem to show)."""
        async with self._counter_lock(guild_id):
            if await self.db.scalar("SELECT COUNT(*) FROM stat_counters WHERE guild_id = ?", (guild_id,)) >= MAX_COUNTER_CHANNELS:
                raise Invalid(f"A server can have {MAX_COUNTER_CHANNELS} counter channels. Delete one first.")
            number = (await self.db.scalar("SELECT MAX(number) FROM stat_counters WHERE guild_id = ?", (guild_id,)) or 0) + 1
            now = time.time()
            body = {"text": setup["text"], "counters": setup["counters"], "frequency": setup["frequency"]}
            await self.db.run("INSERT INTO stat_counters (guild_id, number, name, setup, enabled, parent_id, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                              (guild_id, number, setup.get("name") or "", json.dumps(body), parent_id, now, now))
        problem = None
        if enabled and guild is not None:
            counter = await self._counter(guild_id, number)
            _, problem = await self.counter_on(guild, counter, channel_id=channel_id, parent_id=parent_id)
        return number, problem

    async def edit_counter(self, guild: Any, counter: dict, setup: dict) -> dict:
        """Save a changed setup and, when its channel is up, rename it now if Discord's limit allows."""
        same = setup["counters"] == counter["setup"]["counters"]
        body = {"text": setup["text"], "counters": setup["counters"], "frequency": setup["frequency"]}
        await self.db.run("UPDATE stat_counters SET name = ?, setup = ?, readings = ?, checked_at = NULL, updated_at = ? WHERE guild_id = ? AND number = ?",
                          (setup.get("name") or "", json.dumps(body), json.dumps(counter["readings"] if same else []), time.time(), counter["guild_id"], counter["number"]))
        fresh = await self._counter(counter["guild_id"], counter["number"])
        if guild is None or fresh is None or not fresh["enabled"]:
            return {}
        place = await self.counter_place(guild, time.time(), [fresh["setup"]])
        return await self.refresh_counter(guild, fresh, place)

    async def copy_counter(self, guild_id: int, counter: dict) -> int:
        setup = {**counter["setup"], "name": (counter["name"] + " (copy)")[:counters.LABEL_LIMIT] if counter["name"] else "Copy"}
        number, _ = await self.add_counter(guild_id, setup, parent_id=counter["parent_id"])
        return number

    async def remove_counter(self, guild: Any, counter: dict, *, keep_channel: bool = False) -> str | None:
        problem = None
        if counter["enabled"] and guild is not None:
            problem = await self.counter_off(guild, counter, keep_channel=keep_channel)
        await self.db.run("DELETE FROM stat_counters WHERE guild_id = ? AND number = ?", (counter["guild_id"], counter["number"]))
        return problem

    async def quick_counters(self, guild: Any) -> tuple[list[int], str | None]:
        """A new category at the top of the list with the three counters most servers start with: members, members online and the date."""
        try:
            category = await guild.create_category(QUICK_CATEGORY, position=0, reason="Counter channels")
        except discord.Forbidden:
            return [], CANT_MAKE
        except (discord.HTTPException, *TRANSIENT) as error:
            return [], f"Discord couldn't make the category: {getattr(error, 'text', '') or error}"
        made, problems = [], []
        for setup in quick_setups():
            try:
                number, problem = await self.add_counter(guild.id, counters.clean_setup(setup), guild=guild, enabled=True, parent_id=category.id)
            except Invalid as error:
                problems.append(str(error))
                break
            made.append(number)
            if problem:
                problems.append(problem)
        return made, problems[0] if problems else None

    # ---- stat roles: rules ----

    async def _rule(self, guild_id: int, number: int) -> dict | None:
        row = await self.db.one("SELECT * FROM stat_roles WHERE guild_id = ? AND number = ?", (guild_id, number))
        return rule_from_row(row) if row else None

    async def _rules(self, guild_id: int) -> list[dict]:
        rows = await self.db.all("SELECT * FROM stat_roles WHERE guild_id = ? ORDER BY number LIMIT ?", (guild_id, MAX_STAT_ROLES))
        return [found for found in (rule_from_row(row) for row in rows) if found is not None]

    async def _rules_by_number(self, guild_id: int) -> dict[int, dict]:
        return {found["number"]: found["rule"] for found in await self._rules(guild_id)}

    def snapshot(self, guild: Any, now: float) -> statroles.Snapshot:
        """The server's members as they are now, which a run reads from start to finish."""
        me_id = guild.me.id if guild.me is not None else None
        people = {member.id: statroles.person_from(member) for member in guild.members if member.id != me_id}
        return statroles.Snapshot(guild.id, now, self._zone(guild.id), people, frozenset(self.private), self.cfg(guild.id), guild)

    def statrole_role_problem(self, guild: Any, role: Any, editor: Any = None) -> str | None:
        """Why a stat role can't hand out this role, or None. `editor` is the person setting it up, held to their own highest role."""
        if role is None:
            return "That role isn't in this server."
        if role.is_default():
            return "Everyone always has @everyone, so a stat role can't give it."
        if role.managed:
            return f"{role.name} belongs to an integration, and Discord doesn't let anyone hand it out."
        if not self._safe_to_hand_out(guild, role):
            return f"{role.name} holds permissions the bot won't hand out while elevated roles are off on the Commands page."
        if guild.me is not None and role >= guild.me.top_role:
            return f"{role.name} sits at or above the bot's highest role, so the bot can't hand it out."
        return self._above_editor(guild, role, editor)

    def _above_editor(self, guild: Any, role: Any, editor: Any) -> str | None:
        """Why the person setting up a stat role can't pick this role: it sits at or above their own highest role, and they aren't the owner
        or an Administrator, and the server doesn't allow it. `editor` None is someone the check doesn't hold."""
        if editor is not None and role is not None and not access.is_admin(editor) and not self.cfg(guild.id)["statrole_above_own"] and role >= editor.top_role:
            return f"{role.name} sits at or above your highest role, so you can't set it up to be handed out."
        return None

    def _safe_to_hand_out(self, guild: Any, role: Any) -> bool:
        return role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE))

    async def statroles_due(self, now: float) -> None:
        rows = await self.db.all("SELECT * FROM stat_roles WHERE enabled = 1 AND (next_run IS NULL OR next_run <= ?)", (now,))
        for row in rows:
            guild_id = row["guild_id"]
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.unavailable or not self.on_in(guild_id) or guild_id in self._restoring:
                continue
            found = rule_from_row(row)
            following = statroles.next_run(found["rule"]["schedule"], now, self._zone(guild_id)) if found else None
            await self.db.run("UPDATE stat_roles SET next_run = ? WHERE guild_id = ? AND number = ?",
                              (following if following is not None else now + 30 * 86400, guild_id, row["number"]))
            if found is None or _number(row["next_run"]) is None:
                continue
            if self._tools_applying(guild_id):
                continue  # a recount or merge is swapping the counts, so this time is skipped and the rule runs at its next one
            last = await self.db.scalar("SELECT MAX(started_at) FROM stat_role_runs WHERE guild_id = ? AND rule = ?", (guild_id, found["number"]))
            if last is not None and now - last < REPEAT_GAP:
                continue  # a clock change can bring two of a schedule's times within minutes of each other
            await self.start_run(guild_id, found["number"], "schedule")

    async def start_run(self, guild_id: int, number: int, reason: str) -> str:
        """Start a run, or line one up behind the run already going. Returns "started" or "queued", or, when nothing starts, "held" while a
        backup is being restored into the server, "applying" while a recount or merge applies there, and "off" while Statistics is off."""
        if guild_id in self._restoring:
            return "held"
        if not self.on_in(guild_id):
            return "off"
        if self._tools_applying(guild_id):
            return "applying"
        state = self._server_runs.setdefault(guild_id, ServerRuns())
        if number in state.runs or number in state.working_out:
            state.again[number] = reason
            return "queued"
        state.working_out.add(number)
        self._tools_spawn(self._run_rule(guild_id, number, reason))
        return "started"

    async def _run_rule(self, guild_id: int, number: int, reason: str) -> None:
        state = self._server_runs.setdefault(guild_id, ServerRuns())
        run: Run | None = None
        restores = self._restores.get(guild_id, 0)
        try:
            found = await self._rule(guild_id, number)
            guild = self.bot.get_guild(guild_id)
            if found is None or guild is None:
                return
            started = time.time()
            run = Run(guild_id, number, found["name"], found["role_id"], found["rule"], reason, started)
            await self.db.run("INSERT OR REPLACE INTO stat_role_runs (guild_id, rule, started_at, reason, state) VALUES (?, ?, ?, ?, 'running')",
                              (guild_id, number, started, reason))
            run.stopped = self._run_blocker(guild, found["role_id"])
            if run.stopped == ELEVATED:
                self._tools_notice(guild, f"statrole-elevated:{number}", f"Stat role {found['name']}: {ELEVATED}")
            if run.stopped is None:
                async with self._run_gate:
                    if not await whole_member_list(guild):
                        run.stopped = "The member list was still loading after a restart, so this run stopped. The next one tries again."
                    else:
                        await self.up_to_date()
                        now = time.time()
                        snap = self.snapshot(guild, now)
                        rules = await self._rules_by_number(guild_id)
                        if statroles.missing_rules(found["rule"], rules):
                            run.stopped = FOLLOWS_MISSING  # a rule that follows nothing would take its role from everyone holding it
                            self._tools_notice(guild, f"statrole-follows:{number}", f"Stat role {found['name']}: {FOLLOWS_MISSING}")
                        else:
                            evaluation = await asyncio.wait_for(statroles.Evaluator(self.message_db, snap, rules).evaluate(found["rule"]), EVALUATION_SECONDS)
                            change = statroles.plan(found["rule"], evaluation, snap, found["role_id"])
                            run.matching, run.to_give, run.to_take = change.matching, len(change.give), len(change.take)
                            run.give.extend(change.give)
                            run.take.extend(change.take)
            if self._restores.get(guild_id, 0) != restores or guild_id in self._restoring:
                run.stopped = RESTORED  # it worked out the rule as it was before the restore
            elif not self.on_in(guild_id):
                run.stopped = SWITCHED_OFF
            elif self._tools_applying(guild_id):
                run.stopped = APPLIED_DURING
            if run.stopped:
                run.give.clear()
                run.take.clear()
        except asyncio.TimeoutError:
            if run is not None:
                run.stopped = f"Working out who passes took over {int(EVALUATION_SECONDS)} seconds, so the run stopped. Look for a condition reading all time."
        except Exception:
            log.exception("Stat role %s in %s failed", number, guild_id)
            if run is not None:
                run.stopped = "Something went wrong working out this run. It's been logged."
        finally:
            state.working_out.discard(number)
        if run is None:
            again = state.again.pop(number, None)
            if again:
                await self.start_run(guild_id, number, again)
            return
        if run.stopped or (not run.give and not run.take):
            await self._finish_run(guild_id, run)
            return
        state.runs[number] = run
        state.order.append(number)
        if state.worker is None or state.worker.done():
            state.worker = asyncio.ensure_future(self._role_worker(guild_id))

    def _run_blocker(self, guild: Any, role_id: int) -> str | None:
        me = guild.me
        role = guild.get_role(role_id)
        if role is None:
            return "The role was deleted."
        if me is None or not me.guild_permissions.manage_roles:
            return "The bot needs Manage Roles to give and take stat roles."
        if role >= me.top_role:
            return ROLE_TOO_HIGH
        if not self._safe_to_hand_out(guild, role):
            return ELEVATED
        return None

    async def _role_worker(self, guild_id: int) -> None:
        """Works through the server's changes about one a second, taking turns between the rules running. A long stretch of members it
        passes over, such as people who left, lets other work in every SKIPS_PER_TURN of them."""
        state = self._server_runs[guild_id]
        failures = 0
        skipped = 0
        try:
            while state.order:
                number = state.order.popleft()
                run = state.runs.get(number)
                if run is None:
                    continue
                if run.stopped or (not run.give and not run.take):
                    await self._finish_run(guild_id, run)
                    continue
                state.order.append(number)
                adding = bool(run.give)
                member_id = run.give.popleft() if adding else run.take.popleft()
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    self._stop_server(guild_id, "The bot left the server or lost its connection to it.")
                    continue
                try:
                    outcome = await self._change_role(guild, run, member_id, adding)
                except asyncio.CancelledError:
                    raise
                except Exception:  # one member's change going wrong costs that change, never the runs behind it
                    log.exception("A stat role change in %s for %s failed", guild_id, member_id)
                    run.failed += 1
                    outcome = "edited"
                if outcome == "again":
                    (run.give if adding else run.take).append(member_id)
                    failures += 1
                    await asyncio.sleep(min(OUTAGE_PAUSE, SPACING * 2 ** min(failures, 10)))
                    continue
                failures = 0
                if outcome == "edited":
                    await asyncio.sleep(SPACING)
                else:
                    skipped += 1
                    if skipped % SKIPS_PER_TURN == 0:
                        await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Stat roles stopped working through the changes in %s", guild_id)
            for run in list(state.runs.values()):  # so the rules can run again, rather than wait on a worker that's gone
                run.stopped = run.stopped or "Something went wrong during this run. It's been logged."
                try:
                    await self._finish_run(guild_id, run)
                except Exception:
                    state.runs.pop(run.number, None)
                    log.exception("Couldn't record the end of stat role %s in %s", run.number, guild_id)
            if state.runs:  # a run that started while these were closing found this worker still going, so it gets a new one
                state.worker = asyncio.ensure_future(self._role_worker(guild_id))

    def _tools_applying(self, guild_id: int) -> bool:
        """Whether a recount or merge is applying in the server's statistics, when counts are half swapped and nothing should act on them."""
        check = getattr(self, "stats_applying", None)
        return bool(check(guild_id)) if check is not None else False

    def _stop_server(self, guild_id: int, note: str) -> None:
        state = self._server_runs.get(guild_id)
        for run in list(state.runs.values()) if state else []:
            run.stopped = run.stopped or note
            run.give.clear()
            run.take.clear()

    def _stop_runs(self, guild_id: int, note: str) -> None:
        """Every run in the server stops where it is, and the runs lined up behind them never start."""
        state = self._server_runs.get(guild_id)
        if state is not None:
            state.again.clear()
        self._stop_server(guild_id, note)

    async def _change_role(self, guild: Any, run: Run, member_id: int, adding: bool) -> str:
        """One member's change: "edited", "skipped", "again" when Discord failed in a way that passes, or "stopped"."""
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            self._stop_server(guild.id, LOST_MANAGE_ROLES)
            self._tools_notice(guild, "statroles:manage_roles", f"Stat roles: {LOST_MANAGE_ROLES}")
            return "stopped"
        role = guild.get_role(run.role_id)
        if role is None or role >= me.top_role:
            run.stopped = "The role was deleted during the run." if role is None else ROLE_TOO_HIGH
            run.give.clear()
            run.take.clear()
            return "stopped"
        if not self._safe_to_hand_out(guild, role):  # someone gave the role moderation permissions during the run
            run.stopped = ELEVATED
            run.give.clear()
            run.take.clear()
            self._tools_notice(guild, f"statrole-elevated:{run.number}", f"Stat role {run.name}: {ELEVATED}")
            return "stopped"
        member = guild.get_member(member_id)
        if member is None or getattr(member, "pending", False):
            return "skipped"
        if any(held.id == role.id for held in member.roles) == adding:
            return "skipped"
        try:
            await (member.add_roles if adding else member.remove_roles)(role, reason=f"Stat role: {run.name}"[:500])
        except discord.NotFound:
            return "skipped"
        except discord.Forbidden:
            run.stopped = REFUSED
            run.give.clear()
            run.take.clear()
            self._tools_notice(guild, f"statrole:{run.number}", f"Stat role {run.name}: {REFUSED}")
            return "stopped"
        except discord.HTTPException as error:
            if error.status >= 500 or error.status == 429:
                return "again"
            run.failed += 1
            return "edited"
        except TRANSIENT:
            return "again"
        if adding:
            run.gave += 1
        else:
            run.took += 1
        try:
            await self._notify(guild, run, member, role, adding)
        except Exception:  # the role changed, and a message that went wrong doesn't undo that
            log.exception("A stat role notification in %s failed", guild.id)
            problem = "A notification couldn't be sent. It's been logged."
            if problem not in run.notes:
                run.notes.append(problem)
        return "edited"

    async def _finish_run(self, guild_id: int, run: Run) -> None:
        state = self._server_runs.get(guild_id)
        if state is not None:
            state.runs.pop(run.number, None)
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            try:
                await self._send_grouped(guild, run)
            except Exception:
                log.exception("Grouped stat role notifications failed in %s", guild_id)
        note = run.stopped or (" ".join(run.notes[:3]) if run.notes else None)
        try:
            await self.db.run(
                "UPDATE stat_role_runs SET finished_at = ?, state = ?, matching = ?, to_give = ?, to_take = ?, gave = ?, took = ?, failed = ?, note = ? "
                "WHERE guild_id = ? AND rule = ? AND started_at = ?",
                (time.time(), "stopped" if run.stopped else "done", run.matching, run.to_give, run.to_take, run.gave, run.took, run.failed, note, guild_id, run.number, run.started_at),
            )
            await self.db.run("DELETE FROM stat_role_runs WHERE guild_id = ? AND rule = ? AND started_at NOT IN "
                              "(SELECT started_at FROM stat_role_runs WHERE guild_id = ? AND rule = ? ORDER BY started_at DESC LIMIT ?)",
                              (guild_id, run.number, guild_id, run.number, RUNS_KEPT))
        except Exception:  # the run is over whether or not its record could be written. A row left saying it runs is picked up at the next start
            log.exception("Couldn't record the end of stat role %s in %s", run.number, guild_id)
        again = state.again.pop(run.number, None) if state is not None else None
        if again:
            await self.start_run(guild_id, run.number, again)

    async def _resume_runs(self) -> None:
        """Runs a restart cut short start again once the member lists are in."""
        await self.bot.members_settled.wait()
        for row in await self.db.all("SELECT guild_id, rule, started_at FROM stat_role_runs WHERE state = 'running'"):
            await self.db.run("UPDATE stat_role_runs SET state = 'stopped', finished_at = ?, note = ? WHERE guild_id = ? AND rule = ? AND started_at = ?",
                              (time.time(), "The bot restarted during this run, so it ran again.", row["guild_id"], row["rule"], row["started_at"]))
            if self.on_in(row["guild_id"]) and self.bot.get_guild(row["guild_id"]) is not None:
                await self.start_run(row["guild_id"], row["rule"], "resumed")

    @commands.Cog.listener("on_guild_role_delete")
    async def _statrole_role_deleted(self, role: Any) -> None:
        for row in await self.db.all("SELECT number, name FROM stat_roles WHERE guild_id = ? AND role_id = ? AND enabled = 1", (role.guild.id, role.id)):
            await self.db.run("UPDATE stat_roles SET enabled = 0, paused = ? WHERE guild_id = ? AND number = ?", (PAUSED_ROLE_GONE, role.guild.id, row["number"]))
            self._tools_notice(role.guild, f"statrole-gone:{row['number']}", f"Stat role {row['name']}: {PAUSED_ROLE_GONE}", title="Stat role switched off")
        if role.id in self._held_roles.get(role.guild.id, ()):
            await self._remember_held_roles(role.guild.id)

    # ---- stat roles: notifications ----

    async def _notify(self, guild: Any, run: Run, member: Any, role: Any, adding: bool) -> None:
        note = run.rule.get("give" if adding else "remove")
        if not note:
            return
        if note["group"]:
            run.grouped[adding].append(member.id)
            return
        if note["send"] in ("dm", "dm_fallback"):
            if run.dms >= DM_LIMIT:  # a burst of DMs can look like spam to Discord, so a big run stops sending them
                if not note.get("channel_id"):
                    run.note(f"Past {DM_LIMIT} DMs in one run, the rest of its notifications weren't sent.")
                    return
                note = {**note, "send": "channel"}
                run.note(f"Past {DM_LIMIT} DMs in one run, the rest of its notifications went to its channel.")
            else:
                run.dms += 1
        problem = await self.send_notification(guild, note, role, adding, member=member, paced=True)
        if problem:
            run.note(problem)

    async def _dm_turn(self, guild_id: int) -> None:
        """Waits until the server may send its next DM: DMs from one server go out at most one every DM_SPACING seconds. Called right
        before the send, and it waits until the clock has passed the server's next turn, since a sleep can end a little early."""
        ready = self._dm_next.get(guild_id, 0.0)
        while (now := time.monotonic()) < ready:
            await asyncio.sleep(ready - now)
        self._dm_next[guild_id] = now + DM_SPACING

    async def send_notification(self, guild: Any, note: dict, role: Any, adding: bool, *, member: Any = None, extra: dict | None = None,
                                payload: dict | None = None, paced: bool = False) -> str | None:
        """One notification, where the rule sends it. Returns the problem, or None when it went out. A paced DM waits for the server's
        next DM turn just before it's sent."""
        try:
            channel = await delivery.find_channel(guild, note["channel_id"]) if note.get("channel_id") else None
        except (discord.HTTPException, *TRANSIENT) as error:
            return f"The notification's channel couldn't be reached: {getattr(error, 'text', '') or error}"
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=not note["suppress"], replied_user=False)
        scope = Scope(guild=guild, member=member, channel=channel, role=role, role_added=adding, extra=extra or {})
        rendered = await render_message(payload or note["message"], scope)
        if not messages.has_content(rendered):
            return None
        kwargs = messages.send_kwargs(rendered, allowed_mentions=mentions)
        if note["send"] in ("dm", "dm_fallback") and member is not None:
            if paced:
                await self._dm_turn(guild.id)
            try:
                await member.send(**kwargs)
                return None
            except (discord.HTTPException, *TRANSIENT):
                if note["send"] == "dm":
                    return "Some members have DMs closed, so their notification wasn't sent."
        if channel is None or not hasattr(channel, "send"):
            return "The notification's channel is gone. Pick another one."
        try:
            await channel.send(**kwargs)
        except discord.Forbidden:
            return f"The bot can't post notifications in #{channel.name}. Give it View Channel, Send Messages and Embed Links there."
        except (discord.HTTPException, *TRANSIENT) as error:
            return f"Discord refused a notification: {getattr(error, 'text', '') or error}"
        return None

    async def _send_grouped(self, guild: Any, run: Run) -> None:
        """One message for everyone a run changed, split over several when the list is long, up to GROUPED_MESSAGES of them, the last
        ending "and N more". Each message is sized from the text its template really makes, so a list never ends partway through a mention."""
        role = guild.get_role(run.role_id)
        for adding in (True, False):
            people = run.grouped[adding]
            note = run.rule.get("give" if adding else "remove")
            if not people or not note:
                continue
            payload = messages.map_text(note["message"], lambda text: text.replace("{mention}", "{members}").replace("{user}", "{members}"))
            count = {"count": str(len(people))}
            measured = await self._render_note(guild, note, role, adding, extra={"members": MEMBERS_MARK, **count}, payload=payload)
            room = members_room(measured)
            if room is None or room < 60:  # the message doesn't list members, or leaves no room for one: it goes once, with the number
                problem = await self.send_notification(guild, note, role, adding, extra={"members": f"{len(people):,} members", **count}, payload=payload)
                if problem:
                    run.note(problem)
                continue
            for listed in grouped_lists([f"<@{person}>" for person in people], room):
                problem = await self.send_notification(guild, note, role, adding, extra={"members": listed, **count}, payload=payload)
                if problem:
                    run.note(problem)
                    break

    async def _render_note(self, guild: Any, note: dict, role: Any, adding: bool, *, extra: dict, payload: dict) -> dict:
        """A notification as send_notification would make it, without sending it."""
        try:
            channel = await delivery.find_channel(guild, note["channel_id"]) if note.get("channel_id") else None
        except (discord.HTTPException, *TRANSIENT):
            channel = None
        return await render_message(payload, Scope(guild=guild, member=None, channel=channel, role=role, role_added=adding, extra=extra))

    # ---- stat roles: checking and previewing a rule ----

    async def rule_form(self, guild: Any, editor: Any, form: dict, number: int | None) -> dict:
        """A stat role from a form, with every check the dashboard and commands share. Raises ValidationError naming the field."""
        try:
            name = " ".join(str(form.get("name") or "").split())
            if not name or len(name) > statroles.NAME_LIMIT:
                raise ValidationError({"name": f"Give the stat role a name of up to {statroles.NAME_LIMIT} characters."})
            role_id = snowflake(form.get("role_id"))
        except Invalid as error:
            raise ValidationError({"role_id": "Pick the role it gives."}) from error
        try:
            rule = statroles.clean_rule(form)
        except Invalid as error:
            raise ValidationError({"rule": str(error)}) from error
        others = [found for found in await self._rules(guild.id) if found["number"] != number]
        if any(found["name"].casefold() == name.casefold() for found in others):
            raise ValidationError({"name": f"Another stat role is called {name}. Each needs its own name."})
        holder = next((found for found in others if found["role_id"] == role_id), None)
        if holder is not None:
            raise ValidationError({"role_id": f"Stat role {holder['name']} already gives that role. A role follows one stat role."})
        current = await self._rule(guild.id, number) if number is not None else None
        role = guild.get_role(role_id)
        if current is None or current["role_id"] != role_id:
            problem = self.statrole_role_problem(guild, role, editor)
        else:
            problem = self._above_editor(guild, role, editor)  # rewriting who passes is handing the role out, so the editor's height holds too
        if problem:
            raise ValidationError({"role_id": problem})
        rules = {found["number"]: found["rule"] for found in others}
        for target in statroles.followed_rules(rule):
            if target not in rules:
                raise ValidationError({"rule": "A stat role condition follows a stat role that isn't there anymore."})
        mine = number if number is not None else max([*rules, 0]) + 1
        rules[mine] = rule
        loop = statroles.loop_through(mine, rules)
        if loop:
            raise ValidationError({"rule": "Its stat role conditions loop back to it. A stat role can't follow itself, directly or through others."})
        for role in role_ids(rule):
            if guild.get_role(role) is None:
                raise ValidationError({"rule": "A filter names a role that isn't in this server."})
        return {"name": name, "role_id": role_id, "rule": rule, "enabled": bool(form.get("enabled", True))}

    async def evaluate_form(self, guild: Any, rule: dict) -> tuple[statroles.Evaluation, statroles.Snapshot]:
        """Who passes a rule now, kept for two minutes, so checking several members after a preview doesn't read everything again."""
        key = (guild.id, setup_hash(rule))
        now = time.time()
        for stale in [found for found, (at, _, _) in self._previews.items() if now - at > PREVIEW_KEPT_SECONDS]:
            self._previews.pop(stale, None)
        kept = self._previews.get(key)
        if kept is not None:
            return kept[1], kept[2]
        if not await whole_member_list(guild):
            raise ApiError(409, "The bot is still loading this server's member list after a restart. Try again in a few minutes.")
        if self._tools_applying(guild.id):
            raise ApiError(409, APPLYING)
        await self.up_to_date()
        snap = self.snapshot(guild, now)
        rules = await self._rules_by_number(guild.id)
        async with self._run_gate:
            try:
                evaluation = await asyncio.wait_for(statroles.Evaluator(self.message_db, snap, rules).evaluate(rule), PREVIEW_SECONDS)
            except asyncio.TimeoutError as error:
                raise ApiError(504, f"Working out who passes took over {int(PREVIEW_SECONDS)} seconds. Look for a condition that reads all time.") from error
        self._previews[key] = (now, evaluation, snap)
        while len(self._previews) > PREVIEWS_KEPT:  # each holds every member's results, so only the newest few stay
            self._previews.pop(min(self._previews, key=lambda found: self._previews[found][0]))
        return evaluation, snap

    def _person_label(self, guild: Any, user_id: int) -> dict:
        member = guild.get_member(user_id)
        return {"id": user_id, "name": member.display_name if member is not None else str(user_id)}

    async def preview(self, guild: Any, cleaned: dict, *, everyone: bool = False) -> dict:
        started = time.monotonic()
        evaluation, snap = await self.evaluate_form(guild, cleaned["rule"])
        change = statroles.plan(cleaned["rule"], evaluation, snap, cleaned["role_id"])
        rule = cleaned["rule"]
        notes = 0
        for key, people in (("give", change.give), ("remove", change.take)):
            note = rule.get(key)
            if note and people:
                notes += min(GROUPED_MESSAGES, max(1, math.ceil(len(people) / 60))) if note["group"] else len(people)
        out = {
            "matching": change.matching, "gain": len(change.give), "lose": len(change.take), "left_alone": change.left_alone,
            "gain_names": [self._person_label(guild, person) for person in change.give[:PREVIEW_NAMES]],
            "lose_names": [self._person_label(guild, person) for person in change.take[:PREVIEW_NAMES]],
            "notifications": notes, "seconds": round(time.monotonic() - started, 1),
        }
        if everyone:
            passing = sorted(evaluation.passing & set(snap.people))
            out["everyone"] = [self._person_label(guild, person) for person in passing[:EVERYONE_NAMES]]
        return out

    async def check_member(self, guild: Any, cleaned: dict, user_id: int) -> dict:
        evaluation, snap = await self.evaluate_form(guild, cleaned["rule"])
        names = {found["number"]: found["name"] for found in await self._rules(guild.id)}
        person = snap.people.get(user_id)
        return {"member": self._person_label(guild, user_id), "in_server": person is not None, "private": user_id in snap.private,
                "passes": user_id in evaluation.passing, "conditions": statroles.explain(cleaned["rule"], evaluation, user_id, names)}

    async def save_rule(self, guild_id: int, cleaned: dict, number: int | None) -> int:
        """Store a stat role. Saving doesn't run it: its schedule does, or Run now."""
        now = time.time()
        next_time = statroles.next_run(cleaned["rule"]["schedule"], now, self._zone(guild_id))
        async with self._counter_lock(guild_id):
            if number is None:
                if await self.db.scalar("SELECT COUNT(*) FROM stat_roles WHERE guild_id = ?", (guild_id,)) >= MAX_STAT_ROLES:
                    raise ValidationError({"name": f"A server can have {MAX_STAT_ROLES} stat roles, one for each of its roles."})
                number = (await self.db.scalar("SELECT MAX(number) FROM stat_roles WHERE guild_id = ?", (guild_id,)) or 0) + 1
                await self.db.run("INSERT INTO stat_roles (guild_id, number, name, role_id, enabled, rule, next_run, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                  (guild_id, number, cleaned["name"], cleaned["role_id"], int(cleaned["enabled"]), json.dumps(cleaned["rule"]), next_time, now, now))
            else:
                await self.db.run("UPDATE stat_roles SET name = ?, role_id = ?, enabled = ?, rule = ?, paused = NULL, next_run = ?, updated_at = ? WHERE guild_id = ? AND number = ?",
                                  (cleaned["name"], cleaned["role_id"], int(cleaned["enabled"]), json.dumps(cleaned["rule"]), next_time, now, guild_id, number))
        await self._remember_held_roles(guild_id)
        return number

    def running(self, guild_id: int) -> dict[int, dict]:
        state = self._server_runs.get(guild_id)
        if state is None:
            return {}
        out = {number: run.json() for number, run in state.runs.items()}
        for number in state.working_out:
            out.setdefault(number, {"rule": number, "running": True, "working_out": True})
        return out

    # ---- commands ----

    def _counter_preset(self, guild: Any, kind: str, *, text: str | None, frequency: int | None, roles: str | None, account: str | None, ends: str | None,
                        date_format: str | None, zone_name: str | None, days: int | None, goal: int | None, style: str | None) -> dict:
        """A setup from one of the ready-made counters and the options given with it. Raises Invalid with the sentence to show."""
        if kind not in COUNTER_PRESETS:
            raise Invalid("Pick a counter from the list.")
        _, counter, default_text = COUNTER_PRESETS[kind]
        counter = copy.deepcopy(counter)
        if "stat" in counter and days:
            counter["stat"]["period"] = {"amount": days, "unit": "days"}
        if counter["type"] == "role_members":
            from .roles import find_roles, missing_roles_sentence
            found, missing = find_roles(guild, roles or "")
            if missing:
                raise Invalid(missing_roles_sentence(missing))
            if not found:
                raise Invalid("Name the roles to count members in, by mention, ID or name.")
            counter["roles"] = [role.id for role in found]
        if counter["type"] in counters.SOCIAL_TYPES:
            if not account:
                raise Invalid("Give the account to follow, with the account option.")
            counter["account"] = account
        if counter["type"] == "countdown":
            counter["end"] = parse_end(ends, self._zone(guild.id), time.time())
        if counter["type"] == "datetime":
            if date_format:
                counter["format"] = date_format
            counter["zone"] = counters.clean_zone(zone_name or "server")
        if goal is not None and counter["type"] in counters.GOAL_TYPES:
            counter["goal"] = goal
        if style and counter["type"] not in counters.WORD_TYPES:
            counter["style"] = style
        return counters.clean_setup({"name": COUNTER_PRESETS[kind][0], "text": text or default_text, "counters": [counter],
                                     "frequency": frequency or counters.DEFAULT_FREQUENCY})

    async def _command_gate(self, ctx: commands.Context, area: str) -> bool:
        """Whether the person may manage this area from Discord, telling them why not when they can't. See may_manage."""
        if self.may_manage(ctx.author, area):
            return True
        await ctx.fail(f"Managing {TOOLS_AREAS[area][2]} takes {COUNTERS_WHO if area == 'counters' else STATROLES_WHO}.")
        return False

    async def _discord_said(self, ctx: commands.Context, error: discord.HTTPException) -> None:
        await ctx.fail(discord_words(error))

    async def _find_counter(self, ctx: commands.Context, number: int) -> dict | None:
        if not await self._command_gate(ctx, "counters"):
            return None
        found = await self._counter(ctx.guild.id, number)
        if found is None:
            await ctx.fail(f"There's no counter {number}. `/counter list` shows them.")
        return found

    @commands.hybrid_group(name="counter", aliases=["counters"], fallback="list", description="Voice channels named with live counts, such as members online")
    @commands.guild_only()
    @staff_only(COUNTERS_WHO)
    async def counter(self, ctx: commands.Context) -> None:
        from .roles import send_pages
        if not await self._command_gate(ctx, "counters"):
            return
        lines = []
        for found in await self._counter_rows(ctx.guild.id):
            where = f"<#{found['channel_id']}>" if found["channel_id"] else "no channel"
            label = discord.utils.escape_markdown(found["name"] or f"Counter {found['number']}")
            parts = "; ".join(describe_counter(item) for item in found["setup"]["counters"])
            line = f"**{found['number']}. {label}** ({'on' if found['enabled'] else 'off'}, every {FREQUENCY_NAMES[found['setup']['frequency']]}) {where}\n`{found['setup']['text']}`: {parts}"
            if found["problem"]:
                line += f"\n{found['problem']}"
            lines.append(line)
        await send_pages(ctx, "Counter channels", lines, empty="No counter channels yet. Make one with `/counter add` or `/counter quick`, or on the dashboard.")

    @counter.command(name="add", description="Make a counter channel from a ready-made counter")
    @staff_only(COUNTERS_WHO)
    @app_commands.describe(
        kind="What it counts", text="The channel's name, with {1} where the count goes", frequency="How often it updates",
        category="The category to make it in", channel="A voice channel to take over instead of making one, keeping its place and permissions",
        roles="For members in roles: the roles, by mention, ID or name", account="For follower counts: the account's handle or ID",
        ends="For a countdown: when it ends, as a Discord timestamp, YYYY-MM-DD HH:MM, or 3d or 12h from now",
        date_format="For a date: its format, such as dddd, MMM Do", zone_name="For a date: a time zone such as Europe/Paris or UTC+05:30",
        days="For statistics: how many days back to count", goal="Show how far the count is from this number", style="How numbers show")
    @app_commands.rename(zone_name="zone")
    @app_commands.choices(
        kind=[app_commands.Choice(name=label, value=key) for key, (label, _, _) in list(COUNTER_PRESET_ITEMS)],
        frequency=[app_commands.Choice(name=label, value=minutes) for minutes, label in FREQUENCY_NAMES.items()],
        style=[app_commands.Choice(name="13742", value="plain"), app_commands.Choice(name="13,742", value="separated"), app_commands.Choice(name="13.7k", value="short")])
    async def counter_add(self, ctx: commands.Context, kind: str, text: Optional[str] = None, frequency: Optional[int] = None,
                          category: Optional[discord.CategoryChannel] = None, channel: Optional[discord.VoiceChannel] = None, roles: Optional[str] = None,
                          account: Optional[str] = None, ends: Optional[str] = None, date_format: Optional[str] = None, zone_name: Optional[str] = None,
                          days: Optional[int] = None, goal: Optional[int] = None, style: Optional[str] = None) -> None:
        guild = ctx.guild
        if not await self._command_gate(ctx, "counters"):
            return
        try:
            if days is not None and not 1 <= days <= 36500:
                raise Invalid("Count back from 1 to 36,500 days.")
            setup = self._counter_preset(guild, kind, text=text, frequency=frequency, roles=roles, account=account, ends=ends, date_format=date_format,
                                         zone_name=zone_name, days=days, goal=goal, style=style)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        for place in (channel, category):
            if place is not None and not access.can_view(ctx.author, place):
                await ctx.fail("You can't see that channel.")
                return
        await ctx.defer(ephemeral=True)
        try:
            number, problem = await self.add_counter(guild.id, setup, guild=guild, enabled=True, channel_id=channel.id if channel else None,
                                                     parent_id=category.id if category else None)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        except discord.HTTPException as error:
            await self._discord_said(ctx, error)
            return
        await self.bot.activity_log.record(guild.id, self.bot.actor(ctx.author), "counters.add", detail=setup["text"])
        found = await self._counter(guild.id, number)
        if found is None or not found["enabled"]:
            await ctx.fail(f"Saved as counter {number}, switched off. {problem or ''}".strip())
            return
        await ctx.ok(f"Counter {number} is <#{found['channel_id']}>. It updates every {FREQUENCY_NAMES[setup['frequency']]}." + (f" {problem}" if problem else ""))

    @counter.command(name="edit", description="Change a counter channel's text, name or how often it updates")
    @staff_only(COUNTERS_WHO)
    @app_commands.describe(number="The counter's number, from /counter list", text="The channel's name, with {1} and so on where the counts go",
                           frequency="How often it updates", name="A label for the list")
    @app_commands.choices(frequency=[app_commands.Choice(name=label, value=minutes) for minutes, label in FREQUENCY_NAMES.items()])
    async def counter_edit(self, ctx: commands.Context, number: int, text: Optional[str] = None, frequency: Optional[int] = None, name: Optional[str] = None) -> None:
        found = await self._find_counter(ctx, number)
        if found is None:
            return
        try:
            setup = counters.clean_setup({**found["setup"], "name": found["name"] if name is None else name, "text": text or found["setup"]["text"],
                                          "frequency": frequency or found["setup"]["frequency"]})
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await ctx.defer(ephemeral=True)
        try:
            result = await self.edit_counter(ctx.guild, found, setup)
        except discord.HTTPException as error:
            await self._discord_said(ctx, error)
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "counters.edit", detail=setup["text"])
        await ctx.ok(f"Saved counter {number}." + self._rename_words(result))

    def _rename_words(self, result: dict) -> str:
        if result.get("waiting_until"):
            return f" Discord allows two renames in ten minutes, so the new name shows <t:{int(result['waiting_until'])}:R>."
        if result.get("problem"):
            return f" {result['problem']}"
        return ""

    @counter.command(name="remove", description="Delete a counter channel's setup, and its channel unless you keep it")
    @staff_only(COUNTERS_WHO)
    @app_commands.describe(number="The counter's number, from /counter list", keep_channel="Keep the channel, which then stops updating")
    async def counter_remove(self, ctx: commands.Context, number: int, keep_channel: bool = False) -> None:
        found = await self._find_counter(ctx, number)
        if found is None:
            return
        await ctx.defer(ephemeral=True)  # switching off waits for any other counter switch in the server to finish
        try:
            problem = await self.remove_counter(ctx.guild, found, keep_channel=keep_channel)
        except discord.HTTPException as error:
            await self._discord_said(ctx, error)
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "counters.remove", detail=found["setup"]["text"])
        await ctx.ok(f"Deleted counter {number}." + (f" {problem}" if problem else ""))

    @counter.command(name="on", description="Switch a counter on: make its channel, or take over one")
    @staff_only(COUNTERS_WHO)
    @app_commands.describe(number="The counter's number, from /counter list", channel="A voice channel to take over instead of making one",
                           category="The category to make its channel in")
    async def counter_on_command(self, ctx: commands.Context, number: int, channel: Optional[discord.VoiceChannel] = None,
                                 category: Optional[discord.CategoryChannel] = None) -> None:
        found = await self._find_counter(ctx, number)
        if found is None:
            return
        if found["enabled"]:
            await ctx.fail(f"Counter {number} is already on, in <#{found['channel_id']}>.")
            return
        for place in (channel, category):
            if place is not None and not access.can_view(ctx.author, place):
                await ctx.fail("You can't see that channel.")
                return
        await ctx.defer(ephemeral=True)
        try:
            on, problem = await self.counter_on(ctx.guild, found, channel_id=channel.id if channel else None, parent_id=category.id if category else None)
        except discord.HTTPException as error:
            await self._discord_said(ctx, error)
            return
        if not on:
            await ctx.fail(problem or CANT_MAKE)
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "counters.on", detail=found["setup"]["text"])
        fresh = await self._counter(ctx.guild.id, number)
        await ctx.ok(f"Counter {number} is on, in <#{fresh['channel_id']}>." + (f" {problem}" if problem else ""))

    @counter.command(name="off", description="Switch a counter off. Its channel is deleted and its setup kept.")
    @staff_only(COUNTERS_WHO)
    @app_commands.describe(number="The counter's number, from /counter list")
    async def counter_off_command(self, ctx: commands.Context, number: int) -> None:
        found = await self._find_counter(ctx, number)
        if found is None:
            return
        if not found["enabled"]:
            await ctx.fail(f"Counter {number} is already off.")
            return
        await ctx.defer(ephemeral=True)  # switching off waits for any other counter switch in the server to finish
        try:
            problem = await self.counter_off(ctx.guild, found)
        except discord.HTTPException as error:
            await self._discord_said(ctx, error)
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "counters.off", detail=found["setup"]["text"])
        await ctx.ok(problem or f"Counter {number} is off. Its setup stays, and `/counter on {number}` makes a new channel.")

    @counter.command(name="update", description="Update a counter now, within Discord's two renames in ten minutes")
    @staff_only(COUNTERS_WHO)
    @app_commands.describe(number="The counter's number, from /counter list")
    async def counter_update(self, ctx: commands.Context, number: int) -> None:
        found = await self._find_counter(ctx, number)
        if found is None:
            return
        if not found["enabled"]:
            await ctx.fail(f"Counter {number} is off. `/counter on {number}` switches it on.")
            return
        await ctx.defer(ephemeral=True)
        try:
            result = await self.refresh_counter(ctx.guild, found, await self.counter_place(ctx.guild, time.time(), [found["setup"]]))
        except discord.HTTPException as error:
            await self._discord_said(ctx, error)
            return
        if result.get("gone"):
            await ctx.fail(GONE_NOTE)
            return
        if result.get("busy"):
            await ctx.ok("It's updating right now.")
            return
        if result.get("applying"):
            await ctx.fail(APPLYING)
            return
        await ctx.ok(f"Counter {number} reads {discord.utils.escape_markdown(result['name'])}." + self._rename_words(result))

    @counter.command(name="copy", description="Copy a counter's setup into a new one, switched off")
    @staff_only(COUNTERS_WHO)
    @app_commands.describe(number="The counter's number, from /counter list")
    async def counter_copy(self, ctx: commands.Context, number: int) -> None:
        found = await self._find_counter(ctx, number)
        if found is None:
            return
        try:
            made = await self.copy_counter(ctx.guild.id, found)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "counters.copy", detail=found["setup"]["text"])
        await ctx.ok(f"Copied counter {number} to counter {made}, switched off. `/counter on {made}` makes its channel.")

    @counter.command(name="quick", description="Make the three usual counters in a new category: members, online and the date")
    @staff_only(COUNTERS_WHO)
    async def counter_quick(self, ctx: commands.Context) -> None:
        if not await self._command_gate(ctx, "counters"):
            return
        await ctx.defer(ephemeral=True)
        try:
            made, problem = await self.quick_counters(ctx.guild)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        except discord.HTTPException as error:
            await self._discord_said(ctx, error)
            return
        if not made:
            await ctx.fail(problem or CANT_MAKE)
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "counters.quick", detail=", ".join(str(number) for number in made))
        await ctx.ok(f"Made counters {', '.join(str(number) for number in made)} in a new category at the top." + (f" {problem}" if problem else ""))

    async def _counter_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        if not self.may_manage(interaction.user if isinstance(interaction.user, discord.Member) else None, "counters"):
            return []
        needle = current.strip().lower()
        out = []
        for found in await self._counter_rows(interaction.guild_id):
            label = f"{found['number']}. {found['name'] or found['setup']['text']}"[:100]
            if not needle or needle in label.lower():
                out.append(app_commands.Choice(name=label, value=found["number"]))
        return out[:25]

    for _command in (counter_edit, counter_remove, counter_on_command, counter_off_command, counter_update, counter_copy):
        _command.autocomplete("number")(_counter_choices)
    del _command

    async def _find_rule(self, ctx: commands.Context, name: str) -> dict | None:
        if not await self._command_gate(ctx, "statroles"):
            return None
        folded = name.strip().casefold()
        for found in await self._rules(ctx.guild.id):
            if found["name"].casefold() == folded or str(found["number"]) == folded:
                return found
        await ctx.fail(f"There's no stat role called {discord.utils.escape_markdown(name[:100])}. `/statrole list` shows them.")
        return None

    @commands.hybrid_group(name="statrole", aliases=["statroles"], fallback="list", description="Roles given to members whose statistics pass a rule")
    @commands.guild_only()
    @staff_only(STATROLES_WHO)
    async def statrole(self, ctx: commands.Context) -> None:
        from .roles import send_pages
        if not await self._command_gate(ctx, "statroles"):
            return
        names = {found["number"]: found["name"] for found in await self._rules(ctx.guild.id)}
        running = self.running(ctx.guild.id)
        lines = []
        for found in await self._rules(ctx.guild.id):
            role = ctx.guild.get_role(found["role_id"])
            state = "running" if found["number"] in running else "on" if found["enabled"] else ("paused" if found["paused"] else "off")
            conditions = "; ".join(statroles.describe(node, names) for node in found["rule"]["conditions"]["conditions"])
            schedule = statroles.PRESET_LABELS.get(found["rule"]["schedule"]["preset"], "Custom")
            if found["rule"]["schedule"]["preset"] == "custom":
                schedule = f"`{found['rule']['schedule']['cron']}`"
            line = f"**{discord.utils.escape_markdown(found['name'])}** gives {role.mention if role else 'a deleted role'} ({state}, {schedule})\n{conditions}"
            if found["paused"]:
                line += f"\n{found['paused']}"
            lines.append(line)
        await send_pages(ctx, "Stat roles", lines, empty="No stat roles yet. Make one on the dashboard's Stat roles page.")

    @statrole.command(name="run", description="Run a stat role now, giving and taking its role")
    @staff_only(STATROLES_WHO)
    @app_commands.describe(name="The stat role's name")
    async def statrole_run(self, ctx: commands.Context, *, name: str) -> None:
        found = await self._find_rule(ctx, name)
        if found is None:
            return
        blocker = STATISTICS_OFF if not self.on_in(ctx.guild.id) else self._run_blocker(ctx.guild, found["role_id"])
        if blocker:
            await ctx.fail(blocker)
            return
        started = await self.start_run(ctx.guild.id, found["number"], "manual")
        if started in ("held", "applying"):
            await ctx.fail(RESTORING if started == "held" else APPLYING)
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "statroles.run", detail=found["name"])
        if started == "queued":
            await ctx.ok(f"{discord.utils.escape_markdown(found['name'])} is running now, so it runs again when that run ends.")
        else:
            await ctx.ok(f"Running {discord.utils.escape_markdown(found['name'])}. Roles change at about one a second.")

    @statrole.command(name="check", description="Show how a member does on each of a stat role's conditions")
    @staff_only(STATROLES_WHO)
    @app_commands.describe(name="The stat role's name", member="The member to check. Leave it out to check yourself.")
    async def statrole_check(self, ctx: commands.Context, name: str, member: Optional[discord.Member] = None) -> None:
        from .roles import send_pages
        found = await self._find_rule(ctx, name)
        if found is None:
            return
        await ctx.defer(ephemeral=True)
        member = member or ctx.author
        try:
            result = await self.check_member(ctx.guild, found, member.id)
        except ApiError as error:
            await ctx.fail(error.message)
            return
        lines = [f"{member.mention} {'passes' if result['passes'] else 'does not pass'} {discord.utils.escape_markdown(found['name'])}."]
        if result["private"]:
            lines.append("They chose privacy for their statistics, so stat roles leave their roles as they are.")

        def walk(entries: list[dict], depth: int) -> None:
            for entry in entries:
                value = f" ({entry['value']:,})" if entry.get("value") is not None else ""
                lines.append(f"{'  ' * depth}{'yes' if entry['passed'] else 'no'}: {entry['text']}{value}")
                walk(entry["children"], depth + 1)
        walk(result["conditions"], 0)
        await send_pages(ctx, "Stat role check", lines)

    async def _rule_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not self.may_manage(interaction.user if isinstance(interaction.user, discord.Member) else None, "statroles"):
            return []
        needle = current.strip().lower()
        return [app_commands.Choice(name=found["name"][:100], value=found["name"][:100]) for found in await self._rules(interaction.guild_id)
                if not needle or needle in found["name"].lower()][:25]

    statrole_run.autocomplete("name")(_rule_choices)
    statrole_check.autocomplete("name")(_rule_choices)

    # ---- dashboard ----

    def _tools_routes(self, router: Any) -> None:
        # Every route is open as far as the statistics pages are, and _tools_gate decides: full access, or a role picked for the area.
        def counters_read(handler: Any) -> Any:
            return self._gated(("counters",), handler, change=False)

        def counters_change(handler: Any) -> Any:
            return self._gated(("counters",), handler, change=True)

        def statroles_read(handler: Any) -> Any:
            return self._gated(("statroles",), handler, change=False)

        def statroles_change(handler: Any) -> Any:
            return self._gated(("statroles",), handler, change=True)

        router.get("/counters", counters_read(self._counters_list), access="viewer")
        router.get("/counters/choices", self._gated(("counters", "statroles"), self._counters_choices, change=False), access="viewer")
        router.get("/counters/export", counters_read(self._counters_export), access="viewer")
        router.post("/counters", counters_change(self._counters_add), access="viewer")
        router.post("/counters/preview", counters_change(self._counters_preview), access="viewer")
        router.post("/counters/quick", counters_change(self._counters_quick), access="viewer")
        router.post("/counters/import", counters_change(self._counters_import), access="viewer")
        router.put(r"/counters/{number:\d+}", counters_change(self._counters_edit), access="viewer")
        router.delete(r"/counters/{number:\d+}", counters_change(self._counters_remove), access="viewer")
        router.post(r"/counters/{number:\d+}/on", counters_change(self._counters_on), access="viewer")
        router.post(r"/counters/{number:\d+}/off", counters_change(self._counters_off), access="viewer")
        router.post(r"/counters/{number:\d+}/update", counters_change(self._counters_update), access="viewer")
        router.post(r"/counters/{number:\d+}/copy", counters_change(self._counters_copy), access="viewer")
        router.get("/statroles", statroles_read(self._statroles_list), access="viewer")
        router.post("/statroles", statroles_change(self._statroles_add), access="viewer")
        router.post("/statroles/preview", statroles_change(self._statroles_preview), access="viewer")
        router.post("/statroles/check", statroles_change(self._statroles_check), access="viewer")
        router.post("/statroles/test", statroles_change(self._statroles_test), access="viewer")
        router.put(r"/statroles/{number:\d+}", statroles_change(self._statroles_edit), access="viewer")
        router.patch(r"/statroles/{number:\d+}", statroles_change(self._statroles_switch), access="viewer")
        router.delete(r"/statroles/{number:\d+}", statroles_change(self._statroles_remove), access="viewer")
        router.post(r"/statroles/{number:\d+}/run", statroles_change(self._statroles_run), access="viewer")
        router.get(r"/statroles/{number:\d+}/runs", statroles_read(self._statroles_runs), access="viewer")

    def _area_holder(self, guild_id: int, member: Any, area: str) -> bool:
        """Whether a member holds a role the server's owner or Administrators picked for this area in Statistics settings."""
        if member is None:
            return False
        picked = {whole_number(role) for role in (self.cfg(guild_id).get("area_roles") or {}).get(area) or []} - {None}
        return bool(picked & {role.id for role in getattr(member, "roles", [])})

    def may_manage(self, member: Any, area: str) -> bool:
        """Who manages counter channels or stat roles from Discord: the owner and Administrators, someone with Manage Server and the
        permission the bot uses for it, and anyone holding a role picked for that area."""
        if member is None:
            return False
        if self._area_holder(member.guild.id, member, area) or access.is_admin(member):
            return True
        permissions = member.guild_permissions
        return bool(permissions.manage_guild and getattr(permissions, TOOLS_AREAS[area][0]))

    def _tools_gate(self, req: Any, areas: tuple[str, ...], *, change: bool) -> None:
        """The dashboard's check for counters and stat roles. A role picked for the area lets someone in. Otherwise it takes full access,
        and for a change the Discord permission the bot uses for it as well, since the bot shouldn't do for someone what they couldn't do
        themselves unless the server's Administrators picked their role for it."""
        guild = req.bot.get_guild(req.guild_id)
        member = guild.get_member(req.user.id) if guild is not None else None
        if any(self._area_holder(req.guild_id, member, area) for area in areas):
            return
        if req.access == "full":
            if change:
                req.require_permission(TOOLS_AREAS[areas[0]][0])
            return
        permission, permission_name, what = TOOLS_AREAS[areas[0]]
        raise ApiError(403, f"Managing {what} takes Manage Server and {permission_name}, or a role picked for {what} in Statistics settings.")

    def _gated(self, areas: tuple[str, ...], handler: Any, *, change: bool) -> Any:
        """A route behind _tools_gate, whose Discord refusals come back as a plain answer rather than an error."""
        async def run(req: Any) -> Any:
            self._tools_gate(req, areas, change=change)
            try:
                return await handler(req)
            except discord.HTTPException as error:
                status = 409 if isinstance(error, discord.Forbidden) else 404 if isinstance(error, discord.NotFound) else 502
                raise ApiError(status, discord_words(error)) from error
        return run

    def _connected(self, req: Any) -> Any:
        guild = req.bot.get_guild(req.guild_id)
        if guild is None or getattr(guild, "unavailable", False):
            raise ApiError(409, NOT_CONNECTED)
        return guild

    def _counter_json(self, counter: dict, now: float) -> dict:
        tz = counters.schedule_zone(counter["setup"], self._zone(counter["guild_id"]))
        rename_at = counters.rename_allowed_at(counter["renames"], now)
        return {
            "number": counter["number"], "name": counter["name"], "text": counter["setup"]["text"], "counters": counter["setup"]["counters"],
            "frequency": counter["setup"]["frequency"], "enabled": counter["enabled"], "channel_id": counter["channel_id"], "parent_id": counter["parent_id"],
            "shown": counter["shown"], "readings": counter["readings"], "problem": counter["problem"], "checked_at": counter["checked_at"],
            "next_update": counters.next_boundary(counter["setup"]["frequency"], now, tz) if counter["enabled"] else None,
            "rename_at": rename_at if rename_at > now else None, "described": [describe_counter(item) for item in counter["setup"]["counters"]],
        }

    async def _counters_list(self, req: Any) -> dict:
        now = time.time()
        found = await self._counter_rows(req.guild_id)
        presences = bool(self.bot.intents.presences)
        split = not presences and bool(await self._status_shares(req.guild_id, now))
        return {"counters": [self._counter_json(counter, now) for counter in found], "most": MAX_COUNTER_CHANNELS, "on": bool(self.cfg(req.guild_id)["enabled"]),
                "presences": presences, "status_split": split, "frequencies": [{"value": minutes, "label": label} for minutes, label in FREQUENCY_NAMES.items()],
                "date_presets": list(counters.DATE_PRESETS), "connected": req.bot.get_guild(req.guild_id) is not None}

    async def _counters_choices(self, req: Any) -> dict:
        """The apps and emoji the server's statistics know, for the pickers on the counter and stat role pages."""
        apps = [{"id": number, "name": row["name"], "kind": row["kind"], "verified": bool(row["verified"])}
                for number, row in (await query.activity_names(self.message_db, req.guild_id)).items()]
        apps.sort(key=lambda row: row["name"].casefold())
        emoji = [{"key": key, "name": row["name"], "deleted": row["deleted_at"] is not None}
                 for key, row in (await query.emoji_info(self.message_db, req.guild_id)).items()]
        emoji.sort(key=lambda row: row["name"].casefold())
        return {"activities": apps[:2000], "emoji": emoji[:2000]}

    def _setup_form(self, req: Any, form: Any) -> dict:
        if not isinstance(form, dict):
            raise ValidationError({"setup": "Expected a counter channel's setup."})
        try:
            setup = counters.clean_setup(form)
        except Invalid as error:
            raise ValidationError({"setup": str(error)}) from error
        req.refuse_other_servers({"setup": setup})
        for channel_id in channel_ids(setup):
            if not req.can_see(channel_id):
                raise ValidationError({"setup": "You can't see one of the channels it counts."})
        guild = req.bot.get_guild(req.guild_id)
        if guild is not None:
            for role_id in role_ids(setup):
                if guild.get_role(role_id) is None:
                    raise ValidationError({"setup": "A counter names a role that isn't in this server."})
        return setup

    def _place_form(self, req: Any, guild: Any, form: dict) -> tuple[int | None, int | None]:
        """The channel to take over, or the category to make the channel in, from a form."""
        channel_id = parent_id = None
        if form.get("channel_id"):
            channel_id = req.channel(form["channel_id"], "channel_id")
            channel = guild.get_channel(channel_id)
            if channel is None or getattr(channel, "type", None) not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                raise ValidationError({"channel_id": "Pick a voice channel to take over."})
        elif form.get("parent_id"):
            parent_id = req.channel(form["parent_id"], "parent_id")
            category = guild.get_channel(parent_id)
            if category is None or getattr(category, "type", None) != discord.ChannelType.category:
                raise ValidationError({"parent_id": "Pick a category."})
        return channel_id, parent_id

    async def _counter_param(self, req: Any) -> dict:
        found = await self._counter(req.guild_id, req.int_param("number"))
        if found is None:
            raise ApiError(404, "That counter channel isn't there anymore.")
        return found

    async def _counters_add(self, req: Any) -> dict:
        form = await req.json()
        setup = self._setup_form(req, form)
        enabled = bool(form.get("enabled", True))
        guild = req.bot.get_guild(req.guild_id)
        channel_id = parent_id = None
        if enabled:
            guild = self._connected(req)
            channel_id, parent_id = self._place_form(req, guild, form)
        try:
            number, problem = await self.add_counter(req.guild_id, setup, guild=guild, enabled=enabled, channel_id=channel_id, parent_id=parent_id)
        except Invalid as error:
            raise ValidationError({"setup": str(error)}) from error
        await req.record("counters.add", detail=setup["text"])
        found = await self._counter(req.guild_id, number)
        return {"counter": self._counter_json(found, time.time()), "problem": problem}

    async def _counters_edit(self, req: Any) -> dict:
        found = await self._counter_param(req)
        setup = self._setup_form(req, await req.json())
        result = await self.edit_counter(req.bot.get_guild(req.guild_id), found, setup)
        await req.record("counters.edit", detail=setup["text"])
        return {"counter": self._counter_json(await self._counter(req.guild_id, found["number"]), time.time()), "result": result}

    async def _counters_remove(self, req: Any) -> dict:
        found = await self._counter_param(req)
        problem = await self.remove_counter(req.bot.get_guild(req.guild_id), found, keep_channel=req.query.get("keep_channel") in ("1", "true"))
        await req.record("counters.remove", detail=found["setup"]["text"])
        return {"ok": True, "problem": problem}

    async def _counters_on(self, req: Any) -> dict:
        found = await self._counter_param(req)
        guild = self._connected(req)
        if found["enabled"]:
            raise ApiError(409, "That counter is already on.")
        form = await req.json()
        if form.get("parent_id") and guild.get_channel(whole_number(form["parent_id"]) or 0) is None:
            form = {**form, "parent_id": None}  # the page sends the category the counter was in, which may be gone: then it goes in none
        channel_id, parent_id = self._place_form(req, guild, form)
        on, problem = await self.counter_on(guild, found, channel_id=channel_id, parent_id=parent_id)
        if not on:
            raise ApiError(409, problem or CANT_MAKE)
        await req.record("counters.on", detail=found["setup"]["text"])
        return {"counter": self._counter_json(await self._counter(req.guild_id, found["number"]), time.time()), "problem": problem}

    async def _counters_off(self, req: Any) -> dict:
        found = await self._counter_param(req)
        form = await req.json()
        problem = await self.counter_off(req.bot.get_guild(req.guild_id), found, keep_channel=bool(form.get("keep_channel")))
        await req.record("counters.off", detail=found["setup"]["text"])
        return {"counter": self._counter_json(await self._counter(req.guild_id, found["number"]), time.time()), "problem": problem}

    async def _counters_update(self, req: Any) -> dict:
        found = await self._counter_param(req)
        guild = self._connected(req)
        if not found["enabled"]:
            raise ApiError(409, "That counter is off. Switch it on first.")
        if self._tools_applying(req.guild_id):
            raise ApiError(409, APPLYING)
        place = await self.counter_place(guild, time.time(), [found["setup"]])
        result = await self.refresh_counter(guild, found, place)
        return {"counter": self._counter_json(await self._counter(req.guild_id, found["number"]) or found, time.time()), "result": result}

    async def _counters_copy(self, req: Any) -> dict:
        found = await self._counter_param(req)
        try:
            number = await self.copy_counter(req.guild_id, found)
        except Invalid as error:
            raise ApiError(409, str(error)) from error
        await req.record("counters.copy", detail=found["setup"]["text"])
        return {"counter": self._counter_json(await self._counter(req.guild_id, number), time.time())}

    async def _counters_preview(self, req: Any) -> dict:
        """The name a setup makes now. It reads what its channel would show: the channel it takes over, the category it goes in, or the
        channel it's on, as long as the person can see that place, and the server's public channels otherwise."""
        form = await req.json()
        setup = self._setup_form(req, form)
        guild = req.bot.get_guild(req.guild_id)
        channel_id, parent_id = whole_number(form.get("channel_id")), whole_number(form.get("parent_id"))
        number = whole_number(form.get("number"))
        if number and not channel_id and not parent_id:
            found = await self._counter(req.guild_id, number)
            if found is not None:
                channel_id, parent_id = (found["channel_id"] if found["enabled"] else None), found["parent_id"]
        channel_id = channel_id if channel_id and guild is not None and guild.get_channel(channel_id) is not None and req.can_see(channel_id) else None
        parent_id = parent_id if parent_id and req.can_see(parent_id) else None
        place = await self.counter_place(guild, time.time(), [setup])
        readings = await self.counter_readings(setup, self.public_place(place, self._shown_to_everyone(guild, channel_id=channel_id, parent_id=parent_id)))
        return {"name": counters.render(setup, readings), "readings": [reading.as_json() for reading in readings]}

    async def _counters_quick(self, req: Any) -> dict:
        guild = self._connected(req)
        try:
            made, problem = await self.quick_counters(guild)
        except Invalid as error:
            raise ApiError(409, str(error)) from error
        if not made:
            raise ApiError(409, problem or CANT_MAKE)
        await req.record("counters.quick", detail=", ".join(str(number) for number in made))
        return {"made": made, "problem": problem}

    async def _counters_export(self, req: Any) -> list[dict]:
        """Every counter channel's setup, as the list the import reads back: its name, text, counters and schedule, whether it's on, and the
        channel it's on."""
        return [{"name": counter["name"], "text": counter["setup"]["text"], "counters": counter["setup"]["counters"], "frequency": counter["setup"]["frequency"],
                 "on": counter["enabled"], "channel_id": counter["channel_id"] if counter["enabled"] else None}
                for counter in await self._counter_rows(req.guild_id)]

    async def _counters_import(self, req: Any) -> dict:
        """Counter setups as the export writes them: a list of {"name", "text", "counters", "frequency", "on", "channel_id"}. Each goes
        through the same checks as the counter form. One that's on takes over its channel in place, keeping its spot and permissions, when
        that's a voice channel here the person can see and no counter uses. One whose channel is gone or isn't a voice channel comes in
        switched off, and one naming a channel the person can't see is left out. With `apply` off nothing changes."""
        guild = self._connected(req)
        form = await req.json()
        data = form.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError as error:
                raise ValidationError({"data": "That isn't valid JSON."}) from error
        if not isinstance(data, list) or not data or not all(isinstance(item, dict) for item in data):
            raise ValidationError({"data": "That holds no counter channels. An export from this page is a list of them."})
        if len(data) > MAX_COUNTER_CHANNELS:
            raise ValidationError({"data": f"That holds more than {MAX_COUNTER_CHANNELS} counter channels, the most a server can have."})
        apply = bool(form.get("apply"))
        rows = await self._counter_rows(req.guild_id)
        taken = {row["channel_id"] for row in rows if row["channel_id"]}
        room = MAX_COUNTER_CHANNELS - await self.db.scalar("SELECT COUNT(*) FROM stat_counters WHERE guild_id = ?", (req.guild_id,))
        report = []
        for item in data:
            entry: dict[str, Any] = {"name": str(item.get("name") or "")[:counters.LABEL_LIMIT], "text": str(item.get("text") or "")[:300], "channel_id": None,
                                     "action": "skip", "problems": []}
            report.append(entry)
            try:
                setup = self._setup_form(req, item)
            except ValidationError as error:
                entry["problems"].append(next(iter(error.fields.values()), "It can't come in as it is."))
                continue
            channel_id = whole_number(item.get("channel_id")) if item.get("on", True) else None
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is not None and not req.can_see(channel_id):
                entry["problems"].append("You can't see its channel, so it was left out.")
                continue
            if channel is not None:
                entry["channel_id"] = channel_id
            if channel_id in taken:
                entry["action"] = "already"
                continue
            adopt = channel is not None and getattr(channel, "type", None) in (discord.ChannelType.voice, discord.ChannelType.stage_voice)
            entry["action"] = "adopt" if adopt else "off"
            if channel_id and not adopt:
                entry["problems"].append("Its channel isn't a voice channel in this server anymore, so it comes in switched off.")
            if not apply:
                continue
            if room <= 0:
                entry["action"] = "skip"
                entry["problems"].append(f"The server has {MAX_COUNTER_CHANNELS} counter channels, the most it can have.")
                continue
            try:
                number, problem = await self.add_counter(req.guild_id, setup, guild=guild, enabled=adopt, channel_id=channel_id if adopt else None)
            except Invalid as error:
                entry["action"] = "skip"
                entry["problems"].append(str(error))
                continue
            room -= 1
            entry["number"] = number
            if adopt:
                taken.add(channel_id)
            if problem:
                entry["problems"].append(problem)
        if apply:
            await req.record("counters.import", detail=f"{sum(1 for entry in report if entry.get('number'))} counters")
        return {"setups": report}

    async def _statroles_list(self, req: Any) -> dict:
        found = await self._rules(req.guild_id)
        names = {rule["number"]: rule["name"] for rule in found}
        latest: dict[int, dict] = {}
        for row in await self.db.all("SELECT * FROM stat_role_runs WHERE guild_id = ? ORDER BY started_at DESC", (req.guild_id,)):
            latest.setdefault(row["rule"], row)
        running = self.running(req.guild_id)
        out = []
        for rule in found:
            out.append({
                "number": rule["number"], "name": rule["name"], "role_id": rule["role_id"], "enabled": rule["enabled"], "paused": rule["paused"],
                "next_run": rule["next_run"], **rule["rule"], "last_run": latest.get(rule["number"]), "running": running.get(rule["number"]),
                "described": [statroles.describe(node, names) for node in rule["rule"]["conditions"]["conditions"]],
            })
        settings = self.cfg(req.guild_id)
        return {"rules": out, "most": MAX_STAT_ROLES, "on": bool(settings["enabled"]), "above_own": settings["statrole_above_own"], "is_admin": req.is_admin,
                "presets": [{"value": key, "label": label} for key, label in statroles.PRESET_LABELS.items()], "timezone": settings["timezone"],
                "presences": bool(self.bot.intents.presences), "track_activities": bool(settings["track_activities"])}

    async def _rule_from_req(self, req: Any, form: dict, number: int | None) -> dict:
        guild = self._connected(req)
        member = guild.get_member(req.user.id)
        req.refuse_other_servers(form)
        cleaned = await self.rule_form(guild, member if not req.is_admin else None, form, number)
        for channel_id in channel_ids(cleaned["rule"]["conditions"]):
            if not req.can_see(channel_id):
                raise ValidationError({"rule": "You can't see one of the channels it counts."})
        before = (await self._rule(req.guild_id, number) or {}).get("rule", {}) if number is not None else {}
        for key in ("give", "remove"):
            note = cleaned["rule"].get(key)
            if note and note.get("channel_id"):
                kept = ((before.get(key) or {}).get("channel_id"))
                await req.ops.find_thread(req.guild_id, note["channel_id"])  # an archived thread isn't in the cache until it's looked up
                note["channel_id"] = delivery.form_channel(req, note["channel_id"], key, keeping=kept)
        return cleaned

    async def _statroles_add(self, req: Any) -> dict:
        cleaned = await self._rule_from_req(req, await req.json(), None)
        number = await self.save_rule(req.guild_id, cleaned, None)
        await req.record("statroles.add", detail=cleaned["name"])
        return {"number": number}

    async def _statroles_edit(self, req: Any) -> dict:
        number = req.int_param("number")
        if await self._rule(req.guild_id, number) is None:
            raise ApiError(404, "That stat role isn't there anymore.")
        cleaned = await self._rule_from_req(req, await req.json(), number)
        await self.save_rule(req.guild_id, cleaned, number)
        await req.record("statroles.edit", detail=cleaned["name"])
        return {"number": number}

    async def _statroles_switch(self, req: Any) -> dict:
        number = req.int_param("number")
        found = await self._rule(req.guild_id, number)
        if found is None:
            raise ApiError(404, "That stat role isn't there anymore.")
        form = await req.json()
        enabled = form.get("enabled")
        if not isinstance(enabled, bool):
            raise ValidationError({"enabled": "Must be on or off."})
        if enabled:
            guild = self._connected(req)
            editor = None if req.is_admin else guild.get_member(req.user.id)
            problem = self.statrole_role_problem(guild, guild.get_role(found["role_id"]), editor)
            if problem:
                raise ValidationError({"enabled": problem})
        next_time = statroles.next_run(found["rule"]["schedule"], time.time(), self._zone(req.guild_id)) if enabled else None
        await self.db.run("UPDATE stat_roles SET enabled = ?, paused = NULL, next_run = ?, updated_at = ? WHERE guild_id = ? AND number = ?",
                          (int(enabled), next_time, time.time(), req.guild_id, number))
        await self._remember_held_roles(req.guild_id)
        await req.record("statroles.enable" if enabled else "statroles.disable", detail=found["name"])
        return {"ok": True}

    async def _statroles_remove(self, req: Any) -> dict:
        number = req.int_param("number")
        found = await self._rule(req.guild_id, number)
        if found is None:
            raise ApiError(404, "That stat role isn't there anymore.")
        followers = [rule["name"] for rule in await self._rules(req.guild_id) if number in statroles.followed_rules(rule["rule"])]
        if followers:
            raise ApiError(409, f"{', '.join(followers[:5])} follow{'s' if len(followers) == 1 else ''} this stat role. Change {'it' if len(followers) == 1 else 'them'} first.")
        state = self._server_runs.get(req.guild_id)
        if state is not None and number in state.runs:
            state.runs[number].stopped = "The stat role was deleted."
            state.runs[number].give.clear()
            state.runs[number].take.clear()
        await self.db.run("DELETE FROM stat_roles WHERE guild_id = ? AND number = ?", (req.guild_id, number))
        await self.db.run("DELETE FROM stat_role_runs WHERE guild_id = ? AND rule = ?", (req.guild_id, number))
        await self._remember_held_roles(req.guild_id)
        await req.record("statroles.remove", detail=found["name"])
        return {"ok": True}

    async def _statroles_run(self, req: Any) -> dict:
        number = req.int_param("number")
        found = await self._rule(req.guild_id, number)
        if found is None:
            raise ApiError(404, "That stat role isn't there anymore.")
        guild = self._connected(req)
        blocker = STATISTICS_OFF if not self.on_in(req.guild_id) else self._run_blocker(guild, found["role_id"])
        if blocker:
            raise ApiError(409, blocker)
        started = await self.start_run(req.guild_id, number, "manual")
        if started in ("held", "applying"):
            raise ApiError(409, RESTORING if started == "held" else APPLYING)
        await req.record("statroles.run", detail=found["name"])
        return {"started": started}

    async def _statroles_runs(self, req: Any) -> dict:
        number = req.int_param("number")
        rows = await self.db.all("SELECT * FROM stat_role_runs WHERE guild_id = ? AND rule = ? ORDER BY started_at DESC LIMIT ?", (req.guild_id, number, RUNS_KEPT))
        return {"runs": rows, "running": self.running(req.guild_id).get(number)}

    async def _statroles_preview(self, req: Any) -> dict:
        form = await req.json()
        number = whole_number(form.get("number"))
        cleaned = await self._rule_from_req(req, form, number)
        return await self.preview(self._connected(req), cleaned, everyone=bool(form.get("everyone")))

    async def _statroles_check(self, req: Any) -> dict:
        form = await req.json()
        number = whole_number(form.get("number"))
        user_id = whole_number(form.get("member_id"))
        if not user_id:
            raise ValidationError({"member_id": "Pick a member to check."})
        cleaned = await self._rule_from_req(req, form, number)
        return await self.check_member(self._connected(req), cleaned, user_id)

    async def _statroles_test(self, req: Any) -> dict:
        """Sends one notification as a run would, with the person testing as the member who changed."""
        guild = self._connected(req)
        form = await req.json()
        which = form.get("which", "give")
        if which not in ("give", "remove"):
            raise ValidationError({"which": "Test the message for gaining or for losing the role."})
        try:
            note = statroles.clean_notification(form.get("notification"), statroles.DEFAULT_GIVE if which == "give" else statroles.DEFAULT_REMOVE)
        except Invalid as error:
            raise ValidationError({which: str(error)}) from error
        if note is None:
            raise ValidationError({which: "Switch the notification on first."})
        if note.get("channel_id"):
            await req.ops.find_thread(req.guild_id, note["channel_id"])
            note["channel_id"] = delivery.form_channel(req, note["channel_id"], which)
        member = guild.get_member(req.user.id)
        if member is None:
            raise ApiError(409, "You need to be in the server to test a notification.")
        role = guild.get_role(whole_number(form.get("role_id")) or 0)
        problem = await self.send_notification(guild, note, role, which == "give", member=member)
        if problem:
            raise ApiError(409, problem)
        return {"ok": True}


def quick_setups() -> list[dict]:
    """What /counter quick makes: members, members not offline, and the date in the server's zone."""
    return [
        {"name": "Members", "text": "Members: {1}", "counters": [{"type": "members", "include": "both"}], "frequency": 10},
        {"name": "Online", "text": "Online: {1}", "counters": [{"type": "statuses", "statuses": ["online", "idle", "dnd"]}], "frequency": 10},
        {"name": "Date", "text": "\N{CALENDAR} {1}", "counters": [{"type": "datetime", "format": "dddd, MMM Do", "zone": "server"}], "frequency": 10},
    ]
