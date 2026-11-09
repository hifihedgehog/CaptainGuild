"""Statistics: recounts from Discord's history, merged channels, the statistics API and personal data, and the extras that go further:
moderation over time, help forums, newcomers who stay, and scheduled reports. A mixin the Statistics pack module inherits, so its commands
belong to the pack.

Recounts and merges are background work, one of each at a time per server, saved as they go so a restart carries on where they were. The
statistics API answers on its own paths with a key per server instead of a dashboard session. The privacy switch moves someone's counts to
no one in every server, and the My data page and the privacy command both use it.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

from ..core import access
from ..core.stats import beyond, devapi, history, merge, query, store, views
from ..core.stats.timeframe import SLOT_SECONDS, day_label, floor_bucket, period_for, slot_of, slot_time, zone, zone_label
from ..core.validate import Invalid, ValidationError, snowflake, text, whole_number
from ..crypto import token_hash
from ..web.api import ApiError

log = logging.getLogger(__name__)

DATA_FIELDS: dict[str, Any] = {}
DATA_MIGRATIONS: list[Any] = [
    """
    CREATE TABLE stat_api_keys (
        guild_id INTEGER PRIMARY KEY,
        key_hash TEXT NOT NULL,
        hint TEXT NOT NULL,
        created_by INTEGER,
        created_by_name TEXT,
        created_at REAL NOT NULL,
        used_at REAL
    );
    CREATE UNIQUE INDEX stat_api_keys_hash ON stat_api_keys (key_hash);
    CREATE TABLE stat_reports (
        guild_id INTEGER NOT NULL,
        id TEXT NOT NULL,
        channel_id INTEGER NOT NULL,
        cadence TEXT NOT NULL,
        weekday INTEGER NOT NULL DEFAULT 0,
        hour INTEGER NOT NULL DEFAULT 9,
        next_at REAL,
        last_at REAL,
        last_error TEXT,
        created_by INTEGER,
        created_by_name TEXT,
        created_at REAL NOT NULL,
        PRIMARY KEY (guild_id, id)
    );
    """,
]
DATA_MESSAGE_MIGRATIONS: list[Any] = [
    history.MIGRATIONS + beyond.FORUM_MIGRATION + """
    CREATE TABLE stat_merges (
        guild_id INTEGER PRIMARY KEY,
        target_id INTEGER NOT NULL,
        kinds TEXT NOT NULL,
        forget INTEGER NOT NULL,
        state TEXT NOT NULL,
        until_slot INTEGER NOT NULL,
        total INTEGER NOT NULL DEFAULT 0,
        done INTEGER NOT NULL DEFAULT 0,
        rows_moved INTEGER NOT NULL DEFAULT 0,
        forgotten INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        started_by INTEGER,
        started_by_name TEXT,
        created_at REAL NOT NULL,
        finished_at REAL
    );
    CREATE TABLE stat_merge_sources (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        state TEXT NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    );
    CREATE INDEX IF NOT EXISTS stat_emoji_channel ON stat_emoji (guild_id, channel_id, slot);
    """,
    """
    ALTER TABLE stat_recounts ADD COLUMN left_out TEXT;
    ALTER TABLE stat_recount_places ADD COLUMN private INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE stat_merged_channels (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        until_slot INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    );
    CREATE TABLE stat_auto_merges (
        guild_id INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        from_slot INTEGER NOT NULL,
        to_slot INTEGER,
        PRIMARY KEY (guild_id, category_id, from_slot)
    );
    """,
]

API_BASE = "/api/stats"
REPORT_TIMER = "statistics.report"
PRIVACY_WAIT = 86400  # the privacy choice changes at most once a day
CHANNELS_WARNING = 1000  # past this many kept channels, lists and pickers slow down
READERS = 4  # channels a recount reads at once, within Discord's limits
READ_BATCH = 500  # messages read between saves
READ_TRIES = 3
RETRY_SECONDS = 5  # the wait before reading again after Discord didn't answer, longer each time
MERGED_MOST = 50
REPORTS_MOST = 10
NAME_MOST = 100
THREADS_SHOWN = 25  # the biggest threads listed under each channel on the approval list
FORUM_SEEN_MOST = 100_000
API_AT_ONCE = 2  # API requests one server's key runs at once
HIDDEN = "Hidden channel"
READABLE_TYPES = (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum, discord.ChannelType.media, discord.ChannelType.voice,
                  discord.ChannelType.stage_voice)
THREAD_PARENTS = (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum, discord.ChannelType.media)
POST_PARENTS = (discord.ChannelType.forum, discord.ChannelType.media)
VOICE_TYPES = (discord.ChannelType.voice, discord.ChannelType.stage_voice)
THREAD_TYPES = (discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread)
LIST_PLACE = "list"  # a place that stands for a channel's thread list, kept until Discord gives the whole list
NOT_ADMIN_NOTE = "Only the server owner or an Administrator can see this."
UNEXPECTED = "Something went wrong. It's been logged. Resume to try again, or cancel."
DAYS_BY_DAY = 1_000  # the longest insights period shown day by day
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _period(form: dict, now: float, created: float) -> tuple[float, float]:
    """A recount's period from a form's start and end, in Unix seconds, cut to what Discord can hold: from the server's creation to now."""
    def moment(name: str, otherwise: float) -> float:
        value = form.get(name)
        if value in (None, ""):
            return otherwise
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise ValidationError({name: "Pick a date and time."})
        return float(value)
    start = max(moment("start", created), created, history.DISCORD_EPOCH_MS / 1000)
    end = min(moment("end", now), now)
    if end - start < SLOT_SECONDS:
        raise ValidationError({"end": "The end has to come at least 15 minutes after the start, and neither can be in the future."})
    return start, end


def _refusal(error: discord.HTTPException) -> str:
    """What Discord said, short enough for a line on the page."""
    return f"{error.status} {error.text}".strip() if error.text else str(error.status)


def _in_forum(thread: Any) -> bool:
    parent = getattr(thread, "parent", None)
    return getattr(thread, "type", None) in THREAD_TYPES and parent is not None and getattr(parent, "type", None) in POST_PARENTS


def _created(thing: Any) -> float:
    made = getattr(thing, "created_at", None)
    return made.timestamp() if made is not None else discord.utils.snowflake_time(thing.id).timestamp()


def _visible_to_viewers(guild: Any, where: Any, shown: Any) -> bool:
    """Whether everyone who can see `where` can see `shown` too, so a post in `where` may name it. Members with the same roles see the same
    channels, so each set of roles is checked once. The owner, who sees everything whatever their roles, and someone a channel names by
    themselves are checked alone."""
    if shown is None:
        return True  # a channel that's gone has nothing left to hide
    singled = {target.id for channel in (where, shown) for target in getattr(channel, "overwrites", {}) if isinstance(target, (discord.Member, discord.User, discord.Object))
               and not isinstance(target, discord.Role)}
    checked: dict[frozenset, bool] = {}
    for member in guild.members:
        key = frozenset({-member.id}) if member.id in singled or member.id == guild.owner_id else frozenset(role.id for role in member.roles)
        if key in checked:
            if not checked[key]:
                return False
            continue
        sees_here = where.permissions_for(member).view_channel
        checked[key] = not sees_here or shown.permissions_for(member).view_channel
        if not checked[key]:
            return False
    return True


async def _forget_in_data_tables(db: Any, user_ids: list[int], guild_id: int | None) -> None:
    """Takes names off the tables only this part keeps, in one server or in every one: what a recount has read but not applied yet, and
    help-forum posts. store.anonymize and store.anonymize_restored run it, so it happens whichever way privacy came on or came back."""
    servers: set[int] = {guild_id} if guild_id is not None else set()
    if guild_id is None:
        for table in ("stat_recount_places", "stat_recount_messages", "stat_recount_emoji", "stat_recount_people"):
            servers.update(await store.servers_in(db, table))
    for server in sorted(servers):
        await history.hide_private(db, server, user_ids)
    marks = ", ".join("?" for _ in user_ids)
    where, args = ("guild_id = ? AND ", (guild_id,)) if guild_id is not None else ("", ())
    await db.run(f"UPDATE stat_forum_posts SET owner_id = 0 WHERE {where}owner_id IN ({marks})", (*args, *user_ids))
    await db.run(f"UPDATE stat_forum_posts SET replier_id = 0 WHERE {where}replier_id IN ({marks})", (*args, *user_ids))


if _forget_in_data_tables not in store.ANONYMIZE_STEPS:
    store.ANONYMIZE_STEPS.append(_forget_in_data_tables)


@dataclass
class DataState:
    """What this part of Statistics keeps in memory."""
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    reading: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))  # one recount reads Discord's history at a time, whichever server it's for
    limiter: devapi.Limiter = field(default_factory=devapi.Limiter)
    gates: dict[int, asyncio.Semaphore] = field(default_factory=dict)
    used: dict[int, float] = field(default_factory=dict)
    forum_seen: set[tuple[int, int]] = field(default_factory=set)
    starting: dict[int, asyncio.Lock] = field(default_factory=dict)  # one start of a recount or a merge at a time per server
    key_hashes: set[str] | None = None  # the hashes of the API keys that exist, read once, so a wrong key costs no database read
    applying: dict[int, int] = field(default_factory=dict)  # servers where a recount's swap or a merge is changing counts now


