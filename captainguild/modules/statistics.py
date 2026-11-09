"""Statistics: counts of what happens in a server, kept per quarter hour, read back as cards, charts, rankings, counters and roles.

What's counted, from the moment a server switches the pack on:
- Messages, per channel and person. A thread or forum post counts toward its parent channel. Webhook posts and system messages don't count.
- Voice time, per channel and person, split by state: AFK, deafened or muted by staff, deafened or muted by themselves, or neither.
- Members: the count, read once a minute, and joins and leaves as they happen, with the invite each join came through.
- Statuses: how many people are online, idle and on do not disturb, read once a minute. Without the Presence intent, Discord's count of
  members who aren't offline, read every five minutes.
- Activities: minutes in each game or app, read once a minute, when the server switches it on and the bot has the Presence intent.
- Emoji and stickers: this server's own and standard emoji, in reactions, in message text, and as stickers. Bots' use doesn't count.

Nothing a person writes is kept here, only counts and the names that go with them. Someone who chooses privacy is counted as nobody: their
numbers still add to the server's totals, under no name, in every server, from then on and back to the start.

A server's filters change what its views show. They never change what's recorded, except for channels it keeps out entirely.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import discord
from discord.ext import commands

from ..core.module import Module
from ..core.stats import emoji as emoji_text
from ..core.stats import invites as invite_rules
from ..core.stats import store
from ..core.stats.timeframe import DEFAULT_LOOKBACK, INTERVALS, lookback_check, slot_of, time_zone
from ..core.validate import Field, Invalid, Schema, boolean, choice, integer, list_of, mapping_of, optional, snowflake, snowflakes, text
from .statistics_commands import COMMANDS_FIELDS, COMMANDS_MESSAGE_MIGRATIONS, COMMANDS_MIGRATIONS, StatsCommands
from .statistics_data import DATA_FIELDS, DATA_MESSAGE_MIGRATIONS, DATA_MIGRATIONS, StatsData
from .statistics_tools import TOOLS_ADMIN_FIELDS, TOOLS_FIELDS, TOOLS_MESSAGE_MIGRATIONS, TOOLS_MIGRATIONS, TOOLS_ROW_LIMITS, StatsTools
from .statistics_web import WEB_FIELDS, WEB_MESSAGE_MIGRATIONS, WEB_MIGRATIONS, WEB_ROW_LIMITS, StatsWeb

log = logging.getLogger(__name__)

TICK_SECONDS = 60
PRESENT_SECONDS = 300  # how often Discord's count of members present is read, when statuses one by one aren't available
ACTIVITY_GAP_SECONDS = 2 * TICK_SECONDS  # a longer gap between readings, such as a restart, isn't counted as time in a game
AGGREGATE_ID_LIMIT = 2 ** 39  # a merged channel's number stays far below any real Discord ID
EMOJI_STATES = ("used", "unused", "deleted", "unavailable")
EMOJI_TYPES = ("server", "animated", "managed", "restricted", "super")
COUNTED_TYPES = frozenset({discord.MessageType.default, discord.MessageType.reply, discord.MessageType.chat_input_command, discord.MessageType.context_menu_command})
APP_REPLIES = frozenset({discord.MessageType.chat_input_command, discord.MessageType.context_menu_command})
CUSTOM_EMOJI = re.compile(r"<a?:\w{2,32}:(\d{15,21})>")
INVITE_CODE = re.compile(r"[A-Za-z0-9-]{2,32}")
VERIFIED_STREAMS = ("twitch.tv", "youtube.com", "youtu.be")
THREAD_TYPES = frozenset({discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread})
VOICE_TYPES = frozenset({discord.ChannelType.voice, discord.ChannelType.stage_voice})


def invite_code(value: Any) -> str:
    if not isinstance(value, str) or not INVITE_CODE.fullmatch(value.strip()):
        raise Invalid("That isn't an invite code.")
    return value.strip()


def activity_number(value: Any) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2 ** 63:
        raise Invalid("That isn't an activity.")
    return value


def emoji_key(value: Any) -> str:
    """An emoji as the statistics key it, from the dashboard's picker: a standard emoji, or a server emoji or sticker by ID."""
    if isinstance(value, str):
        cleaned = value.strip()
        if re.fullmatch(r"[cs]:\d{15,21}", cleaned):
            return cleaned
        if cleaned.startswith("u:") and 1 < len(cleaned) <= 40:
            return store.unicode_key(emoji_text.bare(cleaned[2:]))
    raise Invalid("Pick an emoji from the list.")


