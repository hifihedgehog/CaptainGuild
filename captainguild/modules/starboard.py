"""A board of the server's favorite messages. Enough star reactions and a message is copied to the board channel.

Stars are counted from raw events, because the bot keeps no message cache. The first star the bot sees on a message also brings in the
stars it already holds, from before the bot arrived or while it was down. A person's star counts once, whether it's on the message or on
its board post. A board post a moderator deletes stays gone, even if the message collects more stars later.

A board post copies a message to everyone who can open the board, so a message stays off the board when one of them couldn't read its
channel, unless staff opted that channel in.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.module import Module, requires
from ..core.usage import new_channel_name
from ..core.ops import OpsError
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, integer, optional, snowflake, snowflakes, text
from ..web.api import ApiError

log = logging.getLogger(__name__)

STAR = "\N{WHITE MEDIUM STAR}"
VARIATION = "\N{VARIATION SELECTOR-16}"
GOLD = 0xFFAC33
CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_~]{2,32}:(\d{15,21})>")
MESSAGE_LINK = re.compile(r"/channels/\d+/\d+/(\d+)")
CHANNEL_WORD = re.compile(r"<#(\d+)>|(\d{15,21})")
SPOILER = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
LINK = re.compile(r"https?://[^\s|<>]+")
ON_MESSAGE, ON_POST = 0, 1
TOP = 25
EARLIER_STARS_MAX = 1000
CANT_POST = "The bot can't post in the starboard channel. It needs View Channel, Send Messages and Embed Links there."
NOT_A_BOARD = "The starboard channel is gone or can't hold messages. Pick another one."
NO_TEXT = "The bot can't read message text, so it can't copy messages to the board."


def _emoji_value(value: Any) -> str:
    """One emoji: a server's own as <:name:id>, or a Unicode one. Letters, digits, plain punctuation and spaces can't be a reaction, so they're refused here and not by Discord later."""
    cleaned = text(100, minimum=1)(value)
    if CUSTOM_EMOJI.fullmatch(cleaned):
        return cleaned
    keycap = "\N{COMBINING ENCLOSING KEYCAP}" in cleaned  # the number emoji start with a plain digit
    if len(cleaned) > 16 or cleaned.isascii() or any(c.isspace() or c.isalpha() or c.isdigit() and not keycap for c in cleaned):
        raise Invalid("That isn't an emoji.")
    return cleaned


def _is_people(role: Any) -> bool:
    """A bot's role says nothing about who reads a channel. The role Discord gives boosters is managed too, and that one is people."""
    return not role.managed or role.is_premium_subscriber()


def wider_audience(wide: Any, narrow: Any) -> bool:
    """Whether someone who can open `wide` couldn't read `narrow`, going by roles. Nobody's role says who was added to a private thread, so one always has a narrower audience."""
    if narrow.type == discord.ChannelType.private_thread:
        return True
    for role in wide.guild.roles:
        if _is_people(role) and access.permissions_in(role, wide).view_channel:
            allowed = access.permissions_in(role, narrow)
            if not allowed.view_channel or not allowed.read_message_history:
                return True
    return False


def _forward_of(message: Any) -> Any:
    """What a forward carries. It has no text or files of its own."""
    if message.content or message.attachments:
        return None
    return next(iter(getattr(message, "message_snapshots", None) or []), None)


