"""Posting and editing messages: the editor's backend, the library of everything posted, scheduled posts, and reposting another bot's messages.

Discord lets a bot edit only what it wrote, so moving a post off another bot means reposting it here. An adoption is a stored job that
walks its messages in order and saves its place after every step, so a restart carries on where it stopped and never posts a message twice.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, delivery, images, messages
from ..core.module import Module, requires
from ..core.ops import MessageGone, OpsError, SendUnknown
from ..core.settings import Actor
from ..core.validate import Invalid, ValidationError, hex_color, number, snowflake
from ..web.api import ApiError, read_json

log = logging.getLogger(__name__)

MESSAGE_LINK = re.compile(r"https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
PASTE_LINK = re.compile(r"https?://(?:www\.)?(pastebin\.com|starb\.in)/(?:raw/)?([A-Za-z0-9]+)(?:\.[a-z]+)?/?$")
CHANNEL_AT_END = re.compile(r"\s*<#(\d+)>\s*$")
# A channel at the very start or the very end of a typed line, as a mention or an ID, next to the JSON or the link.
CHANNEL_NEXT_TO_JSON = re.compile(r"^(?:<#(\d+)>|(\d{17,20}))(?:\s+|$)|\s+(?:<#(\d+)>|(\d{17,20}))$")

SCHEDULED = "embeds.scheduled"
MAX_VERSIONS = 200
MAX_ADOPT = 100
MAX_LISTED = 1000
MAX_PASTE_BYTES = 256 * 1024
FARTHEST_AHEAD = 366 * 86400
SCHEDULE_TRIES = 4
MAX_SCHEDULED = 200
MAX_SAVED = 200
MAX_POST_FILES = 5
MAX_POST_FILE_BYTES = 8 * 1024 * 1024
SEND_WAIT_SECONDS = 1.0  # how long a timer waits on a send before moving on, so one server's flood of posts can't hold up everyone's unmutes
SEND_WATCH_SECONDS = 3600
TIME_PASSED = "Its time passed before this backup came back. Post it again if it should go out."
WAS_SENDING = "The bot was posting this when the backup was made. Check the channel, and post it again if it isn't there."


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def like_pattern(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _id_field(value: Any, field: str) -> int:
    try:
        return snowflake(value)
    except Invalid as error:
        raise ValidationError({field: str(error)}) from error


def _when_field(value: Any) -> float:
    try:
        when = number()(value)
    except Invalid as error:
        raise ValidationError({"at": "Pick a date and time."}) from error
    now = time.time()
    if when < now + 30:
        raise ValidationError({"at": "Pick a time in the future."})
    if when > now + FARTHEST_AHEAD:
        raise ValidationError({"at": "A post can be scheduled up to a year ahead."})
    return when


def _describe(row: dict) -> str:
    """One line for a message read from Discord. A message that's only files has no text to show, so its first file stands in."""
    if messages.has_content(row["payload"]):
        return messages.summary(row["payload"])
    return row["attachments"][0]["filename"] if row.get("attachments") else "Nothing the bot can post"


def _as_actor(user_id: int | None, name: str | None) -> Actor | None:
    return Actor(user_id, name or "") if user_id else None


