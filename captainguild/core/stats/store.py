"""The statistics tables, the counts held in memory between writes, and voice sessions. Nothing here touches Discord.

Counts land in memory as events arrive and go to the database together, once a minute or when the bot stops, in one transaction. A write
that fails puts everything back, so the next one carries it. A crash loses at most the minute since the last write.

Each count table is keyed by server, quarter-hour slot (see timeframe.py) and whatever the count is about, and a write adds to the row
already there. A person who chose privacy is recorded as user 0: their counts still add to the server's totals and to no one's name.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Iterator

from ...db import Database
from .timeframe import SLOT_SECONDS

log = logging.getLogger(__name__)

# Message flags
BOT, THREAD, VOICE_CHAT = 1, 2, 4
# Voice flags (BOT is shared)
GUEST = 2
# Voice states, one per moment, in the order that decides which one applies
TALKING, AFK, SELF_MUTED, SELF_DEAFENED, SERVER_MUTED, SERVER_DEAFENED = 1, 2, 3, 4, 5, 6
VOICE_STATES = {"talking": TALKING, "afk": AFK, "self_muted": SELF_MUTED, "self_deafened": SELF_DEAFENED, "server_muted": SERVER_MUTED, "server_deafened": SERVER_DEAFENED}
# How an emoji was used
REACTION, SELF_REACTION, IN_TEXT, STICKER = 1, 2, 3, 4
EMOJI_USES = {"reactions": REACTION, "self_reactions": SELF_REACTION, "messages": IN_TEXT, "stickers": STICKER}
SUPER = 1  # emoji flag: a super reaction
# Facts about a join, known when it happens
JOIN_SELF, JOIN_REJOIN, JOIN_NO_AVATAR, JOIN_BOT, JOIN_TEMPORARY = 1, 2, 4, 8, 16
# Activity kinds, as Discord numbers them
PLAYING, STREAMING, LISTENING, WATCHING, COMPETING = 0, 1, 2, 3, 5
ACTIVITY_KINDS = {"playing": PLAYING, "streaming": STREAMING, "listening": LISTENING, "watching": WATCHING, "competing": COMPETING}

MAX_PENDING_ROWS = 500_000  # what memory holds while the database keeps failing, before the oldest counts go

# The indexes by person, channel, activity and emoji cover every column their reads filter and sum on, so a ranking over years reads the
# index alone in the order it groups by, and never goes back to the table row by row.
MESSAGE_MIGRATIONS = [
    """
    CREATE TABLE stat_messages (
        guild_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        flags INTEGER NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (guild_id, slot, channel_id, user_id, flags)
    ) WITHOUT ROWID;
    CREATE INDEX stat_messages_user ON stat_messages (guild_id, user_id, slot, channel_id, flags, count);
    CREATE INDEX stat_messages_channel ON stat_messages (guild_id, channel_id, slot, user_id, flags, count);
    CREATE TABLE stat_voice (
        guild_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        state INTEGER NOT NULL,
        flags INTEGER NOT NULL,
        seconds INTEGER NOT NULL,
        PRIMARY KEY (guild_id, slot, channel_id, user_id, state, flags)
    ) WITHOUT ROWID;
    CREATE INDEX stat_voice_user ON stat_voice (guild_id, user_id, slot, channel_id, state, flags, seconds);
    CREATE INDEX stat_voice_channel ON stat_voice (guild_id, channel_id, slot, user_id, state, flags, seconds);
    CREATE TABLE stat_members (
        guild_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        members INTEGER,
        humans INTEGER,
        bots INTEGER,
        joins INTEGER NOT NULL DEFAULT 0,
        leaves INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, slot)
    ) WITHOUT ROWID;
    CREATE TABLE stat_status (
        guild_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        samples INTEGER NOT NULL DEFAULT 0,
        online INTEGER NOT NULL DEFAULT 0,
        idle INTEGER NOT NULL DEFAULT 0,
        dnd INTEGER NOT NULL DEFAULT 0,
        bot_online INTEGER NOT NULL DEFAULT 0,
        bot_idle INTEGER NOT NULL DEFAULT 0,
        bot_dnd INTEGER NOT NULL DEFAULT 0,
        present_samples INTEGER NOT NULL DEFAULT 0,
        present INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, slot)
    ) WITHOUT ROWID;
    CREATE TABLE stat_activities (
        guild_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        activity_id INTEGER NOT NULL,
        flags INTEGER NOT NULL,
        seconds INTEGER NOT NULL,
        PRIMARY KEY (guild_id, slot, user_id, activity_id, flags)
    ) WITHOUT ROWID;
    CREATE INDEX stat_activities_activity ON stat_activities (guild_id, activity_id, slot, user_id, flags, seconds);
    CREATE INDEX stat_activities_user ON stat_activities (guild_id, user_id, slot);
    CREATE TABLE stat_activity_names (
        guild_id INTEGER NOT NULL,
        activity_id INTEGER NOT NULL,
        kind INTEGER NOT NULL,
        application_id INTEGER,
        name TEXT NOT NULL,
        verified INTEGER NOT NULL,
        first_seen REAL NOT NULL,
        PRIMARY KEY (guild_id, activity_id)
    );
    CREATE TABLE stat_emoji (
        guild_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        use INTEGER NOT NULL,
        flags INTEGER NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags)
    ) WITHOUT ROWID;
    CREATE INDEX stat_emoji_emoji ON stat_emoji (guild_id, emoji, slot);
    CREATE INDEX stat_emoji_by_use ON stat_emoji (guild_id, emoji, flags, use, slot, count);
    CREATE INDEX stat_emoji_user ON stat_emoji (guild_id, user_id, slot);
    CREATE INDEX stat_emoji_target ON stat_emoji (guild_id, target_id, slot);
    CREATE TABLE stat_emoji_info (
        guild_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        name TEXT NOT NULL,
        animated INTEGER NOT NULL DEFAULT 0,
        managed INTEGER NOT NULL DEFAULT 0,
        restricted INTEGER NOT NULL DEFAULT 0,
        available INTEGER NOT NULL DEFAULT 1,
        created_at REAL,
        deleted_at REAL,
        PRIMARY KEY (guild_id, emoji)
    );
    CREATE TABLE stat_joins (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        at REAL NOT NULL,
        code TEXT,
        inviter_id INTEGER,
        account_created REAL,
        facts INTEGER NOT NULL DEFAULT 0,
        role_met_at REAL,
        left_at REAL,
        PRIMARY KEY (guild_id, user_id, at)
    );
    CREATE INDEX stat_joins_time ON stat_joins (guild_id, at);
    CREATE INDEX stat_joins_code ON stat_joins (guild_id, code, at);
    CREATE INDEX stat_joins_inviter ON stat_joins (guild_id, inviter_id, at);
    CREATE TABLE stat_invites (
        guild_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        inviter_id INTEGER,
        channel_id INTEGER,
        uses INTEGER NOT NULL DEFAULT 0,
        max_uses INTEGER,
        max_age INTEGER,
        temporary INTEGER NOT NULL DEFAULT 0,
        vanity INTEGER NOT NULL DEFAULT 0,
        created_at REAL,
        deleted_at REAL,
        PRIMARY KEY (guild_id, code)
    );
    CREATE TABLE stat_channels (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        type INTEGER NOT NULL,
        parent_id INTEGER,
        created_at REAL,
        deleted_at REAL,
        PRIMARY KEY (guild_id, channel_id)
    );
    CREATE TABLE stat_people (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        display_name TEXT,
        nick TEXT,
        avatar TEXT,
        bot INTEGER NOT NULL DEFAULT 0,
        created_at REAL,
        joined_at REAL,
        left_at REAL,
        seen_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    );
    """,
]

# Per person, across every server: whether their statistics carry their name, and whether replies to them are private.
MAIN_MIGRATIONS = [
    """
    CREATE TABLE stat_user_settings (
        user_id INTEGER PRIMARY KEY,
        private INTEGER NOT NULL DEFAULT 0,
        private_changed_at REAL,
        ephemeral INTEGER NOT NULL DEFAULT 0
    );
    """,
]


def spread(start: int, end: int) -> Iterator[tuple[int, int]]:
    """The whole seconds of [start, end) split at quarter-hour edges, as (slot, seconds)."""
    while start < end:
        slot = start // SLOT_SECONDS
        edge = min(end, (slot + 1) * SLOT_SECONDS)
        yield slot, edge - start
        start = edge


def activity_id(kind: int, application_id: int | None, name: str) -> int:
    """A stable number for an activity, the same in every database: from Discord's application ID when it has one, else from its name."""
    basis = f"{kind}:app:{application_id}" if application_id else f"{kind}:name:{name.strip().casefold()}"
    return int.from_bytes(hashlib.blake2b(basis.encode(), digest_size=8).digest(), "big") >> 1


