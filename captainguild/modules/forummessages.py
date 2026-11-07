"""Forum messages: a welcome in each new post of the forums a server sets up, one reminder in a post that goes quiet, and two ways to mark a
post solved.

Each forum on the list can have a welcome, posted in each new post once its author's first message is in. With a solved tag set, the
welcome carries a Mark as Solved button. The post's author, the forum's helper roles and anyone who can manage posts there can press it,
or right-click the message that fixed it and pick Mark as Solution, which also pins that message. Either one adds the solved tag and
closes the post.

Once an hour the bot looks at the open posts of each forum with a solved tag. A post with no message for six days gets the forum's
reminder once, naming its author, and with the default text the helper roles too. The reminder is written down before it's sent, so a
post never gets a second one, even across a restart.

A welcome or a reminder pings the post's author and the people and roles the server's own text names. Text a placeholder fills in, such
as a display name, pings nobody. Every change and every action goes to the forum log channel as plain text that pings nobody.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, delivery
from ..core.module import Module, requires
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, list_of, optional, snowflake, snowflakes

log = logging.getLogger(__name__)

MESSAGE_LIMIT = 2000  # Discord's limit on a message's text
MAX_FORUMS = 50
MAX_HELPER_ROLES = 25
SOLVED_ID = "captainguild:forum:solved"
SOLVED_LABEL = "Mark as Solved"
MENU_NAME = "Mark as Solution"
TAGS_PER_POST = 5  # Discord's limit on the tags one post carries
SWITCHED_OFF = "That's switched off in this server. A manager can switch it on from the dashboard."
CLOSED_POST = "This post is closed. Send a message in it to open it again, then mark it solved."
TAGS_FULL = "This post already has five tags, the most Discord allows, so the solved tag can't be added. Remove one and try again."
TAG_GONE = "The solved tag set for this forum no longer exists. Pick another one on the Forum messages page."
ALREADY_SOLVED = "This post is already marked solved and closed."
INACTIVE_SECONDS = 6 * 86400  # a post with no message for longer than this gets the reminder
REMINDER_TIMER = "forummessages.reminders"
CHECK_SECONDS = 3600
FIRST_CHECK_SECONDS = 60
GONE_KEPT = 10_000  # posts whose author has left, remembered so each is logged once a session
THREAD_GAP = 1.0  # seconds between posts in /clean_threads and /close_solved_threads, the old bot's pace
RECENT_MOST = 100
HISTORY_READ = 100  # messages /check_recent_threads reads from each end of a post, looking for a welcome
EXCERPT = 50
CHOICES = 25
DEFAULT_REMINDER = (
    "{{OP.Mention}}, this forum post has been inactive for 6 days. Please consider marking the message that resolved your issue as the solution if your "
    "issue is resolved. If your issue is not resolved, please provide us with an update. Thank you. To mark a message as a solution, tap and hold or right "
    "click on the solution message, go to *Apps*, and select *Mark as Solution*. This will also close your forum post. ***Please do not reply after closing "
    "your forum post or it will reopen it!***"
)
LOG_KEPT = 500  # lines waiting for one server's log channel. A channel Discord stopped taking messages in can't fill memory.
PLACEHOLDER = re.compile(r"\{\{OP\.(ID|Name|Username|Discriminator|GlobalName|Mention|CreatedAt|JoinedAt|Avatar|Bot)\}\}")
EVERYONE_WORD = re.compile(r"@(?:everyone|here)")
ROLE_MENTION = re.compile(r"<@&([0-9]{15,21})>")
USER_MENTION = re.compile(r"<@!?([0-9]{15,21})>")
MENTIONS_KEPT = 100  # Discord takes at most 100 roles and 100 people in one message's list of who may be pinged


def fill(text: str, member: Any) -> str:
    """The server's text with the post author's details in its placeholders. Each placeholder is read once, so a name that looks like
    another placeholder stays as written, and names keep their markdown characters as plain text."""
    joined = getattr(member, "joined_at", None)
    values = {
        "ID": str(member.id),
        "Name": discord.utils.escape_markdown(str(member)),  # the name, with its number for a name that still has one
        "Username": discord.utils.escape_markdown(member.name),
        "Discriminator": member.discriminator,
        "GlobalName": discord.utils.escape_markdown(member.global_name or member.name),
        "Mention": member.mention,
        "CreatedAt": discord.utils.format_dt(member.created_at, "R"),
        "JoinedAt": discord.utils.format_dt(joined, "R") if joined else "Unknown",
        "Avatar": member.display_avatar.url,
        "Bot": str(member.bot),
    }
    return PLACEHOLDER.sub(lambda found: values[found.group(1)], text)


def mentions_for(text: str, author: Any) -> discord.AllowedMentions:
    """Who a post may ping: its author, and the people, roles, @everyone and @here the server's own text names, before any placeholder is
    filled in. The default reminder names the helper roles in its text."""
    role_ids = list(dict.fromkeys(int(found) for found in ROLE_MENTION.findall(text)))[:MENTIONS_KEPT]
    user_ids = [user_id for user_id in dict.fromkeys(int(found) for found in USER_MENTION.findall(text)) if user_id != author.id][:MENTIONS_KEPT - 1]
    return discord.AllowedMentions(everyone=bool(EVERYONE_WORD.search(text)), users=[author, *(discord.Object(user_id) for user_id in user_ids)],
                                   roles=[discord.Object(role_id) for role_id in role_ids], replied_user=False)


def reminder_text(row: dict) -> str:
    """A forum's reminder. With none set, the default text, with the forum's helper roles above it."""
    if row["inactivity_message"]:
        return row["inactivity_message"]
    helpers = " ".join(f"<@&{role_id}>" for role_id in row["helper_roles"])
    return f"{helpers}\n\n{DEFAULT_REMINDER}" if helpers else DEFAULT_REMINDER


def pieces(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Text as messages Discord takes, breaking between lines. A filled-in placeholder can take text past the limit the setting holds to."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        joined = f"{current}\n{line}" if current else line
        if len(joined) > limit:
            parts.append(current)
            current = line
        else:
            current = joined
    parts.append(current)
    return [part for part in parts if part.strip()]


def message_text(value: Any) -> str:
    """A welcome or reminder, kept exactly as written. Text that's only spaces is no text, and Discord refuses a message past 2,000 characters."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Invalid("Must be text.")
    if not value.strip():
        return ""
    if len(value) > MESSAGE_LIMIT:
        raise Invalid(f"Must be at most {MESSAGE_LIMIT} characters. This is {len(value)}.")
    return value


def forum_entry(value: Any) -> dict:
    if not isinstance(value, dict):
        raise Invalid("Each forum needs its channel.")
    return {
        "forum_id": snowflake(value.get("forum_id")),
        "message": message_text(value.get("message", "")),
        "inactivity_message": message_text(value.get("inactivity_message", "")),
        "solved_tag": optional(snowflake)(value.get("solved_tag")),
        "helper_roles": snowflakes(MAX_HELPER_ROLES)(value.get("helper_roles", [])),
    }


def forum_list(value: Any) -> list[dict]:
    rows = list_of(forum_entry, MAX_FORUMS, unique=False)(value)
    seen: set[int] = set()
    for row in rows:
        if row["forum_id"] in seen:
            raise Invalid("A forum is listed twice. Give each forum one entry.")
        seen.add(row["forum_id"])
    return rows


