"""Recounts: a period's message counts replaced with what Discord holds now.

A recount fills the time before statistics began, which no import covers, or takes a raid's spam out once its messages are deleted. It
reads each channel's history for the period, and each thread's when threads are included, and counts every message the way live tracking
does: the same message types, a thread under its parent channel with the thread flag, a voice channel's text chat with its own flag, bots
flagged, and someone who chose privacy under no name.

What it reads waits in staging tables until a person approves it, with each place's read position saved in the same transaction as its
counts, so a restart resumes where it was and counts nothing twice. Approving swaps one channel at a time, each in one short transaction:
that channel's recorded messages for the period go and the recount's go in.

Emoji are never taken away. Text emoji and stickers count per author, reactions as Discord's totals with no member, and only what the
recount finds above what's already recorded is added, so running it twice adds nothing the second time. The bot's own reactions are left
out. Reactions are added only for a period that ends before statistics began counting them live: a live reaction counts in the quarter
hour it was added, not in its message's, so nothing else can tell the two apart. Voice time, member counts and statuses can't be recovered
from Discord, so a recount leaves them alone.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ...db import Database
from . import emoji as emoji_text
from . import store
from .timeframe import SLOT_SECONDS, slot_of

CUSTOM_EMOJI = re.compile(r"<a?:\w{2,32}:(\d{15,21})>")
DISCORD_EPOCH_MS = 1420070400000
TEXT_USES = (store.IN_TEXT, store.STICKER)

MIGRATIONS = """
    CREATE TABLE stat_recounts (
        guild_id INTEGER PRIMARY KEY,
        state TEXT NOT NULL,
        first_slot INTEGER NOT NULL,
        end_slot INTEGER NOT NULL,
        threads INTEGER NOT NULL,
        everything INTEGER NOT NULL,
        channels TEXT NOT NULL DEFAULT '[]',
        found INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        started_by INTEGER,
        started_by_name TEXT,
        created_at REAL NOT NULL,
        read_at REAL,
        decided_by INTEGER,
        decided_by_name TEXT,
        finished_at REAL
    );
    CREATE TABLE stat_recount_places (
        guild_id INTEGER NOT NULL,
        place_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        flags INTEGER NOT NULL,
        owner_id INTEGER,
        created_at REAL,
        solved_at REAL,
        answered_at REAL,
        answered_by INTEGER,
        state TEXT NOT NULL,
        cursor INTEGER,
        found INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        PRIMARY KEY (guild_id, place_id)
    );
    CREATE INDEX stat_recount_places_channel ON stat_recount_places (guild_id, channel_id);
    CREATE TABLE stat_recount_messages (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        flags INTEGER NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id, slot, user_id, flags)
    ) WITHOUT ROWID;
    CREATE INDEX stat_recount_messages_user ON stat_recount_messages (guild_id, user_id);
    CREATE TABLE stat_recount_emoji (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        use INTEGER NOT NULL,
        flags INTEGER NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id, slot, user_id, emoji, use, flags)
    ) WITHOUT ROWID;
    CREATE INDEX stat_recount_emoji_user ON stat_recount_emoji (guild_id, user_id);
    CREATE TABLE stat_recount_people (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        display_name TEXT,
        avatar TEXT,
        created_at REAL,
        seen_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    );