class Starboard(Module):
    key = "starboard"
    category = "Starboard"
    schema = Schema({
        "channel_id": Field(None, optional(snowflake)),
        "threshold": Field(3, integer(1, 100)),
        "nsfw": Field(False, boolean),
        "show_jump": Field(True, boolean),
        "star_self": Field(False, boolean),
        "locked": Field(False, boolean),
        "autostar": Field(False, boolean),
        "emoji": Field(None, optional(_emoji_value)),
        "blacklist": Field([], snowflakes()),
        "allow_private": Field([], snowflakes()),
    })
    migrations = [
        """
        CREATE TABLE starboard_entries (
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            author_id INTEGER,
            stars INTEGER NOT NULL DEFAULT 0,
            star_channel_id INTEGER,
            star_message_id INTEGER,
            removed INTEGER NOT NULL DEFAULT 0,
            preview TEXT,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, message_id)
        );
        CREATE INDEX starboard_entries_post ON starboard_entries (guild_id, star_message_id);
        CREATE INDEX starboard_entries_top ON starboard_entries (guild_id, stars DESC);
        CREATE INDEX starboard_entries_author ON starboard_entries (guild_id, author_id);
        CREATE TABLE starboard_stars (
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            place INTEGER NOT NULL,
            PRIMARY KEY (guild_id, message_id, user_id, place)
        );
        CREATE INDEX starboard_stars_user ON starboard_stars (guild_id, user_id);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        # One message's stars change one at a time, or two quick stars could both post it. Different messages don't wait on each other.
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self._last_errors: dict[int, tuple[str, float]] = {}

    def _lock(self, message_id: int) -> asyncio.Lock:
        lock = self._locks.get(message_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[message_id] = lock
        return lock

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {"channel_id": {settings["channel_id"]}} if settings.get("channel_id") else {}

    # ---- what counts ----

    @staticmethod
    def _is_star(cfg: dict, emoji: Any) -> bool:
        wanted = cfg["emoji"] or STAR
        custom = CUSTOM_EMOJI.fullmatch(wanted)
        if custom:
            return emoji.id == int(custom.group(1))  # a renamed emoji keeps its ID
        return emoji.id is None and (emoji.name or "").replace(VARIATION, "") == wanted.replace(VARIATION, "")

    @staticmethod
    def _channel_counts(cfg: dict, channel: Any) -> bool:
        if channel is None or channel.id == cfg["channel_id"]:
            return False
        # An ignored category covers every channel and thread in it.
        places = {channel.id, getattr(channel, "parent_id", None), getattr(channel, "category_id", None)}
        return not places & set(cfg["blacklist"])

    async def _entry(self, guild_id: int, message_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM starboard_entries WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))

    async def _entry_of_post(self, guild_id: int, post_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM starboard_entries WHERE guild_id = ? AND star_message_id = ?", (guild_id, post_id))

    async def _recount(self, guild_id: int, message_id: int) -> int:
        count = await self.db.scalar("SELECT COUNT(DISTINCT user_id) FROM starboard_stars WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))
        await self.db.run("UPDATE starboard_entries SET stars = ? WHERE guild_id = ? AND message_id = ?", (count, guild_id, message_id))
        return count

    async def _fill_earlier_stars(self, guild: Any, cfg: dict, channel_id: int, message_id: int) -> None:
        """Stars the message already holds, read the one time the bot first hears of it."""
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return
        rows = []
        try:
            message = await channel.fetch_message(message_id)
            for reaction in message.reactions:
                emoji = discord.PartialEmoji(name=reaction.emoji) if isinstance(reaction.emoji, str) else reaction.emoji
                if self._is_star(cfg, emoji) and reaction.count > 1:
                    async for person in reaction.users(limit=EARLIER_STARS_MAX):
                        if not person.bot and (cfg["star_self"] or person.id != message.author.id):
                            rows.append((guild.id, message_id, person.id, ON_MESSAGE))
        except discord.HTTPException as error:
            log.info("Could not read the earlier stars on %s: %s", message_id, error)
            return
        if rows:
            await self.db.many("INSERT INTO starboard_stars (guild_id, message_id, user_id, place) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING", rows)

    # ---- reactions ----

    def _usable(self, payload: Any) -> tuple[Any, dict] | None:
        """The server and its settings, when this reaction event could change a count at all."""
        if payload.guild_id is None:
            return None
        cfg = self.cfg(payload.guild_id)
        if not cfg["channel_id"] or cfg["locked"] or not self._is_star(cfg, payload.emoji):
            return None
        guild = self.bot.get_guild(payload.guild_id)
        return (guild, cfg) if guild is not None else None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        usable = self._usable(payload)
        if usable is None or (payload.member is not None and payload.member.bot):
            return
        guild, cfg = usable
        post = await self._entry_of_post(guild.id, payload.message_id)
        if post is not None:
            place, message_id, channel_id, author_id = ON_POST, post["message_id"], post["channel_id"], post["author_id"]
        else:
            channel = guild.get_channel_or_thread(payload.channel_id)
            if not self._channel_counts(cfg, channel):
                return
            place, message_id, channel_id, author_id = ON_MESSAGE, payload.message_id, payload.channel_id, payload.message_author_id
            if author_id is None:
                try:
                    author_id = (await channel.fetch_message(message_id)).author.id
                except discord.HTTPException:
                    return
        if author_id == payload.user_id and not cfg["star_self"]:
            return
        async with self._lock(message_id):
            await self.db.run(
                "INSERT INTO starboard_stars (guild_id, message_id, user_id, place) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (guild.id, message_id, payload.user_id, place),
            )
            first_seen = await self.db.run(
                "INSERT INTO starboard_entries (guild_id, message_id, channel_id, author_id, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (guild.id, message_id, channel_id, author_id, time.time()),
            )
            if first_seen:
                await self._fill_earlier_stars(guild, cfg, channel_id, message_id)
            await self._recount(guild.id, message_id)
            await self._refresh(guild, cfg, message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        usable = self._usable(payload)
        if usable is None:
            return
        guild, cfg = usable
        post = await self._entry_of_post(guild.id, payload.message_id)
        place, message_id = (ON_POST, post["message_id"]) if post is not None else (ON_MESSAGE, payload.message_id)
        async with self._lock(message_id):
            removed = await self.db.run(
                "DELETE FROM starboard_stars WHERE guild_id = ? AND message_id = ? AND user_id = ? AND place = ?",
                (guild.id, message_id, payload.user_id, place),
            )
            if removed:
                await self._recount(guild.id, message_id)
                await self._refresh(guild, cfg, message_id)

    async def _reactions_cleared(self, payload: Any) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        cfg = self.cfg(guild.id)
        if cfg["locked"]:
            return
        post = await self._entry_of_post(guild.id, payload.message_id)
        place, message_id = (ON_POST, post["message_id"]) if post is not None else (ON_MESSAGE, payload.message_id)
        async with self._lock(message_id):
            removed = await self.db.run("DELETE FROM starboard_stars WHERE guild_id = ? AND message_id = ? AND place = ?", (guild.id, message_id, place))
            if removed:
                await self._recount(guild.id, message_id)
                await self._refresh(guild, cfg, message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        await self._reactions_cleared(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        if payload.guild_id and self._is_star(self.cfg(payload.guild_id), payload.emoji):
            await self._reactions_cleared(payload)

    # ---- the board post ----

    async def _refresh(self, guild: Any, cfg: dict, message_id: int) -> None:
        """Make the board match the count. Call this holding the message's lock."""
        entry = await self._entry(guild.id, message_id)
        if entry is None or entry["removed"]:
            return
        if entry["stars"] < cfg["threshold"]:
            if entry["star_message_id"]:
                await self._delete_post(guild, entry)
        elif entry["star_message_id"]:
            await self._show_count(guild, cfg, entry)
        else:
            await self._create_post(guild, cfg, entry)

    def _count_line(self, cfg: dict, stars: int, channel_id: int) -> str:
        return f"{cfg['emoji'] or STAR} **{stars}** <#{channel_id}>"

    def _build_embed(self, cfg: dict, message: Any, source: Any, board: Any) -> discord.Embed:
        hide_images = source.is_nsfw() and not cfg["nsfw"] and not board.is_nsfw()
        carried = _forward_of(message) or message
        text_part = carried.content or ""
        spoiled = {link for inside in SPOILER.findall(text_part) for link in LINK.findall(inside)}
        image: str | None = None
        files = []
        hidden = 0
        for attachment in carried.attachments:
            is_image = (attachment.content_type or "").startswith("image/")
            if is_image and hide_images:
                hidden += 1
            elif is_image and image is None and not attachment.filename.startswith("SPOILER_"):
                image = attachment.url
            else:
                files.append(attachment)
        for shown in carried.embeds:
            if shown.type in ("image", "gifv") and shown.url in spoiled:
                continue  # the poster put this link behind a spoiler warning, and the board keeps it there
            if shown.type in ("image", "gifv") and image is None and not hide_images:
                image = shown.thumbnail.url or shown.url
            elif shown.type == "rich":
                if not text_part:
                    text_part = "\n".join(part for part in (shown.title, shown.description) if part)
                if image is None and not hide_images and shown.image.url:
                    image = shown.image.url
        if image is None and not hide_images and carried.stickers:
            image = carried.stickers[0].url
        embed = discord.Embed(description=text_part[:4096] or None, color=GOLD, timestamp=message.created_at)
        embed.set_author(name=message.author.display_name, icon_url=str(message.author.display_avatar.url))
        if image:
            embed.set_image(url=image)
        if files or hidden:
            lines = [f"{hidden} image{'' if hidden == 1 else 's'} hidden"] if hidden else []
            lines += [f"[{discord.utils.escape_markdown(a.filename)}]({a.url})" for a in files]
            embed.add_field(name="Attachments", value="\n".join(lines)[:1024], inline=False)
        replied = getattr(message.reference, "resolved", None) if message.reference else None
        if isinstance(replied, discord.Message):
            embed.add_field(name="Replying to", value=f"[{discord.utils.escape_markdown(replied.author.display_name)}]({replied.jump_url})", inline=False)
        if cfg["show_jump"]:
            embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url})", inline=False)
        embed.set_footer(text=f"ID: {message.id}")
        return embed

    @staticmethod
    def _preview(message: Any) -> str:
        """Stored for the numbers pages and commands, where nothing can hold a spoiler shut."""
        carried = _forward_of(message) or message
        words = SPOILER.sub("[spoiler]", carried.content or "")
        return " ".join(words.split())[:200] or ("An image" if carried.attachments or carried.embeds else "No text")

    async def _drop(self, guild_id: int, message_id: int) -> None:
        async with self.db.transaction():
            await self.db.run("DELETE FROM starboard_stars WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))
            await self.db.run("DELETE FROM starboard_entries WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))

    def _note(self, guild_id: int, error: str | None) -> None:
        """What the Settings tab shows about the last try at a board post."""
        if error is None:
            self._last_errors.pop(guild_id, None)
        else:
            self._last_errors[guild_id] = (error, time.time())

    @staticmethod
    def _opted_in(cfg: dict, source: Any) -> bool:
        if source.type == discord.ChannelType.private_thread:
            return False
        return bool({source.id, getattr(source, "parent_id", None), getattr(source, "category_id", None)} & set(cfg["allow_private"]))

    async def _create_post(self, guild: Any, cfg: dict, entry: dict) -> None:
        board = guild.get_channel_or_thread(cfg["channel_id"])
        source = guild.get_channel_or_thread(entry["channel_id"])
        if not hasattr(board, "send"):  # a saved ID can name a category or a channel that's gone
            self._note(guild.id, NOT_A_BOARD)
            return
        if source is None:
            return
        if wider_audience(board, source) and not self._opted_in(cfg, source):
            return
        if not self.bot.reads_message_text:
            self._note(guild.id, NO_TEXT)
            return
        try:
            message = await source.fetch_message(entry["message_id"])
        except discord.NotFound:
            await self._drop(guild.id, entry["message_id"])
            return
        except discord.HTTPException as error:
            log.warning("Could not read a starred message in %s: %s", source.id, error)
            return
        try:
            post = await board.send(self._count_line(cfg, entry["stars"], source.id), embed=self._build_embed(cfg, message, source, board), allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden as error:
            log.warning("Could not post to the starboard in %s: %s", board.id, error)
            self._note(guild.id, CANT_POST)
            return
        except discord.HTTPException as error:
            log.warning("Could not post to the starboard in %s: %s", board.id, error)
            self._note(guild.id, f"Discord refused the last board post: {error.text or error.status}")
            return
        self._note(guild.id, None)
        await self.db.run(
            "UPDATE starboard_entries SET star_channel_id = ?, star_message_id = ?, preview = ?, author_id = ? WHERE guild_id = ? AND message_id = ?",
            (board.id, post.id, self._preview(message), message.author.id, guild.id, entry["message_id"]),
        )
        if cfg["autostar"]:
            try:
                await post.add_reaction(cfg["emoji"] or STAR)
            except discord.HTTPException as error:
                log.info("Could not add the star to a new board post: %s", error)

    async def _show_count(self, guild: Any, cfg: dict, entry: dict) -> None:
        board = guild.get_channel_or_thread(entry["star_channel_id"])
        if board is None:
            return
        try:
            # Only the count line changes, so this costs one call and no reread of the message.
            await board.get_partial_message(entry["star_message_id"]).edit(content=self._count_line(cfg, entry["stars"], entry["channel_id"]))
        except discord.NotFound:
            await self._note_post_gone(guild.id, entry["star_message_id"])
        except discord.HTTPException as error:
            log.warning("Could not update a board post in %s: %s", board.id, error)

    async def _delete_post(self, guild: Any, entry: dict) -> None:
        # The row goes first, so the delete event that comes back doesn't read as a moderator removing the post.
        await self.db.run("UPDATE starboard_entries SET star_message_id = NULL WHERE guild_id = ? AND message_id = ?", (guild.id, entry["message_id"]))
        board = guild.get_channel_or_thread(entry["star_channel_id"]) if entry["star_channel_id"] else None
        if board is not None:
            try:
                await board.get_partial_message(entry["star_message_id"]).delete()
            except discord.HTTPException as error:
                log.info("Could not delete a board post in %s: %s", board.id, error)

    async def _note_post_gone(self, guild_id: int, post_id: int) -> None:
        await self.db.run("UPDATE starboard_entries SET star_message_id = NULL, removed = 1 WHERE guild_id = ? AND star_message_id = ?", (guild_id, post_id))

    # ---- messages and channels changing ----

    async def _messages_deleted(self, guild_id: int, message_ids: set[int]) -> None:
        cfg = self.cfg(guild_id)
        if not cfg["channel_id"] or not message_ids:
            return
        guild = self.bot.get_guild(guild_id)
        marks = ",".join("?" * len(message_ids))
        for found in await self.db.all(f"SELECT message_id FROM starboard_entries WHERE guild_id = ? AND message_id IN ({marks})", (guild_id, *message_ids)):
            # The lock lets a board post on its way finish, so its ID is on the row and it comes down with the message.
            async with self._lock(found["message_id"]):
                entry = await self._entry(guild_id, found["message_id"])
                if entry is None:
                    continue
                if entry["star_message_id"] and guild is not None:
                    await self._delete_post(guild, entry)
                await self._drop(guild_id, entry["message_id"])
        for entry in await self.db.all(f"SELECT star_message_id FROM starboard_entries WHERE guild_id = ? AND star_message_id IN ({marks})", (guild_id, *message_ids)):
            await self._note_post_gone(guild_id, entry["star_message_id"])

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id:
            await self._messages_deleted(payload.guild_id, {payload.message_id})

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id:
            await self._messages_deleted(payload.guild_id, set(payload.message_ids))

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if not payload.guild_id or not self.bot.reads_message_text:
            return
        cfg = self.cfg(payload.guild_id)
        if not cfg["channel_id"]:
            return
        entry = await self._entry(payload.guild_id, payload.message_id)
        if entry is None or not entry["star_message_id"]:
            return
        guild = self.bot.get_guild(payload.guild_id)
        source = guild.get_channel_or_thread(entry["channel_id"]) if guild else None
        board = guild.get_channel_or_thread(entry["star_channel_id"]) if guild else None
        if source is None or board is None:
            return
        try:
            message = await source.fetch_message(entry["message_id"])
            await board.get_partial_message(entry["star_message_id"]).edit(embed=self._build_embed(cfg, message, source, board))
        except discord.HTTPException as error:
            log.info("Could not bring a board post up to date: %s", error)
            return
        await self.db.run("UPDATE starboard_entries SET preview = ? WHERE guild_id = ? AND message_id = ?", (self._preview(message), payload.guild_id, payload.message_id))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        guild_id = channel.guild.id
        if self.cfg(guild_id)["channel_id"] == channel.id:
            await self.bot.settings.save(guild_id, self.key, {"channel_id": None}, action="starboard.channel_deleted")
        await self.db.run("UPDATE starboard_entries SET star_channel_id = NULL, star_message_id = NULL WHERE guild_id = ? AND star_channel_id = ?", (guild_id, channel.id))

    # ---- numbers ----

    async def _totals(self, guild_id: int) -> dict:
        row = await self.db.one(
            "SELECT COUNT(*) AS posts, COALESCE(SUM(stars), 0) AS stars, MIN(created_at) AS since FROM starboard_entries WHERE guild_id = ? AND star_message_id IS NOT NULL",
            (guild_id,),
        )
        given = await self.db.scalar("SELECT COUNT(*) FROM (SELECT DISTINCT message_id, user_id FROM starboard_stars WHERE guild_id = ?)", (guild_id,))
        givers = await self.db.scalar("SELECT COUNT(DISTINCT user_id) FROM starboard_stars WHERE guild_id = ?", (guild_id,))
        return {**row, "given": given, "givers": givers}

    async def _top_posts(self, guild_id: int, limit: int = TOP) -> list[dict]:
        return await self.db.all(
            "SELECT message_id, channel_id, author_id, stars, star_channel_id, star_message_id, preview FROM starboard_entries "
            "WHERE guild_id = ? AND star_message_id IS NOT NULL ORDER BY stars DESC, message_id DESC LIMIT ?",
            (guild_id, limit),
        )

    async def _top_authors(self, guild_id: int, limit: int = TOP) -> list[dict]:
        return await self.db.all(
            "SELECT author_id AS user_id, SUM(stars) AS stars, COUNT(*) AS posts FROM starboard_entries "
            "WHERE guild_id = ? AND star_message_id IS NOT NULL AND author_id IS NOT NULL GROUP BY author_id ORDER BY stars DESC LIMIT ?",
            (guild_id, limit),
        )

    async def _top_givers(self, guild_id: int, limit: int = TOP) -> list[dict]:
        return await self.db.all(
            "SELECT user_id, COUNT(DISTINCT message_id) AS given FROM starboard_stars WHERE guild_id = ? GROUP BY user_id ORDER BY given DESC LIMIT ?",
            (guild_id, limit),
        )

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/starboard/stats", self._stats_endpoint)
        router.post("/starboard/create", self._create_endpoint)
        router.get("/starboard/status", self._status_endpoint)

    async def _stats_endpoint(self, req: Any) -> dict:
        def name_of(user_id: int) -> str | None:
            member = req.ops.member(req.guild_id, user_id)
            return member["display_name"] if member else None

        posts = [post for post in await self._top_posts(req.guild_id) if req.can_read(post["channel_id"]) and req.can_read(post["star_channel_id"])]
        for post in posts:
            post["author_name"] = name_of(post["author_id"]) if post["author_id"] else None
            post["url"] = f"https://discord.com/channels/{req.guild_id}/{post['star_channel_id']}/{post['star_message_id']}"
            post["source_url"] = f"https://discord.com/channels/{req.guild_id}/{post['channel_id']}/{post['message_id']}"
        authors = await self._top_authors(req.guild_id)
        givers = await self._top_givers(req.guild_id)
        for row in (*authors, *givers):
            row["name"] = name_of(row["user_id"])
        return {"totals": await self._totals(req.guild_id), "posts": posts, "authors": authors, "givers": givers}

    @staticmethod
    def _board_problem(guild: Any, channel_id: int | None) -> str | None:
        """Why the bot can't post to the board channel as things stand, or None."""
        if guild is None or not channel_id or getattr(guild, "unavailable", False):
            return None
        board = guild.get_channel_or_thread(channel_id)
        if not hasattr(board, "send"):
            return NOT_A_BOARD
        allowed = board.permissions_for(guild.me)
        return None if allowed.view_channel and allowed.send_messages and allowed.embed_links else CANT_POST

    async def _status_endpoint(self, req: Any) -> dict:
        error, at = self._last_errors.get(req.guild_id, (None, None))
        return {"problem": self._board_problem(self.bot.get_guild(req.guild_id), self.cfg(req.guild_id)["channel_id"]), "last_error": error, "last_error_at": at}

    async def _create_endpoint(self, req: Any) -> dict:
        req.require_permission("manage_channels")
        form = await req.json()
        try:
            name = text(100, minimum=1)(form.get("name"))
        except Invalid as error:
            raise ValidationError({"name": str(error)}) from error
        if self.cfg(req.guild_id)["channel_id"]:
            raise ApiError(409, "This server already has a starboard channel.")
        channel = await req.ops.create_channel(req.guild_id, name, read_only=True)
        await req.settings.save(req.guild_id, self.key, {"channel_id": channel["id"]}, actor=req.actor, action="starboard.create")
        return {"channel": channel, "settings": self.cfg(req.guild_id)}

    # ---- /starboard ----

    async def _save(self, ctx: commands.Context, action: str, form: dict) -> bool:
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values()), "That can't be saved."))
            return False
        return True

    async def _flip(self, ctx: commands.Context, field: str, action: str, on_text: str, off_text: str) -> None:
        turned_on = not self.cfg(ctx.guild.id)[field]
        if await self._save(ctx, action, {field: turned_on}):
            await ctx.ok(on_text if turned_on else off_text)

    @commands.hybrid_group(name="starboard", fallback="config", description="Show how the starboard is set up")
    @commands.guild_only()
    @requires("manage_guild")
    async def starboard(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        ignored = ", ".join(f"<#{c}>" for c in cfg["blacklist"]) or "none"
        where = f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "not set"
        await ctx.send(
            f"Channel: {where}\nStars needed: {cfg['threshold']}\nEmoji: {cfg['emoji'] or STAR}\nNSFW images: {'on' if cfg['nsfw'] else 'off'}\n"
            f"Jump link: {'on' if cfg['show_jump'] else 'off'}\nSelf stars: {'on' if cfg['star_self'] else 'off'}\nAutomatic star: {'on' if cfg['autostar'] else 'off'}\n"
            f"Locked: {'yes' if cfg['locked'] else 'no'}\nIgnored channels: {ignored}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @starboard.command(name="setup", description="Set the starboard channel, or make a new one")
    @requires("manage_guild")
    @app_commands.describe(channel="An existing channel to use", name="The name for a new channel. Defaults to starboard.")
    async def starboard_setup(self, ctx: commands.Context, channel: discord.TextChannel | None = None, *, name: str = "starboard") -> None:
        await ctx.defer(ephemeral=True)
        name, problem = new_channel_name(ctx, name, "starboard")
        if problem:
            await ctx.fail(problem)
            return
        found = channel or discord.utils.get(ctx.guild.text_channels, name=name)
        if found is None:
            if not access.is_admin(ctx.author) and not ctx.author.guild_permissions.manage_channels:
                await ctx.fail("You need Manage Channels to make a channel.")
                return
            try:
                channel_id = (await self.bot.ops.create_channel(ctx.guild.id, name[:100], read_only=True))["id"]
            except OpsError as error:
                await ctx.fail(str(error))
                return
        else:
            refusal = access.post_refusal(ctx.author, found)
            if refusal is not None:
                await ctx.fail(refusal)
                return
            channel_id = found.id
        if await self._save(ctx, "starboard.setup", {"channel_id": channel_id}):
            problem = self._board_problem(ctx.guild, channel_id)
            await ctx.ok(f"Messages with enough stars now go to <#{channel_id}>.{' ' + problem if problem else ''}")

    @starboard.command(name="limit", description="Set how many stars a message needs")
    @requires("manage_guild")
    @app_commands.describe(limit="Between 1 and 100")
    async def starboard_limit(self, ctx: commands.Context, limit: int) -> None:
        if await self._save(ctx, "starboard.limit", {"threshold": limit}):
            await ctx.ok(f"A message now needs {limit} star{'' if limit == 1 else 's'}.")

    @starboard.command(name="nsfw", description="Turn images from NSFW channels on or off")
    @requires("manage_guild")
    async def starboard_nsfw(self, ctx: commands.Context) -> None:
        await self._flip(ctx, "nsfw", "starboard.nsfw", "Images from NSFW channels now show on the board.", "Images from NSFW channels no longer show on the board.")

    @starboard.command(name="self", description="Turn starring your own messages on or off")
    @requires("manage_guild")
    async def starboard_self(self, ctx: commands.Context) -> None:
        await self._flip(ctx, "star_self", "starboard.self", "People's stars on their own messages now count.", "People's stars on their own messages no longer count.")

    @starboard.command(name="jump", aliases=["source"], description="Turn the link back to the original message on or off")
    @requires("manage_guild")
    async def starboard_jump(self, ctx: commands.Context) -> None:
        await self._flip(ctx, "show_jump", "starboard.jump", "New board posts now link back to the original.", "New board posts no longer link back to the original.")

    @starboard.command(name="autostar", description="Turn the bot's own star on new board posts on or off")
    @requires("manage_guild")
    async def starboard_autostar(self, ctx: commands.Context) -> None:
        await self._flip(ctx, "autostar", "starboard.autostar", "The bot now stars new board posts.", "The bot no longer stars new board posts.")

    @starboard.command(name="lock", description="Lock or unlock the starboard")
    @requires("manage_guild")
    async def starboard_lock(self, ctx: commands.Context) -> None:
        await self._flip(ctx, "locked", "starboard.lock", "The starboard is locked. Stars change nothing until it's unlocked.", "The starboard is unlocked.")

    @staticmethod
    def _channels_named(ctx: commands.Context, words: str) -> list[Any]:
        found = []
        for word in words.replace(",", " ").split():
            number = CHANNEL_WORD.fullmatch(word)
            channel = ctx.guild.get_channel_or_thread(int(number.group(1) or number.group(2))) if number else discord.utils.get(ctx.guild.channels, name=word.lstrip("#"))
            if channel is not None and channel not in found and access.can_view(ctx.author, channel):  # one they can't see reads as one that isn't there, as on the dashboard
                found.append(channel)
        return found

    @starboard.command(name="blacklist", description="Stop counting stars in these channels")
    @requires("manage_guild")
    @app_commands.describe(channels="One or more channels, as mentions, names or IDs")
    async def starboard_blacklist(self, ctx: commands.Context, *, channels: str) -> None:
        found = self._channels_named(ctx, channels)
        if not found:
            await ctx.fail("None of those are channels in this server.")
            return
        current = self.cfg(ctx.guild.id)["blacklist"]
        if await self._save(ctx, "starboard.blacklist", {"blacklist": [*current, *(c.id for c in found)]}):
            await ctx.ok(f"Stars no longer count in {', '.join(c.mention for c in found)}.")

    @starboard.command(name="unblacklist", description="Count stars in these channels again")
    @requires("manage_guild")
    @app_commands.describe(channels="One or more channels, as mentions, names or IDs")
    async def starboard_unblacklist(self, ctx: commands.Context, *, channels: str) -> None:
        found = self._channels_named(ctx, channels)
        if not found:
            await ctx.fail("None of those are channels in this server.")
            return
        ids = {c.id for c in found}
        if await self._save(ctx, "starboard.unblacklist", {"blacklist": [c for c in self.cfg(ctx.guild.id)["blacklist"] if c not in ids]}):
            await ctx.ok(f"Stars count in {', '.join(c.mention for c in found)} again.")

    @starboard.command(name="remove", description="Stop using a starboard channel. The channel itself stays.")
    @requires("manage_guild")
    async def starboard_remove(self, ctx: commands.Context) -> None:
        if not self.cfg(ctx.guild.id)["channel_id"]:
            await ctx.fail("This server has no starboard channel.")
            return
        if await self._save(ctx, "starboard.remove", {"channel_id": None}):
            await ctx.ok("The starboard channel is removed. Nothing is posted until you set a new one.")

    @starboard.command(name="emoji", description="Set or reset the emoji that counts as a star")
    @requires("manage_guild")
    @app_commands.describe(choice="Set a new emoji, or go back to the star", emoji="The emoji, when setting one")
    async def starboard_emoji(self, ctx: commands.Context, choice: Literal["set", "reset"], emoji: str | None = None) -> None:
        if choice == "reset":
            if await self._save(ctx, "starboard.emoji", {"emoji": None}):
                await ctx.ok(f"The starboard emoji is {STAR} again.")
            return
        if not emoji:
            await ctx.fail("Give the emoji to use.")
            return
        if await self._save(ctx, "starboard.emoji", {"emoji": emoji}):
            await ctx.ok(f"{emoji} now counts as a star.")

    def _top_line(self, ctx: commands.Context, row: dict) -> str:
        """One post in a numbers answer. Anyone can ask, and the whole channel reads the answer, so its text shows only when the asker and everyone there could read it anyway."""
        line = f"**{row['stars']}** <#{row['channel_id']}>"
        if isinstance(ctx.channel, discord.PartialMessageable):  # a thread the cache doesn't hold has no audience to read
            return line
        for channel_id in (row["channel_id"], row["star_channel_id"]):
            channel = ctx.guild.get_channel_or_thread(channel_id) if channel_id else None
            if channel is None or not access.can_read(ctx.author, channel) or wider_audience(ctx.channel, channel):
                return line
        return f"{line}: {discord.utils.escape_markdown(row['preview'] or '')[:60]}"

    def _stats_embed(self, ctx: commands.Context, totals: dict, posts: list[dict], authors: list[dict], givers: list[dict]) -> discord.Embed:
        def named(row: dict) -> str:
            return f"<@{row['user_id']}>"

        embed = discord.Embed(title=f"Starboard in {ctx.guild.name}", color=GOLD)
        embed.add_field(name="Board posts", value=str(totals["posts"]))
        embed.add_field(name="Stars on them", value=str(totals["stars"]))
        embed.add_field(name="People starring", value=str(totals["givers"]))
        if posts:
            embed.add_field(name="Most starred", value="\n".join(self._top_line(ctx, p) for p in posts[:5]), inline=False)
        if authors:
            embed.add_field(name="Most starred people", value="\n".join(f"{named(a)}: {a['stars']}" for a in authors[:5]))
        if givers:
            embed.add_field(name="Most generous", value="\n".join(f"{named(g)}: {g['given']}" for g in givers[:5]))
        return embed

    @starboard.command(name="server", description="Show numbers for this server's starboard")
    async def starboard_server(self, ctx: commands.Context) -> None:
        gid = ctx.guild.id
        await ctx.send(embed=self._stats_embed(ctx, await self._totals(gid), await self._top_posts(gid, 5), await self._top_authors(gid, 5), await self._top_givers(gid, 5)), allowed_mentions=discord.AllowedMentions.none())

    @starboard.command(name="stats", aliases=["top"], description="Show starboard numbers for the server or for one person")
    @app_commands.describe(member="Whose numbers. Leave it out for the whole server.")
    async def starboard_stats(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if member is None:
            await self.starboard_server.callback(self, ctx)
            return
        gid = ctx.guild.id
        received = await self.db.one(
            "SELECT COUNT(*) AS posts, COALESCE(SUM(stars), 0) AS stars FROM starboard_entries WHERE guild_id = ? AND author_id = ? AND star_message_id IS NOT NULL", (gid, member.id),
        )
        given = await self.db.scalar("SELECT COUNT(DISTINCT message_id) FROM starboard_stars WHERE guild_id = ? AND user_id = ?", (gid, member.id))
        best = await self.db.all(
            "SELECT stars, channel_id, star_channel_id, preview FROM starboard_entries WHERE guild_id = ? AND author_id = ? AND star_message_id IS NOT NULL ORDER BY stars DESC LIMIT 3", (gid, member.id),
        )
        embed = discord.Embed(title=f"Stars for {member.display_name}", color=GOLD)
        embed.add_field(name="Board posts", value=str(received["posts"]))
        embed.add_field(name="Stars received", value=str(received["stars"]))
        embed.add_field(name="Stars given", value=str(given))
        if best:
            embed.add_field(name="Most starred", value="\n".join(self._top_line(ctx, b) for b in best), inline=False)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _show_entry(self, ctx: commands.Context, entry: dict) -> None:
        await ctx.defer()
        board = ctx.guild.get_channel_or_thread(entry["star_channel_id"]) if entry["star_channel_id"] else None
        if board is None:
            await ctx.fail("That post's channel is gone.")
            return
        # Anyone can run this, so it must not read a board channel out to someone who can't open it.
        if not access.can_read(ctx.author, board):
            await ctx.fail("You can't read the starboard channel.")
            return
        try:
            post = await board.fetch_message(entry["star_message_id"])
        except discord.HTTPException:
            await ctx.fail("That board post can't be read.")
            return
        await ctx.send(post.content, embeds=post.embeds, allowed_mentions=discord.AllowedMentions.none())

    @starboard.command(name="show", description="Show a starred post here")
    @app_commands.describe(message_id="The ID or link of the message or its board post")
    async def starboard_show(self, ctx: commands.Context, message_id: str) -> None:
        link = MESSAGE_LINK.search(message_id)
        try:
            found = snowflake(link.group(1) if link else message_id)
        except Invalid:
            await ctx.fail("That isn't a message ID or link.")
            return
        entry = await self._entry(ctx.guild.id, found) or await self._entry_of_post(ctx.guild.id, found)
        if entry is None or not entry["star_message_id"]:
            await ctx.fail("That message isn't on the starboard.")
            return
        await self._show_entry(ctx, entry)

    @starboard.command(name="random", description="Show a random starred post")
    async def starboard_random(self, ctx: commands.Context) -> None:
        entry = await self.db.one("SELECT * FROM starboard_entries WHERE guild_id = ? AND star_message_id IS NOT NULL ORDER BY RANDOM() LIMIT 1", (ctx.guild.id,))
        if entry is None:
            await ctx.fail("The starboard is empty.")
            return
        await self._show_entry(ctx, entry)
