"""Channel archive: copy a text or forum channel, threads included, into another channel or thread.

Each message goes out through the bot's webhook under its author's name and picture, with its text, embeds, files, stickers and a poll's
results, pinging nobody. Markers under the name Archive Marker say where the parent channel and each thread start and end, and every 100
messages a progress line says how far the copy has got. A copy goes at one message every two seconds, the pace Discord allows a webhook.

A copy is a stored job. It saves its place after every message, in a cursor per source and target, so a restart carries on where it stopped
and a later copy into the same target brings only what's new. A stop waits for a message or marker already on its way and saves its place,
so nothing is posted twice. One copy runs at a time in a server, and the next waits its turn.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import time
from typing import Any, Union

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.module import Module, requires
from ..core.ops import OpsError
from ..core.validate import Field, Schema, ValidationError, boolean
from ..web.api import ApiError

log = logging.getLogger(__name__)

MARKER = "Archive Marker"
PAUSE_SECONDS = 2.0
PER_MINUTE = 30
PROGRESS_EVERY = 100
MESSAGE_LIMIT = 2000
DESCRIPTION_LIMIT = 4096
EMBEDS_PER_MESSAGE = 10
FILES_PER_MESSAGE = 10
FAILURES_KEPT = 50
JOBS_SHOWN = 20
MAX_QUEUED = 10  # copies running or waiting in one server
JOBS_KEPT = 50  # finished and stopped copies a server keeps on its page
STOP_SECONDS = 5.0  # how long a stop waits for a post already on its way
COPIED_TYPES = (discord.MessageType.default, discord.MessageType.reply)
STICKER_EXTENSIONS = {discord.StickerFormatType.png: "png", discord.StickerFormatType.apng: "gif", discord.StickerFormatType.lottie: "json", discord.StickerFormatType.gif: "gif"}
NEEDS_TEXT = "Copying a channel needs the Message Content intent, which this bot doesn't have. Whoever hosts it turns it on in the Discord Developer Portal."
SOURCE_GONE = "The source channel is gone."
TARGET_GONE = "The target channel or thread is gone."
CHANNEL_DELETED = "The source or target channel was deleted."
UNEXPECTED = "Something went wrong with the copy. It's been logged. Resume it on the Channel archive page to try again."
FORWARDED = "-# Forwarded"
NOTHING_TO_COPY = "Nothing in it could be copied."


def eta_text(total: int) -> str:
    """How long a copy takes at 30 messages a minute, as the old estimate wrote it."""
    minutes_total = total / PER_MINUTE
    hours = math.floor(minutes_total / 60)
    minutes = math.floor(minutes_total % 60)
    seconds = math.floor((minutes_total * 60) % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"


def poll_embed(poll: Any) -> discord.Embed:
    """A poll's results at the time of the copy, since a copied poll can't be voted on."""
    embed = discord.Embed(title=f"Poll: {poll.question}"[:256])
    total = sum(answer.vote_count for answer in poll.answers)
    for answer in poll.answers[:25]:
        share = answer.vote_count / total * 100 if total else 0.0
        embed.add_field(name=(answer.text or "-")[:256], value=f"Votes: {answer.vote_count} ({share:.1f}%)", inline=False)
    return embed


def created_of(thread: Any) -> Any:
    """When a thread was made. Threads from before 2022 don't say, and their ID does."""
    return thread.created_at or discord.utils.snowflake_time(thread.id)


