"""Suggestions from members, voted on with reactions and decided by staff.

Each suggestion gets a number per server. A decision is posted to the channel for that decision, or the decision channel, or the suggestion
channel. With editing on, the original post changes in place as well. With it off, a final decision moves the suggestion out of the voting channel.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.module import Module, requires
from ..core.usage import new_channel_name
from ..core.ops import MessageGone, OpsError
from ..core.settings import Actor
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, integer, optional, page_number, snowflake, text
from ..web.api import ApiError

log = logging.getLogger(__name__)

UP = "\N{THUMBS UP SIGN}"
DOWN = "\N{THUMBS DOWN SIGN}"
NUMBER_MAX = 2 ** 31  # past any real count, and a number a person types must still fit a database column
NEUTRAL = 0x4F545C
GREEN = 0x2ECC71
RED = 0xE74C3C
CONTENT_MAX = 4000
REASON_MAX = 1000
PAGE_MAX = 100

OPEN = "open"
CONSIDERED = "considered"
DECISIONS = {
    "approved": {"label": "Approved", "channel": "approve_channel"},
    "denied": {"label": "Denied", "channel": "deny_channel"},
    CONSIDERED: {"label": "Being considered", "channel": "consider_channel"},
    "implemented": {"label": "Implemented", "channel": "implemented_channel"},
}


class SuggestionError(Exception):
    """Text for the person trying to act on a suggestion."""


def vote_color(up: int, down: int, threshold: int) -> int:
    """Votes pull the color from gray toward green or red, all the way once the difference reaches the threshold. A threshold of 0 goes straight there."""
    difference = up - down
    if difference == 0:
        return NEUTRAL
    target = GREEN if difference > 0 else RED
    strength = 1.0 if threshold == 0 else min(1.0, abs(difference) / threshold)
    mixed = 0
    for shift in (16, 8, 0):
        start, end = (NEUTRAL >> shift) & 0xFF, (target >> shift) & 0xFF
        mixed |= round(start + (end - start) * strength) << shift
    return mixed


def _reason_field(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return text(REASON_MAX)(value) or None
    except Invalid as error:
        raise ValidationError({"reason": str(error)}) from error


class Suggestions(Module):
    key = "suggestions"
    category = "Suggestions"
    schema = Schema({
        "channel_id": Field(None, optional(snowflake)),
        "decision_channel": Field(None, optional(snowflake)),
        "approve_channel": Field(None, optional(snowflake)),
        "deny_channel": Field(None, optional(snowflake)),
        "consider_channel": Field(None, optional(snowflake)),
        "implemented_channel": Field(None, optional(snowflake)),
        "submit_channel": Field(None, optional(snowflake)),
        "threshold": Field(5, integer(0, 100)),
        "dm": Field(False, boolean),
        "edit": Field(False, boolean),
        "anon": Field(False, boolean),
    })
    migrations = [
        """
        CREATE TABLE suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            author_avatar TEXT,
            anonymous INTEGER NOT NULL,
            content TEXT NOT NULL,
            channel_id INTEGER,
            message_id INTEGER,
            up INTEGER NOT NULL DEFAULT 0,
            down INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'open',
            decided_by INTEGER,
            decided_by_name TEXT,
            reason TEXT,
            decided_at REAL,
            entry_channel_id INTEGER,
            entry_message_id INTEGER,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX suggestions_number ON suggestions (guild_id, number);
        CREATE INDEX suggestions_message ON suggestions (guild_id, message_id);
        CREATE INDEX suggestions_entry ON suggestions (guild_id, entry_message_id);
        CREATE INDEX suggestions_state ON suggestions (guild_id, state, number DESC);
        """,
    ]

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("channel_id", "decision_channel", "approve_channel", "deny_channel", "consider_channel", "implemented_channel")}

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._decide_locks: dict[tuple[int, int], asyncio.Lock] = {}

    def _decide_lock(self, guild_id: int, number: int) -> asyncio.Lock:
        """One decision at a time on a suggestion. Each reads the row, posts, then writes, so two together would leave a post the row no longer knows of."""
        key = (guild_id, number)
        lock = self._decide_locks.get(key)
        if lock is None:
            if len(self._decide_locks) > 1000:
                self._decide_locks = {held: value for held, value in self._decide_locks.items() if value.locked()}
            lock = self._decide_locks[key] = asyncio.Lock()
        return lock

    # ---- posting ----

    def _embed(self, row: dict, state: str, up: int, down: int, threshold: int) -> dict:
        embed: dict[str, Any] = {"description": row["content"], "footer": {"text": f"Suggestion #{row['number']}"}, "timestamp": _iso(row["created_at"])}
        if not row["anonymous"]:
            embed["author"] = {"name": row["author_name"][:256]}
            if row["author_avatar"]:
                embed["author"]["icon_url"] = row["author_avatar"]
        if state == OPEN:
            embed["title"] = f"Suggestion #{row['number']}"
            embed["color"] = NEUTRAL
            return embed
        label = DECISIONS[state]["label"]
        embed["title"] = f"Suggestion #{row['number']}: {label}"
        embed["color"] = vote_color(up, down, threshold)
        embed["fields"] = [
            {"name": f"{label} by {(row['decided_by_name'] or 'a moderator')[:200]}", "value": row["reason"] or "No reason given.", "inline": False},
            {"name": "Votes", "value": f"{UP} {up}   {DOWN} {down}", "inline": True},
        ]
        return embed

    @staticmethod
    def _message(embed: dict) -> dict:
        return {"content": "", "embeds": [embed], "buttons": []}

    async def create(self, guild_id: int, member: Any, content: str) -> dict:
        """Post a new suggestion and start its vote. Raises SuggestionError or OpsError with the reason it can't happen."""
        cfg = self.cfg(guild_id)
        if not cfg["channel_id"] or self.bot.ops.channel(guild_id, cfg["channel_id"]) is None:
            raise SuggestionError("This server has no suggestion channel yet.")
        now = time.time()
        async with self.db.transaction():
            number = await self.db.scalar("SELECT COALESCE(MAX(number), 0) + 1 FROM suggestions WHERE guild_id = ?", (guild_id,))
            row_id = await self.db.insert(
                "INSERT INTO suggestions (guild_id, number, author_id, author_name, author_avatar, anonymous, content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, number, member.id, member.display_name, str(member.display_avatar.url), int(cfg["anon"]), content, now),
            )
        row = await self.db.one("SELECT * FROM suggestions WHERE id = ?", (row_id,))
        ops = self.bot.ops
        try:
            sent = await ops.send_message(cfg["channel_id"], self._message(self._embed(row, OPEN, 0, 0, cfg["threshold"])), silent=True)
        except OpsError:
            await self.db.run("DELETE FROM suggestions WHERE id = ?", (row_id,))  # gives the number back, since nobody saw it
            raise
        await self.db.run("UPDATE suggestions SET channel_id = ?, message_id = ? WHERE id = ?", (cfg["channel_id"], sent["id"], row_id))
        for emoji in (UP, DOWN):
            try:
                await ops.add_reaction(cfg["channel_id"], sent["id"], emoji)
            except OpsError as error:
                log.info("Could not add a vote reaction: %s", error)
        return {**row, "channel_id": cfg["channel_id"], "message_id": sent["id"], "url": sent["url"]}

    async def _count_votes(self, row: dict) -> tuple[int, int]:
        """The count on Discord right now, without the bot's own two reactions. Falls back on what events added up."""
        if row["channel_id"] and row["message_id"]:
            try:
                current = await self.bot.ops.fetch_message(row["channel_id"], row["message_id"])
            except OpsError:
                return row["up"], row["down"]
            counts = {r["emoji"]: r["count"] - (1 if r.get("me") else 0) for r in current["reactions"]}
            return max(counts.get(UP, 0), 0), max(counts.get(DOWN, 0), 0)
        return row["up"], row["down"]

    async def decide(self, guild_id: int, number: int, decision: str, *, moderator: Actor, reason: str | None) -> dict:
        """Record a decision, update Discord and tell the author. Raises SuggestionError or OpsError with the reason it can't happen."""
        async with self._decide_lock(guild_id, number):
            return await self._decide(guild_id, number, decision, moderator=moderator, reason=reason)

    async def _decide(self, guild_id: int, number: int, decision: str, *, moderator: Actor, reason: str | None) -> dict:
        cfg = self.cfg(guild_id)
        row = await self.db.one("SELECT * FROM suggestions WHERE guild_id = ? AND number = ?", (guild_id, number)) if 0 < number < NUMBER_MAX else None
        if row is None:
            raise SuggestionError(f"There is no suggestion #{number}.")
        ops = self.bot.ops
        up, down = await self._count_votes(row)
        decided = {**row, "decided_by_name": moderator.name, "reason": reason}
        message = self._message(self._embed(decided, decision, up, down, cfg["threshold"]))
        target = cfg[DECISIONS[decision]["channel"]] or cfg["decision_channel"]
        # A setting can hold a channel that has since been deleted or never belonged to this server.
        if target and ops.channel(guild_id, target) is None:
            target = None

        edited = False
        if cfg["edit"] and row["message_id"]:
            try:
                await ops.edit_message(row["channel_id"], row["message_id"], message)
                edited = True
            except MessageGone:
                await self.db.run("UPDATE suggestions SET message_id = NULL WHERE id = ?", (row["id"],))
                row = {**row, "message_id": None}
        entry_channel, entry_message = row["entry_channel_id"], row["entry_message_id"]
        previous_entry = (entry_channel, entry_message) if entry_message else None
        if target and target == row["channel_id"] and edited:
            target = None  # the edited post already says it, in the channel it would go to
        if not edited or target:
            where = target or cfg["channel_id"] or row["channel_id"]
            if where is None or ops.channel(guild_id, where) is None:
                raise SuggestionError("This server has no channel left to post that decision in.")
            sent = await ops.send_message(where, message, silent=True)
            entry_channel, entry_message = where, sent["id"]
        else:
            entry_channel, entry_message = None, None

        keep_original = cfg["edit"] or decision == CONSIDERED
        await self.db.run(
            "UPDATE suggestions SET state = ?, up = ?, down = ?, decided_by = ?, decided_by_name = ?, reason = ?, decided_at = ?, entry_channel_id = ?, entry_message_id = ?, "
            "channel_id = ?, message_id = ? WHERE id = ?",
            (decision, up, down, moderator.id, moderator.name, reason, time.time(), entry_channel, entry_message,
             row["channel_id"] if keep_original else None, row["message_id"] if keep_original else None, row["id"]),
        )
        stale = []
        if previous_entry and previous_entry[1] != entry_message:
            stale.append(previous_entry)
        if not keep_original and row["message_id"]:
            stale.append((row["channel_id"], row["message_id"]))
        for channel_id, message_id in stale:
            try:
                await ops.delete_message(channel_id, message_id)
            except OpsError as error:
                log.info("Could not delete an old suggestion post: %s", error)
        if cfg["dm"]:
            await self._tell_author(guild_id, row, decision, moderator, reason)
        return await self.db.one("SELECT * FROM suggestions WHERE id = ?", (row["id"],))

    async def _tell_author(self, guild_id: int, row: dict, decision: str, moderator: Actor, reason: str | None) -> None:
        info = self.bot.get_guild(guild_id)
        where = info.name if info else "the server"
        embed = {
            "title": f"Your suggestion in {where}"[:256],
            "description": row["content"],
            "color": GREEN if decision in ("approved", "implemented") else RED if decision == "denied" else NEUTRAL,
            "fields": [{"name": f"{DECISIONS[decision]['label']} by {moderator.name}"[:256], "value": reason or "No reason given.", "inline": False}],
        }
        try:
            await self.bot.ops.send_dm(row["author_id"], self._message(embed))
        except OpsError:
            pass  # plenty of people close their DMs

    async def get(self, guild_id: int, number: int) -> dict | None:
        """A suggestion by its number. None if there's no such suggestion."""
        if not 0 < number < NUMBER_MAX:
            return None
        return await self.db.one("SELECT * FROM suggestions WHERE guild_id = ? AND number = ?", (guild_id, number))

    # ---- votes and deleted posts ----

    async def _vote_changed(self, payload: Any, change: int) -> None:
        if payload.guild_id is None or payload.emoji.id is not None or payload.emoji.name not in (UP, DOWN):
            return
        if payload.channel_id != self.cfg(payload.guild_id)["channel_id"]:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or payload.user_id == guild.me.id:
            return
        if change > 0 and payload.member is not None and payload.member.bot:
            return
        column = "up" if payload.emoji.name == UP else "down"
        await self.db.run(f"UPDATE suggestions SET {column} = MAX({column} + ?, 0) WHERE guild_id = ? AND message_id = ?", (change, payload.guild_id, payload.message_id))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._vote_changed(payload, 1)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._vote_changed(payload, -1)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        if payload.guild_id and payload.channel_id == self.cfg(payload.guild_id)["channel_id"]:
            await self.db.run("UPDATE suggestions SET up = 0, down = 0 WHERE guild_id = ? AND message_id = ?", (payload.guild_id, payload.message_id))

    async def _posts_gone(self, guild_id: int | None, message_ids: Any) -> None:
        """A post can be in any channel: the setting may have moved since, and decisions have channels of their own. Both indexes are on the message ID, so this stays cheap on every delete."""
        if not guild_id:
            return
        ids = list(message_ids)
        marks = ", ".join("?" * len(ids))
        await self.db.run(f"UPDATE suggestions SET message_id = NULL WHERE guild_id = ? AND message_id IN ({marks})", (guild_id, *ids))
        await self.db.run(f"UPDATE suggestions SET entry_message_id = NULL, entry_channel_id = NULL WHERE guild_id = ? AND entry_message_id IN ({marks})", (guild_id, *ids))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self._posts_gone(payload.guild_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        await self._posts_gone(payload.guild_id, payload.message_ids)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        guild_id = channel.guild.id
        cfg = self.cfg(guild_id)
        cleared = {name: None for name in ("channel_id", "decision_channel", "approve_channel", "deny_channel", "consider_channel", "implemented_channel", "submit_channel") if cfg[name] == channel.id}
        if cleared:
            await self.bot.settings.save(guild_id, self.key, cleared, action="suggestions.channel_deleted")
        await self.db.run("UPDATE suggestions SET message_id = NULL WHERE guild_id = ? AND channel_id = ?", (guild_id, channel.id))
        await self.db.run("UPDATE suggestions SET entry_message_id = NULL, entry_channel_id = NULL WHERE guild_id = ? AND entry_channel_id = ?", (guild_id, channel.id))

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/suggestions/list", self._list_endpoint)
        router.post("/suggestions/create-channel", self._create_channel_endpoint)
        router.post(r"/suggestions/{number:\d+}/decide", self._decide_endpoint)
        router.delete(r"/suggestions/{number:\d+}", self._delete_endpoint)

    def _view(self, guild_id: int, row: dict) -> dict:
        def link(channel_id: int | None, message_id: int | None) -> str | None:
            return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}" if channel_id and message_id else None

        return {
            "number": row["number"], "author_id": row["author_id"], "author_name": row["author_name"], "author_avatar": row["author_avatar"], "anonymous": bool(row["anonymous"]),
            "content": row["content"], "up": row["up"], "down": row["down"], "state": row["state"], "decided_by_name": row["decided_by_name"], "reason": row["reason"],
            "decided_at": row["decided_at"], "created_at": row["created_at"], "url": link(row["channel_id"], row["message_id"]), "entry_url": link(row["entry_channel_id"], row["entry_message_id"]),
        }

    async def _list_endpoint(self, req: Any) -> dict:
        where = "guild_id = ?"
        args: list[Any] = [req.guild_id]
        state = req.query.get("state", "")
        if state:
            where += " AND state = ?"
            args.append(state)
        needle = req.query.get("q", "").strip().lower()[:200]
        if needle:
            where += " AND (LOWER(content) LIKE ? ESCAPE '\\' OR LOWER(author_name) LIKE ? ESCAPE '\\')"
            pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [pattern, pattern]
        page = page_number(req.query.get("page", "1"), 1, NUMBER_MAX)
        size = page_number(req.query.get("size", "10"), 10, PAGE_MAX)
        total = await self.db.scalar(f"SELECT COUNT(*) FROM suggestions WHERE {where}", args)
        rows = await self.db.all(f"SELECT * FROM suggestions WHERE {where} ORDER BY number DESC LIMIT ? OFFSET ?", (*args, size, (page - 1) * size))
        return {"rows": [self._view(req.guild_id, row) for row in rows], "total": total, "page": page, "size": size}

    async def _create_channel_endpoint(self, req: Any) -> dict:
        req.require_permission("manage_channels")
        form = await req.json()
        try:
            name = text(100, minimum=1)(form.get("name"))
        except Invalid as error:
            raise ValidationError({"name": str(error)}) from error
        channel = await req.ops.create_channel(req.guild_id, name, read_only=True)
        await req.settings.save(req.guild_id, self.key, {"channel_id": channel["id"]}, actor=req.actor, action="suggestions.create_channel")
        return {"channel": channel, "settings": self.cfg(req.guild_id)}

    async def _decide_endpoint(self, req: Any) -> dict:
        form = await req.json()
        decision = form.get("decision")
        if not isinstance(decision, str) or decision not in DECISIONS:
            raise ValidationError({"decision": "Pick a decision."})
        number = req.int_param("number")
        try:
            row = await self.decide(req.guild_id, number, decision, moderator=req.actor, reason=_reason_field(form.get("reason")))
        except SuggestionError as error:
            raise ApiError(404, str(error)) from error
        await req.record(f"suggestion.{decision}", detail=f"#{number}")
        return self._view(req.guild_id, row)

    async def _delete_endpoint(self, req: Any) -> dict:
        number = req.int_param("number")
        row = await self.db.one("SELECT * FROM suggestions WHERE guild_id = ? AND number = ?", (req.guild_id, number))
        if row is None:
            raise ApiError(404, f"There is no suggestion #{number}.")
        proposals = self.bot.module("proposals")
        running = await proposals.vote_on_suggestion(req.guild_id, number) if proposals else None
        if running:
            raise ApiError(409, f"Suggestion #{number} is being voted on as proposal #{running}. Withdraw the proposal first.")
        left = []
        for channel_id, message_id in ((row["channel_id"], row["message_id"]), (row["entry_channel_id"], row["entry_message_id"])):
            if channel_id and message_id:
                try:
                    await req.ops.delete_message(channel_id, message_id)
                except OpsError as error:  # a channel that's gone or a refused delete mustn't keep the row forever
                    log.info("Could not delete a post of suggestion #%s in %s: %s", number, req.guild_id, error)
                    left.append(f"https://discord.com/channels/{req.guild_id}/{channel_id}/{message_id}")
        await self.db.run("DELETE FROM suggestions WHERE id = ?", (row["id"],))
        if proposals:  # numbers get used again, and an old vote's result must never land on the next suggestion to hold this one
            await proposals.forget_suggestion(req.guild_id, number)
        await req.record("suggestion.delete", detail=f"#{number}")
        return {"ok": True, "left": left}

    # ---- /suggestion ----

    async def _save(self, ctx: commands.Context, action: str, form: dict) -> bool:
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values()), "That can't be saved."))
            return False
        return True

    @commands.hybrid_group(name="suggestion", fallback="server", description="Show how suggestions are set up")
    @commands.guild_only()
    @requires("manage_guild")
    async def suggestion(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)

        def where(channel_id: int | None, otherwise: str) -> str:
            return f"<#{channel_id}>" if channel_id else otherwise

        await ctx.send(
            f"Suggestion channel: {where(cfg['channel_id'], 'not set')}\nDecision channel: {where(cfg['decision_channel'], 'none')}\n"
            f"Approved: {where(cfg['approve_channel'], 'decision channel')}, denied: {where(cfg['deny_channel'], 'decision channel')}, "
            f"considered: {where(cfg['consider_channel'], 'decision channel')}, implemented: {where(cfg['implemented_channel'], 'decision channel')}\n"
            f"Suggestions only from: {where(cfg['submit_channel'], 'anywhere')}\nVote color intensity: {cfg['threshold']}\n"
            f"DM on decision: {'on' if cfg['dm'] else 'off'}, edit in place: {'on' if cfg['edit'] else 'off'}, anonymous: {'on' if cfg['anon'] else 'off'}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @suggestion.command(name="channel", description="Set the suggestion channel, or make a new one")
    @requires("manage_guild")
    @app_commands.describe(channel="An existing channel to use", name="The name for a new channel. Defaults to suggestions.")
    async def suggestion_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None, *, name: str = "suggestions") -> None:
        await ctx.defer(ephemeral=True)
        name, problem = new_channel_name(ctx, name, "suggestions")
        if problem:
            await ctx.fail(problem)
            return
        channel_id = await self._channel_or_new(ctx, channel, name)
        if channel_id and await self._save(ctx, "suggestion.channel", {"channel_id": channel_id}):
            await ctx.ok(f"Suggestions now go to <#{channel_id}>.")

    async def _channel_or_new(self, ctx: commands.Context, channel: Any, name: str) -> int | None:
        """The channel given, or one with this name, or a new one members can read and react in. None after telling the person if they couldn't post there or make one themselves, or if Discord refuses."""
        found = channel or discord.utils.get(ctx.guild.text_channels, name=name)
        if found is not None:
            refusal = access.post_refusal(ctx.author, found)
            if refusal:
                await ctx.fail(refusal)
                return None
            return found.id
        if not access.is_admin(ctx.author) and not ctx.author.guild_permissions.manage_channels:
            await ctx.fail("You need Manage Channels to make a channel. Name one that's already there.")
            return None
        try:
            return (await self.bot.ops.create_channel(ctx.guild.id, name[:100], read_only=True))["id"]
        except OpsError as error:
            await ctx.fail(str(error))
            return None

    async def _flip(self, ctx: commands.Context, field: str, action: str, on_text: str, off_text: str) -> None:
        turned_on = not self.cfg(ctx.guild.id)[field]
        if await self._save(ctx, action, {field: turned_on}):
            await ctx.ok(on_text if turned_on else off_text)

    @suggestion.command(name="dm", description="Turn telling the author about a decision on or off")
    @requires("manage_guild")
    async def suggestion_dm(self, ctx: commands.Context) -> None:
        await self._flip(ctx, "dm", "suggestion.dm", "Authors now get a DM when their suggestion is decided.", "Authors no longer get a DM about decisions.")

    @suggestion.command(name="edit", description="Turn editing the original post with the decision on or off")
    @requires("manage_guild")
    async def suggestion_edit(self, ctx: commands.Context) -> None:
        await self._flip(ctx, "edit", "suggestion.edit", "Decisions now edit the original post.", "Decisions now post a new message.")

    @suggestion.command(name="anonymous", aliases=["anon"], description="Turn hiding who made a suggestion on or off. Past ones stay as they are.")
    @requires("manage_guild")
    async def suggestion_anonymous(self, ctx: commands.Context) -> None:
        await self._flip(ctx, "anon", "suggestion.anonymous", "New suggestions hide who made them.", "New suggestions show who made them.")

    @suggestion.command(name="limit", description="Set the vote difference that turns a decision fully red or green")
    @requires("manage_guild")
    @app_commands.describe(limit="Between 0 and 100. With 0, any difference gives the full color.")
    async def suggestion_limit(self, ctx: commands.Context, limit: int) -> None:
        if await self._save(ctx, "suggestion.limit", {"threshold": limit}):
            await ctx.ok(f"The vote color intensity is now {limit}.")

    @suggestion.command(name="move", description="Set a channel for decisions, or make one named suggestion-log")
    @requires("manage_guild")
    async def suggestion_move(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await ctx.defer(ephemeral=True)
        channel_id = await self._channel_or_new(ctx, channel, "suggestion-log")
        if channel_id and await self._save(ctx, "suggestion.move", {"decision_channel": channel_id}):
            await ctx.ok(f"Decisions now go to <#{channel_id}>. To stop that, clear the decision channel on the dashboard.")

    @suggestion.command(name="submit", description="Limit suggestions to one channel. Leave it out to allow them anywhere.")
    @requires("manage_guild")
    async def suggestion_submit(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        refusal = access.view_refusal(ctx.author, channel) if channel is not None else None
        if refusal:
            await ctx.fail(refusal)
            return
        if await self._save(ctx, "suggestion.submit", {"submit_channel": channel.id if channel else None}):
            await ctx.ok(f"Suggestions can now only be made in {channel.mention}." if channel else "Suggestions can now be made anywhere.")

    @suggestion.command(name="who", description="Show who made a suggestion")
    @requires("manage_guild")
    @app_commands.describe(suggestion_id="The suggestion's number")
    async def suggestion_who(self, ctx: commands.Context, suggestion_id: int) -> None:
        row = await self.db.one("SELECT author_id, author_name FROM suggestions WHERE guild_id = ? AND number = ?", (ctx.guild.id, suggestion_id)) if 0 < suggestion_id < NUMBER_MAX else None
        if row is None:
            await ctx.fail(f"There is no suggestion #{suggestion_id}.")
            return
        await ctx.send(f"Suggestion #{suggestion_id} came from <@{row['author_id']}> ({discord.utils.escape_markdown(row['author_name'])}).", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @suggestion.command(name="suggest", description="Make a suggestion")
    @commands.cooldown(1, 30, commands.BucketType.member)
    @app_commands.describe(content="What you suggest")
    async def suggestion_suggest(self, ctx: commands.Context, *, content: str) -> None:
        cfg = self.cfg(ctx.guild.id)
        # A typed suggestion sits in the open with its author's name on it, which anonymous mode can't allow. It comes down first, and the answers don't stay either.
        hidden = cfg["anon"] and ctx.interaction is None
        if hidden and not await self._take_down_typed(ctx):
            return
        fail: dict[str, Any] = {"delete_after": 10} if hidden else {}
        await ctx.defer(ephemeral=True)
        cleaned = content.strip()
        if not cleaned:
            await ctx.fail("Write what you suggest.", **fail)
            return
        if len(cleaned) > CONTENT_MAX:
            await ctx.fail(f"A suggestion can be at most {CONTENT_MAX} characters. This is {len(cleaned)}.", **fail)
            return
        if cfg["submit_channel"] and ctx.channel.id != cfg["submit_channel"]:
            await ctx.fail(f"Suggestions can only be made in <#{cfg['submit_channel']}>.", **fail)
            return
        try:
            row = await self.create(ctx.guild.id, ctx.author, cleaned)
        except (SuggestionError, OpsError) as error:
            await ctx.fail(str(error), **fail)
            return
        answer = f"Your suggestion is #{row['number']}: {row['url']}"
        if not hidden:
            await ctx.ok(answer)
            return
        try:
            await ctx.author.send(answer)
        except discord.HTTPException:  # their DMs are closed, and a number or a link here would name them
            await commands.Context.send(ctx, "Received.", delete_after=8)

    async def _take_down_typed(self, ctx: commands.Context) -> bool:
        """False when the message can't come down, after saying so, because going ahead would pair the name with the suggestion."""
        try:
            await ctx.message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            await ctx.fail("Suggestions here are anonymous, and I can't remove your message to keep it that way. Use the slash command /suggestion suggest.", delete_after=15)
            return False
        return True

    @suggestion_suggest.error
    async def _suggest_refused(self, ctx: commands.Context, error: Exception) -> None:
        """A refusal before the command's body, such as the cooldown, would leave an anonymous suggestion up with the name on it."""
        if ctx.interaction is not None or not self.cfg(ctx.guild.id)["anon"]:
            return
        if isinstance(error, (commands.CheckFailure, commands.UserInputError, commands.CommandOnCooldown, commands.DisabledCommand)):
            ctx.error_answered = True
            if await self._take_down_typed(ctx):
                await ctx.fail(str(error), delete_after=10)

    async def _decide_from_command(self, ctx: commands.Context, number: int, decision: str, reason: str | None) -> None:
        await ctx.defer(ephemeral=True)
        if reason and len(reason) > REASON_MAX:
            await ctx.fail(f"A reason can be at most {REASON_MAX} characters.")
            return
        actor = self.bot.actor(ctx.author)
        try:
            await self.decide(ctx.guild.id, number, decision, moderator=actor, reason=reason)
        except (SuggestionError, OpsError) as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, actor, f"suggestion.{decision}", detail=f"#{number}")
        await ctx.ok(f"Suggestion #{number}: {DECISIONS[decision]['label'].lower()}.")

    @suggestion.command(name="approve", description="Approve a suggestion")
    @requires("manage_guild")
    @app_commands.describe(suggestion_id="The suggestion's number", reason="Why, shown with the decision")
    async def suggestion_approve(self, ctx: commands.Context, suggestion_id: int, *, reason: str | None = None) -> None:
        await self._decide_from_command(ctx, suggestion_id, "approved", reason)

    @suggestion.command(name="deny", description="Deny a suggestion")
    @requires("manage_guild")
    @app_commands.describe(suggestion_id="The suggestion's number", reason="Why, shown with the decision")
    async def suggestion_deny(self, ctx: commands.Context, suggestion_id: int, *, reason: str | None = None) -> None:
        await self._decide_from_command(ctx, suggestion_id, "denied", reason)

    @suggestion.command(name="consider", description="Mark a suggestion as being considered")
    @requires("manage_guild")
    @app_commands.describe(suggestion_id="The suggestion's number", reason="Why, shown with the decision")
    async def suggestion_consider(self, ctx: commands.Context, suggestion_id: int, *, reason: str | None = None) -> None:
        await self._decide_from_command(ctx, suggestion_id, CONSIDERED, reason)

    @suggestion.command(name="implemented", description="Mark a suggestion as implemented")
    @requires("manage_guild")
    @app_commands.describe(suggestion_id="The suggestion's number", reason="Why, shown with the decision")
    async def suggestion_implemented(self, ctx: commands.Context, suggestion_id: int, *, reason: str | None = None) -> None:
        await self._decide_from_command(ctx, suggestion_id, "implemented", reason)


def _iso(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()