class StatsData:
    @property
    def _data(self) -> DataState:
        """Made on first use, so a part of the module that failed to load earlier can't leave these missing."""
        state = self.__dict__.get("_data_state")
        if state is None:
            state = self.__dict__["_data_state"] = DataState()
        return state

    # ---- start and stop ----

    async def _data_load(self) -> None:
        self.bot.timers.handler(REPORT_TIMER, self._report_due)
        self.bot.settings.listen(self.key, self._data_settings_saved)
        for guild_id in self.bot.settings.guilds_with(self.key):
            await self._note_auto_merges(guild_id, self.cfg(guild_id), first=True)
        await self._arm_reports()

    async def _data_settings_saved(self, guild_id: int, settings: dict) -> None:
        await self._note_auto_merges(guild_id, settings)

    async def _note_auto_merges(self, guild_id: int, settings: dict, *, first: bool = False) -> None:
        """Keeps when each category's messages went to a merged channel, so a recount leaves those channels out of a period they were merged in.
        A category already merging when this was first kept is taken to have merged from the start."""
        now = slot_of(time.time())
        current = {(category, row["id"]) for row in settings.get("aggregates") or [] if row.get("kinds") in ("messages", "both") for category in row.get("auto_categories") or []}
        kept = {(row["category_id"], row["target_id"]) for row in await self.message_db.all("SELECT category_id, target_id FROM stat_auto_merges WHERE guild_id = ? AND to_slot IS NULL",
                                                                                              (guild_id,))}
        if current == kept:
            return
        async with self.message_db.transaction():
            for category, target in kept - current:
                await self.message_db.run("UPDATE stat_auto_merges SET to_slot = ? WHERE guild_id = ? AND category_id = ? AND target_id = ? AND to_slot IS NULL",
                                          (now + 1, guild_id, category, target))
            for category, target in current - kept:
                await self.message_db.run("INSERT OR IGNORE INTO stat_auto_merges (guild_id, category_id, target_id, from_slot, to_slot) VALUES (?, ?, ?, ?, NULL)",
                                          (guild_id, category, target, 0 if first else now))

    async def _data_unload(self) -> None:
        for task in list(self._data.tasks.values()):
            task.cancel()

    def _data_routes(self, router: Any) -> None:
        router.get("/statistics/data", self._data_page)
        router.get("/statistics/data/recount", self._recount_page)
        router.post("/statistics/data/recount", self._recount_start)
        router.post("/statistics/data/recount/resume", self._recount_resume)
        router.post("/statistics/data/recount/skip", self._recount_skip)
        router.post("/statistics/data/recount/approve", self._recount_approve)
        router.post("/statistics/data/recount/cancel", self._recount_cancel)
        router.get("/statistics/data/merged", self._merged_page)
        router.post("/statistics/data/merged", self._merged_create)
        router.patch(r"/statistics/data/merged/{aggregate_id:\d+}", self._merged_edit)
        router.delete(r"/statistics/data/merged/{aggregate_id:\d+}", self._merged_delete)
        router.post("/statistics/data/merged/preview", self._merge_preview)
        router.post("/statistics/data/merged/merge", self._merge_start)
        router.get("/statistics/data/api", self._api_page)
        router.post("/statistics/data/api/key", self._api_key_create)
        router.delete("/statistics/data/api/key", self._api_key_delete)
        router.get("/statistics/data/reports", self._reports_page)
        router.post("/statistics/data/reports", self._report_create)
        router.delete("/statistics/data/reports/{report_id}", self._report_delete)
        router.post("/statistics/data/reports/{report_id}/send", self._report_send_now)
        router.get("/statistics/insights/moderation", self._insights_moderation, access="staff")
        router.get("/statistics/insights/forums", self._insights_forums, access="staff")
        router.get("/statistics/insights/newcomers", self._insights_newcomers, access="staff")

    def site_routes(self, app: web.Application) -> None:
        """Paths of Statistics' own outside /api/g, from each part of the module that has some, named _<part>_site_routes."""
        for part in ("commands", "web", "tools", "data"):
            hook = getattr(self, f"_{part}_site_routes", None)
            if hook is not None:
                hook(app)

    def _data_site_routes(self, app: web.Application) -> None:
        """The statistics API's own paths, which take a server's key instead of a dashboard session."""
        app.router.add_get(f"{API_BASE}/v1/activities", self._api_answer(None))
        for path, handler in devapi.GUILD_ROUTES.items():
            app.router.add_get(f"{API_BASE}/v1/guilds/{{guild_id}}{path}", self._api_answer(handler))

    @commands.Cog.listener("on_ready")
    async def _data_ready(self) -> None:
        self._spawn(self._resume_work())

    async def _resume_work(self) -> None:
        """Recounts and merges that were reading or applying when the bot stopped carry on."""
        await self.bot.members_settled.wait()
        for row in await self.message_db.all("SELECT guild_id FROM stat_recounts WHERE state IN (?, ?, ?)", history.RUNNING):
            self._start_recount(row["guild_id"])
        for row in await self.message_db.all("SELECT guild_id FROM stat_merges WHERE state = 'running'"):
            self._start_merge(row["guild_id"])

    async def on_guild_restored(self, guild_id: int) -> None:
        """Work a backup caught running waits for someone to look at it, and report schedules get their timers back."""
        await super().on_guild_restored(guild_id)
        for name in (f"recount:{guild_id}", f"merge:{guild_id}"):
            task = self._data.tasks.pop(name, None)
            if task is not None:
                task.cancel()
        await self.message_db.run("UPDATE stat_recounts SET state = ?, note = ? WHERE guild_id = ? AND state IN (?, ?, ?)",
                                  (history.REVIEW, "A backup was put back while this recount was running. Resume it to carry on, or cancel it.", guild_id, *history.RUNNING))
        await self.message_db.run("UPDATE stat_merges SET state = 'stopped', note = ? WHERE guild_id = ? AND state = 'running'",
                                  ("A backup was put back while this merge was running. Start it again to finish it.", guild_id))
        await self._arm_reports()

    async def on_messages_restored(self, guild_id: int) -> None:
        """A backup's statistics are back, some from before people chose privacy. Everyone who chose it has their name taken off again."""
        await super().on_messages_restored(guild_id)
        private = [row["user_id"] for row in await self.db.all("SELECT user_id FROM stat_user_settings WHERE private = 1")]
        if private:
            await store.anonymize_restored(self.message_db, guild_id, private)

    def _task(self, name: str, work: Any) -> None:
        tasks = self._data.tasks
        running = tasks.get(name)
        if running is not None and not running.done():
            work.close()
            return
        task = asyncio.ensure_future(work)
        tasks[name] = task

        def done(finished: asyncio.Task) -> None:
            if tasks.get(name) is finished:
                del tasks[name]
            if not finished.cancelled() and finished.exception() is not None:
                log.error("Statistics %s failed", name, exc_info=finished.exception())
        task.add_done_callback(done)

    def stats_applying(self, guild_id: int) -> bool:
        """True while a recount is swapping counts or a merge is moving them in this server. Stat roles and counters wait it out, since their
        numbers would be read halfway through."""
        return self._data.applying.get(guild_id, 0) > 0

    def _applying(self, guild_id: int, step: int) -> None:
        left = self._data.applying.get(guild_id, 0) + step
        if left > 0:
            self._data.applying[guild_id] = left
        else:
            self._data.applying.pop(guild_id, None)

    async def finish_data_work(self) -> None:
        """Waits for every recount and merge under way. The tests use this."""
        while self._data.tasks:
            await asyncio.gather(*list(self._data.tasks.values()), return_exceptions=True)

    # ---- the privacy choice ----

    async def privacy_state(self, user_id: int, now: float | None = None) -> dict:
        """Someone's statistics choices: whether their counts carry their name, when that last changed, whether moving what was counted before is
        still to finish, and whether replies come only to them. A move still to finish can be asked for again at any time, so can_change is on."""
        now = time.time() if now is None else now
        row = await self.db.one("SELECT * FROM stat_user_settings WHERE user_id = ?", (user_id,)) or {}
        changed = row.get("private_changed_at")
        private = bool(row.get("private"))
        pending = private and (row.get("anonymized_at") or 0) < (changed or 0)
        return {"private": private, "pending": pending, "changed_at": changed, "can_change_at": (changed + PRIVACY_WAIT) if changed else None,
                "can_change": pending or not changed or now >= changed + PRIVACY_WAIT, "private_replies": bool(row.get("ephemeral"))}

    async def privacy_guilds(self) -> set[int]:
        """Every server that could hold someone's counts: those the bot is in, and every server that ever kept statistics."""
        found = {guild.id for guild in self.bot.guilds}
        found.update(self.bot.settings.guilds_with(self.key))
        for table in ("stat_channels", "stat_members", "stat_messages", "stat_voice", "stat_activities", "stat_emoji", "stat_joins", "stat_invites", "stat_people",
                      "stat_forum_posts"):
            found.update(await store.servers_in(self.message_db, table))
        return found

    # ---- help forums ----

    @commands.Cog.listener("on_thread_create")
    async def _forum_post_made(self, thread: Any) -> None:
        guild = getattr(thread, "guild", None)
        if guild is None or not self.tracking(guild.id) or not _in_forum(thread) or self._place(guild, thread, "messages") is None:
            return  # a forum kept out of statistics records nothing about who posts or answers there
        owner = getattr(thread, "owner_id", None)
        solved = self._solved_tag(guild.id, thread.parent_id)
        solved_at = time.time() if solved is not None and solved in [tag.id for tag in getattr(thread, "applied_tags", [])] else None
        await self.message_db.run("INSERT OR IGNORE INTO stat_forum_posts (guild_id, thread_id, forum_id, owner_id, created_at, solved_at) VALUES (?, ?, ?, ?, ?, ?)",
                                  (guild.id, thread.id, thread.parent_id, self.recorded_as(owner) if owner else None, _created(thread), solved_at))

    @commands.Cog.listener("on_message")
    async def _forum_answer(self, message: Any) -> None:
        """The first message in a forum post from someone other than its author, bots aside, is its first answer."""
        guild = message.guild
        thread = message.channel
        if guild is None or message.author.bot or not _in_forum(thread) or not self.tracking(guild.id) or self._place(guild, thread, "messages") is None:
            return
        key = (guild.id, thread.id)
        seen = self._data.forum_seen
        if key in seen or message.author.id == getattr(thread, "owner_id", None) or message.id == thread.id:
            return
        at = message.created_at.timestamp()
        changed = await self.message_db.run("UPDATE stat_forum_posts SET replied_at = ?, replier_id = ? WHERE guild_id = ? AND thread_id = ? AND replied_at IS NULL",
                                            (at, self.recorded_as(message.author.id), guild.id, thread.id))
        if not changed and time.time() - _created(thread) < 60:
            return  # the post's own row may not be in yet
        if len(seen) >= FORUM_SEEN_MOST:
            seen.clear()
        seen.add(key)

    @commands.Cog.listener("on_thread_update")
    async def _forum_tags(self, before: Any, after: Any) -> None:
        guild = getattr(after, "guild", None)
        if guild is None or not self.tracking(guild.id) or not _in_forum(after) or self._place(guild, after, "messages") is None:
            return
        solved = self._solved_tag(guild.id, after.parent_id)
        if solved is None:
            return
        had = solved in [tag.id for tag in getattr(before, "applied_tags", [])]
        has = solved in [tag.id for tag in getattr(after, "applied_tags", [])]
        if has and not had:
            await self.message_db.run("UPDATE stat_forum_posts SET solved_at = ? WHERE guild_id = ? AND thread_id = ? AND solved_at IS NULL", (time.time(), guild.id, after.id))
        elif had and not has:
            await self.message_db.run("UPDATE stat_forum_posts SET solved_at = NULL WHERE guild_id = ? AND thread_id = ?", (guild.id, after.id))

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forum_gone(self, channel: Any) -> None:
        """Discord reports only the cached threads of a deleted forum as deleted, so its archived posts' rows go here."""
        if getattr(channel, "type", None) in POST_PARENTS:
            await self.message_db.run("DELETE FROM stat_forum_posts WHERE guild_id = ? AND forum_id = ?", (channel.guild.id, channel.id))

    @commands.Cog.listener("on_raw_thread_delete")
    async def _forum_post_gone(self, payload: Any) -> None:
        await self.message_db.run("DELETE FROM stat_forum_posts WHERE guild_id = ? AND thread_id = ?", (payload.guild_id, payload.thread_id))

    def _solved_tags(self, guild_id: int) -> dict[int, int | None]:
        """Each forum on the Forum messages page, with its solved tag."""
        if self.bot.module("forummessages") is None:
            return {}
        return {row["forum_id"]: row.get("solved_tag") for row in self.bot.settings.get(guild_id, "forummessages").get("forums") or []}

    def _solved_tag(self, guild_id: int, forum_id: int | None) -> int | None:
        return self._solved_tags(guild_id).get(forum_id) if forum_id else None

    # ---- the data page ----

    def _name(self, req: Any, channel_id: int, stored: str | None = None) -> str:
        """A channel's name for this person: Hidden channel when they can't see it, and its last known name when it's gone."""
        if not req.can_see(channel_id):
            return HIDDEN
        found = req.ops.channel(req.guild_id, channel_id)
        return (found or {}).get("name") or stored or "deleted-channel"

    async def _data_page(self, req: Any) -> dict:
        await self.up_to_date()
        rows = await self.message_db.all("SELECT channel_id, type, deleted_at FROM stat_channels WHERE guild_id = ?", (req.guild_id,))
        guild = self.bot.get_guild(req.guild_id)
        live = {channel.id for channel in guild.channels} if guild is not None else None
        kept = merge.channel_summary(rows, live)
        key = await self.db.one("SELECT hint, created_at FROM stat_api_keys WHERE guild_id = ?", (req.guild_id,))
        return {"enabled": self.on_in(req.guild_id), "channels": kept, "channels_warning": CHANNELS_WARNING, "recount": await self._recount_summary(req),
                "merge": await self._merge_summary(req), "api_key": bool(key), "reports": await self.db.scalar("SELECT COUNT(*) FROM stat_reports WHERE guild_id = ?", (req.guild_id,))}

    # ---- recounts ----

    async def _recount(self, guild_id: int) -> dict | None:
        return await self.message_db.one("SELECT * FROM stat_recounts WHERE guild_id = ?", (guild_id,))

    def _start_recount(self, guild_id: int) -> None:
        self._task(f"recount:{guild_id}", self._recount_task(guild_id))

    async def _recount_defaults(self, guild_id: int) -> dict:
        """The period a recount offers first: from the server's creation to the first message statistics recorded, or to now."""
        guild = self.bot.get_guild(guild_id)
        created = _created(guild) if guild is not None else history.DISCORD_EPOCH_MS / 1000
        first = await query.first_slot(self.message_db, "messages", guild_id)
        return {"start": created, "end": slot_time(first) if first is not None else time.time()}

    async def _recount_summary(self, req: Any) -> dict | None:
        job = await self._recount(req.guild_id)
        if job is None:
            return None
        places = await self.message_db.all("SELECT state, COUNT(*) AS n FROM stat_recount_places WHERE guild_id = ? GROUP BY state", (req.guild_id,))
        return {**{name: job[name] for name in ("state", "found", "note", "left_out", "threads", "everything", "started_by_name", "created_at", "read_at", "decided_by_name",
                                                "finished_at")},
                "start": slot_time(job["first_slot"]), "end": slot_time(job["end_slot"]), "places": {row["state"]: row["n"] for row in places}}

    async def _recount_page(self, req: Any) -> dict:
        await self.up_to_date()
        out: dict[str, Any] = {"defaults": await self._recount_defaults(req.guild_id), "recount": await self._recount_summary(req), "channels": [], "errors": []}
        job = await self._recount(req.guild_id)
        if job is None:
            return out
        names = await query.channels(self.message_db, req.guild_id)
        places = await self.message_db.all("SELECT * FROM stat_recount_places WHERE guild_id = ? ORDER BY found DESC", (req.guild_id,))
        found = await history.staged(self.message_db, req.guild_id)
        grouped: dict[int, dict] = {}
        for place in places:
            entry = grouped.setdefault(place["channel_id"], {"channel_id": place["channel_id"], "found": 0, "threads": [], "thread_count": 0, "states": {}})
            entry["states"][place["state"]] = entry["states"].get(place["state"], 0) + 1
            if place["kind"] == "channel":
                entry["own"] = place["found"]
            elif place["kind"] == LIST_PLACE:
                pass
            else:
                entry["thread_count"] += 1
                if len(entry["threads"]) < THREADS_SHOWN:
                    entry["threads"].append({"id": place["place_id"], "name": self._thread_name(req, place), "found": place["found"], "state": place["state"]})
            entry["found"] += place["found"]
            if place["state"] == history.ERROR:
                out["errors"].append({"id": place["place_id"], "name": self._name(req, place["channel_id"], place["name"]) if place["kind"] in ("channel", LIST_PLACE) else
                                      self._thread_name(req, place), "error": place["error"]})
        first, end = job["first_slot"], job["end_slot"]
        for channel_id, entry in grouped.items():
            entry["name"] = self._name(req, channel_id, (names.get(channel_id) or {}).get("name"))
            entry["staged"] = found.get(channel_id, 0)
            entry["recorded"] = await history.recorded(self.message_db, req.guild_id, channel_id, first, end, threads=bool(job["threads"])) if job["state"] in (history.APPROVAL, history.REVIEW) else None
            out["channels"].append(entry)
        out["channels"].sort(key=lambda entry: -max(entry["found"], entry["recorded"] or 0))
        return out

    def _thread_name(self, req: Any, place: dict) -> str:
        """A thread's name for this person: Hidden channel unless they can see its channel, and for a private thread, unless they also hold
        Manage Threads there, which is what Discord asks of anyone not added to it."""
        if not req.can_see(place["channel_id"]):
            return HIDDEN
        if place.get("private") and not req.is_admin:
            guild = self.bot.get_guild(req.guild_id)
            parent = guild.get_channel(place["channel_id"]) if guild is not None else None
            member = guild.get_member(req.user.id) if guild is not None else None
            if parent is None or member is None or not access.permissions_in(member, parent).manage_threads:
                return HIDDEN
        return place["name"]

    async def _recount_start(self, req: Any) -> dict:
        async with self._data.starting.setdefault(req.guild_id, asyncio.Lock()):  # two quick clicks start one recount, not two
            return await self._recount_begin(req)

    async def _recount_begin(self, req: Any) -> dict:
        form = await req.json()
        guild = self.bot.get_guild(req.guild_id)
        if guild is None:
            raise ApiError(409, "The bot isn't connected to this server right now, so it can't read its history.")
        if not self.on_in(req.guild_id):
            raise ApiError(409, "Switch statistics on first.")
        job = await self._recount(req.guild_id)
        if job is not None and job["state"] in history.OPEN:
            raise ApiError(409, "A recount is already under way here. Approve or cancel it first.")
        now = time.time()
        start, end = _period(form, now, _created(guild))
        threads = form.get("threads", True)
        if not isinstance(threads, bool):
            raise ValidationError({"threads": "Must be on or off."})
        picked = form.get("channels")
        everything = picked is None
        chosen: list[int] = []
        if not everything:
            if not isinstance(picked, list) or not picked or len(picked) > 500:
                raise ValidationError({"channels": "Pick at least one channel or category, or recount every channel."})
            for value in picked:
                channel_id = req.channel(value, "channels")
                chosen.append(channel_id)
        elif not req.is_admin:  # every channel they can see, the same rule a pick follows
            chosen = [channel.id for channel in guild.channels if channel.type in READABLE_TYPES and req.can_see(channel.id)]
            everything = False
        first, last = history.period_slots(start, end)
        last = min(last, slot_of(now))  # the quarter hour still running has messages being counted live
        if last <= first:
            raise ValidationError({"end": "The period has to end at least 15 minutes ago."})
        await history.clear(self.message_db, req.guild_id)
        await self.message_db.run("INSERT OR REPLACE INTO stat_recounts (guild_id, state, first_slot, end_slot, threads, everything, channels, found, note, started_by, "
                                  "started_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)",
                                  (req.guild_id, history.PLANNING, first, last, int(threads), int(everything), json.dumps(chosen), req.user.id, req.user.name, now))
        tz = zone(self.cfg(req.guild_id)["timezone"])
        await req.record("statistics.recount", detail=f"{day_label(dt.datetime.fromtimestamp(slot_time(first), tz).date())} to {day_label(dt.datetime.fromtimestamp(slot_time(last), tz).date())}")
        self._start_recount(req.guild_id)
        return await self._recount_page(req)

    async def _recount_resume(self, req: Any) -> dict:
        job = await self._recount(req.guild_id)
        if job is None or job["state"] != history.REVIEW:
            raise ApiError(409, "Only a recount waiting for review can be resumed.")
        async with self.message_db.transaction():
            await self.message_db.run("UPDATE stat_recount_places SET state = ?, error = NULL WHERE guild_id = ? AND state = ?", (history.WAITING, req.guild_id, history.ERROR))
            planned = await self.message_db.scalar("SELECT COUNT(*) FROM stat_recount_places WHERE guild_id = ?", (req.guild_id,))
            await self.message_db.run("UPDATE stat_recounts SET state = ?, note = NULL WHERE guild_id = ?", (history.READING if planned else history.PLANNING, req.guild_id))
        self._start_recount(req.guild_id)
        return await self._recount_page(req)

    async def _recount_skip(self, req: Any) -> dict:
        """Leaves the places that couldn't be read out and goes on to approval. A channel with a place left out keeps its recorded counts."""
        job = await self._recount(req.guild_id)
        if job is None or job["state"] != history.REVIEW:
            raise ApiError(409, "Only a recount waiting for review can leave places out.")
        async with self.message_db.transaction():
            await self.message_db.run("UPDATE stat_recount_places SET state = ? WHERE guild_id = ? AND state = ?", (history.SKIPPED, req.guild_id, history.ERROR))
            waiting = await self.message_db.scalar("SELECT COUNT(*) FROM stat_recount_places WHERE guild_id = ? AND state = ?", (req.guild_id, history.WAITING))
            await self.message_db.run("UPDATE stat_recounts SET state = ?, note = NULL WHERE guild_id = ?", (history.READING if waiting else history.APPROVAL, req.guild_id))
        if waiting:
            self._start_recount(req.guild_id)
        return await self._recount_page(req)

    async def _recount_approve(self, req: Any) -> dict:
        job = await self._recount(req.guild_id)
        if job is None or job["state"] != history.APPROVAL:
            raise ApiError(409, "There's no recount waiting for approval.")
        await self.message_db.run("UPDATE stat_recounts SET state = ?, decided_by = ?, decided_by_name = ? WHERE guild_id = ? AND state = ?",
                                  (history.APPLYING, req.user.id, req.user.name, req.guild_id, history.APPROVAL))
        await req.record("statistics.recount_approve", detail=f"{job['found']} messages")
        self._start_recount(req.guild_id)
        return await self._recount_page(req)

    async def _recount_cancel(self, req: Any) -> dict:
        job = await self._recount(req.guild_id)
        if job is None or job["state"] not in history.OPEN:
            raise ApiError(409, "There's no recount to cancel.")
        if job["state"] == history.APPLYING:
            raise ApiError(409, "This recount is being applied, which can't be stopped partway. It finishes in moments.")
        task = self._data.tasks.pop(f"recount:{req.guild_id}", None)
        if task is not None:
            task.cancel()
        swapped = [row["channel_id"] for row in await self.message_db.all("SELECT DISTINCT channel_id FROM stat_recount_places WHERE guild_id = ? AND state = ?",
                                                                           (req.guild_id, history.APPLIED))]
        note = None
        if swapped:
            names = await query.channels(self.message_db, req.guild_id, swapped)
            shown = ", ".join(f"#{(names.get(channel_id) or {}).get('name') or channel_id}" for channel_id in sorted(swapped)[:20])
            note = f"Canceled after {len(swapped)} channels were already swapped, which keep the recount's counts: {shown}."
        await self.message_db.run("UPDATE stat_recounts SET state = ?, note = ?, decided_by = ?, decided_by_name = ?, finished_at = ? WHERE guild_id = ?",
                                  (history.CANCELED, note, req.user.id, req.user.name, time.time(), req.guild_id))
        await history.clear(self.message_db, req.guild_id)
        await req.record("statistics.recount_cancel")
        return await self._recount_page(req)

    async def _recount_task(self, guild_id: int) -> None:
        try:
            await self._recount_run(guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("The recount in %s failed", guild_id)
            await self.message_db.run("UPDATE stat_recounts SET state = ?, note = ? WHERE guild_id = ? AND state IN (?, ?, ?)",
                                      (history.REVIEW, UNEXPECTED, guild_id, *history.RUNNING))

    async def _recount_run(self, guild_id: int) -> None:
        job = await self._recount(guild_id)
        if job is None or job["state"] not in history.RUNNING:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None or getattr(guild, "unavailable", False):
            await self.message_db.run("UPDATE stat_recounts SET state = ?, note = ? WHERE guild_id = ?",
                                      (history.REVIEW, "The bot wasn't connected to this server. Resume once it is.", guild_id))
            return
        if job["state"] == history.APPLYING:
            await self._recount_apply(guild, job)
            return
        async with self._data.reading:
            job = await self._recount(guild_id)
            if job is None or job["state"] not in (history.PLANNING, history.READING):
                return
            if job["state"] == history.PLANNING:
                await self._recount_plan(guild, job)
            await self._recount_read(guild)
        job = await self._recount(guild_id)
        if job is None or job["state"] != history.READING:
            return
        errors = await self.message_db.scalar("SELECT COUNT(*) FROM stat_recount_places WHERE guild_id = ? AND state = ?", (guild_id, history.ERROR))
        waiting = await self.message_db.scalar("SELECT COUNT(*) FROM stat_recount_places WHERE guild_id = ? AND state = ?", (guild_id, history.WAITING))
        if waiting:
            return  # stopped partway, as when the bot shuts down
        note = f"{errors} places couldn't be read. Fix what each one says and resume, leave them out, or cancel." if errors else None
        await self.message_db.run("UPDATE stat_recounts SET state = ?, note = ?, read_at = ? WHERE guild_id = ? AND state = ?",
                                  (history.REVIEW if errors else history.APPROVAL, note, time.time(), guild_id, history.READING))

    async def _recount_rules(self, guild: Any) -> history.Rules:
        """What counts. The server's emoji and stickers are the ones it has now and the ones statistics saw before they were deleted."""
        from .statistics import counted  # this file loads first
        known = [row["emoji"] for row in await self.message_db.all("SELECT emoji FROM stat_emoji_info WHERE guild_id = ?", (guild.id,))]
        own_emoji = frozenset(item.id for item in getattr(guild, "emojis", [])) | {int(key[2:]) for key in known if key.startswith("c:")}
        own_stickers = frozenset(item.id for item in getattr(guild, "stickers", [])) | {int(key[2:]) for key in known if key.startswith("s:")}
        return history.Rules(counted=counted, recorded_as=self.recorded_as, own_emoji=frozenset(own_emoji), own_stickers=frozenset(own_stickers),
                             reads_text=self.bot.reads_message_text)

    async def _recount_plan(self, guild: Any, job: dict) -> None:
        """Lists every channel and thread to read. Channels kept out of statistics, and channels whose messages go to a merged channel, are left
        out: a recount can't tell a merged channel's counts apart by where they came from. A channel whose threads Discord won't list in full
        gets a place of its own that waits for review, so approval never swaps a channel with threads left unread."""
        cfg = self.cfg(guild.id)
        untracked = set(cfg["untracked_channels"])
        merged = {category for row in cfg["aggregates"] if row["kinds"] in ("messages", "both") for category in row["auto_categories"]}
        first, last = job["first_slot"], job["end_slot"]
        # Categories whose messages went to a merged channel during the period, and channels a merge moved out of it.
        merged |= {row["category_id"] for row in await self.message_db.all("SELECT category_id FROM stat_auto_merges WHERE guild_id = ? AND from_slot < ? AND (to_slot IS NULL OR to_slot > ?)",
                                                                           (guild.id, last, first))}
        moved = {row["channel_id"] for row in await self.message_db.all("SELECT channel_id FROM stat_merged_channels WHERE guild_id = ? AND until_slot > ?", (guild.id, first))}
        chosen = set(json.loads(job["channels"] or "[]"))
        start, end = slot_time(job["first_slot"]), slot_time(job["end_slot"])
        solved = self._solved_tags(guild.id)
        rows: list[tuple] = []
        left_out = merged_out = 0
        active = None
        for channel in sorted(guild.channels, key=lambda item: item.id):
            if channel.type not in READABLE_TYPES:
                continue
            category = getattr(channel, "category_id", None)
            if not job["everything"] and channel.id not in chosen and category not in chosen:
                continue
            if channel.id in untracked or (category is not None and category in untracked):
                left_out += 1
                continue
            if (category is not None and category in merged) or channel.id in moved:
                merged_out += 1
                continue
            if channel.type not in POST_PARENTS:
                flags = store.VOICE_CHAT if channel.type in VOICE_TYPES else 0
                rows.append((guild.id, channel.id, channel.id, "channel", channel.name, flags, None, None, None, history.WAITING, None, 0))
            if job["threads"] and channel.type in THREAD_PARENTS:
                if active is None:
                    try:
                        active = await self._active_threads(guild)
                    except discord.HTTPException as error:
                        await self.message_db.run("UPDATE stat_recounts SET state = ?, note = ? WHERE guild_id = ? AND state = ?",
                                                  (history.REVIEW, f"Discord didn't list the server's active threads ({_refusal(error)}). Resume to try again.", guild.id,
                                                   history.PLANNING))
                        return
                threads, problem = await self._threads_of(channel, active, start, end)
                rows += self._thread_rows(guild, channel, threads, untracked, solved)
                if problem is not None:
                    rows.append((guild.id, -channel.id, channel.id, LIST_PLACE, channel.name, 0, None, None, None, history.ERROR, problem, 0))
        notes = []
        if left_out:
            notes.append(f"Left out {left_out} channels that statistics don't track.")
        if merged_out:
            notes.append(f"Left out {merged_out} channels whose messages a merged channel holds for this period.")
        async with self.message_db.transaction():
            await self.message_db.many("INSERT OR IGNORE INTO stat_recount_places (guild_id, place_id, channel_id, kind, name, flags, owner_id, created_at, solved_at, state, "
                                       "error, private) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            await self.message_db.run("UPDATE stat_recounts SET state = ?, left_out = ? WHERE guild_id = ? AND state = ?",
                                      (history.READING, " ".join(notes) or None, guild.id, history.PLANNING))

    def _thread_rows(self, guild: Any, channel: Any, threads: list[Any], untracked: set[int], solved: dict[int, int | None]) -> list[tuple]:
        """The places for a channel's threads, as stat_recount_places rows waiting to be read."""
        rows = []
        for thread in threads:
            if thread.id in untracked:
                continue
            post = channel.type in POST_PARENTS
            tag = solved.get(channel.id) if post else None
            solved_at = None
            if tag is not None and tag in [item.id for item in getattr(thread, "applied_tags", [])]:
                stamp = getattr(thread, "archive_timestamp", None)
                solved_at = stamp.timestamp() if stamp is not None else time.time()
            private = getattr(thread, "type", None) == discord.ChannelType.private_thread  # its name shows only to who may see it
            rows.append((guild.id, thread.id, channel.id, "post" if post else "thread", thread.name, store.THREAD, getattr(thread, "owner_id", None),
                         _created(thread), solved_at, history.WAITING, None, int(private)))
        return rows

    async def _active_threads(self, guild: Any) -> list[Any]:
        """Every active thread in the server. Discord refusing the list raises, since threads missing from it would lose their counts."""
        found = {thread.id: thread for thread in getattr(guild, "threads", [])}
        for thread in await guild.active_threads():
            found.setdefault(thread.id, thread)
        return list(found.values())

    async def _threads_of(self, channel: Any, active: list[Any], start: float, end: float) -> tuple[list[Any], str | None]:
        """A channel's threads that can hold messages from the period: active ones, and archived ones, public and private, archived after it
        began. With them, what Discord said when it wouldn't give a list, or None when every list came."""
        found = {thread.id: thread for thread in active if thread.parent_id == channel.id}
        problems: list[str] = []

        async def archived(**options: Any) -> None:
            async for thread in channel.archived_threads(limit=None, **options):
                stamp = getattr(thread, "archive_timestamp", None)
                if stamp is not None and stamp.timestamp() < start and not options.get("joined"):
                    break  # listed newest archived first, so the rest were archived before the period
                found.setdefault(thread.id, thread)
        try:
            await archived()
        except discord.HTTPException as error:
            problems.append(_refusal(error))
        if channel.type in (discord.ChannelType.text, discord.ChannelType.news):
            try:
                await archived(private=True)
            except discord.Forbidden:  # without Manage Threads, Discord lists only the private threads the bot has joined
                try:
                    await archived(private=True, joined=True)
                except discord.HTTPException as error:
                    problems.append(_refusal(error))
            except discord.HTTPException as error:
                problems.append(_refusal(error))
        threads = [thread for thread in found.values() if _created(thread) < end]
        if not problems:
            return threads, None
        return threads, (f"Discord didn't list this channel's archived threads ({problems[0]}). Resume to try again, or leave it out to keep the channel's "
                         "recorded counts as they are.")

    async def _recount_read(self, guild: Any) -> None:
        """Reads every waiting place, a few at a time. A thread list read again on resume adds places, which the next pass reads."""
        rules = await self._recount_rules(guild)
        tried: set[int] = set()
        while True:
            rows = [row for row in await self.message_db.all("SELECT * FROM stat_recount_places WHERE guild_id = ? AND state = ? ORDER BY kind, place_id",
                                                              (guild.id, history.WAITING)) if row["place_id"] not in tried]
            if not rows:
                return
            tried.update(row["place_id"] for row in rows)
            queue: asyncio.Queue = asyncio.Queue()
            for row in rows:
                queue.put_nowait(row)

            async def worker() -> None:
                while not queue.empty():
                    row = queue.get_nowait()
                    job = await self._recount(guild.id)
                    if job is None or job["state"] != history.READING:
                        return
                    if row["kind"] == LIST_PLACE:
                        await self._list_again(guild, job, row)
                    else:
                        await self._read_place(guild, job, row, rules)
            readers = [asyncio.ensure_future(worker()) for _ in range(READERS)]
            try:
                await asyncio.gather(*readers)
            except BaseException:
                # One reader failed, or the recount stopped: the others stop with it, so none goes on reading a place a resume hands out again.
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                raise
            job = await self._recount(guild.id)
            if job is None or job["state"] != history.READING:
                return

    async def _list_again(self, guild: Any, job: dict, row: dict) -> None:
        """Asks Discord for a channel's threads again, after a list that failed. The threads it finds wait to be read."""
        channel = guild.get_channel(row["channel_id"])
        if channel is None:
            await self._place_state(guild.id, row["place_id"], history.GONE, "Discord says this channel is gone, so it was passed over.")
            return
        try:
            active = await self._active_threads(guild)
        except discord.HTTPException as error:
            await self._place_state(guild.id, row["place_id"], history.ERROR, f"Discord didn't list the server's active threads ({_refusal(error)}). Resume to try again.")
            return
        threads, problem = await self._threads_of(channel, active, slot_time(job["first_slot"]), slot_time(job["end_slot"]))
        if problem is not None:
            await self._place_state(guild.id, row["place_id"], history.ERROR, problem)
            return
        cfg = self.cfg(guild.id)
        rows = self._thread_rows(guild, channel, threads, set(cfg["untracked_channels"]), self._solved_tags(guild.id))
        async with self.message_db.transaction():
            await self.message_db.many("INSERT OR IGNORE INTO stat_recount_places (guild_id, place_id, channel_id, kind, name, flags, owner_id, created_at, solved_at, state, "
                                       "error, private) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            await self._place_state(guild.id, row["place_id"], history.PLACE_DONE, None)

    async def _place_of(self, guild: Any, place_id: int) -> Any:
        found = guild.get_channel_or_thread(place_id)
        if found is not None:
            return found
        return await guild.fetch_channel(place_id)

    async def _read_place(self, guild: Any, job: dict, row: dict, rules: history.Rules) -> None:
        """Reads one channel or thread from where it got to, saving every READ_BATCH messages. A place that's gone is passed over, and one the bot
        can't read waits for review."""
        place = history.Place(row["channel_id"], row["flags"], row["place_id"] if row["kind"] != "channel" else None, row["owner_id"])
        before = discord.Object(history.snowflake_at(slot_time(job["end_slot"])))
        for attempt in range(READ_TRIES):
            current = await self.message_db.one("SELECT cursor FROM stat_recount_places WHERE guild_id = ? AND place_id = ?", (guild.id, row["place_id"]))
            cursor = current["cursor"] if current else None
            after = discord.Object(cursor if cursor else history.snowflake_at(slot_time(job["first_slot"])) - 1)
            tally = history.Tally()
            last, read = None, 0
            try:
                channel = await self._place_of(guild, row["place_id"])
                async for message in channel.history(limit=None, after=after, before=before, oldest_first=True):
                    history.tally_message(tally, message, place, rules)
                    last, read = message.id, read + 1
                    if read >= READ_BATCH:
                        await history.stage(self.message_db, guild.id, tally, row["place_id"], last)
                        tally, read = history.Tally(), 0
                        state = await self.message_db.scalar("SELECT state FROM stat_recounts WHERE guild_id = ?", (guild.id,))
                        if state != history.READING:
                            return
                await history.stage(self.message_db, guild.id, tally, row["place_id"], last, finished=True)
                return
            except discord.NotFound:
                await history.stage(self.message_db, guild.id, tally, row["place_id"], last)
                if row["kind"] == "channel":
                    await self._place_state(guild.id, row["place_id"], history.GONE, "Discord says this is gone, so it was passed over.")
                else:
                    await self._place_state(guild.id, row["place_id"], history.ERROR, "Discord says this thread is gone. Resume to look again, or leave it out to keep "
                                            "its channel's recorded counts as they are.")
                return
            except discord.Forbidden:
                await history.stage(self.message_db, guild.id, tally, row["place_id"], last)
                await self._place_state(guild.id, row["place_id"], history.ERROR, "The bot can't read this. Give it View Channel and Read Message History here, then resume.")
                return
            except (discord.DiscordServerError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
                await history.stage(self.message_db, guild.id, tally, row["place_id"], last)
                if attempt + 1 == READ_TRIES:
                    await self._place_state(guild.id, row["place_id"], history.ERROR, f"Discord didn't answer: {error}. Resume to try again.")
                    return
                await asyncio.sleep(RETRY_SECONDS * (attempt + 1))
            except discord.HTTPException as error:
                await history.stage(self.message_db, guild.id, tally, row["place_id"], last)
                await self._place_state(guild.id, row["place_id"], history.ERROR, f"Discord refused: {error.text or error.status}.")
                return

    async def _place_state(self, guild_id: int, place_id: int, state: str, error: str | None) -> None:
        await self.message_db.run("UPDATE stat_recount_places SET state = ?, error = ? WHERE guild_id = ? AND place_id = ?", (state, error, guild_id, place_id))

    async def _recount_apply(self, guild: Any, job: dict) -> None:
        """Swaps each channel whose every place was read. A channel with a place left out, or whose own history is gone, keeps what it has."""
        self._applying(guild.id, 1)
        try:
            await self._recount_swap(guild, job)
        finally:
            self._applying(guild.id, -1)

    async def _recount_swap(self, guild: Any, job: dict) -> None:
        await self.up_to_date()
        await history.hide_private(self.message_db, guild.id, self.private)
        rows = await self.message_db.all("SELECT channel_id, kind, state FROM stat_recount_places WHERE guild_id = ?", (guild.id,))
        live_from = await history.live_reactions_from(self.message_db, guild.id)
        reactions = live_from is None or job["end_slot"] <= live_from  # a period with reactions counted live keeps them as they were counted
        by_channel: dict[int, list[dict]] = {}
        for row in rows:
            by_channel.setdefault(row["channel_id"], []).append(row)
        kept = 0
        for channel_id, places in sorted(by_channel.items()):
            states = {place["state"] for place in places}
            if history.APPLIED in states:
                continue  # applied before a restart: its staging rows went in the same transaction, so a second swap would empty it
            own_gone = guild.get_channel(channel_id) is None or any(place["kind"] == "channel" and place["state"] == history.GONE for place in places)
            if own_gone or states & {history.SKIPPED, history.ERROR, history.WAITING}:
                kept += 1
                await self.message_db.run("DELETE FROM stat_recount_messages WHERE guild_id = ? AND channel_id = ?", (guild.id, channel_id))
                await self.message_db.run("DELETE FROM stat_recount_emoji WHERE guild_id = ? AND channel_id = ?", (guild.id, channel_id))
                continue
            if history.PLACE_DONE in states or history.GONE in states:
                await history.apply_channel(self.message_db, guild.id, channel_id, job["first_slot"], job["end_slot"], threads=bool(job["threads"]), reactions=reactions)
        await history.apply_posts(self.message_db, guild.id, slot_time(job["first_slot"]), slot_time(job["end_slot"]))
        await history.apply_names(self.message_db, guild.id)
        note = f"{kept} channels kept their recorded counts, since part of them couldn't be read." if kept else None
        await history.clear(self.message_db, guild.id)
        await self.message_db.run("UPDATE stat_recounts SET state = ?, note = ?, finished_at = ? WHERE guild_id = ?", (history.DONE, note, time.time(), guild.id))

    # ---- merged channels ----

    def _aggregates(self, guild_id: int) -> list[dict]:
        return [dict(row) for row in self.cfg(guild_id)["aggregates"]]

    def _category(self, req: Any, value: Any, field: str) -> int | None:
        if value in (None, ""):
            return None
        channel_id = req.channel(value, field)
        if (req.ops.channel(req.guild_id, channel_id) or {}).get("type") != discord.ChannelType.category.value:
            raise ValidationError({field: "Pick a category."})
        return channel_id

    def _check_aggregates(self, rows: list[dict], field: str = "name") -> None:
        names: set[str] = set()
        fed: set[int] = set()
        for row in rows:
            folded = row["name"].casefold()
            if folded in names:
                raise ValidationError({field: f"Another merged channel is already called {row['name']}."})
            names.add(folded)
            for category in row["auto_categories"]:
                if category in fed:
                    raise ValidationError({"auto_categories": "Each category can feed one merged channel."})
                fed.add(category)

    async def _save_aggregates(self, req: Any, rows: list[dict], action: str, detail: str) -> None:
        self._check_aggregates(rows)
        await self.bot.settings.save(req.guild_id, self.key, {"aggregates": rows}, actor=req.actor, action=action)
        await req.record(action, detail=detail[:400])

    async def _merged_page(self, req: Any) -> dict:
        await self.up_to_date()
        rows = []
        for row in self._aggregates(req.guild_id):
            rows.append({**row, "holds_counts": await merge.holds_counts(self.message_db, req.guild_id, row["id"]),
                         "category_name": self._name(req, row["category_id"]) if row["category_id"] else None,
                         "auto_names": [self._name(req, category) for category in row["auto_categories"]]})
        return {"aggregates": rows, "most": MERGED_MOST, "merge": await self._merge_summary(req)}

    async def _merged_create(self, req: Any) -> dict:
        form = await req.json()
        try:
            name = text(NAME_MOST, minimum=1)(form.get("name"))
        except Invalid as error:
            raise ValidationError({"name": str(error)}) from error
        category = self._category(req, form.get("category_id"), "category_id")
        rows = self._aggregates(req.guild_id)
        if len(rows) >= MERGED_MOST:
            raise ValidationError({"name": f"A server can have {MERGED_MOST} merged channels. Delete one first."})
        taken = {row["id"] for row in rows} | set((await query.channels(self.message_db, req.guild_id)).keys())
        number = secrets.randbelow(merge_id_room()) + 1_000_000
        while number in taken:
            number = secrets.randbelow(merge_id_room()) + 1_000_000
        rows.append({"id": number, "name": name, "category_id": category, "auto_categories": [], "kinds": "both"})
        await self._save_aggregates(req, rows, "statistics.merged_create", name)
        self.recorder.channel(req.guild_id, number, name, merge.AGGREGATE_TYPE, category, time.time(), None)
        await self.write()
        return await self._merged_page(req)

    async def _merged_edit(self, req: Any) -> dict:
        number = req.int_param("aggregate_id")
        form = await req.json()
        rows = self._aggregates(req.guild_id)
        row = next((item for item in rows if item["id"] == number), None)
        if row is None:
            raise ApiError(404, "That merged channel isn't there anymore.")
        if "name" in form:
            try:
                row["name"] = text(NAME_MOST, minimum=1)(form.get("name"))
            except Invalid as error:
                raise ValidationError({"name": str(error)}) from error
        if "category_id" in form:
            row["category_id"] = self._category(req, form.get("category_id"), "category_id")
        if "auto_categories" in form:
            picked = form.get("auto_categories")
            if not isinstance(picked, list) or len(picked) > 50:
                raise ValidationError({"auto_categories": "Pick up to 50 categories."})
            row["auto_categories"] = list(dict.fromkeys(self._category(req, value, "auto_categories") for value in picked))
        if "kinds" in form:
            if form.get("kinds") not in ("messages", "voice", "both"):
                raise ValidationError({"kinds": "Pick messages, voice time or both."})
            row["kinds"] = form["kinds"]
        await self._save_aggregates(req, rows, "statistics.merged_edit", row["name"])
        self.recorder.channel(req.guild_id, number, row["name"], merge.AGGREGATE_TYPE, row["category_id"], None, None)
        await self.write()
        return await self._merged_page(req)

    async def _merged_delete(self, req: Any) -> dict:
        number = req.int_param("aggregate_id")
        rows = self._aggregates(req.guild_id)
        row = next((item for item in rows if item["id"] == number), None)
        if row is None:
            raise ApiError(404, "That merged channel isn't there anymore.")
        await self.up_to_date()
        running = await self.message_db.one("SELECT target_id FROM stat_merges WHERE guild_id = ? AND state = 'running'", (req.guild_id,))
        if running is not None and running["target_id"] == number:
            raise ApiError(409, "A merge into this channel is running. Wait for it to finish.")
        if await merge.holds_counts(self.message_db, req.guild_id, number):
            raise ApiError(409, f"{row['name']} still holds counts. Merge it into another merged channel first, then delete it.")
        await self._save_aggregates(req, [item for item in rows if item["id"] != number], "statistics.merged_delete", row["name"])
        await self.message_db.run("DELETE FROM stat_channels WHERE guild_id = ? AND channel_id = ? AND type = ?", (req.guild_id, number, merge.AGGREGATE_TYPE))
        return await self._merged_page(req)

    def _merge_form(self, req: Any, form: dict) -> dict:
        target = whole_number(form.get("target_id"))
        if target is None or target not in {row["id"] for row in self._aggregates(req.guild_id)}:
            raise ValidationError({"target_id": "Pick the merged channel to merge into."})
        kinds = form.get("kinds", "both")
        if kinds not in merge.KIND_TABLES:
            raise ValidationError({"kinds": "Pick messages, voice time or both."})
        categories = form.get("categories") or []
        patterns = form.get("patterns") or []
        picked = form.get("channels") or []
        if not all(isinstance(value, list) for value in (categories, patterns, picked)) or len(categories) > 100 or len(patterns) > 50 or len(picked) > 1000:
            raise ValidationError({"channels": "Pick up to 100 categories, 50 name patterns and 1,000 channels."})
        if any(not isinstance(pattern, str) or not pattern.strip() or len(pattern) > NAME_MOST for pattern in patterns):
            raise ValidationError({"patterns": f"Each name pattern is 1 to {NAME_MOST} characters."})
        try:
            category_ids = [snowflake(value) for value in categories]
            channel_ids = [whole_number(value) for value in picked]
        except Invalid as error:
            raise ValidationError({"channels": str(error)}) from error
        if any(value is None for value in channel_ids):
            raise ValidationError({"channels": "That isn't a channel."})
        return {"target_id": target, "kinds": kinds, "categories": category_ids, "patterns": [pattern.strip() for pattern in patterns], "channels": channel_ids,
                "forget": bool(form.get("forget"))}

    async def _merge_candidates(self, req: Any, form: dict) -> list[dict]:
        chosen = await merge.choose(self.message_db, req.guild_id, target_id=form["target_id"], categories=form["categories"], patterns=form["patterns"],
                                    picked=form["channels"])
        # A merged channel shows its counts under its own name to anyone, so nobody moves a channel's counts there unless they can see it.
        if any(not req.can_see(row["channel_id"]) for row in chosen):
            raise ValidationError({"patterns" if form["patterns"] else "channels": "That takes in a channel you can't see. Narrow it to channels you can."})
        guild = self.bot.get_guild(req.guild_id)
        counts = await merge.rows_in(self.message_db, req.guild_id, [row["channel_id"] for row in chosen], merge.KIND_TABLES[form["kinds"]], slot_of(time.time()) + 1)
        out = []
        for row in chosen:
            gone = row["deleted_at"] is not None or (guild is not None and guild.get_channel(row["channel_id"]) is None)
            out.append({"id": row["channel_id"], "name": self._name(req, row["channel_id"], row["name"]), "type": row["type"], "deleted": gone,
                        "rows": counts[row["channel_id"]]})
        return out

    async def _merge_preview(self, req: Any) -> dict:
        form = self._merge_form(req, await req.json())
        await self.up_to_date()
        found = await self._merge_candidates(req, form)
        return {"channels": found, "rows": sum(row["rows"] for row in found), "would_forget": sum(1 for row in found if row["deleted"]) if form["forget"] else 0}

    async def _merge_start(self, req: Any) -> dict:
        async with self._data.starting.setdefault(req.guild_id, asyncio.Lock()):  # two quick clicks start one merge, not two
            return await self._merge_begin(req)

    async def _merge_begin(self, req: Any) -> dict:
        body = await req.json()
        form = self._merge_form(req, body)
        if body.get("confirm") is not True:
            raise ValidationError({"confirm": "Check the list, then confirm the merge."})
        running = await self.message_db.one("SELECT state FROM stat_merges WHERE guild_id = ?", (req.guild_id,))
        if running is not None and running["state"] == "running":
            raise ApiError(409, "A merge is already running here. Wait for it to finish.")
        await self.up_to_date()
        found = await self._merge_candidates(req, form)
        if not found:
            raise ValidationError({"channels": "Nothing matches. Pick categories, channels or name patterns that find something."})
        now = time.time()
        async with self.message_db.transaction():
            await self.message_db.run("DELETE FROM stat_merge_sources WHERE guild_id = ?", (req.guild_id,))
            await self.message_db.run("INSERT OR REPLACE INTO stat_merges (guild_id, target_id, kinds, forget, state, until_slot, total, done, rows_moved, forgotten, note, "
                                      "started_by, started_by_name, created_at) VALUES (?, ?, ?, ?, 'running', ?, ?, 0, 0, 0, NULL, ?, ?, ?)",
                                      (req.guild_id, form["target_id"], form["kinds"], int(form["forget"]), slot_of(now) + 1, len(found), req.user.id, req.user.name, now))
            await self.message_db.many("INSERT INTO stat_merge_sources (guild_id, channel_id, name, state) VALUES (?, ?, ?, 'waiting')",
                                       [(req.guild_id, row["id"], row["name"]) for row in found])
            if form["kinds"] in ("messages", "both"):  # kept for good, so a recount never counts a merged channel's messages again under their old channel
                await self.message_db.many("INSERT INTO stat_merged_channels (guild_id, channel_id, target_id, until_slot) VALUES (?, ?, ?, ?) ON CONFLICT (guild_id, channel_id) "
                                           "DO UPDATE SET target_id = excluded.target_id, until_slot = MAX(until_slot, excluded.until_slot)",
                                           [(req.guild_id, row["id"], form["target_id"], slot_of(now) + 1) for row in found])
        target = next(row["name"] for row in self._aggregates(req.guild_id) if row["id"] == form["target_id"])
        await req.record("statistics.merge", detail=f"{len(found)} channels into {target}")
        self._start_merge(req.guild_id)
        return await self._merged_page(req)

    async def _merge_summary(self, req: Any) -> dict | None:
        row = await self.message_db.one("SELECT * FROM stat_merges WHERE guild_id = ?", (req.guild_id,))
        if row is None:
            return None
        target = next((item["name"] for item in self._aggregates(req.guild_id) if item["id"] == row["target_id"]), "a merged channel that's gone")
        return {**merge.describe(row), "target_name": target, "kinds_label": merge.kinds_label(row["kinds"])}

    def _start_merge(self, guild_id: int) -> None:
        self._task(f"merge:{guild_id}", self._merge_task(guild_id))

    async def _merge_task(self, guild_id: int) -> None:
        try:
            await self._merge_run(guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("The merge in %s failed", guild_id)
            await self.message_db.run("UPDATE stat_merges SET state = 'stopped', note = ?, finished_at = ? WHERE guild_id = ? AND state = 'running'",
                                      ("Something went wrong. It's been logged. Start the merge again to finish it.", time.time(), guild_id))

    async def _merge_run(self, guild_id: int) -> None:
        job = await self.message_db.one("SELECT * FROM stat_merges WHERE guild_id = ?", (guild_id,))
        if job is None or job["state"] != "running":
            return
        self._applying(guild_id, 1)
        try:
            await self._merge_move(guild_id, job)
        finally:
            self._applying(guild_id, -1)

    async def _merge_move(self, guild_id: int, job: dict) -> None:
        await self.up_to_date()
        tables = merge.KIND_TABLES.get(job["kinds"], ())
        guild = self.bot.get_guild(guild_id)

        async def moved(count: int) -> None:
            await self.message_db.run("UPDATE stat_merges SET rows_moved = rows_moved + ? WHERE guild_id = ?", (count, guild_id))
        for source in await self.message_db.all("SELECT * FROM stat_merge_sources WHERE guild_id = ? AND state = 'waiting' ORDER BY channel_id", (guild_id,)):
            for table in tables:
                while await merge.move_batch(self.message_db, guild_id, source["channel_id"], job["target_id"], table, job["until_slot"], then=moved):
                    await asyncio.sleep(0)  # the bot's other work gets a turn between batches
            forgot = False
            if job["forget"]:
                stored = await self.message_db.one("SELECT deleted_at FROM stat_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, source["channel_id"]))
                # A server that isn't connected yet lists no channels, so only a deletion statistics saw says a channel is gone then.
                connected = guild is not None and not getattr(guild, "unavailable", False)
                gone = stored is not None and (stored["deleted_at"] is not None or (connected and guild.get_channel(source["channel_id"]) is None))
                forgot = await merge.forget_if_empty(self.message_db, guild_id, source["channel_id"], deleted_in_discord=gone)
            async with self.message_db.transaction():
                await self.message_db.run("UPDATE stat_merge_sources SET state = 'done' WHERE guild_id = ? AND channel_id = ?", (guild_id, source["channel_id"]))
                await self.message_db.run("UPDATE stat_merges SET done = done + 1, forgotten = forgotten + ? WHERE guild_id = ?", (int(forgot), guild_id))
        await self.message_db.run("UPDATE stat_merges SET state = 'done', finished_at = ? WHERE guild_id = ? AND state = 'running'", (time.time(), guild_id))

    # ---- the statistics API ----

    async def _api_page(self, req: Any) -> dict:
        """A key reads every channel its maker can see, so only the owner and Administrators, who see them all, make or see one."""
        admin = req.is_admin
        row = await self.db.one("SELECT hint, created_by_name, created_at, used_at FROM stat_api_keys WHERE guild_id = ?", (req.guild_id,)) if admin else None
        return {"key": row, "base": f"{self.bot.public_url.rstrip('/')}{API_BASE}", "guild_id": req.guild_id, "paths": ["/v1/activities", *(f"/v1/guilds/{{guild_id}}{path}" for path in devapi.GUILD_ROUTES)],
                "per_minute": devapi.REQUESTS_PER_WINDOW, "is_admin": admin, "note": None if admin else NOT_ADMIN_NOTE}

    async def _api_key_create(self, req: Any) -> dict:
        if not req.is_admin:
            raise ApiError(403, access.ONLY_ADMINS)
        key = devapi.KEY_PREFIX + secrets.token_urlsafe(32)
        had = await self.db.scalar("SELECT 1 FROM stat_api_keys WHERE guild_id = ?", (req.guild_id,))
        await self.db.run("INSERT OR REPLACE INTO stat_api_keys (guild_id, key_hash, hint, created_by, created_by_name, created_at, used_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                          (req.guild_id, token_hash(key), key[-4:], req.user.id, req.user.name, time.time()))
        if self._data.key_hashes is not None:
            self._data.key_hashes.add(token_hash(key))
        await req.record("statistics.api_key_replace" if had else "statistics.api_key_create")
        return {**await self._api_page(req), "new_key": key}

    async def _api_key_delete(self, req: Any) -> dict:
        if not req.is_admin:
            raise ApiError(403, access.ONLY_ADMINS)
        if await self.db.run("DELETE FROM stat_api_keys WHERE guild_id = ?", (req.guild_id,)):
            await req.record("statistics.api_key_delete")
        return await self._api_page(req)

    async def _api_key_row(self, key: str) -> dict | None:
        """The row of the key a request sent, or None. A hash missing from memory is no key at all, so a script trying wrong keys never reaches
        the database. A hash found there is still read from the table, so a key deleted anywhere stops at once."""
        known = self._data.key_hashes
        if known is None:
            known = self._data.key_hashes = {row["key_hash"] for row in await self.db.all("SELECT key_hash FROM stat_api_keys")}
        digest = token_hash(key)
        if digest not in known:
            return None
        row = await self.db.one("SELECT guild_id, created_by FROM stat_api_keys WHERE key_hash = ?", (digest,))
        if row is None:
            known.discard(digest)
        return row

    def _api_answer(self, handler: Any) -> Any:
        async def answer(request: web.Request) -> web.Response:
            key = devapi.read_bearer(request.headers.get("Authorization"))
            row = await self._api_key_row(key) if key else None
            if row is None:
                return _api_json({"message": "Unauthorized"}, 401)
            owner = row["guild_id"]
            allowed, headers = self._data.limiter.take(owner, time.monotonic())
            if not allowed:
                return _api_json({"message": "Too many requests. Wait for the time in retry-after."}, 429, headers)
            if handler is not None and request.match_info.get("guild_id") != str(owner):
                return _api_json({"message": "Unauthorized"}, 401, headers)
            guild = self.bot.get_guild(owner)
            if guild is None:
                return _api_json({"message": "Captain Guild isn't in that server, so its key doesn't work."}, 403, headers)
            maker = guild.get_member(row["created_by"]) if row["created_by"] else None
            if maker is None:
                return _api_json({"message": "Whoever made this key has left the server. An Administrator can make a new one on the dashboard."}, 403, headers)
            hidden = frozenset() if access.is_admin(maker) else frozenset(channel.id for channel in guild.channels if not access.can_view(maker, channel))
            gate = self._data.gates.setdefault(owner, asyncio.Semaphore(API_AT_ONCE))
            try:
                async with gate:
                    await self.up_to_date()
                    source = devapi.Source(self.message_db, owner, guild, self.cfg(owner), time.time(), hidden)
                    data = await (devapi.activities(source) if handler is None else handler(source, request.query, dict(request.match_info)))
            except devapi.Problem as problem:
                return _api_json({"message": problem.message}, problem.status, headers)
            except Exception:
                log.exception("Statistics API %s failed", request.path)
                return _api_json({"message": "Something went wrong on the server. It's been logged."}, 500, headers)
            now = time.time()
            if now - self._data.used.get(owner, 0) >= 60:
                self._data.used[owner] = now
                await self.db.run("UPDATE stat_api_keys SET used_at = ? WHERE guild_id = ?", (now, owner))
            return _api_json(data, 200, headers)
        return answer

    # ---- scheduled reports ----

    async def _reports_page(self, req: Any) -> dict:
        rows = await self.db.all("SELECT * FROM stat_reports WHERE guild_id = ? ORDER BY created_at", (req.guild_id,))
        reports = [{**row, "channel_name": self._name(req, row["channel_id"]), "weekday_name": WEEKDAYS[row["weekday"] % 7]} for row in rows]
        return {"reports": reports, "most": REPORTS_MOST, "timezone": self.cfg(req.guild_id)["timezone"]}

    async def _report_create(self, req: Any) -> dict:
        form = await req.json()
        channel_id = req.channel(form.get("channel_id"), posting=True)
        kind = (req.ops.channel(req.guild_id, channel_id) or {}).get("type")
        if kind not in (discord.ChannelType.text.value, discord.ChannelType.news.value):
            raise ValidationError({"channel_id": "Pick a text or announcement channel."})
        cadence = form.get("cadence")
        if cadence not in ("week", "month"):
            raise ValidationError({"cadence": "Pick weekly or monthly."})
        weekday, hour = form.get("weekday", 0), form.get("hour", 9)
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValidationError({"weekday": "Pick a day of the week."})
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValidationError({"hour": "Pick an hour from 0 to 23."})
        if await self.db.scalar("SELECT COUNT(*) FROM stat_reports WHERE guild_id = ?", (req.guild_id,)) >= REPORTS_MOST:
            raise ValidationError({"cadence": f"A server can have {REPORTS_MOST} scheduled reports. Delete one first."})
        report_id = secrets.token_hex(8)
        now = time.time()
        due = next_report_at(cadence, weekday, hour, zone(self.cfg(req.guild_id)["timezone"]), now)
        await self.db.run("INSERT INTO stat_reports (guild_id, id, channel_id, cadence, weekday, hour, next_at, created_by, created_by_name, created_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (req.guild_id, report_id, channel_id, cadence, weekday, hour, due, req.user.id, req.user.name, now))
        await self.bot.timers.schedule(REPORT_TIMER, due, {"guild_id": req.guild_id, "id": report_id}, guild_id=req.guild_id, key=f"{req.guild_id}:{report_id}")
        await req.record("statistics.report_create", detail=f"{'Weekly' if cadence == 'week' else 'Monthly'} in #{self._name(req, channel_id)}")
        return await self._reports_page(req)

    def _report_id(self, req: Any) -> str:
        """The report a path names. Its format is checked here, after the dashboard checked who's asking."""
        report_id = req.param("report_id")
        if not re.fullmatch("[0-9a-f]{16}", report_id):
            raise ApiError(404, "That report isn't there anymore.")
        return report_id

    async def _report_delete(self, req: Any) -> dict:
        report_id = self._report_id(req)
        if await self.db.run("DELETE FROM stat_reports WHERE guild_id = ? AND id = ?", (req.guild_id, report_id)):
            await self.bot.timers.cancel_key(REPORT_TIMER, f"{req.guild_id}:{report_id}")
            await req.record("statistics.report_delete")
        return await self._reports_page(req)

    async def _report_send_now(self, req: Any) -> dict:
        row = await self.db.one("SELECT * FROM stat_reports WHERE guild_id = ? AND id = ?", (req.guild_id, self._report_id(req)))
        if row is None:
            raise ApiError(404, "That report isn't there anymore.")
        req.channel(row["channel_id"], posting=True)
        problem = await self._post_report(row, time.time())
        if problem:
            raise ApiError(409, problem)
        return await self._reports_page(req)

    async def _arm_reports(self) -> None:
        """A report whose timer is missing, as after a restored backup, gets it back. One whose time passed waits for its next time."""
        now = time.time()
        missing = await self.db.all("SELECT r.* FROM stat_reports r LEFT JOIN timers t ON t.kind = ? AND t.key = CAST(r.guild_id AS TEXT) || ':' || r.id WHERE t.id IS NULL",
                                    (REPORT_TIMER,))
        for row in missing:
            due = row["next_at"]
            if due is None or due <= now:
                due = next_report_at(row["cadence"], row["weekday"], row["hour"], zone(self.cfg(row["guild_id"])["timezone"]), now)
                await self.db.run("UPDATE stat_reports SET next_at = ? WHERE guild_id = ? AND id = ?", (due, row["guild_id"], row["id"]))
            await self.bot.timers.schedule(REPORT_TIMER, due, {"guild_id": row["guild_id"], "id": row["id"]}, guild_id=row["guild_id"], key=f"{row['guild_id']}:{row['id']}")

    async def _report_due(self, payload: dict, timer: dict) -> None:
        row = await self.db.one("SELECT * FROM stat_reports WHERE guild_id = ? AND id = ?", (payload.get("guild_id"), payload.get("id")))
        if row is None:
            return
        now = time.time()
        following = next_report_at(row["cadence"], row["weekday"], row["hour"], zone(self.cfg(row["guild_id"])["timezone"]), now + 60)
        problem = await self._post_report(row, now)
        await self.db.run("UPDATE stat_reports SET next_at = ?, last_error = ? WHERE guild_id = ? AND id = ?", (following, problem, row["guild_id"], row["id"]))
        await self.bot.timers.schedule(REPORT_TIMER, following, {"guild_id": row["guild_id"], "id": row["id"]}, guild_id=row["guild_id"], key=f"{row['guild_id']}:{row['id']}")

    async def report_embed(self, guild: Any, channel: Any, cadence: str, now: float) -> discord.Embed:
        """The summary a scheduled report posts: the last full week or month in the server's time zone, beside the one before."""
        cfg = self.cfg(guild.id)
        tz = zone(cfg["timezone"])
        end, start, previous = report_period(cadence, tz, now)
        await self.up_to_date()
        numbers = await beyond.summary(self.message_db, guild.id, start, end, previous, views.filters_for(cfg, guild))
        names = await query.people(self.message_db, guild.id, [row[0] for row in numbers["top_members"]])
        stored = await query.channels(self.message_db, guild.id, [row[0] for row in numbers["top_channels"]])
        title = "Weekly statistics" if cadence == "week" else "Monthly statistics"
        last_day = dt.datetime.fromtimestamp(end - 1, tz).date()
        first_day = dt.datetime.fromtimestamp(start, tz).date()
        embed = discord.Embed(title=title, description=f"{day_label(first_day)} to {day_label(last_day)}, {zone_label(tz, end)}", color=0x5865F2)
        hours = numbers["voice"]["now"] / 3600
        embed.add_field(name="Messages", value=f"{numbers['messages']['now']:,} ({beyond.change_text(numbers['messages']['now'], numbers['messages']['before'])})")
        embed.add_field(name="Voice", value=f"{hours:,.1f} hours ({beyond.change_text(numbers['voice']['now'], numbers['voice']['before'])})")
        embed.add_field(name="Active members", value=f"{numbers['active']['now']:,} ({beyond.change_text(numbers['active']['now'], numbers['active']['before'])})")
        members_now, members_then = numbers["members"]["now"], numbers["members"]["before"]
        if members_now is not None:
            moved = members_now - (members_then or members_now)
            embed.add_field(name="Members", value=f"{members_now:,} ({'+' if moved >= 0 else ''}{moved:,})")
        embed.add_field(name="Joined and left", value=f"{numbers['joins']:,} joined, {numbers['leaves']:,} left")
        lines = []
        for rank, (user_id, count, _channels) in enumerate(numbers["top_members"], 1):
            member = guild.get_member(user_id)
            name = member.display_name if member is not None else beyond.kept_names(names, user_id)
            lines.append(f"{rank}. {discord.utils.escape_markdown(name)}: {count:,}")
        if lines:
            embed.add_field(name="Most messages", value="\n".join(lines), inline=False)
        lines = []
        for rank, (channel_id, count, _people) in enumerate(numbers["top_channels"], 1):
            shown = guild.get_channel(channel_id)
            if shown is not None and not _visible_to_viewers(guild, channel, shown):
                name = HIDDEN
            else:
                name = "#" + discord.utils.escape_markdown(shown.name if shown is not None else (stored.get(channel_id) or {}).get("name") or "deleted-channel")
            lines.append(f"{rank}. {name}: {count:,}")
        if lines:
            embed.add_field(name="Busiest channels", value="\n".join(lines), inline=False)
        embed.set_footer(text="Captain Guild statistics")
        return embed

    async def _post_report(self, row: dict, now: float) -> str | None:
        """Posts one report. Returns why not, for the page, when only a person can fix it."""
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            return "The bot isn't in this server."
        if not self.on_in(guild.id):
            return "Statistics are switched off here."
        channel = guild.get_channel(row["channel_id"])
        if channel is None:
            return "The report's channel no longer exists."
        try:
            embed = await self.report_embed(guild, channel, row["cadence"], now)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            return "The bot can't post in that channel. It needs View Channel, Send Messages and Embed Links."
        except discord.HTTPException as error:
            return f"Discord refused the report: {error.text or error.status}"
        await self.db.run("UPDATE stat_reports SET last_at = ?, last_error = NULL WHERE guild_id = ? AND id = ?", (now, row["guild_id"], row["id"]))
        return None

    # ---- insights ----

    def _insight_period(self, req: Any, default_days: int) -> tuple[float, float, str, Any]:
        """The period an insights page asks for: up to every day since Discord began, and day by day only up to DAYS_BY_DAY days, so no answer
        runs to thousands of points for every moderator."""
        cfg = self.cfg(req.guild_id)
        tz = zone(cfg["timezone"])
        now = time.time()
        asked = whole_number(req.query.get("days"), 1_000_000) or default_days
        days = min(asked, int((now - history.DISCORD_EPOCH_MS / 1000) // 86400) + 1)  # nothing statistics hold is older than Discord
        interval = req.query.get("interval") or ("day" if days <= 60 else "week" if days <= 730 else "month")
        if interval not in ("day", "week", "month"):
            raise ValidationError({"interval": "Pick days, weeks or months."})
        if interval == "day" and days > DAYS_BY_DAY:
            raise ValidationError({"interval": f"Pick weeks or months for more than {DAYS_BY_DAY:,} days."})
        period = period_for({"mode": "days", "days": days}, now, interval, tz)
        return period.start, period.end, interval, tz

    async def _insights_moderation(self, req: Any) -> dict:
        start, end, interval, tz = self._insight_period(req, 90)
        logs = self.bot.module("logging")
        readable = await logs._visible_channels(req) if logs is not None else None  # automod's entries show for the channels the log shows them
        out = await beyond.moderation(self.db, self.message_db, req.guild_id, start, end, interval, tz, readable=readable)
        out["log_days"] = self.bot.settings.get(req.guild_id, "logging").get("log_retention_days") if logs is not None else None
        return {**out, "interval": interval, "start": start, "end": end}

    def _statistics_reader(self, req: Any) -> None:
        """Help forum and newcomer numbers are statistics, so they follow the server's choice of who reads statistics on the dashboard."""
        if self.viewer_level(req.guild_id, req.user.id, req.access) is None:
            raise ApiError(403, "You don't have access to that here.")

    async def _insights_forums(self, req: Any) -> dict:
        self._statistics_reader(req)
        start, end, interval, tz = self._insight_period(req, 90)
        solved = self._solved_tags(req.guild_id)
        out = await beyond.forums(self.message_db, req.guild_id, start, end, interval, tz, solved, shown=req.can_see)
        names = await query.channels(self.message_db, req.guild_id, [row["forum_id"] for row in out["forums"]])
        out["forums"] = [{**row, "name": self._name(req, row["forum_id"], (names.get(row["forum_id"]) or {}).get("name"))} for row in out["forums"]]
        return {**out, "interval": interval, "start": start, "end": end, "tracked_since": await self.message_db.scalar("SELECT MIN(created_at) FROM stat_forum_posts WHERE guild_id = ?", (req.guild_id,))}

    async def _insights_newcomers(self, req: Any) -> dict:
        self._statistics_reader(req)
        tz = zone(self.cfg(req.guild_id)["timezone"])
        weeks = whole_number(req.query.get("weeks"), beyond.MAX_WEEKS) or beyond.DEFAULT_WEEKS
        guild = self.bot.get_guild(req.guild_id)
        members_now = {member.id for member in guild.members} if guild is not None and self.bot.members_settled.is_set() else None
        await self.up_to_date()
        cohorts = await beyond.newcomers(self.message_db, req.guild_id, time.time(), tz, weeks=weeks, members_now=members_now)
        return {"weeks": cohorts, "marks": list(beyond.RETENTION_DAYS), "tracked_since": await self.message_db.scalar("SELECT MIN(at) FROM stat_joins WHERE guild_id = ?", (req.guild_id,))}

    # ---- the generic settings form ----

    def settings_refusal(self, guild_id: int, before: dict, after: dict) -> dict[str, str] | None:
        """Merged channels change only on the Statistics data page, which checks that one is empty before it goes. Other features' refusals
        come first, through the classes after this one."""
        found = super().settings_refusal(guild_id, before, after) or {}
        kept_before = {row["id"]: row for row in before.get("aggregates") or []}
        kept_after = {row["id"]: row for row in after.get("aggregates") or []}
        if set(kept_before) - set(kept_after):
            found["aggregates"] = "Delete a merged channel on the Statistics data page, which checks that it holds nothing first."
        else:
            try:
                self._check_aggregates(list(kept_after.values()), "aggregates")
            except ValidationError as error:
                found.update({"aggregates": next(iter(error.fields.values()))})
        return found or None


def merge_id_room() -> int:
    return 2 ** 38


def _api_json(data: Any, status: int, headers: dict[str, str] | None = None) -> web.Response:
    return web.Response(text=json.dumps(data, separators=(",", ":")), status=status, content_type="application/json",
                        headers={"Cache-Control": "no-store", **(headers or {})})


def next_report_at(cadence: str, weekday: int, hour: int, tz: Any, now: float) -> float:
    """The next time a report goes out after `now`: the hour on that weekday each week, or on the first of each month, in the server's zone."""
    local = dt.datetime.fromtimestamp(now, tz)
    if cadence == "week":
        day = local.date() + dt.timedelta(days=(weekday - local.weekday()) % 7)
        for _ in range(3):
            moment = dt.datetime(day.year, day.month, day.day, hour, tzinfo=tz).timestamp()
            if moment > now:
                return moment
            day += dt.timedelta(days=7)
        return now + 7 * 86400
    day = local.date().replace(day=1)
    for _ in range(3):
        moment = dt.datetime(day.year, day.month, day.day, hour, tzinfo=tz).timestamp()
        if moment > now:
            return moment
        day = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return now + 31 * 86400


def report_period(cadence: str, tz: Any, now: float) -> tuple[float, float, float]:
    """(end, start, start of the period before) for a report sent now: the seven days up to today's start, or last calendar month."""
    if cadence == "week":
        end = floor_bucket(now, "day", tz)
        today = dt.datetime.fromtimestamp(end, tz).date()
        start = floor_bucket(dt.datetime.combine(today - dt.timedelta(days=7), dt.time(12), tz).timestamp(), "day", tz)
        previous = floor_bucket(dt.datetime.combine(today - dt.timedelta(days=14), dt.time(12), tz).timestamp(), "day", tz)
        return end, start, previous
    end = floor_bucket(now, "month", tz)
    start = floor_bucket(end - 1, "month", tz)
    previous = floor_bucket(start - 1, "month", tz)
    return end, start, previous