"""

# Where a recount stands. Reading resumes after a restart, and so does applying. A person decides the two waits.
PLANNING, READING, REVIEW, APPROVAL, APPLYING, DONE, CANCELED = "planning", "reading", "review", "approval", "applying", "done", "canceled"
RUNNING = (PLANNING, READING, APPLYING)
OPEN = (PLANNING, READING, REVIEW, APPROVAL, APPLYING)
# Where a place stands.
WAITING, PLACE_DONE, ERROR, GONE, SKIPPED, APPLIED = "waiting", "done", "error", "gone", "skipped", "applied"


def snowflake_at(seconds: float) -> int:
    """The smallest Discord ID made at this moment, so `ID < snowflake_at(t)` means made before t."""
    return max(0, int(seconds * 1000) - DISCORD_EPOCH_MS) << 22


@dataclass(frozen=True)
class Place:
    """Where one channel's or thread's messages count: under `channel_id` with `flags`. A forum post also names its author, for the first answer."""
    channel_id: int
    flags: int
    thread_id: int | None = None
    owner_id: int | None = None


@dataclass
class Rules:
    """What counts, as live tracking sees it."""
    counted: Callable[[Any], bool]  # the message types and senders that count
    recorded_as: Callable[[int], int]  # 0 for someone who chose privacy
    own_emoji: frozenset[int]
    own_stickers: frozenset[int]
    reads_text: bool = True


@dataclass
class Tally:
    """What a run of messages adds up to."""
    messages: Counter = field(default_factory=Counter)  # (channel_id, slot, user_id, flags) -> count
    emoji: Counter = field(default_factory=Counter)  # (channel_id, slot, user_id, emoji, use, flags) -> count
    people: dict = field(default_factory=dict)  # user_id -> (name, display_name, avatar, created_at, seen_at)
    answers: dict = field(default_factory=dict)  # thread_id -> (when, who) of the first answer by someone other than the poster
    found: int = 0

    def __bool__(self) -> bool:
        return bool(self.messages or self.emoji or self.people or self.answers or self.found)


def emoji_key(reaction_emoji: Any, own: frozenset[int]) -> str | None:
    """A reaction's emoji as statistics key it, or None for another server's emoji."""
    if isinstance(reaction_emoji, str):
        return store.unicode_key(emoji_text.bare(reaction_emoji)) if reaction_emoji else None
    emoji_id = getattr(reaction_emoji, "id", None)
    if emoji_id is None:
        name = getattr(reaction_emoji, "name", None)
        return store.unicode_key(emoji_text.bare(name)) if name else None
    return store.custom_key(emoji_id) if emoji_id in own else None


def tally_message(tally: Tally, message: Any, place: Place, rules: Rules) -> bool:
    """Counts one message from Discord's history into the tally, as live tracking would have when it was sent. False when it doesn't count."""
    if not rules.counted(message):
        return False
    author = message.author
    at = message.created_at.timestamp()
    slot = slot_of(at)
    flags = place.flags | (store.BOT if author.bot else 0)
    user_id = rules.recorded_as(author.id)
    tally.messages[(place.channel_id, slot, user_id, flags)] += 1
    tally.found += 1
    if place.thread_id is not None and place.owner_id is not None and not author.bot and author.id != place.owner_id and message.id != place.thread_id:
        first = tally.answers.get(place.thread_id)
        if first is None or at < first[0]:
            tally.answers[place.thread_id] = (at, user_id)
    _reactions(tally, message, place, slot, rules)
    if author.bot:
        return True  # bots' own emoji and stickers don't count, the reactions people put on their messages do
    if user_id:
        avatar = getattr(getattr(author, "avatar", None), "key", None)
        created = getattr(author, "created_at", None)
        mine = tally.people.get(user_id)
        if mine is None or mine[4] < at:
            tally.people[user_id] = (author.name, getattr(author, "global_name", None), avatar, created.timestamp() if created else None, at)
    found: set[tuple[str, int]] = set()
    content = getattr(message, "content", "") or ""
    if rules.reads_text and content:
        if "<" in content:
            found.update((store.custom_key(int(number)), store.IN_TEXT) for number in CUSTOM_EMOJI.findall(content) if int(number) in rules.own_emoji)
        found.update((store.unicode_key(item), store.IN_TEXT) for item in emoji_text.find(content))
    for sticker in getattr(message, "stickers", None) or []:
        if sticker.id in rules.own_stickers:
            found.add((store.sticker_key(sticker.id), store.STICKER))
    for key, use in found:
        tally.emoji[(place.channel_id, slot, user_id, key, use, 0)] += 1
    return True


def _reactions(tally: Tally, message: Any, place: Place, slot: int, rules: Rules) -> None:
    """Discord's reaction totals, under no member. The bot's own reaction comes off, since live tracking never counts it."""
    for reaction in getattr(message, "reactions", None) or []:
        key = emoji_key(reaction.emoji, rules.own_emoji)
        if key is None:
            continue
        normal, burst = getattr(reaction, "normal_count", 0) or 0, getattr(reaction, "burst_count", 0) or 0
        if not normal and not burst:
            normal = reaction.count or 0
        normal -= 1 if getattr(reaction, "me", False) else 0
        burst -= 1 if getattr(reaction, "me_burst", False) else 0
        if normal > 0:
            tally.emoji[(place.channel_id, slot, 0, key, store.REACTION, 0)] += normal
        if burst > 0:
            tally.emoji[(place.channel_id, slot, 0, key, store.REACTION, store.SUPER)] += burst


async def stage(db: Database, guild_id: int, tally: Tally, place_id: int, cursor: int | None, *, finished: bool = False) -> None:
    """Saves what was read since the last save and where the place's reading got to, in one transaction."""
    async with db.transaction():
        if tally.messages:
            await db.many("INSERT INTO stat_recount_messages (guild_id, channel_id, slot, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, channel_id, slot, user_id, flags) DO UPDATE SET count = count + excluded.count",
                          [(guild_id, *key, count) for key, count in tally.messages.items()])
        if tally.emoji:
            await db.many("INSERT INTO stat_recount_emoji (guild_id, channel_id, slot, user_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, channel_id, slot, user_id, emoji, use, flags) DO UPDATE SET count = count + excluded.count",
                          [(guild_id, *key, count) for key, count in tally.emoji.items()])
        if tally.people:
            await db.many("INSERT INTO stat_recount_people (guild_id, user_id, name, display_name, avatar, created_at, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (guild_id, user_id) DO UPDATE SET name = excluded.name, display_name = excluded.display_name, avatar = excluded.avatar, "
                          "created_at = COALESCE(excluded.created_at, created_at), seen_at = excluded.seen_at WHERE excluded.seen_at >= seen_at",
                          [(guild_id, user_id, *row) for user_id, row in tally.people.items()])
        for thread_id, (at, who) in tally.answers.items():
            await db.run("UPDATE stat_recount_places SET answered_at = ?, answered_by = ? WHERE guild_id = ? AND place_id = ? AND (answered_at IS NULL OR answered_at > ?)",
                         (at, who, guild_id, thread_id, at))
        state = ", state = 'done'" if finished else ""
        await db.run(f"UPDATE stat_recount_places SET cursor = COALESCE(?, cursor), found = found + ?{state} WHERE guild_id = ? AND place_id = ?",
                     (cursor, tally.found, guild_id, place_id))
        if tally.found:
            await db.run("UPDATE stat_recounts SET found = found + ? WHERE guild_id = ?", (tally.found, guild_id))