def aggregate(value: Any) -> dict:
    """A merged channel: {"id", "name", "category_id", "auto_categories", "kinds"}."""
    if not isinstance(value, dict):
        raise Invalid("Each merged channel needs a name.")
    number = value.get("id")
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    if isinstance(number, bool) or not isinstance(number, int) or not 0 < number < AGGREGATE_ID_LIMIT:
        raise Invalid("A merged channel's number is missing.")
    return {
        "id": number,
        "name": text(100, minimum=1)(value.get("name", "")),
        "category_id": optional(snowflake)(value.get("category_id")),
        "auto_categories": snowflakes(50)(value.get("auto_categories", [])),
        "kinds": choice("messages", "voice", "both")(value.get("kinds", "both")),
    }


CORE_FIELDS = {
    "enabled": Field(False, boolean),
    # how views read time
    "timezone": Field("UTC", time_zone),
    "lookback": Field(dict(DEFAULT_LOOKBACK), lookback_check),
    "interval": Field("day", choice(*INTERVALS)),
    "time_unit": Field("hours", choice("minutes", "hours", "days")),
    # what views show
    "include": Field("humans", choice("humans", "bots", "both")),
    "voice_states": Field(sorted(store.VOICE_STATES), list_of(choice(*store.VOICE_STATES), len(store.VOICE_STATES))),
    "channel_mode": Field("block", choice("block", "allow")),
    "channels": Field([], snowflakes(500)),
    "voice_mode": Field("same", choice("same", "block", "allow")),
    "voice_channels": Field([], snowflakes(500)),
    "member_mode": Field("block", choice("block", "allow")),
    "members": Field([], snowflakes(1000)),
    "roles_allow": Field([], snowflakes(250)),
    "roles_block": Field([], snowflakes(250)),
    "activity_mode": Field("block", choice("block", "allow")),
    "activities": Field([], list_of(activity_number, 500)),
    "invite_mode": Field("block", choice("block", "allow")),
    "invites": Field([], list_of(invite_code, 500)),
    "fake_mode": Field("block", choice("block", "allow")),
    "fake_match": Field("any", choice("any", "all")),
    "fake_reasons": Field([], list_of(choice(*invite_rules.FAKE_REASONS), len(invite_rules.FAKE_REASONS))),
    "emoji_mode": Field("block", choice("block", "allow")),
    "emoji": Field([], list_of(emoji_key, 25)),
    "emoji_states": Field(list(EMOJI_STATES), list_of(choice(*EMOJI_STATES), len(EMOJI_STATES))),
    "emoji_type_mode": Field("block", choice("block", "allow")),
    "emoji_type_match": Field("any", choice("any", "all")),
    "emoji_types": Field([], list_of(choice(*EMOJI_TYPES), len(EMOJI_TYPES))),
    "emoji_uses": Field(list(store.EMOJI_USES), list_of(choice(*store.EMOJI_USES), len(store.EMOJI_USES))),
    # what's recorded
    "untracked_channels": Field([], snowflakes(500)),
    "track_activities": Field(False, boolean),
    "verify_activities": Field(True, boolean),
    "invite_min_account_minutes": Field(0, integer(0, 5_256_000)),
    "invite_min_join_minutes": Field(0, integer(0, 5_256_000)),
    "invite_required_roles": Field([], snowflakes(50)),
    "invite_required_match": Field("any", choice("any", "all")),
    "invite_labels": Field({}, mapping_of(invite_code, text(100, minimum=1), 1000)),
    "aggregates": Field([], list_of(aggregate, 50, unique=False)),
    # who can use it
    "command_access": Field("all", choice("all", "restricted", "self", "admins")),
    "command_channel_mode": Field("block", choice("block", "allow")),
    "command_channels": Field([], snowflakes(500)),
    "override_roles": Field([], snowflakes(50)),
    "dashboard_access": Field("staff", choice("managers", "staff", "members", "public")),
    "viewer_roles": Field([], snowflakes(50)),
}
SCHEMA = Schema({**CORE_FIELDS, **COMMANDS_FIELDS, **WEB_FIELDS, **TOOLS_FIELDS, **DATA_FIELDS})