async def _answer(interaction: discord.Interaction, text: str) -> None:
    """A private answer, whether or not the interaction has had its first one."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as error:
        log.info("Could not answer a forum messages action: %s", error)


def blank_row(forum_id: int) -> dict:
    return {"forum_id": forum_id, "message": "", "inactivity_message": "", "solved_tag": None, "helper_roles": []}


class MessageForm(discord.ui.Modal):
    """AF-1 and AF-2: the forum's welcome or reminder in one box, opened on the text it has now."""

    def __init__(self, cog: "ForumMessages", forum_id: int, current: str, *, inactivity: bool) -> None:
        super().__init__(title="Set Inactivity Message" if inactivity else "Set Custom Message")
        self.cog = cog
        self.forum_id = forum_id
        self.inactivity = inactivity
        self.box = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, default=current or None, max_length=MESSAGE_LIMIT, required=not inactivity,
                                        placeholder="Leave it empty for the default reminder." if inactivity else None)
        self.add_item(self.box)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_message(interaction, self.forum_id, self.box.value, inactivity=self.inactivity)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("The forum message form failed", exc_info=error)
        await _answer(interaction, "Something went wrong with that form. It's been logged.")


class SolvedButton(discord.ui.View):
    """The button under a welcome. It has no timeout and is registered once at start, so it works on welcomes posted before a restart."""

    def __init__(self, cog: "ForumMessages") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label=SOLVED_LABEL, style=discord.ButtonStyle.green, custom_id=SOLVED_ID)
    async def solved(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.solved_pressed(interaction)


class ForumMessages(Module):
    key = "forummessages"
    category = "Forum messages"
    command_pack = True
    schema = Schema({
        "enabled": Field(False, boolean),
        "log_channel": Field(None, optional(snowflake)),
        "forums": Field([], forum_list),
    })
    migrations = [
        """
        CREATE TABLE forum_welcomes (
            guild_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            posted_at REAL NOT NULL,
            PRIMARY KEY (guild_id, thread_id)
        );
        CREATE INDEX forum_welcomes_forum ON forum_welcomes (guild_id, channel_id);
        CREATE TABLE forum_reminders (
            guild_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            reminded_at REAL NOT NULL,
            PRIMARY KEY (guild_id, thread_id)
        );
        CREATE INDEX forum_reminders_forum ON forum_reminders (guild_id, channel_id);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._log_queues: dict[int, deque[str]] = {}
        self._log_tasks: dict[int, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._round: asyncio.Task | None = None
        self._gone_owners: set[int] = set()
        self._edit_locks: dict[int, asyncio.Lock] = {}
        self._busy: set[tuple[int, int]] = set()  # (server, forum) with a /clean_threads or /close_solved_threads running
        self._sending: set[tuple[int, int]] = set()  # (server, post) whose welcome is on its way to Discord now
        self.thread_gap = THREAD_GAP
        self.menu = app_commands.ContextMenu(name=MENU_NAME, callback=self.solution_menu)
        self.menu.guild_only = True

    async def on_load(self) -> None:
        self.bot.tree.add_command(self.menu)
        self.bot.add_view(SolvedButton(self))
        timers = self.bot.timers
        timers.handler(REMINDER_TIMER, self._reminders_due)
        if await timers.get_key(REMINDER_TIMER, REMINDER_TIMER) is None:
            await timers.schedule(REMINDER_TIMER, time.time() + FIRST_CHECK_SECONDS, key=REMINDER_TIMER)

    def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)
        for task in [*self._log_tasks.values(), *self._tasks]:
            task.cancel()

    def _spawn(self, work: Any) -> asyncio.Task:
        """Work that outlives the event that started it. The task is held until it ends, and what it raises reaches the log."""
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Forum messages work failed", exc_info=task.exception())

    def forum_row(self, guild_id: int, forum_id: int | None) -> dict | None:
        """One forum's settings, or None when the server hasn't set that forum up."""
        return next((row for row in self.cfg(guild_id)["forums"] if row["forum_id"] == forum_id), None)

    # ---- the log channel ----

    def log_line(self, guild_id: int, text: str) -> None:
        """AF-8: one line for the forum log channel. Lines wait in order and go out together, so a burst never holds up an answer to someone."""
        if not self.cfg(guild_id)["log_channel"]:
            return
        queue = self._log_queues.setdefault(guild_id, deque())
        if len(queue) >= LOG_KEPT:
            return
        queue.append(text[:MESSAGE_LIMIT])
        task = self._log_tasks.get(guild_id)
        if task is None or task.done():
            self._log_tasks[guild_id] = asyncio.ensure_future(self._send_log(guild_id))

    async def _send_log(self, guild_id: int) -> None:
        queue = self._log_queues.get(guild_id)
        while queue:
            lines = [queue.popleft()]
            while queue and sum(len(line) + 1 for line in lines) + len(queue[0]) <= MESSAGE_LIMIT:
                lines.append(queue.popleft())
            channel = self.bot.get_channel(self.cfg(guild_id)["log_channel"] or 0)
            if channel is None or not hasattr(channel, "send"):
                queue.clear()
                return
            try:
                await channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
            except delivery.TRANSIENT as error:  # a refusal or a dropped connection costs these lines, not the ones after them
                log.info("Could not post in the forum log of %s: %s", guild_id, error)

    async def finish_log(self, seconds: float = 10.0) -> None:
        """Wait for queued log lines, for at most this long. The tests use this."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline and (any(self._log_queues.values()) or any(not task.done() for task in self._log_tasks.values())):
            await asyncio.sleep(0.01)

    # ---- the welcome ----

    @commands.Cog.listener("on_message")
    async def new_post(self, message: discord.Message) -> None:
        """AF-13: a forum post's first message has the post's own ID. The old bot waited for its author's first message in the post, which
        is that one, and posted the welcome after it."""
        guild = message.guild
        if guild is None or message.id != message.channel.id or not self.on_in(guild.id):
            return
        parent = getattr(message.channel, "parent_id", None)
        if parent is not None and self.forum_row(guild.id, parent) is None:
            return
        self._spawn(self._welcome_new_post(message))

    async def _thread_of(self, channel: Any) -> discord.Thread | None:
        """A post, asking Discord for one the cache doesn't hold yet."""
        if isinstance(channel, discord.Thread):
            return channel
        try:
            found = await self.bot.fetch_channel(channel.id)
        except (discord.HTTPException, discord.InvalidData):
            return None
        return found if isinstance(found, discord.Thread) else None

    async def _member(self, guild: Any, user_id: int) -> Any:
        """A member of the server, or None when they've left."""
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    async def _welcome_new_post(self, message: discord.Message) -> None:
        thread = await self._thread_of(message.channel)
        if thread is None or not isinstance(thread.parent, discord.ForumChannel) or message.author.id != thread.owner_id:
            return
        row = self.forum_row(thread.guild.id, thread.parent_id)
        if row is None or not row["message"]:
            return
        author = message.author if isinstance(message.author, discord.Member) else None
        if author is None:
            try:
                author = await self._member(thread.guild, thread.owner_id)
            except discord.HTTPException as error:
                self.log_line(thread.guild.id, f"Failed to send automated message in thread <#{thread.id}>: {error}")
                return
        if author is None:
            self.log_line(thread.guild.id, f"Failed to fetch thread owner in thread <#{thread.id}>: Owner is no longer a member.")
            return
        await self.post_welcome(thread, row, author)

    async def post_welcome(self, thread: discord.Thread, row: dict, author: Any, *, verb: str = "sent") -> bool:
        """Posts the forum's welcome in a post that hasn't had it, with the Mark as Solved button when the forum has a solved tag. The post is
        written down first, so it gets one welcome however often this runs. A welcome Discord refuses is crossed off again, so it can be tried
        later, and so is one cut off by a dropped connection or a stop. `verb` is how the log line says it went: "sent" for a new post and
        "posted" for /check_recent_threads, as the old bot wrote them."""
        guild = thread.guild
        claimed = await self.db.run("INSERT OR IGNORE INTO forum_welcomes (guild_id, thread_id, channel_id, posted_at) VALUES (?, ?, ?, ?)",
                                    (guild.id, thread.id, row["forum_id"], time.time()))
        if not claimed:
            return False
        self._sending.add((guild.id, thread.id))
        try:
            return await self._send_welcome(thread, row, author, verb)
        finally:
            self._sending.discard((guild.id, thread.id))

    @staticmethod
    async def _take_back(posted: list[discord.Message]) -> None:
        """Deletes the first parts of a welcome whose last part Discord refused, so a later try posts it whole once."""
        for message in posted:
            try:
                await message.delete()
            except discord.HTTPException as error:
                log.info("Could not take back part of a welcome in %s: %s", message.channel.id, error)

    async def _unclaim(self, guild_id: int, thread_id: int) -> None:
        await self.db.run("DELETE FROM forum_welcomes WHERE guild_id = ? AND thread_id = ?", (guild_id, thread_id))

    async def _send_welcome(self, thread: discord.Thread, row: dict, author: Any, verb: str) -> bool:
        guild = thread.guild
        attempt = "send" if verb == "sent" else "post"
        allowed = mentions_for(row["message"], author)
        parts = pieces(fill(row["message"], author))
        forum = f"<#{row['forum_id']}>"
        posted: list[discord.Message] = []
        try:
            for part in parts[:-1]:
                posted.append(await thread.send(part, allowed_mentions=allowed))
            last = parts[-1]
            if not row["solved_tag"]:
                sent = await thread.send(last, allowed_mentions=allowed)
                self.log_line(guild.id, f"Automated message {verb} without Solved button in thread <#{thread.id}> for forum channel {forum} (no solved tag configured)")
            else:
                try:
                    sent = await thread.send(last, view=SolvedButton(self), allowed_mentions=allowed)
                    self.log_line(guild.id, f"Automated message with Solved button {verb} in thread <#{thread.id}> for forum channel {forum}")
                except discord.Forbidden:
                    sent = await thread.send(last, allowed_mentions=allowed)  # without the button, as the old bot did when Discord refused it
                    self.log_line(guild.id, f"Failed to {attempt} Solved button in thread <#{thread.id}> due to missing permissions.")
                except discord.HTTPException as error:
                    sent = await thread.send(last, allowed_mentions=allowed)
                    self.log_line(guild.id, f"Failed to {attempt} Solved button in thread <#{thread.id}>: {error}")
        except discord.Forbidden:
            await self._take_back(posted)
            await self._unclaim(guild.id, thread.id)
            self.log_line(guild.id, f"Failed to {attempt} automated message in thread <#{thread.id}> due to missing permissions.")
            return False
        except delivery.TRANSIENT as error:  # a refusal, or a connection that dropped with no answer, which the library doesn't retry
            await self._take_back(posted)
            await self._unclaim(guild.id, thread.id)
            self.log_line(guild.id, f"Failed to {attempt} automated message in thread <#{thread.id}>: {str(error) or type(error).__name__}")
            return False
        except asyncio.CancelledError:  # a stop while Discord had it. /check_recent_threads reads the post before it posts again.
            try:
                await self._unclaim(guild.id, thread.id)
            except Exception as error:  # the database can be closing too
                log.info("Could not cross off the welcome of %s at a stop: %s", thread.id, error)
            raise
        await self.db.run("UPDATE forum_welcomes SET message_id = ? WHERE guild_id = ? AND thread_id = ?", (sent.id, guild.id, thread.id))
        return True

    # ---- the reminder in a quiet post ----

    async def _reminders_due(self, payload: dict, row: dict) -> None:
        """AF-14: the hourly look. The timer is stored, so it keeps its hour across a restart, and only one look runs at a time."""
        await self.bot.timers.schedule(REMINDER_TIMER, time.time() + CHECK_SECONDS, key=REMINDER_TIMER)
        if self._round is not None and not self._round.done():
            return
        self._round = self._spawn(self.remind_round())

    async def finish_round(self, seconds: float = 10.0) -> None:
        """Wait for a look that's running, for at most this long. The tests use this."""
        if self._round is not None:
            await asyncio.wait([self._round], timeout=seconds)

    async def remind_round(self) -> None:
        """Every open post of every forum with a solved tag, in servers with the pack on. A forum that fails is logged and the rest go on."""
        for guild in list(self.bot.guilds):
            if getattr(guild, "unavailable", False) or not self.on_in(guild.id):
                continue
            for row in list(self.cfg(guild.id)["forums"]):
                forum = guild.get_channel(row["forum_id"])
                if not row["solved_tag"] or not isinstance(forum, discord.ForumChannel):
                    continue  # the reminder asks for the solution to be marked, which takes a solved tag
                try:
                    await self._remind_forum(guild, forum, row)
                except Exception:
                    log.exception("Forum reminders failed in forum %s of %s", forum.id, guild.id)

    async def _remind_forum(self, guild: Any, forum: discord.ForumChannel, row: dict) -> None:
        reminded = {found["thread_id"] for found in await self.db.all("SELECT thread_id FROM forum_reminders WHERE guild_id = ? AND channel_id = ?", (guild.id, forum.id))}
        for thread in sorted(forum.threads, key=lambda found: found.id):
            if thread.archived or thread.id in reminded or row["solved_tag"] in [tag.id for tag in thread.applied_tags]:
                continue  # a closed post would open again for the reminder
            now = discord.utils.utcnow()
            if (now - discord.utils.snowflake_time(thread.id)).total_seconds() <= INACTIVE_SECONDS:
                continue  # a post started less than six days ago can't have been quiet that long
            try:
                quiet = await self._quiet_since(thread)
            except discord.HTTPException as error:
                log.info("Could not read the newest message in %s: %s", thread.id, error)
                continue
            if (now - quiet).total_seconds() > INACTIVE_SECONDS:
                await self._remind(guild, thread, row)

    @staticmethod
    async def _quiet_since(thread: discord.Thread) -> Any:
        """When the post's newest message was sent, as Discord has it now, or when the post started if it has none."""
        async for message in thread.history(limit=1):
            return message.created_at
        return discord.utils.snowflake_time(thread.id)

    async def _remind(self, guild: Any, thread: discord.Thread, row: dict) -> None:
        """The reminder, written down before it's sent, so two looks or a restart can't send it twice. One Discord refuses is crossed off
        again and tried the next hour, as the old bot did."""
        try:
            author = await self._member(guild, thread.owner_id)
        except discord.HTTPException as error:
            self.log_line(guild.id, f"Failed to send inactivity ping in thread <#{thread.id}>: {error}")
            return
        if author is None:
            if thread.id not in self._gone_owners:
                if len(self._gone_owners) >= GONE_KEPT:
                    self._gone_owners.clear()
                self._gone_owners.add(thread.id)
                self.log_line(guild.id, f"Failed to send inactivity ping in thread <#{thread.id}>: Thread owner is no longer a member.")
            return
        claimed = await self.db.run("INSERT OR IGNORE INTO forum_reminders (guild_id, thread_id, channel_id, reminded_at) VALUES (?, ?, ?, ?)",
                                    (guild.id, thread.id, row["forum_id"], time.time()))
        if not claimed:
            return
        template = reminder_text(row)
        allowed = mentions_for(template, author)
        try:
            for part in pieces(fill(template, author)):
                await thread.send(part, allowed_mentions=allowed)
        except discord.Forbidden:
            await self.db.run("DELETE FROM forum_reminders WHERE guild_id = ? AND thread_id = ?", (guild.id, thread.id))
            self.log_line(guild.id, f"Failed to send inactivity ping in thread <#{thread.id}> due to missing permissions.")
            return
        except discord.HTTPException as error:
            await self.db.run("DELETE FROM forum_reminders WHERE guild_id = ? AND thread_id = ?", (guild.id, thread.id))
            self.log_line(guild.id, f"Failed to send inactivity ping in thread <#{thread.id}>: {error}")
            return
        self.log_line(guild.id, f"Inactivity ping sent in thread <#{thread.id}> for forum channel <#{row['forum_id']}>")

    # A deleted post or forum takes its rows along. This is the pack's own bookkeeping, so it runs with the pack off too.
    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        await self.db.run("DELETE FROM forum_welcomes WHERE guild_id = ? AND thread_id = ?", (payload.guild_id, payload.thread_id))
        await self.db.run("DELETE FROM forum_reminders WHERE guild_id = ? AND thread_id = ?", (payload.guild_id, payload.thread_id))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        if getattr(channel, "type", None) in (discord.ChannelType.forum, discord.ChannelType.media):
            await self.db.run("DELETE FROM forum_welcomes WHERE guild_id = ? AND channel_id = ?", (channel.guild.id, channel.id))
            await self.db.run("DELETE FROM forum_reminders WHERE guild_id = ? AND channel_id = ?", (channel.guild.id, channel.id))

    # ---- marking a post solved ----

    def manages_posts(self, member: Any, forum: Any) -> bool:
        """AF-17: who may close anyone's post in this forum, as the old bot's role named Moderator could. By hand that takes Manage Threads there,
        which Discord calls Manage Posts in a forum. The bot manager role counts too, and so does a role that may skip Manage Threads on
        /close_solved_threads, the command that closes every solved post, which is how a server gives its moderators this without Manage Threads."""
        if access.is_admin(member) or (forum is not None and access.can_view(member, forum) and access.permissions_in(member, forum).manage_threads):
            return True
        rules = self.bot.settings.get(member.guild.id, COMMANDS_MODULE)
        held = {role.id for role in getattr(member, "roles", [])}
        bypass = set((rules.get("overwrites", {}).get("close_solved_threads") or {}).get("bypass_roles") or [])
        return bool(rules.get("mod_role") and rules["mod_role"] in held) or bool(bypass & held)

    def may_mark(self, member: Any, thread: discord.Thread, row: dict | None) -> bool:
        """The post's author, the forum's helper roles, and anyone who manages posts there."""
        helpers = set(row["helper_roles"]) if row else set()
        return member.id == thread.owner_id or any(role.id in helpers for role in getattr(member, "roles", [])) or self.manages_posts(member, thread.parent)

    @staticmethod
    def solved_refusal(thread: discord.Thread, row: dict) -> str | None:
        """Why the solved tag can't go on this post now, checked before anything changes. A closed post takes no edits, and Discord refuses a
        sixth tag."""
        if thread.archived:
            return CLOSED_POST
        tag = thread.parent.get_tag(row["solved_tag"]) if thread.parent is not None else None
        if tag is None:
            return TAG_GONE
        applied = [found.id for found in thread.applied_tags]
        if tag.id not in applied and len(applied) >= TAGS_PER_POST:
            return TAGS_FULL
        return None

    async def _close_solved(self, thread: discord.Thread, row: dict, who: str) -> None:
        """Adds the solved tag to the tags the post has, then closes it, as the old bot did, in two edits."""
        current = [tag.id for tag in thread.applied_tags]
        if row["solved_tag"] not in current:
            current.append(row["solved_tag"])
        tags = [tag for tag_id in current if (tag := thread.parent.get_tag(tag_id)) is not None]
        self.log_line(thread.guild.id, f"Applying solved tag to thread <#{thread.id}> by {who}")
        await thread.edit(applied_tags=tags)
        self.log_line(thread.guild.id, f"Archiving thread <#{thread.id}> by {who}")
        await thread.edit(archived=True)

    async def solved_pressed(self, interaction: discord.Interaction) -> None:
        """AF-12: the Mark as Solved button. The click turns the button off and answers in one step, before the post closes and takes no more edits."""
        guild = interaction.guild
        if guild is None or not self.on_in(guild.id):
            await _answer(interaction, SWITCHED_OFF)
            return
        who = interaction.user.mention
        self.log_line(guild.id, f"Mark as Solved button clicked by {who} in channel ID {interaction.channel_id}")
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await _answer(interaction, "This button can only be used in a thread.")
            self.log_line(guild.id, f"Invalid channel for button interaction: {interaction.channel_id} is not a thread.")
            return
        if not isinstance(thread.parent, discord.ForumChannel):
            await _answer(interaction, "This thread is not in a forum channel.")
            self.log_line(guild.id, f"Thread ID {thread.id} is not in a forum channel.")
            return
        row = self.forum_row(guild.id, thread.parent_id)
        if row is None or not row["solved_tag"]:
            await _answer(interaction, "No solved tag configured for this forum channel.")
            self.log_line(guild.id, f"No solved tag configured for forum channel ID {thread.parent_id}.")
            return
        if not self.may_mark(interaction.user, thread, row):
            await _answer(interaction, "You don't have permission to mark this thread as solved.")
            self.log_line(guild.id, f"User {who} lacks permission to mark thread ID {thread.id} as solved.")
            return
        refusal = self.solved_refusal(thread, row)
        if refusal:
            await _answer(interaction, refusal)
            return
        done = SolvedButton(self)
        done.solved.disabled = True
        try:
            self.log_line(guild.id, f"Disabling Mark as Solved button in thread <#{thread.id}> by {who}")
            await interaction.response.edit_message(view=done)
            await self._close_solved(thread, row, who)
            after = await guild.fetch_channel(thread.id)
        except discord.HTTPException as error:
            if await self._solved_since(thread, row):  # another click closed it a moment ago, and Discord takes no edits to a closed post
                await _answer(interaction, ALREADY_SOLVED)
                return
            await self._button_back(interaction)
            if isinstance(error, discord.Forbidden):
                await _answer(interaction, "I don't have permission to edit this thread.")
                self.log_line(guild.id, f"Failed to mark thread <#{thread.id}> as solved or archive: Missing permissions.")
            else:
                await _answer(interaction, f"Failed to mark the thread as solved or close it: {error}")
                self.log_line(guild.id, f"Failed to mark thread <#{thread.id}> as solved or archive: {error}")
            return
        if getattr(after, "archived", False):
            self.log_line(guild.id, f"Thread <#{thread.id}> confirmed archived by {who}")
        else:
            self.log_line(guild.id, f"Thread <#{thread.id}> unexpectedly unarchived after operation")
        self.log_line(guild.id, f"Thread <#{thread.id}> marked as solved and archived by {who}")
        await _answer(interaction, "Thread marked as solved and closed!")

    @staticmethod
    async def _solved_since(thread: discord.Thread, row: dict) -> bool:
        """Whether the post is closed with the solved tag now, as Discord has it. The cache learns of a close a moment after it."""
        try:
            fresh = await thread.guild.fetch_channel(thread.id)
        except (discord.HTTPException, discord.InvalidData):
            return False
        return isinstance(fresh, discord.Thread) and fresh.archived and row["solved_tag"] in [tag.id for tag in fresh.applied_tags]

    async def _button_back(self, interaction: discord.Interaction) -> None:
        """Turns the button on again after its click turned it off and the close then failed, so the post's author can try once more."""
        if not interaction.response.is_done():
            return  # the click's own edit is what failed, so the button never went off
        try:
            await interaction.edit_original_response(view=SolvedButton(self))
        except discord.HTTPException as error:
            log.info("Could not turn the Mark as Solved button back on in %s: %s", interaction.channel_id, error)

    async def _post_of(self, interaction: discord.Interaction, message: discord.Message) -> discord.Thread | None:
        """The post a message is in. A closed post isn't in the cache, and the menu's own channel or Discord names it."""
        if isinstance(message.channel, discord.Thread):
            return message.channel
        if isinstance(interaction.channel, discord.Thread) and interaction.channel.id == message.channel.id:
            return interaction.channel
        return await self._thread_of(message.channel)

    async def solution_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """AF-11: Mark as Solution pins the message, adds the solved tag and closes the post."""
        guild = interaction.guild
        if guild is None or not self.on_in(guild.id):
            await _answer(interaction, SWITCHED_OFF)
            return
        thread = await self._post_of(interaction, message)
        if thread is None or not isinstance(thread.parent, discord.ForumChannel):
            await _answer(interaction, "This command can only be used on messages in forum threads.")
            return
        row = self.forum_row(guild.id, thread.parent_id)
        if not self.may_mark(interaction.user, thread, row):
            await _answer(interaction, "You don't have permission to mark this message as the solution.")
            return
        if row is None or not row["solved_tag"]:
            await _answer(interaction, "No solved tag configured for this forum channel.")
            return
        refusal = self.solved_refusal(thread, row)
        if refusal:
            await _answer(interaction, refusal)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        who = interaction.user.mention
        try:
            self.log_line(guild.id, f"Pinning message in thread <#{thread.id}> by {who}")
            await message.pin()
            await self._close_solved(thread, row, who)
        except discord.HTTPException as error:
            if await self._solved_since(thread, row):  # someone else closed it a moment ago
                await _answer(interaction, ALREADY_SOLVED)
                return
            self.log_line(guild.id, f"Failed to pin message, mark thread as solved, or archive thread <#{thread.id}>: {error}")
            await _answer(interaction, "I don't have permission to perform this action." if isinstance(error, discord.Forbidden) else f"Failed to process the action: {error}")
            return
        self.log_line(guild.id, f"Message pinned and thread <#{thread.id}> marked as solved and archived by {who}")
        await _answer(interaction, "Message pinned, thread marked as solved, and closed!")

    # ---- what a setting may do ----

    @staticmethod
    def text_pings(guild: Any, text: str) -> set[str]:
        """What one text pings that takes Mention Everyone: @everyone, @here and roles members can't ping."""
        words = set(EVERYONE_WORD.findall(text))
        return words | {f"<@&{role_id}>" for role_id in ROLE_MENTION.findall(text) if delivery.needs_ping_right(guild, int(role_id))}

    def pings_needing_right(self, guild: Any, row: dict | None) -> set[str]:
        """What a forum's texts ping that takes Mention Everyone, each under the text that holds it, as "message:@everyone". A ping is new to a
        text that didn't hold it, even when the forum's other text does. The default reminder pings the helper roles, so with no reminder of
        its own they count under the reminder."""
        if not row:
            return set()
        words = {f"message:{word}" for word in self.text_pings(guild, row["message"])}
        reminder = self.text_pings(guild, row["inactivity_message"]) if row["inactivity_message"] else \
            {f"<@&{role_id}>" for role_id in row["helper_roles"] if delivery.needs_ping_right(guild, role_id)}
        return words | {f"inactivity_message:{word}" for word in reminder}

    def text_refusal(self, member: Any, forum: Any, before: dict | None, after: dict) -> str | None:
        """DECISIONS, posting: the bot posts a forum's texts in its posts for whoever saved them, so they must be able to post there, and to send
        any ping that takes Mention Everyone. Only what's new is checked, so a typo can be fixed in a forum someone else set up."""
        had_text = bool(before and (before["message"] or before["inactivity_message"]))
        if not had_text and (after["message"] or after["inactivity_message"]):
            refusal = access.post_refusal(member, forum)
            if refusal:
                return refusal
        if self.pings_needing_right(forum.guild, after) - self.pings_needing_right(forum.guild, before):
            return access.ping_refusal(member, forum)
        return None

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {
            "log_channel": {settings["log_channel"]} if settings.get("log_channel") else set(),
            "forums": {row["forum_id"] for row in settings.get("forums") or [] if row["message"] or row["inactivity_message"]},
        }

    def pinged_channels(self, settings: dict) -> dict[str, set[int]]:
        """Each ping by its own field name, so a new one is checked and fixing a typo around an old one isn't."""
        found: dict[str, set[int]] = {}
        for row in settings.get("forums") or []:
            guild = getattr(self.bot.get_channel(row["forum_id"]), "guild", None)
            for word in self.pings_needing_right(guild, row):
                found[f"forums:{row['forum_id']}:{word}"] = {row["forum_id"]}
        return found

    def power_roles(self, settings: dict) -> dict[str, set[int]]:
        """Helper roles can close anyone's post in their forum, so @everyone can't be one."""
        return {"forums": {role_id for row in settings.get("forums") or [] for role_id in row["helper_roles"]}}

    async def _change_forum(self, guild_id: int, forum_id: int, actor: Any, action: str, change: Callable[[dict], None]) -> dict:
        """Changes one forum's entry, adding it when the server hasn't set that forum up. One change at a time in a server, so two commands
        can't each save the list without the other's change."""
        async with self._edit_locks.setdefault(guild_id, asyncio.Lock()):
            rows = [dict(row) for row in self.cfg(guild_id)["forums"]]
            row = next((found for found in rows if found["forum_id"] == forum_id), None)
            if row is None:
                row = blank_row(forum_id)
                rows.append(row)
            change(row)
            saved = await self.bot.settings.save(guild_id, self.key, {"forums": rows}, actor=self.bot.actor(actor), action=action)
        return next(found for found in saved["forums"] if found["forum_id"] == forum_id)

    @staticmethod
    async def _say(ctx: commands.Context, text: str) -> None:
        """A private answer, in the old bot's words, that a command's later answers can follow. One that can't go anymore, after Discord's 15
        minutes, is dropped, since the log channel holds the summary. The library would post it in the channel for everyone instead."""
        if ctx.interaction is not None and ctx.interaction.is_expired():
            return
        try:
            await ctx.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.info("Could not answer a forum messages command: %s", error)

    # ---- the old bot's commands ----

    @commands.hybrid_command(name="set_message", description="Set a custom reply message for a forum channel")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(forum_channel="The forum channel")
    async def set_message(self, ctx: commands.Context, forum_channel: discord.ForumChannel) -> None:
        await self._open_form(ctx, forum_channel, inactivity=False)

    @commands.hybrid_command(name="set_inactivity_message", description="Set a custom inactivity message for a forum channel")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(forum_channel="The forum channel")
    async def set_inactivity_message(self, ctx: commands.Context, forum_channel: discord.ForumChannel) -> None:
        await self._open_form(ctx, forum_channel, inactivity=True)

    async def _open_form(self, ctx: commands.Context, forum: discord.ForumChannel, *, inactivity: bool) -> None:
        if ctx.interaction is None:
            name = "set_inactivity_message" if inactivity else "set_message"
            await ctx.fail(f"The form only opens from the slash command `/{name}`. The Forum messages page on the dashboard edits it too.")
            return
        refusal = access.view_refusal(ctx.author, forum)
        if refusal:
            await ctx.fail(refusal)
            return
        row = self.forum_row(ctx.guild.id, forum.id) or blank_row(forum.id)
        await ctx.interaction.response.send_modal(MessageForm(self, forum.id, row["inactivity_message" if inactivity else "message"], inactivity=inactivity))

    async def submit_message(self, interaction: discord.Interaction, forum_id: int, text: str, *, inactivity: bool) -> None:
        """Saves what the form holds, once the person has passed the command's rules again and may post and ping what the text holds there."""
        guild = interaction.guild
        if guild is None or not self.on_in(guild.id):
            await _answer(interaction, SWITCHED_OFF)
            return
        member = interaction.user
        decision = self.bot.command_decision(self.set_inactivity_message if inactivity else self.set_message, member, interaction.channel)
        if not decision.allowed:
            await _answer(interaction, decision.reason or "You can't use this command here.")
            return
        forum = guild.get_channel(forum_id)
        if not isinstance(forum, discord.ForumChannel):
            await _answer(interaction, "That forum is gone.")
            return
        field = "inactivity_message" if inactivity else "message"
        try:
            clean = message_text(text)
        except Invalid as error:
            await _answer(interaction, str(error))
            return
        before = self.forum_row(guild.id, forum_id)
        refusal = self.text_refusal(member, forum, before, {**(before or blank_row(forum_id)), field: clean})
        if refusal:
            await _answer(interaction, refusal)
            return
        try:
            await self._change_forum(guild.id, forum_id, member, f"forummessages.{field}", lambda row: row.update({field: clean}))
        except ValidationError as error:  # a server at the most forums the list holds
            await _answer(interaction, next(iter(error.fields.values()), "That setting was refused."))
            return
        if inactivity and not clean:
            await _answer(interaction, f"Inactivity message for forum channel <#{forum_id}> set back to the default")
            self.log_line(guild.id, f"Inactivity message for forum channel <#{forum_id}> set back to the default by {member.mention}")
            return
        label = "Inactivity message" if inactivity else "Message"
        await _answer(interaction, f"{label} set for forum channel <#{forum_id}>")
        self.log_line(guild.id, f"{label} set for forum channel <#{forum_id}> by {member.mention}")

    @commands.hybrid_command(name="set_solved_tag", description="Set the solved tag for a forum channel")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(forum_channel="The forum channel", tag="The tag that marks a post solved")
    async def set_solved_tag(self, ctx: commands.Context, forum_channel: discord.ForumChannel, *, tag: str) -> None:
        refusal = access.view_refusal(ctx.author, forum_channel)
        if refusal:
            await ctx.fail(refusal)
            return
        found = next((option for option in forum_channel.available_tags if option.name.lower() == tag.lower()), None)
        if found is None:
            await self._say(ctx, f"No tag named '{discord.utils.escape_markdown(tag[:100])}' found in <#{forum_channel.id}>.")
            return
        await self._change_forum(ctx.guild.id, forum_channel.id, ctx.author, "forummessages.solved_tag", lambda row: row.update(solved_tag=found.id))
        said = f"Solved tag set to '{discord.utils.escape_markdown(found.name)}' for forum channel <#{forum_channel.id}>"
        await self._say(ctx, said)
        self.log_line(ctx.guild.id, f"{said} by {ctx.author.mention}")

    @set_solved_tag.autocomplete("tag")
    async def _tag_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """The forum's tags, the ones holding what's typed so far."""
        picked = getattr(interaction.namespace, "forum_channel", None)
        forum = interaction.guild.get_channel(picked.id) if picked is not None and interaction.guild is not None else None
        if not isinstance(forum, discord.ForumChannel) or not access.can_view(interaction.user, forum):
            return []
        needle = current.lower()
        return [app_commands.Choice(name=tag.name, value=tag.name) for tag in forum.available_tags if needle in tag.name.lower()][:CHOICES]

    @commands.hybrid_command(name="set_helper_roles", description="Set helper roles for a forum channel (first role required, others optional)")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(forum_channel="The forum channel", role1="A helper role", role2="Another helper role", role3="Another helper role",
                           role4="Another helper role", role5="Another helper role")
    async def set_helper_roles(self, ctx: commands.Context, forum_channel: discord.ForumChannel, role1: discord.Role, role2: Optional[discord.Role] = None,
                               role3: Optional[discord.Role] = None, role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None) -> None:
        refusal = access.view_refusal(ctx.author, forum_channel)
        if refusal:
            await ctx.fail(refusal)
            return
        roles: list[discord.Role] = []
        for role in (role1, role2, role3, role4, role5):
            if role is not None and all(role.id != other.id for other in roles):
                roles.append(role)
        if any(role.is_default() for role in roles):
            await ctx.fail(access.NOT_EVERYONE)
            return
        before = self.forum_row(ctx.guild.id, forum_channel.id)
        refusal = self.text_refusal(ctx.author, forum_channel, before, {**(before or blank_row(forum_channel.id)), "helper_roles": [role.id for role in roles]})
        if refusal:
            await ctx.fail(refusal)
            return
        await self._change_forum(ctx.guild.id, forum_channel.id, ctx.author, "forummessages.helper_roles", lambda row: row.update(helper_roles=[role.id for role in roles]))
        said = f"Helper roles set to {', '.join(role.mention for role in roles)} for forum channel <#{forum_channel.id}>"
        await self._say(ctx, said)
        self.log_line(ctx.guild.id, f"{said} by {ctx.author.mention}")

    @commands.hybrid_command(name="list_messages", description="List all configured forum messages")
    @commands.guild_only()
    @requires("manage_threads")
    async def list_messages(self, ctx: commands.Context) -> None:
        """AF-5, in the old bot's layout, leaving out forums the person can't see."""
        entries = []
        for row in self.cfg(ctx.guild.id)["forums"]:
            forum = ctx.guild.get_channel(row["forum_id"])
            if forum is not None and not access.can_view(ctx.author, forum):
                continue
            roles = ", ".join(f"<@&{role_id}>" for role_id in row["helper_roles"]) or "None"
            entries.append(f"<#{row['forum_id']}>:\n  Message: {row['message'][:EXCERPT] or 'Not set'}...\n  Inactivity Message: {row['inactivity_message'][:EXCERPT] or 'Not set'}...\n"
                           f"  Solved Tag ID: {row['solved_tag'] or 'Not set'}\n  Helper Roles: {roles}\n")
        if not entries:
            await self._say(ctx, "No messages configured.")
            return
        parts: list[str] = []
        current = "Configured forum messages:\n"
        for entry in entries:
            if len(current) + len(entry) > MESSAGE_LIMIT:
                parts.append(current)
                current = ""
            current += entry
        parts.append(current)
        for part in parts:
            await self._say(ctx, part[:MESSAGE_LIMIT])

    @commands.hybrid_command(name="delete_message", description="Delete a custom message and settings for a forum channel")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(forum_channel="The forum channel")
    async def delete_message(self, ctx: commands.Context, forum_channel: discord.ForumChannel) -> None:
        refusal = access.view_refusal(ctx.author, forum_channel)
        if refusal:
            await ctx.fail(refusal)
            return
        async with self._edit_locks.setdefault(ctx.guild.id, asyncio.Lock()):
            rows = [row for row in self.cfg(ctx.guild.id)["forums"] if row["forum_id"] != forum_channel.id]
            found = len(rows) < len(self.cfg(ctx.guild.id)["forums"])
            if found:
                await self.bot.settings.save(ctx.guild.id, self.key, {"forums": rows}, actor=self.bot.actor(ctx.author), action="forummessages.delete")
        if not found:
            await self._say(ctx, "No message configured for this channel.")
            return
        said = f"Message, inactivity message, and settings deleted for forum channel <#{forum_channel.id}>"
        await self._say(ctx, said)
        self.log_line(ctx.guild.id, f"{said} by {ctx.author.mention}")

    @commands.hybrid_command(name="forum_log_channel", description="Set the channel that logs forum message actions")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(log_channel="The channel for the log")
    async def forum_log_channel(self, ctx: commands.Context, log_channel: discord.TextChannel) -> None:
        """AF-7: the old /set_log_channel. Three old bots used that name, so each one's log channel command carries its feature's word."""
        refusal = access.post_refusal(ctx.author, log_channel)
        if refusal:
            await ctx.fail(refusal)
            return
        await self.bot.settings.save(ctx.guild.id, self.key, {"log_channel": log_channel.id}, actor=self.bot.actor(ctx.author), action="forummessages.log_channel")
        await self._say(ctx, f"Log channel set to <#{log_channel.id}>")
        self.log_line(ctx.guild.id, f"Log channel set to <#{log_channel.id}> by {ctx.author.mention}")

    async def _one_at_a_time(self, ctx: commands.Context, forum: Any, work: Any) -> None:
        """A sweep of a forum's posts runs once at a time in each forum, since a second would repeat the first's edits."""
        key = (ctx.guild.id, forum.id)
        if key in self._busy:
            work.close()
            await ctx.fail("A cleanup is already running in that forum. Try again when it finishes.")
            return
        self._busy.add(key)
        try:
            await work
        finally:
            self._busy.discard(key)

    @commands.hybrid_command(name="clean_threads", description="Remove users with specified roles from all threads in a forum channel")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(forum_channel="The forum channel", role1="Members with this role leave every post", role2="Another role", role3="Another role",
                           role4="Another role", role5="Another role")
    async def clean_threads(self, ctx: commands.Context, forum_channel: discord.ForumChannel, role1: discord.Role, role2: Optional[discord.Role] = None,
                            role3: Optional[discord.Role] = None, role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None) -> None:
        refusal = access.view_refusal(ctx.author, forum_channel)
        if refusal:
            await ctx.fail(refusal)
            return
        roles = [role for role in (role1, role2, role3, role4, role5) if role is not None]
        await self._one_at_a_time(ctx, forum_channel, self._clean(ctx, forum_channel, roles))

    async def _clean(self, ctx: commands.Context, forum: discord.ForumChannel, roles: list[discord.Role]) -> None:
        """AF-9: every open post, newest first, one a second. The bot never takes itself out of a post."""
        guild = ctx.guild
        await self._say(ctx, "Thread cleaning process started...")
        role_ids = {role.id for role in roles}
        self.log_line(guild.id, f"Starting thread cleanup in forum channel <#{forum.id}> for roles: {', '.join(role.mention for role in roles)} by {ctx.author.mention}")
        processed = removed = errors = 0
        me = self.bot.user
        for thread in sorted(forum.threads, key=lambda found: found.id, reverse=True):
            processed += 1
            try:
                for thread_member in await thread.fetch_members():
                    if me is not None and thread_member.id == me.id:
                        continue
                    try:
                        member = await self._member(guild, thread_member.id)
                    except discord.HTTPException:
                        continue
                    if member is None or not any(role.id in role_ids for role in member.roles):
                        continue
                    try:
                        await thread.remove_user(member)
                        removed += 1
                        self.log_line(guild.id, f"Removed {member.mention} from thread <#{thread.id}>")
                    except discord.Forbidden:
                        errors += 1
                        self.log_line(guild.id, f"Failed to remove {member.mention} from thread <#{thread.id}>: Missing permissions.")
                    except discord.HTTPException as error:
                        errors += 1
                        self.log_line(guild.id, f"Failed to remove {member.mention} from thread <#{thread.id}>: {error}")
            except discord.Forbidden:
                errors += 1
                self.log_line(guild.id, f"Failed to access thread <#{thread.id}>: Missing permissions.")
            except discord.HTTPException as error:
                errors += 1
                self.log_line(guild.id, f"Failed to process thread <#{thread.id}>: {error}")
            await asyncio.sleep(self.thread_gap)
        summary = f"Thread cleanup completed in <#{forum.id}>:\n- Threads processed: {processed}\n- Users removed: {removed}\n- Errors encountered: {errors}"
        self.log_line(guild.id, f"{summary}\nNotified {ctx.author.mention}")
        await self._say(ctx, "Thread cleaning process completed. Check the log channel for details.")

    @commands.hybrid_command(name="close_solved_threads", description="Close all threads with the solved tag in a forum channel")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(forum_channel="The forum channel")
    async def close_solved_threads(self, ctx: commands.Context, forum_channel: discord.ForumChannel) -> None:
        refusal = access.view_refusal(ctx.author, forum_channel)
        if refusal:
            await ctx.fail(refusal)
            return
        row = self.forum_row(ctx.guild.id, forum_channel.id)
        if row is None or not row["solved_tag"]:
            await self._say(ctx, "No solved tag configured for this forum channel.")
            return
        await self._one_at_a_time(ctx, forum_channel, self._close_all(ctx, forum_channel, row["solved_tag"]))

    async def _close_all(self, ctx: commands.Context, forum: discord.ForumChannel, solved_tag: int) -> None:
        """AF-10: closes every open post with the solved tag, newest first, waiting a second after each one it closes."""
        guild = ctx.guild
        await self._say(ctx, "Closing solved threads started...")
        processed = closed = errors = 0
        for thread in sorted(forum.threads, key=lambda found: found.id, reverse=True):
            processed += 1
            if thread.archived or solved_tag not in [tag.id for tag in thread.applied_tags]:
                continue
            try:
                await thread.edit(archived=True)
                closed += 1
                self.log_line(guild.id, f"Closed thread <#{thread.id}> with solved tag")
            except discord.Forbidden:
                errors += 1
                self.log_line(guild.id, f"Failed to close thread <#{thread.id}>: Missing permissions.")
            except discord.HTTPException as error:
                errors += 1
                self.log_line(guild.id, f"Failed to close thread <#{thread.id}>: {error}")
            await asyncio.sleep(self.thread_gap)
        summary = f"Closing solved threads completed in <#{forum.id}>:\n- Threads processed: {processed}\n- Threads closed: {closed}\n- Errors encountered: {errors}"
        self.log_line(guild.id, f"{summary}\nNotified {ctx.author.mention}")
        await self._say(ctx, "Closing solved threads completed. Check the log channel for details.")

    @commands.hybrid_command(name="check_recent_threads", description="Check recent threads in a forum channel and post if necessary")
    @commands.guild_only()
    @requires("manage_threads")
    @app_commands.describe(forum_channel="The forum channel", num_threads="How many of the newest open posts to check")
    async def check_recent_threads(self, ctx: commands.Context, forum_channel: discord.ForumChannel, num_threads: commands.Range[int, 1, RECENT_MOST]) -> None:
        """AF-15: the welcome in each of the newest open posts that doesn't have it, filled in for that post's own author."""
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        row = self.forum_row(ctx.guild.id, forum_channel.id)
        if row is None or not row["message"]:
            await self._say(ctx, "No message configured for this forum channel.")
            return
        refusal = access.post_refusal(ctx.author, forum_channel)
        if not refusal and self.text_pings(ctx.guild, row["message"]):
            refusal = access.ping_refusal(ctx.author, forum_channel)
        if refusal:
            await ctx.fail(refusal)
            return
        for thread in sorted(forum_channel.threads, key=lambda found: found.id, reverse=True)[:num_threads]:
            try:
                if await self._welcomed(thread, row):
                    continue
                author = await self._member(ctx.guild, thread.owner_id)
            except discord.HTTPException as error:
                self.log_line(ctx.guild.id, f"Failed to post automated message in thread <#{thread.id}>: {error}")
                continue
            if author is None:
                self.log_line(ctx.guild.id, f"Failed to fetch thread owner in thread <#{thread.id}>: Owner is no longer a member.")
                continue
            await self.post_welcome(thread, row, author, verb="posted")
        await self._say(ctx, f"Checked the {num_threads} most recent threads in {discord.utils.escape_markdown(forum_channel.name)}.")

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/forummessages/forums", self._forums_endpoint)

    async def _forums_endpoint(self, req: Any) -> dict:
        """The server's forums the person can see, each with its tags for the solved tag picker, and how many posts each forum's welcome and
        reminder have reached."""
        guild = self.bot.get_guild(req.guild_id)
        if guild is not None:
            forums = [{"id": forum.id, "name": forum.name, "type": forum.type.value,
                       "tags": [{"id": tag.id, "name": tag.name, "moderated": tag.moderated} for tag in forum.available_tags]}
                      for forum in sorted((found for found in guild.channels if isinstance(found, discord.ForumChannel)), key=lambda found: (found.position, found.id))
                      if req.can_see(forum.id)]
        else:  # the demo has no server behind it, so its sample forum carries its tags in its row
            rows = (self.bot.ops.describe_guild(req.guild_id) or {}).get("channels", [])
            forums = [{"id": row["id"], "name": row["name"], "type": row["type"], "tags": row.get("available_tags", [])} for row in rows if row.get("type") in (15, 16)]
        counts: dict[int, dict] = {}
        for row in await self.db.all("SELECT channel_id AS forum_id, COUNT(*) AS n FROM forum_welcomes WHERE guild_id = ? GROUP BY channel_id", (req.guild_id,)):
            counts.setdefault(row["forum_id"], {"forum_id": row["forum_id"], "welcomed": 0, "reminded": 0})["welcomed"] = row["n"]
        for row in await self.db.all("SELECT channel_id AS forum_id, COUNT(*) AS n FROM forum_reminders WHERE guild_id = ? GROUP BY channel_id", (req.guild_id,)):
            counts.setdefault(row["forum_id"], {"forum_id": row["forum_id"], "welcomed": 0, "reminded": 0})["reminded"] = row["n"]
        return {"forums": forums, "counts": [counts[forum_id] for forum_id in sorted(counts)], "default_reminder": DEFAULT_REMINDER}

    async def seed_demo(self, guild_id: int) -> None:
        """A forum set up with a welcome, a solved tag, a helper role and a log channel, and some posts welcomed and reminded, so the demo page
        opens with something on it."""
        rows = getattr(self.bot.ops, "channels", [])
        forum = next((row for row in rows if row.get("type") == 15), None)
        log_channel = next((row["id"] for row in rows if row.get("name") == "mod-log"), None)
        helper = next((row["id"] for row in getattr(self.bot.ops, "roles", []) if row.get("name") == "Server Veteran"), None)
        if forum is None:
            return
        tags = [{"id": forum["id"] + number, "name": name, "moderated": name == "Guides"} for number, name in enumerate(("Solved", "Question", "Bug", "Guides"), start=500)]
        forum["available_tags"] = tags
        message = ("Hey {{OP.Mention}}, welcome. Post your printer model, what you've tried and a photo, and a helper will be along.\n\n"
                   "When it's fixed, right-click the message that fixed it, open **Apps** and pick **Mark as Solution**.")
        await self.bot.settings.save(guild_id, self.key, {"enabled": True, "log_channel": log_channel, "forums": [
            {"forum_id": forum["id"], "message": message, "inactivity_message": "", "solved_tag": tags[0]["id"], "helper_roles": [helper] if helper else []}]})
        now = time.time()
        for number in range(14):
            await self.db.run("INSERT INTO forum_welcomes (guild_id, thread_id, channel_id, message_id, posted_at) VALUES (?, ?, ?, ?, ?)",
                              (guild_id, 900000000000400000 + number, forum["id"], 900000000000410000 + number, now - 3600 * number))
        for number in range(3):
            await self.db.run("INSERT INTO forum_reminders (guild_id, thread_id, channel_id, reminded_at) VALUES (?, ?, ?, ?)",
                              (guild_id, 900000000000400000 + number, forum["id"], now - 86400 * number))

    @staticmethod
    async def _both_ends(thread: discord.Thread) -> Any:
        """A post's first messages after its start, where a welcome goes, then its newest ones, each once. A long post holds its welcome past
        the newest 100, and the old bot's welcome sat right after the post's first message. Two reads a post, at most 200 for a run of 100."""
        seen: set[int] = set()
        async for message in thread.history(limit=HISTORY_READ, after=discord.Object(thread.id), oldest_first=True):
            seen.add(message.id)
            yield message
        async for message in thread.history(limit=HISTORY_READ):
            if message.id not in seen:
                yield message

    async def _welcomed(self, thread: discord.Thread, row: dict) -> bool:
        """Whether a post has its welcome. A welcome on its way now counts, and so does one the table holds with its message. Otherwise the post
        is read: a message this bot posted with the Mark as Solved button, or in a forum with no solved tag one of its own that isn't a reply,
        is the welcome of a send whose answer was lost, and a message another bot posted with that button is a welcome from before the bot took
        the feature over. A wiki answer is a reply with no such button. Without Message Content, Discord sends another bot's message with no
        buttons, so any post of another bot's counts, and a post is never welcomed twice. A welcome found this way is written down, so the next
        run doesn't read the post again. A claim with no message and no welcome in the post is crossed off."""
        if (thread.guild.id, thread.id) in self._sending:
            return True
        claim = await self.db.one("SELECT message_id FROM forum_welcomes WHERE guild_id = ? AND thread_id = ?", (thread.guild.id, thread.id))
        if claim is not None and claim["message_id"] is not None:
            return True
        me = self.bot.user
        async for message in self._both_ends(thread):
            if not message.author.bot:
                continue
            parts = [item for part in message.components for item in getattr(part, "children", [part])]
            if me is not None and message.author.id == me.id:
                found = SOLVED_ID in [getattr(item, "custom_id", None) for item in parts] or (not row["solved_tag"] and message.reference is None)
            else:
                found = SOLVED_LABEL in [getattr(item, "label", None) for item in parts] or not self.bot.reads_message_text
            if found:
                await self.db.run("INSERT INTO forum_welcomes (guild_id, thread_id, channel_id, message_id, posted_at) VALUES (?, ?, ?, ?, ?) "
                                  "ON CONFLICT (guild_id, thread_id) DO UPDATE SET message_id = excluded.message_id",
                                  (thread.guild.id, thread.id, row["forum_id"], message.id, message.created_at.timestamp()))
                return True
        if claim is not None:  # a send cut off where nothing could cross it off, such as a process killed during it
            await self.db.run("DELETE FROM forum_welcomes WHERE guild_id = ? AND thread_id = ? AND message_id IS NULL", (thread.guild.id, thread.id))
        return False