async def recorded(db: Database, guild_id: int, channel_id: int, first_slot: int, end_slot: int, *, threads: bool) -> int:
    """The messages recorded now in a channel for the period, the part a recount of it replaces."""
    rule = "" if threads else " AND flags & 2 = 0"
    return int(await db.scalar(f"SELECT COALESCE(SUM(count), 0) FROM stat_messages WHERE guild_id = ? AND channel_id = ? AND slot >= ? AND slot < ?{rule}",
                               (guild_id, channel_id, first_slot, end_slot)))


async def staged(db: Database, guild_id: int) -> dict[int, int]:
    """What the recount found, by channel."""
    rows = await db.all("SELECT channel_id, SUM(count) AS total FROM stat_recount_messages WHERE guild_id = ? GROUP BY channel_id", (guild_id,))
    return {row["channel_id"]: int(row["total"]) for row in rows}


async def hide_private(db: Database, guild_id: int, private: Iterable[int]) -> None:
    """Folds anyone who chose privacy since their messages were read into no one, before anything is applied."""
    private = sorted(set(private))
    for start in range(0, len(private), 500):
        chunk = private[start:start + 500]
        marks = ", ".join("?" for _ in chunk)
        for table, columns, key in (
            ("stat_recount_messages", "guild_id, channel_id, slot, {who}, flags, count", "guild_id, channel_id, slot, user_id, flags"),
            ("stat_recount_emoji", "guild_id, channel_id, slot, {who}, emoji, use, flags, count", "guild_id, channel_id, slot, user_id, emoji, use, flags"),
        ):
            names = columns.replace("{who}", "user_id")
            await db.run(f"INSERT INTO {table} ({names}) SELECT {columns.replace('{who}', '0')} FROM {table} WHERE guild_id = ? AND user_id IN ({marks}) "
                         f"ON CONFLICT ({key}) DO UPDATE SET count = count + excluded.count", (guild_id, *chunk))
            await db.run(f"DELETE FROM {table} WHERE guild_id = ? AND user_id IN ({marks})", (guild_id, *chunk))
        await db.run(f"DELETE FROM stat_recount_people WHERE guild_id = ? AND user_id IN ({marks})", (guild_id, *chunk))
        await db.run(f"UPDATE stat_recount_places SET owner_id = 0 WHERE guild_id = ? AND owner_id IN ({marks})", (guild_id, *chunk))
        await db.run(f"UPDATE stat_recount_places SET answered_by = 0 WHERE guild_id = ? AND answered_by IN ({marks})", (guild_id, *chunk))