def counted(message: Any) -> bool:
    """A message a person or a bot wrote. Webhook posts, followed channels' copies and system messages such as joins and pins don't count.
    A slash command's reply carries the app's webhook ID and counts as the bot's message."""
    if message.type not in COUNTED_TYPES:
        return False
    return message.webhook_id is None or message.type in APP_REPLIES


def voice_state_code(state: Any, afk_channel_id: int | None) -> int:
    if state.channel is not None and afk_channel_id is not None and state.channel.id == afk_channel_id:
        return store.AFK
    if state.deaf:
        return store.SERVER_DEAFENED
    if state.self_deaf:
        return store.SELF_DEAFENED
    if state.mute:
        return store.SERVER_MUTED
    if state.self_mute:
        return store.SELF_MUTED
    return store.TALKING


def activity_identity(activity: Any) -> tuple[int, int | None, str, bool] | None:
    """(kind, application ID, name, verified) for an activity that's counted, or None. A custom status isn't an activity."""
    kind = getattr(getattr(activity, "type", None), "value", None)
    if kind not in store.ACTIVITY_KINDS.values():
        return None
    name = (getattr(activity, "name", None) or "").strip()
    if isinstance(activity, discord.Spotify):
        return store.LISTENING, None, "Spotify", True
    if not name:
        return None
    application_id = getattr(activity, "application_id", None)
    if isinstance(activity, discord.Streaming):
        url = (activity.url or "").lower()
        return store.STREAMING, None, name[:100], any(host in url for host in VERIFIED_STREAMS)
    return kind, application_id, name[:100], application_id is not None