def unicode_key(text: str) -> str:
    return "u:" + text.replace("\N{VARIATION SELECTOR-16}", "")


def custom_key(emoji_id: int) -> str:
    return f"c:{emoji_id}"


def sticker_key(sticker_id: int) -> str:
    return f"s:{sticker_id}"


@dataclass
class Pending:
    """Everything counted since the last write."""
    messages: Counter = field(default_factory=Counter)
    voice: Counter = field(default_factory=Counter)
    emoji: Counter = field(default_factory=Counter)
    activities: Counter = field(default_factory=Counter)
    joins: Counter = field(default_factory=Counter)
    leaves: Counter = field(default_factory=Counter)
    member_counts: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    people: dict = field(default_factory=dict)
    channels: dict = field(default_factory=dict)
    activity_names: dict = field(default_factory=dict)
    emoji_info: dict = field(default_factory=dict)
    invites: dict = field(default_factory=dict)
    join_rows: list = field(default_factory=list)
    departures: list = field(default_factory=list)
    roles_met: list = field(default_factory=list)

    def size(self) -> int:
        return (len(self.messages) + len(self.voice) + len(self.emoji) + len(self.activities) + len(self.joins) + len(self.leaves) + len(self.member_counts)
                + len(self.status) + len(self.people) + len(self.channels) + len(self.activity_names) + len(self.emoji_info) + len(self.invites)
                + len(self.join_rows) + len(self.departures) + len(self.roles_met))

    def absorb(self, newer: "Pending") -> None:
        """Put back counts a failed write took out, under what came in since. Readings and details that came in since are newer, so they win."""
        for name in ("messages", "voice", "emoji", "activities", "joins", "leaves"):
            getattr(self, name).update(getattr(newer, name))
        for key, row in newer.status.items():
            mine = self.status.get(key)
            self.status[key] = row if mine is None else [a + b for a, b in zip(mine, row)]
        for name in ("member_counts", "people", "channels", "activity_names", "emoji_info", "invites"):
            getattr(self, name).update(getattr(newer, name))
        self.join_rows += newer.join_rows
        self.departures += newer.departures
        self.roles_met += newer.roles_met

    def trim(self) -> None:
        """The database has failed for so long that memory is at risk. The oldest message and voice counts go first."""
        for name in ("messages", "voice", "emoji", "activities"):
            table = getattr(self, name)
            if len(table) > MAX_PENDING_ROWS // 4:
                setattr(self, name, Counter(dict(list(table.items())[-(MAX_PENDING_ROWS // 4):])))


class Recorder:
    """Takes counts as they happen and writes them in batches."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.pending = Pending()
        self.written_at = time.time()

    # ---- counting ----

    def message(self, guild_id: int, slot: int, channel_id: int, user_id: int, flags: int, count: int = 1) -> None:
        self.pending.messages[(guild_id, slot, channel_id, user_id, flags)] += count

    def voice(self, guild_id: int, slot: int, channel_id: int, user_id: int, state: int, flags: int, seconds: int) -> None:
        if seconds > 0:
            self.pending.voice[(guild_id, slot, channel_id, user_id, state, flags)] += seconds

    def emoji(self, guild_id: int, slot: int, channel_id: int, user_id: int, target_id: int, emoji: str, use: int, flags: int = 0, count: int = 1) -> None:
        self.pending.emoji[(guild_id, slot, channel_id, user_id, target_id, emoji, use, flags)] += count

    def activity(self, guild_id: int, slot: int, user_id: int, activity: int, flags: int, seconds: int) -> None:
        if seconds > 0:
            self.pending.activities[(guild_id, slot, user_id, activity, flags)] += seconds

    def join(self, guild_id: int, slot: int) -> None:
        self.pending.joins[(guild_id, slot)] += 1

    def leave(self, guild_id: int, slot: int) -> None:
        self.pending.leaves[(guild_id, slot)] += 1

    def member_count(self, guild_id: int, slot: int, members: int, humans: int | None, bots: int | None) -> None:
        self.pending.member_counts[(guild_id, slot)] = (members, humans, bots)

    def statuses(self, guild_id: int, slot: int, online: int, idle: int, dnd: int, bot_online: int, bot_idle: int, bot_dnd: int) -> None:
        self._status(guild_id, slot, [1, online, idle, dnd, bot_online, bot_idle, bot_dnd, 0, 0])

    def present(self, guild_id: int, slot: int, count: int) -> None:
        """Discord's count of members who aren't offline, for when statuses one by one aren't sent to the bot."""
        self._status(guild_id, slot, [0, 0, 0, 0, 0, 0, 0, 1, count])

    def _status(self, guild_id: int, slot: int, reading: list[int]) -> None:
        key = (guild_id, slot)
        mine = self.pending.status.get(key)
        self.pending.status[key] = reading if mine is None else [a + b for a, b in zip(mine, reading)]

    def person(self, guild_id: int, user_id: int, name: str, display_name: str | None, nick: str | None, avatar: str | None, bot: bool,
               created_at: float | None, joined_at: float | None, left_at: float | None, seen_at: float) -> None:
        self.pending.people[(guild_id, user_id)] = (name, display_name, nick, avatar, int(bot), created_at, joined_at, left_at, seen_at)

    def channel(self, guild_id: int, channel_id: int, name: str, kind: int, parent_id: int | None, created_at: float | None, deleted_at: float | None) -> None:
        self.pending.channels[(guild_id, channel_id)] = (name, kind, parent_id, created_at, deleted_at)

    def activity_name(self, guild_id: int, activity: int, kind: int, application_id: int | None, name: str, verified: bool, seen_at: float) -> None:
        self.pending.activity_names[(guild_id, activity)] = (kind, application_id, name, int(verified), seen_at)

    def emoji_detail(self, guild_id: int, emoji: str, name: str, animated: bool, managed: bool, restricted: bool, available: bool,
                     created_at: float | None, deleted_at: float | None) -> None:
        self.pending.emoji_info[(guild_id, emoji)] = (name, int(animated), int(managed), int(restricted), int(available), created_at, deleted_at)

    def invite(self, guild_id: int, code: str, inviter_id: int | None, channel_id: int | None, uses: int, max_uses: int | None, max_age: int | None,
               temporary: bool, vanity: bool, created_at: float | None, deleted_at: float | None) -> None:
        self.pending.invites[(guild_id, code)] = (inviter_id, channel_id, uses, max_uses, max_age, int(temporary), int(vanity), created_at, deleted_at)

    def joined(self, guild_id: int, user_id: int, at: float, code: str | None, inviter_id: int | None, account_created: float | None, facts: int,
               role_met_at: float | None) -> None:
        self.pending.join_rows.append((guild_id, user_id, at, code, inviter_id, account_created, facts, role_met_at))

    def departed(self, guild_id: int, user_id: int, at: float) -> None:
        self.pending.departures.append((at, guild_id, user_id))

    def role_met(self, guild_id: int, user_id: int, at: float) -> None:
        self.pending.roles_met.append((at, guild_id, user_id))

    # ---- writing ----

    async def flush(self) -> None:
        """Write everything waiting, in one transaction. If it fails, nothing is lost: it goes back for the next write."""
        batch, self.pending = self.pending, Pending()
        if not batch.size():
            return
        try:
            async with self.db.transaction():
                await self._write(batch)
        except BaseException:
            batch.absorb(self.pending)
            self.pending = batch
            if self.pending.size() > MAX_PENDING_ROWS:
                self.pending.trim()
            raise
        self.written_at = time.time()

    async def _write(self, b: Pending) -> None:
        db = self.db
        if b.messages:
            await db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, slot, channel_id, user_id, flags) DO UPDATE SET count = count + excluded.count",
                          [(*key, count) for key, count in b.messages.items()])
        if b.voice:
            await db.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, slot, channel_id, user_id, state, flags) DO UPDATE SET seconds = seconds + excluded.seconds",
                          [(*key, seconds) for key, seconds in b.voice.items()])
        if b.emoji:
            await db.many("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags) DO UPDATE SET count = count + excluded.count",
                          [(*key, count) for key, count in b.emoji.items()])
        if b.activities:
            await db.many("INSERT INTO stat_activities (guild_id, slot, user_id, activity_id, flags, seconds) VALUES (?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, slot, user_id, activity_id, flags) DO UPDATE SET seconds = seconds + excluded.seconds",
                          [(*key, seconds) for key, seconds in b.activities.items()])
        member_rows = {key: [None, None, None, 0, 0] for key in {*b.member_counts, *b.joins, *b.leaves}}
        for key, (members, humans, bots) in b.member_counts.items():
            member_rows[key][:3] = [members, humans, bots]
        for key, count in b.joins.items():
            member_rows[key][3] = count
        for key, count in b.leaves.items():
            member_rows[key][4] = count
        if member_rows:
            await db.many("INSERT INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (?, ?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, slot) DO UPDATE SET members = COALESCE(excluded.members, members), humans = COALESCE(excluded.humans, humans), "
                          "bots = COALESCE(excluded.bots, bots), joins = joins + excluded.joins, leaves = leaves + excluded.leaves",
                          [(*key, *row) for key, row in member_rows.items()])
        if b.status:
            await db.many("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, slot) DO UPDATE SET samples = samples + excluded.samples, "
                          "online = online + excluded.online, idle = idle + excluded.idle, dnd = dnd + excluded.dnd, bot_online = bot_online + excluded.bot_online, "
                          "bot_idle = bot_idle + excluded.bot_idle, bot_dnd = bot_dnd + excluded.bot_dnd, present_samples = present_samples + excluded.present_samples, "
                          "present = present + excluded.present",
                          [(*key, *row) for key, row in b.status.items()])
        if b.people:
            await db.many("INSERT INTO stat_people (guild_id, user_id, name, display_name, nick, avatar, bot, created_at, joined_at, left_at, seen_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, user_id) DO UPDATE SET name = excluded.name, "
                          "display_name = excluded.display_name, nick = excluded.nick, avatar = excluded.avatar, bot = excluded.bot, "
                          "created_at = COALESCE(excluded.created_at, created_at), joined_at = COALESCE(excluded.joined_at, joined_at), "
                          "left_at = excluded.left_at, seen_at = MAX(seen_at, excluded.seen_at)",
                          [(*key, *row) for key, row in b.people.items()])
        if b.channels:
            await db.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, channel_id) DO UPDATE SET name = excluded.name, type = excluded.type, parent_id = excluded.parent_id, "
                          "created_at = COALESCE(excluded.created_at, created_at), deleted_at = excluded.deleted_at",
                          [(*key, *row) for key, row in b.channels.items()])
        if b.activity_names:
            await db.many("INSERT INTO stat_activity_names (guild_id, activity_id, kind, application_id, name, verified, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, activity_id) DO UPDATE SET name = excluded.name, verified = MAX(verified, excluded.verified)",
                          [(*key, *row) for key, row in b.activity_names.items()])
        if b.emoji_info:
            await db.many("INSERT INTO stat_emoji_info (guild_id, emoji, name, animated, managed, restricted, available, created_at, deleted_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, emoji) DO UPDATE SET name = excluded.name, animated = excluded.animated, "
                          "managed = excluded.managed, restricted = excluded.restricted, available = excluded.available, "
                          "created_at = COALESCE(excluded.created_at, created_at), deleted_at = excluded.deleted_at",
                          [(*key, *row) for key, row in b.emoji_info.items()])
        if b.invites:
            await db.many("INSERT INTO stat_invites (guild_id, code, inviter_id, channel_id, uses, max_uses, max_age, temporary, vanity, created_at, deleted_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, code) DO UPDATE SET inviter_id = COALESCE(excluded.inviter_id, inviter_id), "
                          "channel_id = COALESCE(excluded.channel_id, channel_id), uses = MAX(uses, excluded.uses), max_uses = excluded.max_uses, "
                          "max_age = excluded.max_age, temporary = excluded.temporary, vanity = excluded.vanity, "
                          "created_at = COALESCE(created_at, excluded.created_at), deleted_at = excluded.deleted_at",
                          [(*key, *row) for key, row in b.invites.items()])
        if b.join_rows:
            await db.many("INSERT OR IGNORE INTO stat_joins (guild_id, user_id, at, code, inviter_id, account_created, facts, role_met_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          b.join_rows)
        if b.departures:
            await db.many("UPDATE stat_joins SET left_at = ? WHERE guild_id = ? AND user_id = ? AND left_at IS NULL", b.departures)
        if b.roles_met:
            await db.many("UPDATE stat_joins SET role_met_at = ? WHERE guild_id = ? AND user_id = ? AND left_at IS NULL AND role_met_at IS NULL", b.roles_met)


@dataclass
class Session:
    channel_id: int
    state: int
    flags: int
    since: int


class VoiceSessions:
    """Who is in voice right now, and since when. Time goes to the counts at every change and at every checkpoint, split at quarter-hour edges.

    `who` turns a member's ID into the ID their time is recorded under, which is 0 for someone who chose privacy. It's asked each time
    time is recorded, so a choice made mid-call applies from then on.
    """

    def __init__(self, recorder: Recorder, who: Callable[[int], int]) -> None:
        self.recorder = recorder
        self.who = who
        self.open: dict[tuple[int, int], Session] = {}

    def _record(self, guild_id: int, user_id: int, session: Session, until: int) -> None:
        recorded_as = self.who(user_id)
        for slot, seconds in spread(session.since, until):
            self.recorder.voice(guild_id, slot, session.channel_id, recorded_as, session.state, session.flags, seconds)

    def update(self, guild_id: int, user_id: int, channel_id: int | None, state: int, flags: int, now: float) -> None:
        """Where someone is now. None means they left voice. The same channel, state and flags as before changes nothing."""
        moment = int(now)
        key = (guild_id, user_id)
        session = self.open.get(key)
        if session is not None:
            if channel_id == session.channel_id and state == session.state and flags == session.flags:
                return
            self._record(guild_id, user_id, session, max(moment, session.since))
            del self.open[key]
        if channel_id is not None:
            self.open[key] = Session(channel_id, state, flags, moment)

    def checkpoint(self, now: float, guild_id: int | None = None) -> None:
        """Record the time so far for everyone in voice, or everyone in one server, and carry on from now."""
        moment = int(now)
        for (place, user_id), session in self.open.items():
            if guild_id is not None and place != guild_id:
                continue
            if moment > session.since:
                self._record(place, user_id, session, moment)
                session.since = moment

    def drop_guild(self, guild_id: int, now: float) -> None:
        """A server switched statistics off or the bot left it. Time so far is kept."""
        self.checkpoint(now, guild_id)
        for key in [key for key in self.open if key[0] == guild_id]:
            del self.open[key]

    def in_guild(self, guild_id: int) -> dict[int, Session]:
        return {user_id: session for (place, user_id), session in self.open.items() if place == guild_id}

    def forget(self, guild_id: int, user_id: int) -> None:
        """Ends a session without recording the time since its last checkpoint, for someone found gone after the bot was away."""
        self.open.pop((guild_id, user_id), None)


# Steps other parts of Statistics add for tables of their own that name a person. anonymize() and anonymize_restored() await each as
# step(db, user_ids, guild_id) inside their transaction, guild_id None meaning every server, so a name comes off every table the same way
# whether the command, the My data page, a restart or a restored backup asked.
ANONYMIZE_STEPS: list[Callable[[Database, list[int], int | None], Awaitable[None]]] = []
STEP_ROWS = 2000  # one person's rows a step of the privacy move takes from one table: a few hundredths of a second
STEP_SLOTS = 30 * 96  # the quarter hours a step takes when a restored backup's rows lose the names of everyone who chose privacy
_MOVES = (
    ("stat_messages", "guild_id, slot, channel_id, {who}, flags, count", "guild_id, slot, channel_id, user_id, flags", "count"),
    ("stat_voice", "guild_id, slot, channel_id, {who}, state, flags, seconds", "guild_id, slot, channel_id, user_id, state, flags", "seconds"),
    ("stat_activities", "guild_id, slot, {who}, activity_id, flags, seconds", "guild_id, slot, user_id, activity_id, flags", "seconds"),
)


async def servers_in(db: Database, table: str) -> list[int]:
    """Every server with rows in a table whose key or an index leads with guild_id, one index seek a server, never a read of the table."""
    rows = await db.all(f"WITH RECURSIVE server(id) AS (SELECT MIN(guild_id) FROM {table} UNION ALL "
                        f"SELECT (SELECT MIN(guild_id) FROM {table} WHERE guild_id > server.id) FROM server WHERE server.id IS NOT NULL) "
                        "SELECT id FROM server WHERE id IS NOT NULL")
    return [row["id"] for row in rows]


async def anonymize(db: Database, user_id: int) -> None:
    """Moves everything recorded under someone's ID, in every server, to user 0, where it still counts toward totals. Their name rows go.
    It can't be undone: once the rows are merged, nothing says which part was theirs.

    The counts move one server at a time through the per-person indexes, which lead with the server, STEP_ROWS of a table's rows at a time.
    Each step is its own short transaction, and the bot's other reads and writes run between steps, so none of them waits long behind a busy
    poster's move. A step that fails leaves every row after it under the ID, and calling this again moves what's left: a row already moved
    isn't under the ID anymore, so nothing moves twice. Whoever calls it marks the move done only once it returns."""
    if not user_id:
        return
    servers: set[int] = set()
    for table in ("stat_messages", "stat_voice", "stat_activities", "stat_emoji"):
        servers.update(await servers_in(db, table))
    for guild_id in sorted(servers):
        for table, column, move in _moves(guild_id, [user_id], missing_only=False):
            lo = await db.scalar(f"SELECT MIN(slot) FROM {table} WHERE guild_id = ? AND {column} = ?", (guild_id, user_id))
            while lo is not None:
                hi = await db.scalar(f"SELECT slot FROM {table} WHERE guild_id = ? AND {column} = ? AND slot >= ? ORDER BY slot LIMIT 1 OFFSET ?",
                                     (guild_id, user_id, lo, STEP_ROWS))
                if hi == lo:
                    hi = lo + 1  # more rows in one quarter hour than a step takes: that quarter hour goes whole
                async with db.transaction():
                    await move(db, lo, hi)
                await asyncio.sleep(0)
                lo = hi
    async with db.transaction():
        await db.run("UPDATE OR IGNORE stat_joins SET user_id = 0 WHERE user_id = ?", (user_id,))
        await db.run("DELETE FROM stat_joins WHERE user_id = ?", (user_id,))
        await db.run("UPDATE stat_joins SET inviter_id = 0 WHERE inviter_id = ?", (user_id,))
        await db.run("UPDATE stat_invites SET inviter_id = 0 WHERE inviter_id = ?", (user_id,))
        await db.run("DELETE FROM stat_people WHERE user_id = ?", (user_id,))
        for step in ANONYMIZE_STEPS:
            await step(db, [user_id], None)


async def anonymize_restored(db: Database, guild_id: int, user_ids: Iterable[int]) -> None:
    """Takes these IDs off the rows a restored backup brought back in one server. Such a row was merged into user 0 when its person chose
    privacy, so it only goes back in, under user 0, where user 0 holds nothing for that quarter hour and channel. That's the restore's own
    rule, which adds only what's missing. It goes STEP_SLOTS quarter hours at a time, each step its own transaction, as anonymize() does."""
    people = sorted({user_id for user_id in user_ids if user_id})
    for start in range(0, len(people), 500):
        chunk = people[start:start + 500]
        marks = ", ".join("?" for _ in chunk)
        for table, column, move in _moves(guild_id, chunk, missing_only=True):
            lo = await db.scalar(f"SELECT MIN(slot) FROM {table} WHERE guild_id = ? AND {column} IN ({marks})", (guild_id, *chunk))
            while lo is not None:
                hi = lo + STEP_SLOTS
                async with db.transaction():
                    await move(db, lo, hi)
                await asyncio.sleep(0)
                lo = await db.scalar(f"SELECT MIN(slot) FROM {table} WHERE guild_id = ? AND {column} IN ({marks}) AND slot >= ?", (guild_id, *chunk, hi))
        async with db.transaction():
            await db.run(f"UPDATE OR IGNORE stat_joins SET user_id = 0 WHERE guild_id = ? AND user_id IN ({marks})", (guild_id, *chunk))
            await db.run(f"DELETE FROM stat_joins WHERE guild_id = ? AND user_id IN ({marks})", (guild_id, *chunk))
            await db.run(f"UPDATE stat_joins SET inviter_id = 0 WHERE guild_id = ? AND inviter_id IN ({marks})", (guild_id, *chunk))
            await db.run(f"UPDATE stat_invites SET inviter_id = 0 WHERE guild_id = ? AND inviter_id IN ({marks})", (guild_id, *chunk))
            await db.run(f"DELETE FROM stat_people WHERE guild_id = ? AND user_id IN ({marks})", (guild_id, *chunk))
            for step in ANONYMIZE_STEPS:
                await step(db, chunk, guild_id)


def _moves(guild_id: int, user_ids: list[int], *, missing_only: bool) -> list[tuple[str, str, Callable[[Database, int, int | None], Awaitable[None]]]]:
    """(table, the column holding the person, a move of one slot range [lo, hi), hi None meaning to the end) for every count table. The move
    merges the rows under these IDs, at most 500 of them, into user 0. With `missing_only`, they're summed and go in only where user 0 holds
    no row for the same key, and the rest are dropped."""
    marks = ", ".join("?" for _ in user_ids)
    found: list[tuple[str, str, Callable[[Database, int, int | None], Awaitable[None]]]] = []

    def span(lo: int, hi: int | None) -> tuple[str, tuple]:
        return ("slot >= ? AND slot < ?", (lo, hi)) if hi is not None else ("slot >= ?", (lo,))

    for table, columns, key, value in _MOVES:
        def move(db: Database, lo: int, hi: int | None, table: str = table, columns: str = columns, key: str = key, value: str = value) -> Awaitable[None]:
            async def run() -> None:
                names = columns.replace("{who}", "user_id")
                inside, bounds = span(lo, hi)
                where = f"guild_id = ? AND user_id IN ({marks}) AND {inside}"
                if missing_only:
                    others = ", ".join(part for part in key.split(", ") if part != "user_id")
                    picked = columns.replace("{who}", "0").replace(value, f"SUM({value})")
                    await db.run(f"INSERT INTO {table} ({names}) SELECT {picked} FROM {table} WHERE {where} GROUP BY {others} ON CONFLICT DO NOTHING",
                                 (guild_id, *user_ids, *bounds))
                else:
                    await db.run(f"INSERT INTO {table} ({names}) SELECT {columns.replace('{who}', '0')} FROM {table} WHERE {where} "
                                 f"ON CONFLICT ({key}) DO UPDATE SET {value} = {value} + excluded.{value}", (guild_id, *user_ids, *bounds))
                await db.run(f"DELETE FROM {table} WHERE {where}", (guild_id, *user_ids, *bounds))
            return run()
        found.append((table, "user_id", move))
    # Who used an emoji first, then whose message got it. A reaction to their own message moves twice and ends with neither.
    for column, picked, kept in (("user_id", "0, target_id", "target_id"), ("target_id", "user_id, 0", "user_id")):
        def move(db: Database, lo: int, hi: int | None, column: str = column, picked: str = picked, kept: str = kept) -> Awaitable[None]:
            async def run() -> None:
                inside, bounds = span(lo, hi)
                where = f"guild_id = ? AND {column} IN ({marks}) AND {inside}"
                if missing_only:
                    await db.run(f"INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) "
                                 f"SELECT guild_id, slot, channel_id, {picked}, emoji, use, flags, SUM(count) FROM stat_emoji WHERE {where} "
                                 f"GROUP BY guild_id, slot, channel_id, {kept}, emoji, use, flags ON CONFLICT DO NOTHING", (guild_id, *user_ids, *bounds))
                else:
                    await db.run(f"INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) "
                                 f"SELECT guild_id, slot, channel_id, {picked}, emoji, use, flags, count FROM stat_emoji WHERE {where} "
                                 f"ON CONFLICT (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags) DO UPDATE SET count = count + excluded.count",
                                 (guild_id, *user_ids, *bounds))
                await db.run(f"DELETE FROM stat_emoji WHERE {where}", (guild_id, *user_ids, *bounds))
            return run()
        found.append(("stat_emoji", column, move))
    return found


def describe_pending(pending: Pending) -> dict[str, Any]:
    """Row counts by kind, for logs."""
    return {name: len(getattr(pending, name)) for name in ("messages", "voice", "emoji", "activities", "people", "status")}