class ChannelArchive(Module):
    key = "channelarchive"
    category = "Utilities"
    command_pack = True
    schema = Schema({"enabled": Field(False, boolean)})
    child_links = {"archive_units": {"job_id": "archive_jobs.id"}}
    row_limits = {"archive_jobs": JOBS_KEPT + MAX_QUEUED}
    migrations = [
        """
        CREATE TABLE archive_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            source_channel_id INTEGER NOT NULL,
            target_channel_id INTEGER NOT NULL,
            source_name TEXT NOT NULL,
            target_name TEXT NOT NULL,
            state TEXT NOT NULL,
            stage TEXT,
            total INTEGER NOT NULL DEFAULT 0,
            copied INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            failures TEXT,
            note TEXT,
            started_by INTEGER,
            started_by_name TEXT,
            created_at REAL NOT NULL,
            finished_at REAL
        );
        CREATE INDEX archive_jobs_guild ON archive_jobs (guild_id, id DESC);
        CREATE INDEX archive_jobs_state ON archive_jobs (state);
        CREATE TABLE archive_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_channel_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            created TEXT,
            first INTEGER NOT NULL,
            total INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL
        );
        CREATE INDEX archive_units_job ON archive_units (job_id, position);
        CREATE TABLE archive_cursors (
            guild_id INTEGER NOT NULL,
            source_channel_id INTEGER NOT NULL,
            target_channel_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            last_message_id INTEGER,
            PRIMARY KEY (guild_id, source_channel_id, target_channel_id, kind)
        );
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._jobs: dict[int, asyncio.Task] = {}
        self._again: set[int] = set()
        self._locks: dict[int, asyncio.Lock] = {}
        self._askers: dict[int, Any] = {}
        self._sleep = asyncio.sleep
        self._in_flight: set[asyncio.Future] = set()

    async def cog_unload(self) -> None:
        for task in self._jobs.values():
            task.cancel()
        await self._land_in_flight()

    async def before_restore(self, guild_id: int) -> None:
        for row in await self.db.all("SELECT id FROM archive_jobs WHERE guild_id = ? AND state = 'running'", (guild_id,)):
            task = self._jobs.pop(row["id"], None)
            if task is not None:
                task.cancel()
        await self._land_in_flight()

    async def _land_in_flight(self) -> None:
        """A message or marker on its way when a copy is stopped goes out and its place is saved, so the copy never posts it twice."""
        if self._in_flight:
            await asyncio.wait(set(self._in_flight), timeout=STOP_SECONDS)
            for task in self._in_flight:
                task.cancel()

    async def _unstoppable(self, work: Any) -> Any:
        """Runs one post and the record that it went out where a stop can't come between them."""
        task = asyncio.ensure_future(work)
        self._in_flight.add(task)
        task.add_done_callback(self._settled)
        return await asyncio.shield(task)

    def _settled(self, task: asyncio.Future) -> None:
        self._in_flight.discard(task)
        if not task.cancelled():
            task.exception()  # a stop may leave nobody waiting for its answer

    async def on_guild_restored(self, guild_id: int) -> None:
        """A copy the backup caught running isn't running now, so it waits for someone to resume it."""
        await self.db.run("UPDATE archive_jobs SET state = 'stopped', note = ? WHERE guild_id = ? AND state = 'running'", ("The backup put this copy back while it was running. Resume it to carry on.", guild_id))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for row in await self.db.all("SELECT id FROM archive_jobs WHERE state = 'running' ORDER BY id"):
            self._start(row["id"])

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        await self._gone(channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        await self._gone(payload.guild_id, payload.thread_id)

    async def _gone(self, guild_id: int, channel_id: int) -> None:
        """A deleted channel or thread takes its places along. A copy from or into it stops, and one that only read a thread of it carries on."""
        await self.db.run("DELETE FROM archive_cursors WHERE guild_id = ? AND (source_channel_id = ? OR target_channel_id = ?)", (guild_id, channel_id, channel_id))
        await self.db.run("UPDATE archive_jobs SET state = 'stopped', stage = NULL, note = ?, finished_at = ? WHERE guild_id = ? AND state = 'running' AND (source_channel_id = ? OR target_channel_id = ?)",
                          (CHANNEL_DELETED, time.time(), guild_id, channel_id, channel_id))

    # ---- starting a copy ----

    def refusal(self, member: Any, source: Any, target: Any) -> str | None:
        """Why this person can't copy this source into this target, or None. The bot reads one and posts in the other for them, under other people's names."""
        if not self.bot.reads_message_text:
            return NEEDS_TEXT
        if target.id == source.id or getattr(target, "parent_id", None) == source.id:
            return "The target can't be the source or one of its threads."
        return access.read_refusal(member, source) or access.post_refusal(member, target)

    async def start(self, guild_id: int, source: Any, target: Any, actor: Any, asker: Any = None, *, run: bool = True) -> int:
        """Stores a copy and, unless told not to, starts it. Raises ValidationError when this pair is already being copied."""
        busy = await self.db.scalar("SELECT id FROM archive_jobs WHERE guild_id = ? AND source_channel_id = ? AND target_channel_id = ? AND state = 'running'", (guild_id, source.id, target.id))
        if busy:
            raise ValidationError({"target_channel_id": f"A copy from <#{source.id}> into <#{target.id}> is already running."})
        if await self.db.scalar("SELECT COUNT(*) FROM archive_jobs WHERE guild_id = ? AND state = 'running'", (guild_id,)) >= MAX_QUEUED:
            raise ValidationError({"target_channel_id": f"This server has {MAX_QUEUED} copies running or waiting, the most it can. Wait for one to finish, or stop one on the Channel archive page."})
        job_id = await self.db.insert(
            "INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, stage, started_by, started_by_name, created_at) VALUES (?, ?, ?, ?, ?, 'running', 'Waiting its turn', ?, ?, ?)",
            (guild_id, source.id, target.id, source.name, target.name, actor.id, actor.name, time.time()),
        )
        await self.bot.activity_log.record(guild_id, actor, "channelarchive.start", detail=f"#{source.name} to #{target.name}")
        if asker is not None:
            self._askers[job_id] = asker
        if run:
            self._start(job_id)
        return job_id

    def _start(self, job_id: int) -> None:
        running = self._jobs.get(job_id)
        if running is not None and not running.done():
            self._again.add(job_id)  # it may be on its way out after a stop, so it looks at the job once more when it ends
            return
        self._jobs[job_id] = asyncio.ensure_future(self._job_task(job_id))

    async def finish_jobs(self) -> None:
        """Wait for every copy under way. The tests use this."""
        while self._jobs:
            await asyncio.gather(*list(self._jobs.values()), return_exceptions=True)

    async def _job_task(self, job_id: int) -> None:
        try:
            await self._run(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Channel copy %s failed", job_id)
            await self.db.run("UPDATE archive_jobs SET state = 'stopped', note = ?, finished_at = ? WHERE id = ? AND state = 'running'", (UNEXPECTED, time.time(), job_id))
            await self._answer(job_id, UNEXPECTED)
        finally:
            self._askers.pop(job_id, None)
            if self._jobs.get(job_id) is asyncio.current_task():
                del self._jobs[job_id]
            if job_id in self._again:
                self._again.discard(job_id)
                self._start(job_id)

    async def _job(self, job_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM archive_jobs WHERE id = ?", (job_id,))

    async def _set(self, job_id: int, **changes: Any) -> None:
        names = ", ".join(f"{name} = ?" for name in changes)
        await self.db.run(f"UPDATE archive_jobs SET {names} WHERE id = ?", (*changes.values(), job_id))

    async def _run(self, job_id: int) -> None:
        job = await self._job(job_id)
        if job is None:
            return
        async with self._locks.setdefault(job["guild_id"], asyncio.Lock()):  # one copy at a time a server, so two can't share the bot's pace
            job = await self._job(job_id)
            if job is None or job["state"] != "running":
                return
            if not self.bot.reads_message_text:  # every message would come with no text, and the copy would pass them all for good
                await self._stop(job_id, NEEDS_TEXT)
                return
            guild = self.bot.get_guild(job["guild_id"])
            source = guild.get_channel(job["source_channel_id"]) if guild is not None else None
            target = guild.get_channel_or_thread(job["target_channel_id"]) if guild is not None else None
            if target is None and guild is not None:
                target = await self._fetch_thread(guild, job["target_channel_id"])
            if source is None or target is None:
                await self._stop(job_id, SOURCE_GONE if source is None else TARGET_GONE)
                return
            try:
                await self.bot.webhooks.hook(target)  # first, as before: without the webhook nothing can be copied
                if not await self.db.scalar("SELECT COUNT(*) FROM archive_units WHERE job_id = ?", (job_id,)):
                    total = await self._plan(job, guild, source)
                    if total == 0:
                        await self._finish(job_id, target, "No new messages to sync.", marker=False)
                        return
                    await self._tell(job_id, target, f"Found {total} messages to sync. Estimated time to completion: {eta_text(total)}.")
                await self._copy(job_id, guild, source, target)
            except OpsError as error:
                await self._stop(job_id, f"An error occurred: {error}")
                await self._tell(job_id, target, f"An error occurred: {error}")

    async def _prune(self, job_id: int) -> None:
        """A finished copy's plan isn't needed anymore, and a server keeps its newest 50 finished and stopped copies. The places stay, since they're what a later copy carries on from."""
        job = await self._job(job_id)
        if job is None:
            return
        guild_id = job["guild_id"]
        async with self.db.transaction():
            await self.db.run("DELETE FROM archive_units WHERE job_id = ?", (job_id,))
            await self.db.run(
                "DELETE FROM archive_jobs WHERE guild_id = ? AND state != 'running' AND id NOT IN (SELECT id FROM archive_jobs WHERE guild_id = ? AND state != 'running' ORDER BY id DESC LIMIT ?)",
                (guild_id, guild_id, JOBS_KEPT),
            )
            await self.db.run("DELETE FROM archive_units WHERE guild_id = ? AND job_id NOT IN (SELECT id FROM archive_jobs WHERE guild_id = ?)", (guild_id, guild_id))

    async def _fetch_thread(self, guild: Any, channel_id: int) -> Any:
        try:
            found = await guild.fetch_channel(channel_id)
        except (discord.HTTPException, discord.InvalidData):
            return None
        return found if isinstance(found, discord.Thread) and found.guild.id == guild.id else None

    async def _stop(self, job_id: int, note: str) -> None:
        await self._set(job_id, state="stopped", stage=None, note=note, finished_at=time.time())

    async def _tell(self, job_id: int, target: Any, text: str) -> None:
        """Tells whoever started the copy, as an answer to their command, or as a marker in the target once that can't go out, as before."""
        if not await self._answer(job_id, text):
            await self._marker(target, text)

    async def _answer(self, job_id: int, text: str) -> bool:
        """Answers the command that started the copy. A slash command's answers end after 15 minutes, and then discord.py would post the text in
        the command's channel for everyone, so nothing goes. A typed command answers in its channel, as it always does. True when it went out."""
        asker = self._askers.get(job_id)
        if asker is None:
            return False
        interaction = getattr(asker, "interaction", None)
        if interaction is not None and interaction.is_expired():
            return False
        try:
            await asker.send(text, ephemeral=True)
        except discord.HTTPException as error:
            log.info("Could not answer the copy command for job %s: %s", job_id, error)
            return False
        return True

    async def _marker(self, target: Any, text: str) -> None:
        try:
            await self.bot.webhooks.send(target, name=MARKER, content=text)
        except OpsError as error:
            log.info("Could not post an archive marker in %s: %s", target.id, error)

    # ---- finding what to copy ----

    async def _threads(self, guild: Any, source: Any) -> list[Any]:
        """Every thread of the source: the active ones the bot holds, and every page of archived ones, public and private."""
        found: dict[int, Any] = {thread.id: thread for thread in getattr(source, "threads", [])}
        try:
            async for thread in source.archived_threads(limit=None):
                found.setdefault(thread.id, thread)
        except discord.HTTPException as error:
            log.info("Could not list the archived threads of %s: %s", source.id, error)
        if isinstance(source, discord.TextChannel):
            try:
                async for thread in source.archived_threads(private=True, limit=None):
                    found.setdefault(thread.id, thread)
            except discord.Forbidden:  # without Manage Threads, Discord lists only the private threads the bot has joined
                try:
                    async for thread in source.archived_threads(private=True, joined=True, limit=None):
                        found.setdefault(thread.id, thread)
                except discord.HTTPException as error:
                    log.info("Could not list the joined private threads of %s: %s", source.id, error)
            except discord.HTTPException as error:
                log.info("Could not list the private threads of %s: %s", source.id, error)
        return sorted(found.values(), key=created_of)

    async def _cursor(self, guild_id: int, source_channel_id: int, target_channel_id: int, kind: str) -> dict | None:
        return await self.db.one("SELECT * FROM archive_cursors WHERE guild_id = ? AND source_channel_id = ? AND target_channel_id = ? AND kind = ?", (guild_id, source_channel_id, target_channel_id, kind))

    async def _count(self, place: Any, after: int | None) -> int:
        count = 0
        async for message in place.history(limit=None, after=discord.Object(after) if after else None, oldest_first=True):
            if message.type in COPIED_TYPES:
                count += 1
        return count

    async def _starter(self, guild: Any, user_id: int | None) -> Any:
        """Whoever started the copy, from the member list or, while that's still filling after a restart, from Discord. None when they've
        left. Raises OpsError when Discord can't say, which stops the copy for a resume."""
        member = guild.get_member(user_id) if user_id else None
        if member is not None or not user_id:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException as error:
            raise OpsError(f"Discord couldn't say whether the person who started this copy is still in the server: {error.text or error.status}") from error

    async def _plan(self, job: dict, guild: Any, source: Any) -> int:
        """Lists what to copy, in order, and counts it. Only what the person who started the copy can read goes in."""
        await self._set(job["id"], stage="Counting messages")
        member = await self._starter(guild, job["started_by"])
        units: list[tuple[Any, str, str, str | None]] = []
        if isinstance(source, discord.TextChannel):
            units.append((source, "parent", source.name, None))
        left_out = private_left_out = 0
        for thread in await self._threads(guild, source):
            if thread.id == job["target_channel_id"]:
                continue
            if member is None:
                if thread.type == discord.ChannelType.private_thread:  # nobody is left to ask whether they could read it
                    private_left_out += 1
                    continue
            elif not access.can_read(member, thread):
                left_out += 1
                continue
            units.append((thread, "thread", thread.name, str(created_of(thread))))
        planned: list[tuple] = []
        for position, (place, kind, name, created) in enumerate(units):
            cursor = await self._cursor(job["guild_id"], place.id, job["target_channel_id"], kind)
            try:
                count = await self._count(place, cursor["last_message_id"] if cursor else None)
            except discord.Forbidden:
                left_out += 1
                continue
            except discord.HTTPException as error:
                raise OpsError(f"Discord refused to read #{name}: {error.text or error.status}") from error
            planned.append((job["guild_id"], job["id"], position, place.id, kind, name, created, int(cursor is None), count))
        total = sum(row[-1] for row in planned)
        notes = [f"Left out {left_out} threads that the person who started this can't read."] if left_out else []
        if private_left_out:
            notes.append(f"Left out {private_left_out} private threads, since the person who started this copy has left the server.")
        note = " ".join(notes) or None
        async with self.db.transaction():  # all of the plan or none of it, so a restart during the count plans again from nothing
            for row in planned:
                await self.db.run("INSERT INTO archive_units (guild_id, job_id, position, source_channel_id, kind, name, created, first, total, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')", row)
            await self.db.run("UPDATE archive_jobs SET total = ?, note = ? WHERE id = ?", (total, note, job["id"]))
        return total

    # ---- copying ----

    async def _copy(self, job_id: int, guild: Any, source: Any, target: Any) -> None:
        for unit in await self.db.all("SELECT * FROM archive_units WHERE job_id = ? AND state != 'done' ORDER BY position", (job_id,)):
            job = await self._job(job_id)
            if job is None or job["state"] != "running":
                return
            place = source if unit["kind"] == "parent" else (guild.get_channel_or_thread(unit["source_channel_id"]) or await self._fetch_thread(guild, unit["source_channel_id"]))
            if place is None:
                await self.db.run("UPDATE archive_units SET state = 'done' WHERE id = ?", (unit["id"],))
                continue
            if not await self._copy_unit(job, unit, guild, place, target):
                return
        job = await self._job(job_id)
        if job is not None and job["state"] == "running":
            status = f"Sync complete! Copied {job['copied']} messages."
            if job["failed"]:
                status += f" {job['failed']} failures. The Channel archive page links each one."
            await self._finish(job_id, target, status)

    async def _copy_unit(self, job: dict, unit: dict, guild: Any, place: Any, target: Any) -> bool:
        """One parent channel or thread, from its cursor on. False when the copy was stopped partway."""
        kind = unit["kind"]
        if unit["state"] == "waiting" and not unit["total"] and (kind == "parent" or not unit["first"]):
            await self.db.run("UPDATE archive_units SET state = 'done' WHERE id = ?", (unit["id"],))
            return True
        if unit["state"] == "waiting":
            await self._unstoppable(self._open_unit(job, unit, target))
        await self._set(job["id"], stage=f"Copying #{unit['name']}")
        cursor = await self._cursor(job["guild_id"], unit["source_channel_id"], job["target_channel_id"], kind)
        after = cursor["last_message_id"] if cursor else None
        async for message in place.history(limit=None, after=discord.Object(after) if after else None, oldest_first=True):
            if message.type not in COPIED_TYPES:
                continue
            state = await self.db.scalar("SELECT state FROM archive_jobs WHERE id = ?", (job["id"],))
            if state != "running":
                return False
            try:
                posted = await self._unstoppable(self._copy_one(job, unit, guild, message, target))
            except OpsError as error:
                if isinstance(error.__cause__, (discord.Forbidden, discord.DiscordServerError)):
                    raise  # a missing permission or Discord being down stops the copy where it is, so it resumes from there once that's over
                await self._failed(job["id"], message, error)
            else:
                if not posted:  # counted and linked, so it can be found by hand, as when Discord refuses one
                    await self._failed(job["id"], message, NOTHING_TO_COPY)
                    continue
                now = await self._job(job["id"])
                if now["copied"] % PROGRESS_EVERY == 0:
                    await self._marker(target, f"Progress: Copied {now['copied']}/{now['total']} messages.")
            await self._sleep(PAUSE_SECONDS)
        await self._unstoppable(self._close_unit(unit, target))
        return True

    async def _open_unit(self, job: dict, unit: dict, target: Any) -> None:
        """A part's start marker and the record that it went out."""
        kind = unit["kind"]
        if unit["first"]:
            await self._marker(target, f"**Start of parent channel archive: {unit['name']}**" if kind == "parent" else f"**Start of thread archive: {unit['name']} (ID: {unit['source_channel_id']}, created: {unit['created']})**")
        elif unit["total"]:
            await self._marker(target, "**Additional messages in parent channel**" if kind == "parent" else f"**Additional messages in thread: {unit['name']}**")
        await self.db.run("INSERT OR IGNORE INTO archive_cursors (guild_id, source_channel_id, target_channel_id, kind, last_message_id) VALUES (?, ?, ?, ?, NULL)",
                          (job["guild_id"], unit["source_channel_id"], job["target_channel_id"], kind))
        await self.db.run("UPDATE archive_units SET state = 'copying' WHERE id = ?", (unit["id"],))

    async def _copy_one(self, job: dict, unit: dict, guild: Any, message: Any, target: Any) -> bool:
        """One message and its saved place. False when it held nothing to post."""
        posted = await self.copy_message(guild, message, target)
        await self.db.run("UPDATE archive_cursors SET last_message_id = ? WHERE guild_id = ? AND source_channel_id = ? AND target_channel_id = ? AND kind = ?",
                          (message.id, job["guild_id"], unit["source_channel_id"], job["target_channel_id"], unit["kind"]))
        if posted:
            await self.db.run("UPDATE archive_jobs SET copied = copied + 1 WHERE id = ?", (job["id"],))
        return posted

    async def _close_unit(self, unit: dict, target: Any) -> None:
        """A part's end marker and the record that the part is done."""
        if unit["first"]:
            await self._marker(target, "**End of parent channel archive**" if unit["kind"] == "parent" else f"**End of thread archive: {unit['name']}**")
        await self.db.run("UPDATE archive_units SET state = 'done' WHERE id = ?", (unit["id"],))

    async def _failed(self, job_id: int, message: Any, error: Any) -> None:
        """A message Discord refused for what it held, or one with nothing in it to copy, is counted and linked on the page, and the copy goes
        on. The next copied message moves the cursor past it, as before. A missing permission or Discord being down stops the copy at the
        message instead."""
        log.info("Could not copy message %s: %s", message.id, error)
        job = await self._job(job_id)
        failures = json.loads(job["failures"]) if job and job["failures"] else []
        failures = (failures + [{"url": message.jump_url, "error": str(error)[:200]}])[-FAILURES_KEPT:]
        await self.db.run("UPDATE archive_jobs SET failed = failed + 1, failures = ? WHERE id = ?", (json.dumps(failures), job_id))

    async def _finish(self, job_id: int, target: Any, status: str, *, marker: bool = True) -> None:
        await self._set(job_id, state="done", stage=None, finished_at=time.time())
        await self._prune(job_id)
        if marker:
            await self._marker(target, status)
        if not await self._answer(job_id, status) and not marker:
            await self._marker(target, status)

    async def copy_message(self, guild: Any, message: Any, target: Any) -> bool:
        """One message under its author's name and picture: its text, embeds, files, stickers and a poll's results. A forward brings what it
        forwarded under a Forwarded line, so the words aren't taken for the forwarder's own. Nobody is pinged. False when it holds nothing to
        post. Raises OpsError with a sentence."""
        source = message
        snapshots = getattr(message, "message_snapshots", None) or []
        forwarded = bool(snapshots) and not (message.content or message.embeds or message.attachments or message.stickers)
        if forwarded:
            source = snapshots[0]  # Discord keeps a forward's text, embeds and files apart from the message itself
        content = source.content or None
        embeds = list(source.embeds)
        if message.poll:
            embeds.append(poll_embed(message.poll))
        room = getattr(guild, "filesize_limit", 10 * 1024 * 1024)  # what one message can carry, and so the most held in memory at once
        files: list[discord.File] = []
        for attachment in source.attachments[:FILES_PER_MESSAGE]:
            if attachment.size > room:
                continue
            room -= attachment.size
            try:
                files.append(await attachment.to_file(spoiler=attachment.is_spoiler()))
            except discord.HTTPException:
                continue  # a file Discord won't hand over is left out, as before
        for sticker in source.stickers:
            try:
                data = await self.bot.http.get_from_cdn(sticker.url)
            except (discord.HTTPException, ValueError):
                continue
            files.append(discord.File(io.BytesIO(data), filename=f"sticker.{STICKER_EXTENSIONS.get(sticker.format, 'png')}"))
        if not (content or embeds or files):
            return False
        if forwarded:
            content = f"{FORWARDED}\n{content}" if content else FORWARDED
        if content and len(content) > MESSAGE_LIMIT:
            embeds.insert(0, discord.Embed(description=content[:DESCRIPTION_LIMIT]))
            content = None
        author = message.author
        await self.bot.webhooks.send(target, name=author.display_name, avatar_url=author.display_avatar.url, content=content, embeds=embeds[:EMBEDS_PER_MESSAGE], files=files[:FILES_PER_MESSAGE])
        return True

    # ---- the command ----

    @commands.hybrid_command(name="copy_channel", description="Sync messages from source to target channel/thread with webhooks.")
    @commands.guild_only()
    @requires("manage_webhooks")
    @app_commands.describe(source="The source text or forum channel to copy from", target="The target channel, thread, or forum post to copy to")
    async def copy_channel(self, ctx: commands.Context, source: Union[discord.TextChannel, discord.ForumChannel], target: Union[discord.TextChannel, discord.Thread]) -> None:
        refusal = self.refusal(ctx.author, source, target)
        if refusal:
            await ctx.fail(refusal)
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        try:
            job_id = await self.start(ctx.guild.id, source, target, self.bot.actor(ctx.author), asker=ctx, run=False)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return
        waiting = await self.db.scalar("SELECT COUNT(*) FROM archive_jobs WHERE guild_id = ? AND state = 'running'", (ctx.guild.id,))
        after = " It starts after the copy before it." if waiting > 1 else ""
        try:
            await ctx.send(f"Copying {source.mention} into {target.mention}. The count of messages follows here.{after}", ephemeral=True)
        finally:
            self._start(job_id)

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/channelarchive", self._list_endpoint)
        router.post("/channelarchive", self._start_endpoint)
        router.post(r"/channelarchive/{job_id:\d+}/stop", self._stop_endpoint)
        router.post(r"/channelarchive/{job_id:\d+}/resume", self._resume_endpoint)

    async def _visible(self, req: Any, job: dict) -> bool:
        return req.can_read(job["source_channel_id"]) and req.can_see(job["target_channel_id"])

    async def _list_endpoint(self, req: Any) -> dict:
        rows = await self.db.all("SELECT * FROM archive_jobs WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (req.guild_id, JOBS_SHOWN))
        jobs = []
        for row in rows:
            if not await self._visible(req, row):
                continue
            jobs.append({**row, "failures": json.loads(row["failures"]) if row["failures"] else []})
        return {"jobs": jobs, "reads_text": self.bot.reads_message_text}

    def _pair(self, req: Any, source_value: Any, target_value: Any) -> tuple[Any, Any]:
        """The source and target, once this person may copy the one into the other: read the source and post in the target themselves.
        Start and Resume both ask, since either makes the bot post other people's messages under their names."""
        req.require_permission("manage_webhooks")
        source_channel_id = req.channel(source_value, "source_channel_id")
        if not req.can_read(source_channel_id):
            raise ValidationError({"source_channel_id": "You can't read the messages in that channel."})
        target_channel_id = req.channel(target_value, "target_channel_id", posting=True)
        guild = self.bot.get_guild(req.guild_id)
        source = guild.get_channel(source_channel_id) if guild is not None else None
        target = guild.get_channel_or_thread(target_channel_id) if guild is not None else None
        if not isinstance(source, (discord.TextChannel, discord.ForumChannel)):
            raise ValidationError({"source_channel_id": "Pick a text channel or a forum."})
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            raise ValidationError({"target_channel_id": "Pick a text channel or a thread."})
        member = guild.get_member(req.user.id)
        refusal = self.refusal(member, source, target) if member is not None else None
        if refusal:
            raise ValidationError({"target_channel_id": refusal})
        return source, target

    async def _start_endpoint(self, req: Any) -> dict:
        form = await req.json()
        source, target = self._pair(req, form.get("source_channel_id"), form.get("target_channel_id"))
        job_id = await self.start(req.guild_id, source, target, req.actor)
        return {"id": job_id}

    async def _owned_job(self, req: Any) -> dict:
        job = await self.db.one("SELECT * FROM archive_jobs WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("job_id")))
        if job is None or not await self._visible(req, job):
            raise ApiError(404, "That copy isn't there anymore.")
        return job

    async def _stop_endpoint(self, req: Any) -> dict:
        job = await self._owned_job(req)
        if not await self.db.run("UPDATE archive_jobs SET state = 'stopped', stage = NULL, note = ?, finished_at = ? WHERE guild_id = ? AND id = ? AND state = 'running'",
                                 (f"Stopped by {req.actor.name}.", time.time(), req.guild_id, job["id"])):
            raise ApiError(409, "That copy isn't running.")
        await req.record("channelarchive.stop", detail=f"#{job['source_name']} to #{job['target_name']}")
        return {"ok": True}

    async def _resume_endpoint(self, req: Any) -> dict:
        job = await self._owned_job(req)
        if not self.bot.reads_message_text:
            raise ApiError(409, NEEDS_TEXT)
        self._pair(req, job["source_channel_id"], job["target_channel_id"])
        passed = await self.db.scalar(
            "SELECT COUNT(*) FROM archive_jobs WHERE guild_id = ? AND source_channel_id = ? AND target_channel_id = ? AND id != ? AND (state = 'running' OR id > ?)",
            (req.guild_id, job["source_channel_id"], job["target_channel_id"], job["id"], job["id"]),
        )
        if passed:  # its plan would mark the start and end of every part again, and two copies would post into one place
            raise ApiError(409, f"A newer copy from #{job['source_name']} into #{job['target_name']} is running or has run since this one. Start a new copy to bring what's new.")
        if not await self.db.run("UPDATE archive_jobs SET state = 'running', note = NULL, finished_at = NULL WHERE guild_id = ? AND id = ? AND state = 'stopped'", (req.guild_id, job["id"])):
            raise ApiError(409, "That copy isn't stopped.")
        await req.record("channelarchive.resume", detail=f"#{job['source_name']} to #{job['target_name']}")
        self._start(job["id"])
        return {"ok": True}

    async def seed_demo(self, guild_id: int) -> None:
        """Sample copies for the demo dashboard."""
        info = self.bot.ops.describe_guild(guild_id) or {}
        channels = {row["name"]: row["id"] for row in info.get("channels", [])}
        if "help-forum" not in channels or "general" not in channels:
            return
        now = time.time()
        await self.db.run(
            "INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, total, copied, failed, failures, started_by, started_by_name, created_at, finished_at) "
            "VALUES (?, ?, ?, 'help-forum', 'general', 'done', 412, 411, 1, ?, ?, 'Demo Admin', ?, ?)",
            (guild_id, channels["help-forum"], channels["general"], json.dumps([{"url": f"https://discord.com/channels/{guild_id}/{channels['help-forum']}/900000000000400001", "error": "Discord refused the post: Request entity too large"}]), guild_id + 2, now - 86400, now - 72000),
        )
        await self.db.run(
            "INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, stage, total, copied, started_by, started_by_name, created_at) "
            "VALUES (?, ?, ?, 'general', 'help-forum', 'stopped', NULL, 1800, 640, ?, 'Demo Admin', ?)",
            (guild_id, channels["general"], channels["help-forum"], guild_id + 2, now - 3600),
        )
        await self.bot.settings.save(guild_id, self.key, {"enabled": True})