class Statistics(StatsCommands, StatsWeb, StatsTools, StatsData, Module):
    key = "statistics"
    category = "Stats"
    command_pack = True
    steps_aside_when_off = True  # its typed names are common words a server's tags often hold, such as top and me
    pack_modules = tuple(f"{__package__}.statistics_{name}" for name in ("commands", "web", "tools", "data"))
    schema = SCHEMA
    migrations = [*store.MAIN_MIGRATIONS, *COMMANDS_MIGRATIONS, *WEB_MIGRATIONS, *TOOLS_MIGRATIONS, *DATA_MIGRATIONS]
    message_migrations = [*store.MESSAGE_MIGRATIONS, *COMMANDS_MESSAGE_MIGRATIONS, *WEB_MESSAGE_MIGRATIONS, *TOOLS_MESSAGE_MIGRATIONS, *DATA_MESSAGE_MIGRATIONS]
    admin_fields = frozenset({"dashboard_access", "viewer_roles", "override_roles"}) | TOOLS_ADMIN_FIELDS
    row_limits = {**TOOLS_ROW_LIMITS, **WEB_ROW_LIMITS}

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.recorder = store.Recorder(self.message_db)
        self.sessions = store.VoiceSessions(self.recorder, self.recorded_as)
        self.private: set[int] = set()
        self._invites: dict[int, dict[str, invite_rules.InviteState]] = {}
        self._invite_locks: dict[int, asyncio.Lock] = {}
        self._present_read: dict[int, float] = {}
        self._activities_read: dict[int, float] = {}
        self._named: set[tuple[int, int]] = set()
        self._on: set[int] = set()  # servers whose sessions, channels and invites are in step, so a settings save doesn't redo it
        self._write_lock = asyncio.Lock()
        self._loop: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    # ---- start and stop ----

    async def on_load(self) -> None:
        self.private = {row["user_id"] for row in await self.db.all("SELECT user_id FROM stat_user_settings WHERE private = 1")}
        self.bot.settings.listen(self.key, self._settings_saved)
        await self._commands_load()
        await self._web_load()
        await self._tools_load()
        await self._data_load()

    def api(self, router: Any) -> None:
        self._commands_routes(router)
        self._web_routes(router)
        self._tools_routes(router)
        self._data_routes(router)

    async def cog_unload(self) -> None:
        self._closing = True
        for unload in (self._commands_unload, self._web_unload, self._tools_unload, self._data_unload):
            try:
                await unload()
            except Exception:
                log.exception("A statistics part didn't stop cleanly.")
        for task in [self._loop, *self._tasks]:
            if task is not None:
                task.cancel()
        self.sessions.checkpoint(time.time())
        await self.write()

    def _spawn(self, work: Any) -> None:
        """Runs work the gateway event shouldn't wait for, keeping hold of it so it isn't dropped and its failure is logged."""
        task = asyncio.ensure_future(work)
        self._tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                log.error("Statistics work failed", exc_info=finished.exception())
        task.add_done_callback(done)

    def tracking(self, guild_id: int | None) -> bool:
        return guild_id is not None and not self._closing and self.on_in(guild_id)

    def recorded_as(self, user_id: int) -> int:
        """The ID someone's counts go under: 0 once they've chosen privacy."""
        return 0 if user_id in self.private else user_id

    async def write(self) -> None:
        """Writes what's been counted. A failure keeps it in memory for the next try."""
        async with self._write_lock:
            try:
                await self.recorder.flush()
            except Exception:
                log.exception("Could not write statistics. They stay in memory for the next try.")

    async def up_to_date(self) -> None:
        """Everything counted so far, voice time included, written: read before answering anything."""
        self.sessions.checkpoint(time.time())
        await self.write()

    # ---- the minute loop ----

    async def _run(self) -> None:
        while not self._closing:
            await asyncio.sleep(TICK_SECONDS - time.time() % TICK_SECONDS)
            try:
                await self.tick(time.time())
            except Exception:
                log.exception("Statistics missed a reading. The next minute tries again.")

    async def tick(self, now: float) -> None:
        """Once a minute: voice time so far, the member count, statuses and activities, then everything goes to the database."""
        slot = slot_of(now)
        self.sessions.checkpoint(now)
        settled = self.bot.members_settled.is_set()
        presences = self.bot.intents.presences
        for guild in list(self.bot.guilds):
            if guild.unavailable or not self.tracking(guild.id):
                continue
            if settled:
                self._count_members(guild, slot)
                if presences:
                    self._read_statuses(guild, slot)
                elif now - self._present_read.get(guild.id, 0.0) >= PRESENT_SECONDS:
                    self._present_read[guild.id] = now
                    self._spawn(self._read_present(guild.id, slot))
            if presences and self.cfg(guild.id)["track_activities"]:
                self._read_activities(guild, slot, now)
        await self.write()

    def _count_members(self, guild: Any, slot: int) -> None:
        bots = sum(1 for member in guild.members if member.bot)
        members = guild.member_count or len(guild.members)
        self.recorder.member_count(guild.id, slot, members, max(members - bots, 0), bots)

    def _read_statuses(self, guild: Any, slot: int) -> None:
        counts = {"online": [0, 0], "idle": [0, 0], "dnd": [0, 0]}
        for member in guild.members:
            found = counts.get(str(member.status))
            if found is not None:
                found[1 if member.bot else 0] += 1
        self.recorder.statuses(guild.id, slot, counts["online"][0], counts["idle"][0], counts["dnd"][0], counts["online"][1], counts["idle"][1], counts["dnd"][1])

    async def _read_present(self, guild_id: int, slot: int) -> None:
        try:
            guild = await self.bot.fetch_guild(guild_id, with_counts=True)
        except discord.HTTPException as error:
            log.info("Could not read how many members are present in %s: %s", guild_id, error)
            return
        if guild.approximate_presence_count is not None:
            self.recorder.present(guild_id, slot, guild.approximate_presence_count)

    def _read_activities(self, guild: Any, slot: int, now: float) -> None:
        last = self._activities_read.get(guild.id)
        self._activities_read[guild.id] = now
        if last is None or now - last > ACTIVITY_GAP_SECONDS:
            return  # the first reading starts the clock
        seconds = int(now) - int(last)
        verify = self.cfg(guild.id)["verify_activities"]
        for member in guild.members:
            if not member.activities or str(member.status) == "offline":
                continue
            seen: set[int] = set()
            for activity in member.activities:
                identity = activity_identity(activity)
                if identity is None or (verify and not identity[3]):
                    continue
                kind, application_id, name, verified = identity
                number = store.activity_id(kind, application_id, name)
                if number in seen:
                    continue
                seen.add(number)
                if (guild.id, number) not in self._named:
                    self._named.add((guild.id, number))
                    self.recorder.activity_name(guild.id, number, kind, application_id, name, verified, now)
                self.recorder.activity(guild.id, slot, self.recorded_as(member.id), number, store.BOT if member.bot else 0, seconds)

    # ---- getting in step with Discord ----

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._loop is None or self._loop.done():
            self._loop = asyncio.ensure_future(self._run())
        self._spawn(self._after_ready())

    async def _after_ready(self) -> None:
        await self.bot.members_settled.wait()
        now = time.time()
        for guild in list(self.bot.guilds):
            if not guild.unavailable and self.tracking(guild.id):
                self._on.add(guild.id)
                await self.catch_up(guild, now)

    async def catch_up(self, guild: Any, now: float) -> None:
        """Bring a server's sessions, channels, emoji and invites in step after a connection or after switching on. Someone whose voice
        state changed while the bot was away has their time recorded up to the last checkpoint and starts again from now."""
        current = {}
        for channel in [*getattr(guild, "voice_channels", []), *getattr(guild, "stage_channels", [])]:
            current.update(getattr(channel, "voice_states", {}))
        for user_id, session in self.sessions.in_guild(guild.id).items():
            member = guild.get_member(user_id)
            state = current.get(user_id)
            place = self._place(guild, state.channel, "voice") if state is not None and state.channel is not None else None
            if member is None or place is None or place[0] != session.channel_id or voice_state_code(state, _afk_id(guild)) != session.state:
                self.sessions.forget(guild.id, user_id)
        for user_id, state in current.items():
            member = guild.get_member(user_id)
            if member is not None and state.channel is not None:
                self._voice_now(member, state, now)
        self._note_channels(guild, now)
        self._note_emoji(guild)
        self._spawn(self._read_invites_into_cache(guild))

    @commands.Cog.listener()
    async def on_guild_join(self, guild: Any) -> None:
        if self.tracking(guild.id):
            self._on.add(guild.id)
            await self.catch_up(guild, time.time())

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: Any) -> None:
        self._on.discard(guild.id)
        self.sessions.drop_guild(guild.id, time.time())
        self._invites.pop(guild.id, None)

    async def _settings_saved(self, guild_id: int, settings: dict) -> None:
        now = time.time()
        if not settings.get("enabled"):
            if guild_id in self._on:
                self._on.discard(guild_id)
                self.sessions.drop_guild(guild_id, now)
                self._invites.pop(guild_id, None)
            return
        self._note_aggregates(guild_id, settings, now)
        if guild_id in self._on:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is not None and not guild.unavailable and self.bot.is_ready():
            self._on.add(guild_id)
            await self.catch_up(guild, now)

    def _note_aggregates(self, guild_id: int, settings: dict, now: float) -> None:
        for row in settings.get("aggregates") or []:
            self.recorder.channel(guild_id, row["id"], row["name"], 999, row.get("category_id"), None, None)

    # ---- where something counts ----

    def _place(self, guild: Any, channel: Any, kind: str) -> tuple[int, int] | None:
        """(the channel a message or voice time counts under, flags), or None in a channel the server keeps out of its statistics."""
        if channel is None:
            return None
        flags = 0
        own_id = channel.id
        if getattr(channel, "type", None) in THREAD_TYPES:
            flags |= store.THREAD
            channel_id = channel.parent_id
            category_id = getattr(getattr(channel, "parent", None), "category_id", None)
        else:
            channel_id = channel.id
            category_id = getattr(channel, "category_id", None)
            if kind == "messages" and getattr(channel, "type", None) in VOICE_TYPES:
                flags |= store.VOICE_CHAT
        cfg = self.cfg(guild.id)
        untracked = cfg["untracked_channels"]
        if untracked and (channel_id in untracked or own_id in untracked or (category_id is not None and category_id in untracked)):
            return None
        if category_id is not None:
            for row in cfg["aggregates"]:
                if category_id in row["auto_categories"] and row["kinds"] in (kind, "both"):
                    return row["id"], flags
        return channel_id, flags

    # ---- counting ----

    @commands.Cog.listener()
    async def on_message(self, message: Any) -> None:
        guild = message.guild
        if guild is None or not self.tracking(guild.id) or not counted(message):
            return
        place = self._place(guild, message.channel, "messages")
        if place is None:
            return
        channel_id, flags = place
        author = message.author
        if author.bot:
            flags |= store.BOT
        slot = slot_of(message.created_at.timestamp())
        user_id = self.recorded_as(author.id)
        self.recorder.message(guild.id, slot, channel_id, user_id, flags)
        if author.bot:
            return
        if user_id and getattr(author, "guild", None) is not None:
            self._note_person(author, time.time())
        self._message_emoji(guild, message, slot, channel_id, user_id)

    def _message_emoji(self, guild: Any, message: Any, slot: int, channel_id: int, user_id: int) -> None:
        """Each of this server's emoji and each standard emoji in a message counts once, however often it repeats, and so does each of
        this server's stickers."""
        content = message.content or ""
        found: set[str] = set()
        if "<" in content:
            own = {item.id for item in guild.emojis}
            found.update(store.custom_key(int(number)) for number in CUSTOM_EMOJI.findall(content) if int(number) in own)
        found.update(store.unicode_key(item) for item in emoji_text.find(content))
        for key in found:
            self.recorder.emoji(guild.id, slot, channel_id, user_id, 0, key, store.IN_TEXT)
        if message.stickers:
            own_stickers = {item.id for item in guild.stickers}
            for sticker in {item.id for item in message.stickers if item.id in own_stickers}:
                self.recorder.emoji(guild.id, slot, channel_id, user_id, 0, store.sticker_key(sticker), store.STICKER)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: Any) -> None:
        if not self.tracking(payload.guild_id):
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = payload.member
        if guild is None or member is None or member.bot:
            return
        if payload.emoji.id is not None:
            if guild.get_emoji(payload.emoji.id) is None:
                return  # another server's emoji
            key = store.custom_key(payload.emoji.id)
        elif payload.emoji.name:
            key = store.unicode_key(emoji_text.bare(payload.emoji.name))
        else:
            return
        place = self._place(guild, guild.get_channel_or_thread(payload.channel_id), "messages")
        if place is None:
            return
        author = payload.message_author_id or 0
        use = store.SELF_REACTION if author == member.id else store.REACTION
        flags = store.SUPER if getattr(payload, "burst", False) else 0
        self.recorder.emoji(guild.id, slot_of(time.time()), place[0], self.recorded_as(member.id), self.recorded_as(author) if author else 0, key, use, flags)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        if self.tracking(member.guild.id):
            self._voice_now(member, after, time.time())

    def _voice_now(self, member: Any, state: Any, now: float) -> None:
        guild = member.guild
        place = self._place(guild, state.channel, "voice") if state is not None and state.channel is not None else None
        if place is None:
            self.sessions.update(guild.id, member.id, None, 0, 0, now)
            return
        flags = (store.BOT if member.bot else 0) | (store.GUEST if getattr(getattr(member, "flags", None), "guest", False) else 0)
        self.sessions.update(guild.id, member.id, place[0], voice_state_code(state, _afk_id(guild)), flags, now)
        if not member.bot and member.id not in self.private:
            self._note_person(member, now)

    @commands.Cog.listener()
    async def on_member_join(self, member: Any) -> None:
        guild = member.guild
        if not self.tracking(guild.id):
            return
        now = time.time()
        self.recorder.join(guild.id, slot_of(now))
        if member.id not in self.private:
            self._note_person(member, now)
        self._spawn(self._attribute_join(member, now))

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: Any) -> None:
        if not self.tracking(payload.guild_id):
            return
        now = time.time()
        user = payload.user
        self.recorder.leave(payload.guild_id, slot_of(now))
        self.sessions.update(payload.guild_id, user.id, None, 0, 0, now)
        recorded = self.recorded_as(user.id)
        if recorded:
            self.recorder.departed(payload.guild_id, recorded, now)
            self.recorder.person(payload.guild_id, user.id, user.name, getattr(user, "global_name", None), getattr(user, "nick", None),
                                 _avatar(user), user.bot, user.created_at.timestamp(), _joined(user), now, now)

    @commands.Cog.listener()
    async def on_member_update(self, before: Any, after: Any) -> None:
        guild = after.guild
        if not self.tracking(guild.id):
            return
        now = time.time()
        cfg = self.cfg(guild.id)
        required = set(cfg["invite_required_roles"])
        if required and before.roles != after.roles:
            match = cfg["invite_required_match"]
            if invite_rules.holds_required(after, required, match) and not invite_rules.holds_required(before, required, match):
                recorded = self.recorded_as(after.id)
                if recorded:
                    self.recorder.role_met(guild.id, recorded, now)
        if (before.nick, before.avatar, before.name, before.global_name) != (after.nick, after.avatar, after.name, after.global_name) and after.id not in self.private:
            self._note_person(after, now)

    def _note_person(self, member: Any, now: float) -> None:
        self.recorder.person(member.guild.id, member.id, member.name, getattr(member, "global_name", None), getattr(member, "nick", None),
                             _avatar(member), member.bot, member.created_at.timestamp(), _joined(member), None, now)

    # ---- channels and emoji ----

    def _note_channels(self, guild: Any, now: float) -> None:
        for channel in guild.channels:
            self._note_channel(guild.id, channel, None)
        self._note_aggregates(guild.id, self.cfg(guild.id), now)

    def _note_channel(self, guild_id: int, channel: Any, deleted_at: float | None) -> None:
        category = getattr(channel, "category_id", None)
        if category is not None and any(category in row["auto_categories"] and row["kinds"] == "both" for row in self.cfg(guild_id)["aggregates"]):
            return  # its messages and voice time both go to a merged channel, so it isn't listed on its own, as tickets come and go all day
        self.recorder.channel(guild_id, channel.id, channel.name, channel.type.value, getattr(channel, "category_id", None),
                              channel.created_at.timestamp(), deleted_at)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: Any) -> None:
        if self.tracking(channel.guild.id):
            self._note_channel(channel.guild.id, channel, None)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: Any, after: Any) -> None:
        if self.tracking(after.guild.id):
            self._note_channel(after.guild.id, after, None)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        if self.tracking(channel.guild.id):
            self._note_channel(channel.guild.id, channel, time.time())

    def _note_emoji(self, guild: Any, deleted: dict[str, Any] | None = None) -> None:
        for item in guild.emojis:
            self.recorder.emoji_detail(guild.id, store.custom_key(item.id), item.name, item.animated, item.managed, bool(item.roles), item.available,
                                       item.created_at.timestamp(), None)
        for item in guild.stickers:
            self.recorder.emoji_detail(guild.id, store.sticker_key(item.id), item.name, str(getattr(item, "format", "")).endswith(("apng", "gif")), False, False,
                                       getattr(item, "available", True), item.created_at.timestamp(), None)
        for key, item in (deleted or {}).items():
            self.recorder.emoji_detail(guild.id, key, item.name, getattr(item, "animated", False), getattr(item, "managed", False), bool(getattr(item, "roles", [])),
                                       False, item.created_at.timestamp(), time.time())

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: Any, before: Any, after: Any) -> None:
        if self.tracking(guild.id):
            kept = {item.id for item in after}
            self._note_emoji(guild, {store.custom_key(item.id): item for item in before if item.id not in kept})

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: Any, before: Any, after: Any) -> None:
        if self.tracking(guild.id):
            kept = {item.id for item in after}
            self._note_emoji(guild, {store.sticker_key(item.id): item for item in before if item.id not in kept})

    # ---- invites ----

    def _invite_lock(self, guild_id: int) -> asyncio.Lock:
        return self._invite_locks.setdefault(guild_id, asyncio.Lock())

    async def _read_invites(self, guild: Any) -> dict[str, invite_rules.InviteState] | None:
        """Every invite's use count, or None when the bot can't read them (it needs Manage Server)."""
        if not guild.me.guild_permissions.manage_guild:
            return None
        try:
            found = await guild.invites()
            vanity = await guild.vanity_invite() if "VANITY_URL" in guild.features else None
        except discord.HTTPException as error:
            log.info("Could not read the invites of %s: %s", guild.id, error)
            return None
        states = {}
        now = time.time()
        for item in found:
            inviter = item.inviter.id if item.inviter else None
            states[item.code] = invite_rules.InviteState(item.uses or 0, item.max_uses or None, inviter, bool(item.temporary))
            self.recorder.invite(guild.id, item.code, inviter, item.channel.id if item.channel else None, item.uses or 0, item.max_uses or None,
                                 item.max_age or None, bool(item.temporary), False, item.created_at.timestamp() if item.created_at else None, None)
        if vanity is not None:
            code = invite_rules.VANITY_PREFIX + vanity.code
            states[code] = invite_rules.InviteState(vanity.uses or 0, None, None, False)
            self.recorder.invite(guild.id, code, None, None, vanity.uses or 0, None, None, False, True, None, None)
        for code in set(self._invites.get(guild.id) or {}) - set(states):
            self.recorder.invite(guild.id, code, None, None, 0, None, None, False, code.startswith(invite_rules.VANITY_PREFIX), None, now)
        return states

    async def _read_invites_into_cache(self, guild: Any) -> None:
        async with self._invite_lock(guild.id):
            states = await self._read_invites(guild)
            if states is not None:
                self._invites[guild.id] = states

    @commands.Cog.listener()
    async def on_invite_create(self, invite: Any) -> None:
        guild = invite.guild
        if guild is None or not self.tracking(guild.id):
            return
        inviter = invite.inviter.id if invite.inviter else None
        self._invites.setdefault(guild.id, {})[invite.code] = invite_rules.InviteState(invite.uses or 0, invite.max_uses or None, inviter, bool(invite.temporary))
        self.recorder.invite(guild.id, invite.code, inviter, invite.channel.id if invite.channel else None, invite.uses or 0, invite.max_uses or None,
                             invite.max_age or None, bool(invite.temporary), False, time.time(), None)

    async def _attribute_join(self, member: Any, now: float) -> None:
        guild = member.guild
        cfg = self.cfg(guild.id)
        async with self._invite_lock(guild.id):
            before = self._invites.get(guild.id)
            after = await self._read_invites(guild)
            if after is not None:
                self._invites[guild.id] = after
        code = invite_rules.attribute(before, after)
        state = (after or {}).get(code) or (before or {}).get(code) if code else None
        inviter = state.inviter_id if state is not None else None
        recorded = self.recorded_as(member.id)
        facts = invite_rules.join_facts(inviter_id=inviter, member_id=member.id, rejoined=bool(getattr(getattr(member, "flags", None), "did_rejoin", False)),
                                        has_avatar=member.avatar is not None, bot=member.bot, temporary=bool(state and state.temporary))
        required = set(cfg["invite_required_roles"])
        met = now if required and invite_rules.holds_required(member, required, cfg["invite_required_match"]) else None
        self.recorder.joined(guild.id, recorded, now, code, self.recorded_as(inviter) if inviter else inviter, member.created_at.timestamp(), facts, met)


def _afk_id(guild: Any) -> int | None:
    channel = getattr(guild, "afk_channel", None)
    return channel.id if channel is not None else None


def _avatar(user: Any) -> str | None:
    """The avatar's hash, from which its address can be built for someone no longer in the server."""
    return getattr(getattr(user, "avatar", None), "key", None)


def _joined(member: Any) -> float | None:
    joined = getattr(member, "joined_at", None)
    return joined.timestamp() if joined is not None else None