class Embeds(Module):
    key = "embeds"
    category = "Embeds"
    role_timers = (SCHEDULED,)
    row_limits = {"scheduled_posts": MAX_SCHEDULED, "saved_messages": MAX_SAVED}
    child_links = {"posted_versions": {"posted_id": "posted_messages.id"}, "adopt_items": {"job_id": "adopt_jobs.id"}}
    migrations = [
        """
        CREATE TABLE posted_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            summary TEXT NOT NULL,
            search TEXT NOT NULL,
            source TEXT NOT NULL,
            source_message_id INTEGER,
            adopted_from TEXT,
            posted_by INTEGER,
            posted_by_name TEXT,
            posted_at REAL NOT NULL,
            edited_at REAL,
            deleted_at REAL
        );
        CREATE UNIQUE INDEX posted_messages_message ON posted_messages (guild_id, message_id);
        CREATE INDEX posted_messages_guild ON posted_messages (guild_id, id DESC);
        CREATE INDEX posted_messages_source ON posted_messages (guild_id, source_message_id);
        CREATE TABLE posted_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            posted_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            payload TEXT NOT NULL,
            note TEXT NOT NULL,
            saved_at REAL NOT NULL,
            saved_by INTEGER,
            saved_by_name TEXT
        );
        CREATE UNIQUE INDEX posted_versions_number ON posted_versions (posted_id, number);
        CREATE INDEX posted_versions_guild ON posted_versions (guild_id, posted_id);
        CREATE TABLE scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            summary TEXT NOT NULL,
            ping INTEGER NOT NULL DEFAULT 0,
            due_at REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'waiting',
            error TEXT,
            created_by INTEGER,
            created_by_name TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX scheduled_posts_guild ON scheduled_posts (guild_id, due_at);
        CREATE TABLE adopt_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            target_channel_id INTEGER,
            delete_originals INTEGER NOT NULL,
            copy_reactions INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_by INTEGER,
            created_by_name TEXT,
            created_at REAL NOT NULL,
            finished_at REAL
        );
        CREATE INDEX adopt_jobs_guild ON adopt_jobs (guild_id, id DESC);
        CREATE INDEX adopt_jobs_state ON adopt_jobs (state);
        CREATE TABLE adopt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_channel_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            state TEXT NOT NULL,
            new_channel_id INTEGER,
            new_message_id INTEGER,
            note TEXT
        );
        CREATE INDEX adopt_items_job ON adopt_items (job_id, position);
        CREATE INDEX adopt_items_guild ON adopt_items (guild_id, source_message_id);
        """,
        """
        CREATE TABLE saved_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            payload TEXT NOT NULL,
            saved_by INTEGER,
            saved_by_name TEXT,
            saved_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX saved_messages_name ON saved_messages (guild_id, name);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._jobs: dict[int, asyncio.Task] = {}
        self._adopt_locks: dict[int, asyncio.Lock] = {}
        self._send_locks: dict[int, asyncio.Lock] = {}
        self._sending: dict[int, asyncio.Task] = {}
        self._menu = app_commands.ContextMenu(name="Edit Message", callback=self.edit_message_menu)
        self._menu.guild_only = True
        self._menu.default_permissions = discord.Permissions(manage_guild=True)

    async def on_load(self) -> None:
        self.bot.timers.handler(SCHEDULED, self._scheduled_due)
        self.bot.tree.add_command(self._menu)

    async def on_guild_restored(self, guild_id: int) -> None:
        """A post whose time passed before this backup came back went out back then or is out of date, and sending it now could repeat an announcement with its ping. One still to come gets its timer, since a file can bring the post back without one."""
        await self.db.run("UPDATE scheduled_posts SET state = 'failed', error = ? WHERE guild_id = ? AND state = 'waiting' AND due_at <= ?", (TIME_PASSED, guild_id, time.time()))
        await self.db.run("UPDATE scheduled_posts SET state = 'failed', error = ? WHERE guild_id = ? AND state = 'posting'", (WAS_SENDING, guild_id))
        for row in await self.db.all("SELECT id, due_at FROM scheduled_posts WHERE guild_id = ? AND state = 'waiting'", (guild_id,)):
            if await self.bot.timers.get_key(SCHEDULED, str(row["id"])) is None:
                await self.bot.timers.schedule(SCHEDULED, row["due_at"], {"id": row["id"]}, guild_id=guild_id, key=str(row["id"]))

    def cog_unload(self) -> None:
        self.bot.tree.remove_command(self._menu.name, type=self._menu.type)
        for task in [*self._jobs.values(), *self._sending.values()]:
            task.cancel()

    # ---- the library ----

    async def _find(self, guild_id: int, message_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM posted_messages WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))

    async def _latest_version(self, guild_id: int, posted_id: int) -> int:
        return await self.db.scalar("SELECT COALESCE(MAX(number), 0) FROM posted_versions WHERE guild_id = ? AND posted_id = ?", (guild_id, posted_id))

    async def _add_version(self, guild_id: int, posted_id: int, payload: dict, *, actor: Any, note: str, edited: bool = True) -> int:
        now = time.time()
        async with self.db.transaction():
            version = await self._latest_version(guild_id, posted_id) + 1
            await self.db.run(
                "INSERT INTO posted_versions (guild_id, posted_id, number, payload, note, saved_at, saved_by, saved_by_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, posted_id, version, json.dumps(payload), note, now, actor.id if actor else None, actor.name if actor else None),
            )
            await self.db.run(
                "UPDATE posted_messages SET summary = ?, search = ?, edited_at = COALESCE(?, edited_at) WHERE guild_id = ? AND id = ?",
                (messages.summary(payload), messages.searchable(payload), now if edited else None, guild_id, posted_id),
            )
            await self.db.run("DELETE FROM posted_versions WHERE guild_id = ? AND posted_id = ? AND number <= ?", (guild_id, posted_id, version - MAX_VERSIONS))
        return version

    async def record_post(self, guild_id: int, channel_id: int, message_id: int, payload: dict, *, actor: Any = None, source: str = "dashboard",
                          note: str = "Posted", adopted_from: str | None = None, source_message_id: int | None = None) -> int:
        """Add a message the bot posted to the library and start its history. Any feature that posts a lasting message can call this."""
        existing = await self._find(guild_id, message_id)
        if existing is not None:
            await self._add_version(guild_id, existing["id"], payload, actor=actor, note=note)
            return existing["id"]
        async with self.db.transaction():
            posted_id = await self.db.insert(
                "INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, source_message_id, adopted_from, posted_by, posted_by_name, posted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, message_id, messages.summary(payload), messages.searchable(payload), source, source_message_id, adopted_from,
                 actor.id if actor else None, actor.name if actor else None, time.time()),
            )
            await self._add_version(guild_id, posted_id, payload, actor=actor, note=note, edited=False)
        return posted_id

    async def post_message(self, guild_id: int, channel_id: int, payload: dict, *, actor: Any, source: str, ping: bool = False) -> dict:
        sent = await self.bot.ops.send_message(channel_id, payload, ping=ping)
        posted_id = await self.record_post(guild_id, channel_id, sent["id"], payload, actor=actor, source=source)
        return {"id": posted_id, "message_id": sent["id"], "channel_id": channel_id, "url": sent["url"]}

    async def edit_post(self, guild_id: int, channel_id: int, message_id: int, payload: dict, *, actor: Any, note: str = "Edited") -> dict:
        """Change a message the bot wrote and add the new text to its history. Raises OpsError with the reason when that can't happen."""
        ops = self.bot.ops
        if not await ops.find_thread(guild_id, channel_id):
            await self._mark_deleted(guild_id, [message_id])
            raise MessageGone("That channel no longer exists, so its message is gone too.")
        try:
            current = await ops.fetch_message(channel_id, message_id)
        except MessageGone:
            await self._mark_deleted(guild_id, [message_id])
            raise
        if not current["mine"]:
            raise OpsError(f"{current['author_name']} wrote that message, and Discord only lets a bot edit its own. Repost it from the Adopt tab on the Embeds page first.")
        row = await self._find(guild_id, message_id)
        if row is None:
            # The bot posted this some other way, so its history starts with what it says now and the edit stays revertible.
            posted_id = await self.record_post(guild_id, channel_id, message_id, current["payload"], source="found", note="As it was before the first edit here")
        else:
            posted_id = row["id"]
        # Compared with what the message says now, because another feature can have edited it since the last version here.
        if current["payload"] == messages.clean_payload(payload):
            return {"id": posted_id, "changed": False, "version": await self._latest_version(guild_id, posted_id), "url": jump_url(guild_id, channel_id, message_id)}
        await ops.edit_message(channel_id, message_id, payload, current=current)
        version = await self._add_version(guild_id, posted_id, payload, actor=actor, note=note)
        return {"id": posted_id, "changed": True, "version": version, "url": jump_url(guild_id, channel_id, message_id)}

    async def _payload_of_version(self, guild_id: int, posted_id: int, version: int) -> dict | None:
        stored = await self.db.scalar("SELECT payload FROM posted_versions WHERE guild_id = ? AND posted_id = ? AND number = ?", (guild_id, posted_id, version))
        return json.loads(stored) if stored else None

    async def versions(self, guild_id: int, posted_id: int) -> list[dict]:
        """A post's history, oldest first, with each payload read back."""
        rows = await self.db.all(
            "SELECT number, payload, note, saved_at, saved_by, saved_by_name FROM posted_versions WHERE guild_id = ? AND posted_id = ? ORDER BY number", (guild_id, posted_id),
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    async def move_post(self, guild_id: int, posted_id: int, channel_id: int, message_id: int) -> None:
        """The post lives at another message now, as after a move, and its history carries on there."""
        await self.db.run("UPDATE posted_messages SET channel_id = ?, message_id = ?, deleted_at = NULL WHERE guild_id = ? AND id = ?", (channel_id, message_id, guild_id, posted_id))

    async def copy_post(self, guild_id: int, posted_id: int, channel_id: int, message_id: int, *, actor: Any, source: str) -> int:
        """A new post for a copy at another message, holding the original's whole history. Returns the new post's number."""
        original = await self.db.one("SELECT * FROM posted_messages WHERE guild_id = ? AND id = ?", (guild_id, posted_id))
        if original is None:
            raise OpsError("That post isn't in the library anymore.")
        history = await self.db.all("SELECT number, payload, note, saved_at, saved_by, saved_by_name FROM posted_versions WHERE guild_id = ? AND posted_id = ? ORDER BY number", (guild_id, posted_id))
        async with self.db.transaction():
            copied = await self.db.insert(
                "INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_by, posted_by_name, posted_at, edited_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, message_id, original["summary"], original["search"], source, actor.id if actor else None, actor.name if actor else None, time.time(), original["edited_at"]),
            )
            await self.db.many(
                "INSERT INTO posted_versions (guild_id, posted_id, number, payload, note, saved_at, saved_by, saved_by_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(guild_id, copied, row["number"], row["payload"], row["note"], row["saved_at"], row["saved_by"], row["saved_by_name"]) for row in history],
            )
        return copied

    async def forget_post(self, guild_id: int, posted_id: int) -> None:
        """Takes a post and its history out of the library. The message itself is left alone."""
        async with self.db.transaction():
            await self.db.run("DELETE FROM posted_versions WHERE guild_id = ? AND posted_id = ?", (guild_id, posted_id))
            await self.db.run("DELETE FROM posted_messages WHERE guild_id = ? AND id = ?", (guild_id, posted_id))

    async def add_earlier_versions(self, guild_id: int, posted_id: int, earlier: list[tuple[dict, float]], *, note: str) -> int:
        """Puts earlier texts in front of a post's history, oldest first, each with the time it was written, as when a post comes over from
        another bot with its history. The history keeps its newest MAX_VERSIONS, so the oldest of these go first. Returns how many went in."""
        async with self.db.transaction():
            held = await self.db.scalar("SELECT COUNT(*) FROM posted_versions WHERE guild_id = ? AND posted_id = ?", (guild_id, posted_id))
            earlier = earlier[max(0, len(earlier) - max(0, MAX_VERSIONS - held)):]
            count = len(earlier)
            if not count:
                return 0
            # Two steps, since one would pass through numbers another row still holds, and each version number is unique to its post.
            await self.db.run("UPDATE posted_versions SET number = -number WHERE guild_id = ? AND posted_id = ?", (guild_id, posted_id))
            await self.db.run("UPDATE posted_versions SET number = ? - number WHERE guild_id = ? AND posted_id = ?", (count, guild_id, posted_id))
            await self.db.many(
                "INSERT INTO posted_versions (guild_id, posted_id, number, payload, note, saved_at, saved_by, saved_by_name) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                [(guild_id, posted_id, number, json.dumps(payload), note, saved_at) for number, (payload, saved_at) in enumerate(earlier, 1)],
            )
        return count

    async def _mark_deleted(self, guild_id: int, message_ids: list[int]) -> None:
        if not message_ids:
            return
        marks = ",".join("?" * len(message_ids))
        known = await self.db.scalar(f"SELECT COUNT(*) FROM posted_messages WHERE guild_id = ? AND deleted_at IS NULL AND message_id IN ({marks})", (guild_id, *message_ids))
        if known:
            await self.db.run(f"UPDATE posted_messages SET deleted_at = ? WHERE guild_id = ? AND deleted_at IS NULL AND message_id IN ({marks})", (time.time(), guild_id, *message_ids))

    # ---- events ----

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id:
            await self._mark_deleted(payload.guild_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id:
            await self._mark_deleted(payload.guild_id, list(payload.message_ids))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        await self._mark_channel_gone(channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        await self._mark_channel_gone(payload.guild_id, payload.thread_id)

    async def _mark_channel_gone(self, guild_id: int, channel_id: int) -> None:
        await self.db.run("UPDATE posted_messages SET deleted_at = ? WHERE guild_id = ? AND channel_id = ? AND deleted_at IS NULL", (time.time(), guild_id, channel_id))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for row in await self.db.all("SELECT id FROM adopt_jobs WHERE state = 'running'"):
            self._start_job(row["id"])

    # ---- the Edit Message menu ----

    async def edit_message_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        allowed = getattr(interaction.user, "guild_permissions", None)
        if allowed is None or not allowed.manage_guild:
            await interaction.response.send_message("You need Manage Server to edit messages through the bot.", ephemeral=True)
            return
        link = f"{self.bot.public_url}/dashboard/{interaction.guild_id}/embeds?edit={quote(message.jump_url, safe='')}"
        await interaction.response.send_message(f"Open the editor for that message: {link}", ephemeral=True)

    # ---- dashboard: posting and editing ----

    def api(self, router: Any) -> None:
        router.post("/embeds/post", self._post_endpoint)
        router.post("/embeds/post-files", self._post_files_endpoint)
        router.get("/embeds/saved", self._saved_endpoint)
        router.post("/embeds/saved", self._save_endpoint)
        router.delete(r"/embeds/saved/{saved_id:\d+}", self._unsave_endpoint)
        router.get("/embeds/lookup", self._lookup_endpoint)
        router.patch("/embeds/message", self._edit_endpoint)
        router.get("/embeds/messages", self._list_endpoint)
        router.get(r"/embeds/messages/{posted_id:\d+}", self._history_endpoint)
        router.post(r"/embeds/messages/{posted_id:\d+}/revert", self._revert_endpoint)
        router.post(r"/embeds/messages/{posted_id:\d+}/repost", self._repost_endpoint)
        router.delete(r"/embeds/messages/{posted_id:\d+}", self._delete_endpoint)
        router.get("/embeds/scheduled", self._scheduled_endpoint)
        router.patch(r"/embeds/scheduled/{post_id:\d+}", self._reschedule_endpoint)
        router.post(r"/embeds/scheduled/{post_id:\d+}/post", self._post_now_endpoint)
        router.delete(r"/embeds/scheduled/{post_id:\d+}", self._unschedule_endpoint)
        router.get("/embeds/channel-messages", self._channel_messages_endpoint)
        router.get("/embeds/adoptions", self._adoptions_endpoint)
        router.post("/embeds/adoptions", self._adopt_endpoint)
        router.get(r"/embeds/adoptions/{job_id:\d+}", self._adoption_endpoint)
        router.post(r"/embeds/adoptions/{job_id:\d+}/stop", self._stop_endpoint)
        router.post(r"/embeds/adoptions/{job_id:\d+}/resume", self._resume_endpoint)

    async def _post_endpoint(self, req: Any) -> dict:
        form = await req.json()
        channel_id = req.channel(form.get("channel_id"), posting=True)
        payload = messages.required_payload(form.get("payload"))
        ping = bool(form.get("ping"))
        if ping:
            req.require_ping(channel_id)
        if form.get("at") not in (None, ""):
            return await self._schedule(req, channel_id, payload, ping, _when_field(form["at"]))
        result = await self.post_message(req.guild_id, channel_id, payload, actor=req.actor, source="dashboard", ping=ping)
        await req.record("embed.post", detail=messages.summary(payload, 80))
        return result

    async def _post_files_endpoint(self, req: Any) -> dict:
        """A post with files. Scheduling isn't offered here, since a file is sent once and never stored."""
        try:
            files, fields = await images.read_form(req.request, file_limit=MAX_POST_FILE_BYTES)
        except Invalid as error:
            raise ValidationError({"files": str(error)}) from error
        try:
            form = read_json(fields.get("form", "{}").encode())
        except ApiError as error:
            raise ValidationError({"form": error.message}) from error
        if not isinstance(form, dict):
            raise ValidationError({"form": "Expected a set of fields."})
        if not files or len(files) > MAX_POST_FILES:
            raise ValidationError({"files": f"Attach from 1 to {MAX_POST_FILES} files."})
        channel_id = req.channel(form.get("channel_id"), posting=True)
        payload = messages.clean_payload(form.get("payload"))
        ping = bool(form.get("ping"))
        if ping:
            req.require_ping(channel_id)
        sent = await self.bot.ops.send_message(channel_id, payload, ping=ping, files=[(_safe_filename(name), body) for name, body in files.items()])
        posted_id = await self.record_post(req.guild_id, channel_id, sent["id"], payload, actor=req.actor, source="dashboard")
        await req.record("embed.post", detail=messages.summary(payload, 80) or f"{len(files)} files")
        return {"id": posted_id, "message_id": sent["id"], "channel_id": channel_id, "url": sent["url"]}

    async def _saved_endpoint(self, req: Any) -> list[dict]:
        rows = await self.db.all("SELECT id, name, payload, saved_by_name, saved_at FROM saved_messages WHERE guild_id = ? ORDER BY name LIMIT ?", (req.guild_id, MAX_SAVED))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    async def _save_endpoint(self, req: Any) -> dict:
        """Keep a message for later under a name. Saving under a name already used replaces that one."""
        form = await req.json()
        name = form.get("name").strip() if isinstance(form.get("name"), str) else ""
        if not 1 <= len(name) <= 100:
            raise ValidationError({"name": "Give it a name of up to 100 characters."})
        payload = messages.required_payload(form.get("payload"))
        held = await self.db.scalar("SELECT COUNT(*) FROM saved_messages WHERE guild_id = ?", (req.guild_id,))
        known = await self.db.scalar("SELECT 1 FROM saved_messages WHERE guild_id = ? AND name = ?", (req.guild_id, name))
        if held >= MAX_SAVED and not known:
            raise ValidationError({"name": f"This server keeps up to {MAX_SAVED} saved messages. Delete one first."})
        await self.db.run(
            "INSERT INTO saved_messages (guild_id, name, payload, saved_by, saved_by_name, saved_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, name) DO UPDATE SET payload = excluded.payload, saved_by = excluded.saved_by, saved_by_name = excluded.saved_by_name, saved_at = excluded.saved_at",
            (req.guild_id, name, json.dumps(payload), req.actor.id, req.actor.name, time.time()),
        )
        await req.record("embed.save", detail=name)
        return {"ok": True}

    async def _unsave_endpoint(self, req: Any) -> dict:
        row = await self.db.one("SELECT name FROM saved_messages WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("saved_id")))
        if row is None:
            raise ApiError(404, "That saved message is gone.")
        await self.db.run("DELETE FROM saved_messages WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("saved_id")))
        await req.record("embed.unsave", detail=row["name"])
        return {"ok": True}

    async def _lookup_endpoint(self, req: Any) -> dict:
        match = MESSAGE_LINK.search(req.query.get("link", ""))
        if match is None:
            raise ValidationError({"link": "Paste a message link. In Discord, right-click the message and pick Copy Message Link."})
        guild_id, channel_id, message_id = (int(part) for part in match.groups())
        if guild_id != req.guild_id:
            raise ValidationError({"link": "That link points to a message in another server."})
        if not await req.ops.find_thread(req.guild_id, channel_id):
            raise ValidationError({"link": "That link's channel is gone, or the bot can't see it."})
        if not req.can_see(channel_id):
            raise ValidationError({"link": "You can't see that channel."})
        if not req.can_read(channel_id):
            raise ValidationError({"link": "You can't read the messages in that channel."})
        row = await req.ops.fetch_message(channel_id, message_id)
        rows = await self._annotate(req.guild_id, [row])
        return {"message": rows[0], "library": await self._library_row(req.guild_id, message_id)}

    async def _library_row(self, guild_id: int, message_id: int) -> dict | None:
        row = await self._find(guild_id, message_id)
        if row is None:
            return None
        return {"id": row["id"], "version": await self._latest_version(guild_id, row["id"])}

    async def _edit_endpoint(self, req: Any) -> dict:
        form = await req.json()
        await self._find_thread_named(req, form.get("channel_id"))
        channel_id = req.channel(form.get("channel_id"), posting=True)
        message_id = _id_field(form.get("message_id"), "message_id")
        payload = messages.required_payload(form.get("payload"))
        if form.get("version") is not None and not form.get("force"):
            library = await self._library_row(req.guild_id, message_id)
            if library is not None and library["version"] != form["version"]:
                raise ApiError(409, "Someone changed this message after you opened it.")
        result = await self.edit_post(req.guild_id, channel_id, message_id, payload, actor=req.actor)
        if result["changed"]:
            await req.record("embed.edit", detail=messages.summary(payload, 80))
        return result

    @staticmethod
    async def _find_thread_named(req: Any, value: Any) -> None:
        """An archived thread isn't in the library's cache until it's asked for, and `req.channel` reads the cache."""
        try:
            await req.ops.find_thread(req.guild_id, snowflake(value))
        except Invalid:
            pass  # req.channel gives the answer for this

    async def _list_endpoint(self, req: Any) -> dict:
        needle = req.query.get("q", "").strip().lower()[:200]
        where = "guild_id = ?"
        args: list[Any] = [req.guild_id]
        if needle:
            where += " AND search LIKE ? ESCAPE '\\'"
            args.append(like_pattern(needle))
        rows = await self.db.all(
            f"SELECT p.*, (SELECT MAX(number) FROM posted_versions v WHERE v.posted_id = p.id AND v.guild_id = p.guild_id) AS version FROM posted_messages p WHERE {where} ORDER BY id DESC LIMIT ?",
            (*args, MAX_LISTED),
        )
        total = await self.db.scalar(f"SELECT COUNT(*) FROM posted_messages WHERE {where}", args)
        rows = [row for row in rows if req.can_read(row["channel_id"])]
        for row in rows:
            row.pop("search", None)
            row["url"] = jump_url(req.guild_id, row["channel_id"], row["message_id"])
        return {"rows": rows, "total": total}

    async def _library_or_404(self, req: Any) -> dict:
        row = await self.db.one("SELECT * FROM posted_messages WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("posted_id")))
        if row is None or not req.can_read(row["channel_id"]):
            raise ApiError(404, "That message isn't in the list anymore.")
        return row

    async def _history_endpoint(self, req: Any) -> dict:
        row = await self._library_or_404(req)
        row.pop("search", None)
        versions = await self.db.all(
            "SELECT number, payload, note, saved_at, saved_by_name FROM posted_versions WHERE guild_id = ? AND posted_id = ? ORDER BY number DESC", (req.guild_id, row["id"]),
        )
        for version in versions:
            version["payload"] = json.loads(version["payload"])
        return {**row, "url": jump_url(req.guild_id, row["channel_id"], row["message_id"]), "versions": versions}

    async def _revert_endpoint(self, req: Any) -> dict:
        row = await self._library_or_404(req)
        await req.ops.find_thread(req.guild_id, row["channel_id"])
        req.channel(row["channel_id"], posting=True)
        form = await req.json()
        version = form.get("number")
        stored = await self._payload_of_version(req.guild_id, row["id"], version) if isinstance(version, int) and 0 < version <= 2 ** 31 else None
        if stored is None:
            raise ApiError(404, "That version isn't in this message's history.")
        result = await self.edit_post(req.guild_id, row["channel_id"], row["message_id"], stored, actor=req.actor, note=f"Reverted to version {version}")
        await req.record("embed.revert", detail=f"{row['summary'][:80]} (version {version})")
        return result

    async def _repost_endpoint(self, req: Any) -> dict:
        """Bring back a post someone deleted. Its history carries on under the new message."""
        row = await self._library_or_404(req)
        if row["deleted_at"] is None:
            raise ApiError(400, "That message still exists.")
        form = await req.json()
        channel_id = req.channel(form.get("channel_id") or row["channel_id"], posting=True)
        payload = await self._payload_of_version(req.guild_id, row["id"], await self._latest_version(req.guild_id, row["id"]))
        if payload is None:
            raise ApiError(409, "This post has no saved text to send.")
        # Claimed before the send, so a second click finds nothing to bring back and can't post it twice.
        if not await self.db.run("UPDATE posted_messages SET deleted_at = NULL WHERE guild_id = ? AND id = ? AND deleted_at IS NOT NULL", (req.guild_id, row["id"])):
            raise ApiError(409, "That post is already back.")
        try:
            sent = await req.ops.send_message(channel_id, payload, silent=True)
        except BaseException:
            await self.db.run("UPDATE posted_messages SET deleted_at = ? WHERE guild_id = ? AND id = ?", (row["deleted_at"], req.guild_id, row["id"]))
            raise
        await self.db.run("UPDATE posted_messages SET channel_id = ?, message_id = ?, deleted_at = NULL WHERE guild_id = ? AND id = ?", (channel_id, sent["id"], req.guild_id, row["id"]))
        await self._add_version(req.guild_id, row["id"], payload, actor=req.actor, note="Posted again after it was deleted", edited=False)
        await req.record("embed.repost", detail=row["summary"][:80])
        return {"id": row["id"], "message_id": sent["id"], "url": sent["url"]}

    async def _delete_endpoint(self, req: Any) -> dict:
        row = await self._library_or_404(req)
        # A channel or thread that's gone took the message with it, and the row can still leave the list.
        if row["deleted_at"] is None and await req.ops.find_thread(req.guild_id, row["channel_id"]):
            await req.ops.delete_message(row["channel_id"], row["message_id"])
        async with self.db.transaction():
            await self.db.run("DELETE FROM posted_versions WHERE guild_id = ? AND posted_id = ?", (req.guild_id, row["id"]))
            await self.db.run("DELETE FROM posted_messages WHERE guild_id = ? AND id = ?", (req.guild_id, row["id"]))
        await req.record("embed.delete", detail=row["summary"][:80])
        return {"ok": True}

    # ---- scheduled posts ----

    async def _schedule(self, req: Any, channel_id: int, payload: dict, ping: bool, when: float) -> dict:
        waiting = await self.db.scalar("SELECT COUNT(*) FROM scheduled_posts WHERE guild_id = ? AND state != 'failed'", (req.guild_id,))
        if waiting >= MAX_SCHEDULED:
            raise ValidationError({"at": f"This server has {MAX_SCHEDULED} posts waiting. Cancel one first."})
        post_id = await self.db.insert(
            "INSERT INTO scheduled_posts (guild_id, channel_id, payload, summary, ping, due_at, created_by, created_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (req.guild_id, channel_id, json.dumps(payload), messages.summary(payload), int(ping), when, req.actor.id, req.actor.name, time.time()),
        )
        await self.bot.timers.schedule(SCHEDULED, when, {"id": post_id}, guild_id=req.guild_id, key=str(post_id))
        await req.record("embed.schedule", detail=messages.summary(payload, 80))
        return {"scheduled": post_id, "due_at": when}

    async def _scheduled_endpoint(self, req: Any) -> list[dict]:
        rows = await self.db.all("SELECT * FROM scheduled_posts WHERE guild_id = ? ORDER BY due_at LIMIT ?", (req.guild_id, MAX_LISTED))
        rows = [row for row in rows if req.can_see(row["channel_id"])]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
            row["ping"] = bool(row["ping"])
        return rows

    async def _scheduled_or_404(self, req: Any) -> dict:
        row = await self.db.one("SELECT * FROM scheduled_posts WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("post_id")))
        if row is None or not req.can_see(row["channel_id"]):
            raise ApiError(404, "That scheduled post is gone. It may have just gone out.")
        return row

    async def _reschedule_endpoint(self, req: Any) -> dict:
        row = await self._scheduled_or_404(req)
        form = await req.json()
        # A manager can fix a typo on a post someone else set up, so only what's new or changed needs their own permission. Once the text changes, it's their text going out.
        channel_id = req.channel(form.get("channel_id", row["channel_id"]), posting=form.get("channel_id", row["channel_id"]) != row["channel_id"] or "payload" in form)
        payload = messages.required_payload(form["payload"]) if "payload" in form else json.loads(row["payload"])
        when = _when_field(form["at"]) if "at" in form else row["due_at"]
        ping = bool(form.get("ping", row["ping"]))
        if ping and (not row["ping"] or channel_id != row["channel_id"] or _adds_ping(req, json.loads(row["payload"]), payload)):
            req.require_ping(channel_id)
        changed = await self.db.run(
            "UPDATE scheduled_posts SET channel_id = ?, payload = ?, summary = ?, ping = ?, due_at = ?, state = 'waiting', error = NULL WHERE guild_id = ? AND id = ? AND state != 'posting'",
            (channel_id, json.dumps(payload), messages.summary(payload), int(ping), when, req.guild_id, row["id"]),
        )
        if not changed:
            raise ApiError(409, "That post is going out right now.")
        await self.bot.timers.schedule(SCHEDULED, when, {"id": row["id"]}, guild_id=req.guild_id, key=str(row["id"]))
        await req.record("embed.reschedule", detail=messages.summary(payload, 80))
        return {"ok": True, "due_at": when}

    async def _post_now_endpoint(self, req: Any) -> dict:
        row = await self._scheduled_or_404(req)
        req.channel(row["channel_id"], posting=True)
        if row["ping"]:
            req.require_ping(row["channel_id"])
        if not await self._claim_scheduled(row["id"]):
            raise ApiError(409, "That post is going out right now.")
        try:
            result = await self._send_scheduled(row)
        except OpsError as error:
            await self._fail_scheduled(row["id"], str(error))
            raise
        await req.record("embed.post", detail=row["summary"][:80])
        return result

    async def _unschedule_endpoint(self, req: Any) -> dict:
        row = await self._scheduled_or_404(req)
        await self.bot.timers.cancel_key(SCHEDULED, str(row["id"]))
        await self.db.run("DELETE FROM scheduled_posts WHERE guild_id = ? AND id = ?", (req.guild_id, row["id"]))
        await req.record("embed.unschedule", detail=row["summary"][:80])
        return {"ok": True}

    async def _claim_scheduled(self, post_id: int) -> bool:
        return await self.db.run("UPDATE scheduled_posts SET state = 'posting', error = NULL WHERE id = ? AND state != 'posting'", (post_id,)) > 0

    async def _fail_scheduled(self, post_id: int, error: str) -> None:
        await self.db.run("UPDATE scheduled_posts SET state = 'failed', error = ? WHERE id = ?", (error, post_id))

    async def _send_scheduled(self, post: dict) -> dict:
        payload = json.loads(post["payload"])
        actor = _as_actor(post["created_by"], post["created_by_name"])
        if not await self.bot.ops.find_thread(post["guild_id"], post["channel_id"]):
            raise OpsError("That channel no longer exists.")
        result = await self.post_message(post["guild_id"], post["channel_id"], payload, actor=actor, source="schedule", ping=bool(post["ping"]))
        await self.db.run("DELETE FROM scheduled_posts WHERE id = ?", (post["id"],))
        await self.bot.timers.cancel_key(SCHEDULED, str(post["id"]))
        return result

    async def _scheduled_due(self, payload: dict, timer: dict) -> None:
        post = await self.db.one("SELECT * FROM scheduled_posts WHERE id = ? AND guild_id = ?", (payload["id"], timer["guild_id"]))
        if post is None or post["state"] == "failed":
            return
        key = str(post["id"])
        if post["state"] == "posting":
            if post["id"] in self._sending:  # a long wait on Discord's rate limit, and the send still has its turn
                await self.bot.timers.schedule(SCHEDULED, time.time() + SEND_WATCH_SECONDS, {"id": post["id"], "tries": payload.get("tries", 0)}, guild_id=post["guild_id"], key=key)
                return
            # Stopped in the middle of a send. Discord may or may not have it, and a doubled announcement is worse than a visible note.
            await self._fail_scheduled(post["id"], "The bot restarted while posting this. It may or may not have gone out. Check the channel, and post it again if it isn't there.")
            return
        if not await self._claim_scheduled(post["id"]):
            return
        # Timers run one at a time for every server, and a channel's rate limit can hold a send for a long while. The send gets its own task, and this timer stays behind to notice a restart.
        await self.bot.timers.schedule(SCHEDULED, time.time() + SEND_WATCH_SECONDS, {"id": post["id"], "tries": payload.get("tries", 0)}, guild_id=post["guild_id"], key=key)
        task = asyncio.ensure_future(self._deliver_scheduled(post, payload.get("tries", 0)))
        self._sending[post["id"]] = task
        task.add_done_callback(lambda _: self._sending.pop(post["id"], None))
        await asyncio.wait({task}, timeout=SEND_WAIT_SECONDS)

    async def _deliver_scheduled(self, post: dict, tries: int) -> None:
        lock = self._send_locks.setdefault(post["guild_id"], asyncio.Lock())
        try:
            async with lock:
                await self._send_scheduled(post)
        except SendUnknown as error:
            await self._scheduled_gave_up(post, f"{error} Check the channel, and post it again if it isn't there.")
        except OpsError as error:
            # A refusal means nothing went out, so a retry can't double it. Discord having a bad minute shouldn't lose the post.
            log.warning("Scheduled post %s was refused, try %d: %s", post["id"], tries + 1, error)
            if tries + 1 >= SCHEDULE_TRIES:
                await self._scheduled_gave_up(post, str(error))
                return
            await self.db.run("UPDATE scheduled_posts SET state = 'waiting' WHERE id = ?", (post["id"],))
            await self.bot.timers.schedule(SCHEDULED, time.time() + 30 * 2 ** (tries + 1), {"id": post["id"], "tries": tries + 1}, guild_id=post["guild_id"], key=str(post["id"]))
        except Exception:
            log.exception("Scheduled post %s failed", post["id"])
            await self._scheduled_gave_up(post, "Something went wrong during the send, so it may or may not have gone out. Check the channel, and post it again if it isn't there.")

    async def _scheduled_gave_up(self, post: dict, reason: str) -> None:
        await self._fail_scheduled(post["id"], reason)
        await self.bot.timers.cancel_key(SCHEDULED, str(post["id"]))
        await self.bot.activity_log.record(post["guild_id"], _as_actor(post["created_by"], post["created_by_name"]), "embed.schedule_failed", detail=post["summary"][:80])

    async def finish_sends(self) -> None:
        """Wait for every scheduled post still going out. The tests use this."""
        await asyncio.gather(*self._sending.values(), return_exceptions=True)

    # ---- adopting another bot's messages ----

    async def _annotate(self, guild_id: int, rows: list[dict]) -> list[dict]:
        """Say what reposting each message would do: whether it was already reposted, and whether it holds anything to carry."""
        ids = [row["id"] for row in rows]
        done: dict[int, dict] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for found in await self.db.all(f"SELECT * FROM posted_messages WHERE guild_id = ? AND deleted_at IS NULL AND source_message_id IN ({marks})", (guild_id, *ids)):
                done[found["source_message_id"]] = found
        holds = getattr(self.bot.module("reactionroles"), "holds_message", None)
        for row in rows:
            found = done.get(row["id"])
            row["adopted_url"] = jump_url(guild_id, found["channel_id"], found["message_id"]) if found else None
            row["empty"] = not messages.has_content(row["payload"]) and not row.get("attachments")
            row["summary"] = _describe(row)
            row["reaction_roles"] = bool(holds and await holds(guild_id, row["id"]))
        return rows

    async def _channel_messages_endpoint(self, req: Any) -> dict:
        channel_id = req.channel(req.query.get("channel_id"))
        if not req.can_read(channel_id):
            raise ValidationError({"channel_id": "You can't read the messages in that channel."})
        before = req.query.get("before")
        data = await req.ops.channel_messages(req.guild_id, channel_id, limit=50, before=_id_field(before, "before") if before else None)
        rows = [row for row in data["messages"] if not row["system"]]
        return {**data, "messages": await self._annotate(req.guild_id, rows), "more": len(data["messages"]) == 50}

    async def _adopt_endpoint(self, req: Any) -> dict:
        form = await req.json()
        picked = form.get("messages")
        if not isinstance(picked, list) or not picked:
            raise ValidationError({"messages": "Pick at least one message."})
        if len(picked) > MAX_ADOPT:
            raise ValidationError({"messages": f"One go can repost up to {MAX_ADOPT} messages."})
        target = form.get("channel_id")
        target_id = req.channel(target, posting=True) if target else None
        items: dict[int, int] = {}
        for raw in picked:
            if not isinstance(raw, dict):
                raise ValidationError({"messages": "Pick at least one message."})
            # With no destination, each copy goes in its own channel, so that's where they post.
            source = req.channel(raw.get("channel_id"), "messages", posting=not target_id)
            if not req.can_read(source):
                raise ValidationError({"messages": "You can't read the messages in that channel."})
            items[_id_field(raw.get("message_id"), "messages")] = source
        await self._check_taking_over(req, items, delete=bool(form.get("delete_originals")))
        marks = ",".join("?" * len(items))
        running = await self.db.scalar(
            f"SELECT COUNT(*) FROM adopt_items i JOIN adopt_jobs j ON j.id = i.job_id AND j.guild_id = i.guild_id WHERE j.guild_id = ? AND j.state = 'running' AND i.source_message_id IN ({marks})",
            (req.guild_id, *items),
        )
        if running:
            raise ApiError(409, "One of those messages is already being reposted.")
        # Snowflakes sort by time, so this keeps the order the posts have in Discord.
        ordered = sorted(items)
        async with self.db.transaction():
            job_id = await self.db.insert(
                "INSERT INTO adopt_jobs (guild_id, target_channel_id, delete_originals, copy_reactions, state, created_by, created_by_name, created_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
                (req.guild_id, target_id, int(bool(form.get("delete_originals"))), int(bool(form.get("copy_reactions"))), req.actor.id, req.actor.name, time.time()),
            )
            for position, message_id in enumerate(ordered):
                await self.db.run(
                    "INSERT INTO adopt_items (guild_id, job_id, position, source_channel_id, source_message_id, author_name, summary, state) VALUES (?, ?, ?, ?, ?, '', '', 'waiting')",
                    (req.guild_id, job_id, position, items[message_id], message_id),
                )
        await req.record("embed.adopt", detail=f"{len(ordered)} message{'' if len(ordered) == 1 else 's'}")
        self._start_job(job_id)
        return await self._job_view(req.guild_id, job_id)

    @staticmethod
    async def _check_taking_over(req: Any, items: dict[int, int], *, delete: bool) -> None:
        """Deleting a message and reposting a person's words under the bot's name both go past what Manage Server allows, so they take Manage Messages in that channel, as Discord asks of anyone deleting another's message."""
        for message_id, channel_id in items.items():
            if req.can_manage_messages(channel_id):
                continue
            if delete:
                raise ValidationError({"messages": "You need Manage Messages in that channel to delete the originals."})
            try:
                source = await req.ops.fetch_message(channel_id, message_id)
            except MessageGone:
                continue  # the job says so when it reaches this one
            if not source["author_bot"]:
                raise ValidationError({"messages": "You need Manage Messages in that channel to repost a member's message."})

    async def _job_view(self, guild_id: int, job_id: int) -> dict | None:
        job = await self.db.one("SELECT * FROM adopt_jobs WHERE guild_id = ? AND id = ?", (guild_id, job_id))
        if job is None:
            return None
        items = await self.db.all("SELECT * FROM adopt_items WHERE guild_id = ? AND job_id = ? ORDER BY position", (guild_id, job_id))
        for item in items:
            item["url"] = jump_url(guild_id, item["new_channel_id"], item["new_message_id"]) if item["new_message_id"] else None
            item["source_url"] = jump_url(guild_id, item["source_channel_id"], item["source_message_id"])
        return {**job, "delete_originals": bool(job["delete_originals"]), "copy_reactions": bool(job["copy_reactions"]), "items": items}

    async def _visible_job(self, req: Any, job_id: int) -> dict | None:
        """A job as this person may see it: only what they could read and where they could see it go. A job with nothing left isn't theirs to see or to change."""
        view = await self._job_view(req.guild_id, job_id)
        if view is None or (view["target_channel_id"] and not req.can_see(view["target_channel_id"])):
            return None
        view["items"] = [item for item in view["items"] if req.can_read(item["source_channel_id"])]
        return view if view["items"] else None

    async def _adoptions_endpoint(self, req: Any) -> list[dict]:
        rows = await self.db.all("SELECT id FROM adopt_jobs WHERE guild_id = ? ORDER BY id DESC LIMIT 20", (req.guild_id,))
        views = [await self._visible_job(req, row["id"]) for row in rows]
        return [view for view in views if view is not None]

    async def _adoption_endpoint(self, req: Any) -> dict:
        view = await self._visible_job(req, req.int_param("job_id"))
        if view is None:
            raise ApiError(404, "That job isn't there anymore.")
        return view

    async def _stop_endpoint(self, req: Any) -> dict:
        job_id = req.int_param("job_id")
        if await self._visible_job(req, job_id) is None:
            raise ApiError(404, "That job isn't running.")
        if not await self.db.run("UPDATE adopt_jobs SET state = 'stopped' WHERE guild_id = ? AND id = ? AND state = 'running'", (req.guild_id, job_id)):
            raise ApiError(404, "That job isn't running.")
        await req.record("embed.adopt_stop")
        return await self._job_view(req.guild_id, job_id)

    async def _resume_endpoint(self, req: Any) -> dict:
        """Carry on with a stopped job. A failed message is tried again, or skipped if the person says so."""
        job_id = req.int_param("job_id")
        form = await req.json()
        if await self._visible_job(req, job_id) is None:
            raise ApiError(404, "That job isn't stopped.")
        if not await self.db.run("UPDATE adopt_jobs SET state = 'running', finished_at = NULL WHERE guild_id = ? AND id = ? AND state = 'stopped'", (req.guild_id, job_id)):
            raise ApiError(404, "That job isn't stopped.")
        next_state = "skipped" if form.get("skip") else "waiting"
        await self.db.run("UPDATE adopt_items SET state = ? WHERE guild_id = ? AND job_id = ? AND state = 'failed'", (next_state, req.guild_id, job_id))
        await req.record("embed.adopt_resume")
        self._start_job(job_id)
        return await self._job_view(req.guild_id, job_id)

    def _start_job(self, job_id: int) -> None:
        running = self._jobs.get(job_id)
        if running is None or running.done():
            self._jobs[job_id] = asyncio.ensure_future(self._job_task(job_id))

    async def finish_jobs(self) -> None:
        """Wait for every adoption under way. The tests use this."""
        await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        self._jobs.clear()

    async def _job_task(self, job_id: int) -> None:
        try:
            await self._run_job(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Adoption %s failed", job_id)
            await self.db.run("UPDATE adopt_jobs SET state = 'stopped' WHERE id = ? AND state = 'running'", (job_id,))

    async def _run_job(self, job_id: int) -> None:
        job = await self.db.one("SELECT * FROM adopt_jobs WHERE id = ?", (job_id,))
        if job is None:
            return
        # Two jobs at once would interleave their posts in a channel, and the order is the point.
        lock = self._adopt_locks.setdefault(job["guild_id"], asyncio.Lock())
        async with lock:
            while True:
                job = await self.db.one("SELECT * FROM adopt_jobs WHERE id = ?", (job_id,))
                if job is None or job["state"] != "running":
                    return
                item = await self.db.one("SELECT * FROM adopt_items WHERE guild_id = ? AND job_id = ? AND state IN ('waiting', 'posting', 'posted') ORDER BY position LIMIT 1", (job["guild_id"], job_id))
                if item is None:
                    await self.db.run("UPDATE adopt_jobs SET state = 'done', finished_at = ? WHERE id = ?", (time.time(), job_id))
                    return
                try:
                    await self._adopt_step(job, item)
                except OpsError as error:
                    await self.db.run("UPDATE adopt_items SET state = 'failed', note = ? WHERE id = ?", (str(error), item["id"]))
                    await self.db.run("UPDATE adopt_jobs SET state = 'stopped', finished_at = ? WHERE id = ?", (time.time(), job_id))
                    return

    async def _adopt_step(self, job: dict, item: dict) -> None:
        if item["state"] == "waiting":
            await self._repost_item(job, item)
        elif item["state"] == "posting":
            raise OpsError("The bot restarted while reposting this message. Check the channel. Skip it if the copy is there, and try again if it isn't.")
        else:
            await self._finish_item(job, item)

    async def _repost_item(self, job: dict, item: dict) -> None:
        ops = self.bot.ops
        source = await ops.fetch_message(item["source_channel_id"], item["source_message_id"])
        hidden = bool(source.get("suppressed"))
        if hidden and not source["mine"] and not source["system"] and not messages.has_content(source["payload"]) and not source["attachments"]:
            # Discord returns no embed while it's hidden. A long reaction role list runs on into such a message, which shows only its reactions.
            source = {**source, "payload": {**source["payload"], "embeds": await ops.read_hidden_embeds(item["source_channel_id"], item["source_message_id"])}}
        await self.db.run("UPDATE adopt_items SET author_name = ?, summary = ? WHERE id = ?", (source["author_name"], _describe(source), item["id"]))
        if source["mine"] or source["system"] or (not messages.has_content(source["payload"]) and not source["attachments"]):
            reason = "The bot already wrote this message." if source["mine"] else "This message has nothing the bot can post."
            await self.db.run("UPDATE adopt_items SET state = 'skipped', note = ? WHERE id = ?", (reason, item["id"]))
            return
        target_id = job["target_channel_id"] or item["source_channel_id"]
        await self.db.run("UPDATE adopt_items SET state = 'posting' WHERE id = ?", (item["id"],))
        try:
            sent = await ops.repost_message(item["source_channel_id"], item["source_message_id"], target_id, source["payload"], suppress_embeds=hidden)
        except OpsError:
            await self.db.run("UPDATE adopt_items SET state = 'waiting' WHERE id = ?", (item["id"],))
            raise
        notes = []
        if sent["skipped"]:
            notes.append(f"These files were too big to carry over: {', '.join(sent['skipped'])}.")
        if source["controls"]:
            notes.append("Its buttons or menus belong to the other bot and didn't carry over.")
        if source["stickers"]:
            notes.append("Its sticker didn't carry over.")
        # Saved before anything else happens, because from here on a restart must not post this message again.
        await self.db.run(
            "UPDATE adopt_items SET state = 'posted', new_channel_id = ?, new_message_id = ?, note = ? WHERE id = ?",
            (target_id, sent["id"], " ".join(notes) or None, item["id"]),
        )
        # A copy posted hidden comes back from Discord with no embed, so what's kept is what was sent.
        await self.record_post(
            job["guild_id"], target_id, sent["id"], source["payload"] if hidden else sent["payload"], actor=_as_actor(job["created_by"], job["created_by_name"]), source="adopted",
            note=f"Copied from {source['author_name']}'s post", adopted_from=source["author_name"], source_message_id=item["source_message_id"],
        )

    async def _finish_item(self, job: dict, item: dict) -> None:
        """Everything after the copy exists: reaction roles, reactions, and the original. A failure here is a note, since the copy already went out."""
        ops = self.bot.ops
        notes = [item["note"]] if item["note"] else []
        moved = True
        move = getattr(self.bot.module("reactionroles"), "move_message", None)
        if move is not None:
            try:
                result = await move(job["guild_id"], item["source_message_id"], item["new_channel_id"], item["new_message_id"])
                notes.extend((result or {}).get("warnings") or [])
            except (Invalid, OpsError) as error:
                moved = False
                notes.append(f"Its reaction roles couldn't be moved, so the original stays where it is: {error}")
            except Exception:  # a bug in another feature must not stop the rest of the posts
                log.exception("Moving reaction roles from %s failed", item["source_message_id"])
                moved = False
                notes.append("Its reaction roles couldn't be moved, so the original stays where it is.")
        if job["copy_reactions"]:
            try:
                source = await ops.fetch_message(item["source_channel_id"], item["source_message_id"])
                for reaction in source["reactions"]:
                    await ops.add_reaction(item["new_channel_id"], item["new_message_id"], reaction["emoji"])
            except OpsError as error:
                notes.append(f"Some reactions didn't carry over: {error}")
        if job["delete_originals"] and moved:
            try:
                await ops.delete_message(item["source_channel_id"], item["source_message_id"])
            except OpsError as error:
                notes.append(f"The original stays: {error}")
        await self.db.run("UPDATE adopt_items SET state = 'done', note = ? WHERE id = ?", (" ".join(notes) or None, item["id"]))

    # ---- /embed ----

    @commands.hybrid_group(name="embed", description="Post and edit embeds")
    @commands.guild_only()
    async def embed(self, ctx: commands.Context, *, text: str | None = None) -> None:
        """Typed with more after it, this is the short form: `embed [channel] <color> <title> | <description>`."""
        if not text:
            await ctx.send(f"Use `/embed create`, `edit`, `custom`, `editcustom`, `source` or `unhide`, or the editor: {self.bot.public_url}/dashboard/{ctx.guild.id}/embeds", ephemeral=True)
            return
        if not access.is_admin(ctx.author) and not ctx.author.guild_permissions.manage_guild:
            await ctx.fail("You need Manage Server to post through the bot.")
            return
        channel = None
        first, _, rest = text.strip().partition(" ")
        named = re.fullmatch(r"<#(\d+)>|(\d{17,20})", first)
        if named:
            channel = ctx.guild.get_channel_or_thread(int(named.group(1) or named.group(2)))
            if channel is None or not hasattr(channel, "send"):
                await ctx.fail("That channel isn't in this server, or the bot can't see it.")
                return
            first, _, rest = rest.strip().partition(" ")
        try:
            payload = _clean_command_embed({**self._split_title(rest), "color": hex_color(first)})
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await self._post_from_command(ctx, payload, channel)

    async def _read_json(self, text: str) -> dict:
        """The message a command was handed: JSON, or a paste link holding JSON."""
        text = text.strip()
        paste = PASTE_LINK.match(text)
        if paste:
            text = await self._fetch_paste(f"https://{paste.group(1)}/raw/{paste.group(2)}")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            data = await asyncio.to_thread(json.loads, text)
        except (ValueError, RecursionError) as error:
            raise Invalid("That isn't valid JSON.") from error
        payload = messages.from_json(data)
        if not messages.has_content(payload):
            raise Invalid("That JSON holds nothing to post.")
        return payload

    async def _fetch_paste(self, url: str) -> str:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise Invalid(f"That paste link answered {response.status}.")
                    # One read returns whatever has arrived so far, and a paste can take several.
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body += chunk
                        if len(body) > MAX_PASTE_BYTES:
                            raise Invalid("That paste is too long to be a message.")
        except (aiohttp.ClientError, TimeoutError) as error:
            raise Invalid("That paste link couldn't be read.") from error
        return body.decode("utf-8", "replace")

    @staticmethod
    def _split_title(text: str) -> dict:
        title, separator, description = text.partition("|")
        return {"title": title.strip(), "description": description.strip()} if separator else {"description": text.strip()}

    async def _post_from_command(self, ctx: commands.Context, payload: dict, channel: Any) -> None:
        target = channel or ctx.channel
        refusal = access.post_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        try:
            result = await self.post_message(ctx.guild.id, target.id, payload, actor=self.bot.actor(ctx.author), source="command")
        except OpsError as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "embed.post", detail=messages.summary(payload, 80))
        await ctx.ok(f"Posted in {target.mention}: {result['url']}")

    @embed.command(name="create", description="Post a simple embed. Separate the title and description with |")
    @requires("manage_guild")
    @app_commands.describe(color="Six hex digits, such as FF0000", text="Title | Description", channel="Where to post. Defaults to here.")
    async def embed_create(self, ctx: commands.Context, color: str, *, text: str, channel: discord.TextChannel | discord.Thread | None = None) -> None:
        await ctx.defer(ephemeral=True)
        if channel is None:
            at_end = CHANNEL_AT_END.search(text)
            found = ctx.guild.get_channel(int(at_end.group(1))) if at_end else None
            if found is not None and hasattr(found, "send"):
                channel, text = found, text[:at_end.start()]
        try:
            payload = _clean_command_embed({**self._split_title(text), "color": hex_color(color)})
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await self._post_from_command(ctx, payload, channel)

    async def _named_message(self, ctx: commands.Context, reference: str, channel: Any) -> tuple[Any, int] | None:
        """The channel and message a command means, from a link or from an ID. An ID alone is looked for in the library, then here."""
        link = MESSAGE_LINK.search(reference)
        if link:
            guild_id, channel_id, message_id = (int(part) for part in link.groups())
            if guild_id != ctx.guild.id:
                await ctx.fail("That link points to a message outside this server.")
                return None
            found = await self._find_channel(ctx.guild, channel_id)
            if found is None:
                await ctx.fail("That link's channel is gone, or the bot can't see it.")
                return None
            return found, message_id
        try:
            message_id = snowflake(reference)
        except Invalid:
            await ctx.fail("That isn't a message ID or link.")
            return None
        if channel is not None:
            return channel, message_id
        known = await self._find(ctx.guild.id, message_id)
        found = await self._find_channel(ctx.guild, known["channel_id"]) if known else None
        return found or ctx.channel, message_id

    async def _find_channel(self, guild: Any, channel_id: int) -> Any:
        """A channel or thread of this server, including an archived thread the library doesn't keep."""
        if not await self.bot.ops.find_thread(guild.id, channel_id):
            return None
        return guild.get_channel_or_thread(channel_id) or self.bot.ops.known_thread(guild.id, channel_id)

    async def _edit_from_command(self, ctx: commands.Context, target: Any, message_id: int, payload: dict) -> None:
        refusal = access.post_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        try:
            result = await self.edit_post(ctx.guild.id, target.id, message_id, payload, actor=self.bot.actor(ctx.author))
        except OpsError as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "embed.edit", detail=messages.summary(payload, 80))
        await ctx.ok(f"Edited: {result['url']}")

    @embed.command(name="edit", description="Change the title and description of an embed the bot posted")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", text="Title | Description", channel="Where the message is, if you give an ID from elsewhere")
    async def embed_edit(self, ctx: commands.Context, message_id: str, *, text: str, channel: discord.TextChannel | discord.Thread | None = None) -> None:
        await ctx.defer(ephemeral=True)
        named = await self._named_message(ctx, message_id, channel)
        if named is None:
            return
        target, found_id = named
        try:
            current = await self.bot.ops.fetch_message(target.id, found_id)
            embeds = current["payload"]["embeds"]
            if not embeds:
                await ctx.fail("That message has no embed to change. Use `/embed editcustom` to give it one.")
                return
            changed = self._split_title(text)
            first = {key: value for key, value in embeds[0].items() if key not in ("title", "description")}
            if "title" not in changed:
                first.pop("url", None)  # a title link can't outlive its title
            payload = messages.clean_payload({**current["payload"], "embeds": [{**first, **changed}, *embeds[1:]]})
        except (OpsError, Invalid) as error:
            await ctx.fail(str(error))
            return
        await self._edit_from_command(ctx, target, found_id, payload)

    @staticmethod
    def _channel_beside_json(ctx: commands.Context, channel: Any, text: str) -> tuple[Any, str, str | None]:
        """A typed command takes the rest of the line as one argument, so a channel named before or after the JSON comes off it here."""
        if ctx.interaction is not None or channel is not None:
            return channel, text, None
        found = CHANNEL_NEXT_TO_JSON.search(text.strip())
        if found is None:
            return channel, text, None
        place = ctx.guild.get_channel_or_thread(int(found.group(1) or found.group(2) or found.group(3) or found.group(4)))
        if place is None or not hasattr(place, "send"):
            return channel, text, "That channel isn't in this server, or the bot can't see it."
        return place, text.strip()[:found.start()] + text.strip()[found.end():], None

    @embed.command(name="custom", description="Post a message from JSON, or from a Pastebin link holding JSON")
    @requires("manage_guild")
    @app_commands.describe(json="The JSON, or a pastebin.com or starb.in link", channel="Where to post. Defaults to here.")
    async def embed_custom(self, ctx: commands.Context, *, json: str, channel: discord.TextChannel | discord.Thread | None = None) -> None:
        await ctx.defer(ephemeral=True)
        channel, json, problem = self._channel_beside_json(ctx, channel, json)
        if problem:
            await ctx.fail(problem)
            return
        try:
            payload = await self._read_json(json)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await self._post_from_command(ctx, payload, channel)

    @embed.command(name="editcustom", description="Replace a message the bot posted with JSON, or a Pastebin link holding JSON")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", json="The JSON, or a pastebin.com or starb.in link", channel="Where the message is, if you give an ID from elsewhere")
    async def embed_editcustom(self, ctx: commands.Context, message_id: str, *, json: str, channel: discord.TextChannel | discord.Thread | None = None) -> None:
        await ctx.defer(ephemeral=True)
        channel, json, problem = self._channel_beside_json(ctx, channel, json)
        if problem:
            await ctx.fail(problem)
            return
        named = await self._named_message(ctx, message_id, channel)
        if named is None:
            return
        try:
            payload = await self._read_json(json)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await self._edit_from_command(ctx, named[0], named[1], payload)

    @embed.command(name="source", description="Get a message's JSON")
    @app_commands.describe(message_id="The message's ID or link", channel="Where the message is, if you give an ID from elsewhere")
    async def embed_source(self, ctx: commands.Context, message_id: str, channel: discord.TextChannel | discord.Thread | None = None) -> None:
        await ctx.defer(ephemeral=True)
        named = await self._named_message(ctx, message_id, channel)
        if named is None:
            return
        target, found_id = named
        # This command is open to everyone, so it must not read out a channel the person asking can't.
        refusal = access.read_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        try:
            current = await self.bot.ops.fetch_message(target.id, found_id)
        except OpsError as error:
            await ctx.fail(str(error))
            return
        data = current["payload"]
        if not data["content"] and not data["buttons"] and len(data["embeds"]) == 1:
            data = data["embeds"][0]
        text = _json_dump(data)
        if len(text) <= 1900:
            await ctx.send(f"```json\n{text}\n```", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await ctx.send("It's too long for a message, so here's a file.", file=discord.File(io.BytesIO(text.encode("utf-8")), "message.json"), ephemeral=True)

    @embed.command(name="unhide", description="Bring back an embed someone closed on a message")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", channel="Where the message is, if you give an ID from elsewhere")
    async def embed_unhide(self, ctx: commands.Context, message_id: str, channel: discord.TextChannel | discord.Thread | None = None) -> None:
        await ctx.defer(ephemeral=True)
        named = await self._named_message(ctx, message_id, channel)
        if named is None:
            return
        target, found_id = named
        refusal = access.view_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        try:
            message = await target.fetch_message(found_id)
            # Discord takes Manage Messages to change this on another's message.
            if message.author.id != self.bot.user.id and not access.can_manage_messages(ctx.author, target):
                await ctx.fail("You need Manage Messages in that channel to do that.")
                return
            await message.edit(suppress=False)
        except discord.NotFound:
            await ctx.fail("That message doesn't exist.")
            return
        except discord.Forbidden:
            await ctx.fail("The bot needs Manage Messages in that channel.")
            return
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused that: {error.text or error.status}")
            return
        await ctx.ok("The embeds on that message are back.")


def _adds_ping(req: Any, was: dict, now: dict) -> bool:
    """Whether new text on a post that pings names someone the old text didn't. A role anyone can ping takes nothing, and fixing a typo around a ping keeps it."""
    new = messages.ping_words(now) - messages.ping_words(was)
    return any(not word.startswith("<@&") or delivery.needs_ping_right(req.bot.get_guild(req.guild_id), int(word[3:-1])) for word in new)


def _clean_command_embed(embed: dict) -> dict:
    payload = messages.clean_payload({"embeds": [embed]})
    if not payload["embeds"]:
        raise Invalid("Give the embed a title or a description.")
    return payload


def _safe_filename(name: str) -> str:
    """The name a file was sent with, without any folder in front. Discord shows it, and an image link in an embed can name it."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name.replace("\\", "/").rsplit("/", 1)[-1])[:100] or "file"


def _json_dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