async def live_reactions_from(db: Database, guild_id: int) -> int | None:
    """The first quarter hour with a reaction counted as it happened, or None when none was. A recount's own reactions have no member and no
    author, so they don't count here."""
    return await db.scalar("SELECT MIN(slot) FROM stat_emoji WHERE guild_id = ? AND use IN (?, ?) AND NOT (user_id = 0 AND target_id = 0)",
                           (guild_id, store.REACTION, store.SELF_REACTION))


async def apply_channel(db: Database, guild_id: int, channel_id: int, first_slot: int, end_slot: int, *, threads: bool, reactions: bool = True) -> int:
    """Swaps one channel's recorded messages for the period with the recount's, adds the emoji found above what's recorded, and clears its
    staging rows, in one transaction. Reactions go in only when `reactions` is on. Returns the messages it put in."""
    rule = "" if threads else " AND flags & 2 = 0"
    async with db.transaction():
        await db.run(f"DELETE FROM stat_messages WHERE guild_id = ? AND channel_id = ? AND slot >= ? AND slot < ?{rule}", (guild_id, channel_id, first_slot, end_slot))
        await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) "
                     "SELECT guild_id, slot, channel_id, user_id, flags, count FROM stat_recount_messages WHERE guild_id = ? AND channel_id = ? "
                     "ON CONFLICT (guild_id, slot, channel_id, user_id, flags) DO UPDATE SET count = count + excluded.count", (guild_id, channel_id))
        # Text emoji and stickers by their author: only what's above the author's recorded count for that quarter hour.
        await db.run("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) "
                     "SELECT guild_id, slot, channel_id, user_id, 0, emoji, use, flags, extra FROM ("
                     "  SELECT r.guild_id, r.slot, r.channel_id, r.user_id, r.emoji, r.use, r.flags, r.count - COALESCE(("
                     "    SELECT SUM(e.count) FROM stat_emoji e WHERE e.guild_id = r.guild_id AND e.slot = r.slot AND e.channel_id = r.channel_id"
                     "    AND e.user_id = r.user_id AND e.emoji = r.emoji AND e.use = r.use AND e.flags = r.flags), 0) AS extra"
                     "  FROM stat_recount_emoji r WHERE r.guild_id = ? AND r.channel_id = ? AND r.use IN (?, ?)"
                     ") WHERE extra > 0 "
                     "ON CONFLICT (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags) DO UPDATE SET count = count + excluded.count",
                     (guild_id, channel_id, *TEXT_USES))
        # Reactions under no one: only what's above every reaction recorded for that emoji in that quarter hour.
        if reactions:
            await db.run("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) "
                         "SELECT guild_id, slot, channel_id, 0, 0, emoji, ?, flags, extra FROM ("
                         "  SELECT r.guild_id, r.slot, r.channel_id, r.emoji, r.flags, r.count - COALESCE(("
                         "    SELECT SUM(e.count) FROM stat_emoji e WHERE e.guild_id = r.guild_id AND e.slot = r.slot AND e.channel_id = r.channel_id"
                         "    AND e.emoji = r.emoji AND e.use IN (?, ?) AND e.flags = r.flags), 0) AS extra"
                         "  FROM stat_recount_emoji r WHERE r.guild_id = ? AND r.channel_id = ? AND r.use = ?"
                         ") WHERE extra > 0 "
                         "ON CONFLICT (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags) DO UPDATE SET count = count + excluded.count",
                         (store.REACTION, store.REACTION, store.SELF_REACTION, guild_id, channel_id, store.REACTION))
        put = int(await db.scalar("SELECT COALESCE(SUM(count), 0) FROM stat_recount_messages WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)))
        await db.run("DELETE FROM stat_recount_messages WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
        await db.run("DELETE FROM stat_recount_emoji WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
        await db.run("UPDATE stat_recount_places SET state = ? WHERE guild_id = ? AND channel_id = ? AND state = ?", (APPLIED, guild_id, channel_id, PLACE_DONE))
    return put


async def apply_posts(db: Database, guild_id: int, start: float, end: float) -> None:
    """What the recount learned about forum posts. A post made in the period is added with its first answer, and one already known takes an
    earlier first answer when the recount found one. A post made before the period isn't added, since its first answer may lie before it."""
    async with db.transaction():
        await db.run("INSERT INTO stat_forum_posts (guild_id, thread_id, forum_id, owner_id, created_at, replied_at, replier_id, solved_at) "
                     "SELECT guild_id, place_id, channel_id, owner_id, created_at, answered_at, answered_by, solved_at FROM stat_recount_places "
                     "WHERE guild_id = ? AND kind = 'post' AND state IN (?, ?) AND created_at >= ? AND created_at < ? "
                     "ON CONFLICT (guild_id, thread_id) DO UPDATE SET replied_at = excluded.replied_at, replier_id = excluded.replier_id "
                     "WHERE excluded.replied_at IS NOT NULL AND (replied_at IS NULL OR replied_at > excluded.replied_at)",
                     (guild_id, PLACE_DONE, APPLIED, start, end))
        for row in await db.all("SELECT p.place_id, p.answered_at, p.answered_by FROM stat_recount_places p JOIN stat_forum_posts f "
                                "ON f.guild_id = p.guild_id AND f.thread_id = p.place_id WHERE p.guild_id = ? AND p.kind = 'post' AND p.state IN (?, ?) "
                                "AND p.answered_at IS NOT NULL AND (f.replied_at IS NULL OR f.replied_at > p.answered_at)", (guild_id, PLACE_DONE, APPLIED)):
            await db.run("UPDATE stat_forum_posts SET replied_at = ?, replier_id = ? WHERE guild_id = ? AND thread_id = ?",
                         (row["answered_at"], row["answered_by"], guild_id, row["place_id"]))


async def apply_names(db: Database, guild_id: int) -> None:
    """Names for the people the recount found who aren't known yet. A name already kept is newer, so it stays."""
    async with db.transaction():
        await db.run("INSERT INTO stat_people (guild_id, user_id, name, display_name, nick, avatar, bot, created_at, joined_at, left_at, seen_at) "
                     "SELECT guild_id, user_id, name, display_name, NULL, avatar, 0, created_at, NULL, NULL, seen_at FROM stat_recount_people WHERE guild_id = ? "
                     "ON CONFLICT (guild_id, user_id) DO NOTHING", (guild_id,))
        await db.run("DELETE FROM stat_recount_people WHERE guild_id = ?", (guild_id,))


async def clear(db: Database, guild_id: int) -> None:
    """Everything a recount staged, once it's applied or canceled. The recount's own row stays as the record of the last one."""
    async with db.transaction():
        for table in ("stat_recount_messages", "stat_recount_emoji", "stat_recount_people", "stat_recount_places"):
            await db.run(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))


def period_slots(start: float, end: float) -> tuple[int, int]:
    """[first slot, end slot) covering whole quarter hours: a recount replaces whole slots, so it reads them whole."""
    first = int(start // SLOT_SECONDS)
    last = -(-int(end) // SLOT_SECONDS)
    return first, max(first, last)
