"""Fills a pair of databases with statistics the size of a large server: four years of counts behind about ten million messages, 43,600
members, joins with invites, a year of emoji and statuses. The benchmark in tests/statistics_bench.py reads it.

Everything comes from one seeded random generator, so two runs build the same rows.
"""
from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field

from captainguild.core.stats import store
from captainguild.core.stats.timeframe import SLOT_SECONDS

DAY_SLOTS = 86400 // SLOT_SECONDS
HOUR_WEIGHTS = [2, 1.5, 1, 0.8, 0.7, 0.8, 1.2, 2, 3, 4, 5, 5.5, 6, 6, 6, 6, 6.5, 7, 7.5, 7.5, 7, 6, 4.5, 3]  # a day's activity by UTC hour


@dataclass
class Seeded:
    guild_id: int
    now: float
    start: float
    text_channels: list[int] = field(default_factory=list)
    voice_channels: list[int] = field(default_factory=list)
    categories: list[int] = field(default_factory=list)
    members: list[int] = field(default_factory=list)  # still in the server
    posters: list[int] = field(default_factory=list)  # most active first
    message_rows: int = 0
    messages: int = 0


def _zipf_weights(count: int, exponent: float = 1.1) -> list[float]:
    return [1 / (rank ** exponent) for rank in range(1, count + 1)]


async def seed(db, message_db, *, guild_id: int = 1, now: float = 1_794_139_200.0, years: float = 4.0, daily_messages: int = 7_000,
               members: int = 43_600, departed: int = 20_000, rng_seed: int = 7) -> Seeded:
    """Writes the rows. `now` defaults to Nov 8, 2026, noon UTC."""
    rng = random.Random(rng_seed)
    days = int(years * 365.25)
    start = now - days * 86400
    info = Seeded(guild_id, now, start)
    next_id = iter(range(900_000_000_000_000_000, 2 ** 62))

    # channels: categories, text, forums and voice
    channel_rows = []
    for number in range(12):
        channel_id = next(next_id)
        info.categories.append(channel_id)
        channel_rows.append((guild_id, channel_id, f"category {number}", 4, None, start - 86400 * 30, None))
    for number in range(130):
        channel_id = next(next_id)
        info.text_channels.append(channel_id)
        kind = 15 if number % 13 == 5 else 0
        deleted = start + rng.random() * days * 86400 if number % 17 == 3 else None
        channel_rows.append((guild_id, channel_id, f"text-{number}", kind, rng.choice(info.categories), start + rng.random() * days * 43200, deleted))
    for number in range(20):
        channel_id = next(next_id)
        info.voice_channels.append(channel_id)
        channel_rows.append((guild_id, channel_id, f"Voice {number}", 2, rng.choice(info.categories), start + rng.random() * days * 43200, None))

    # people: members now, people who left, and the share of them who ever post
    people_rows = []
    everyone = []
    for number in range(members + departed):
        user_id = next(next_id)
        everyone.append(user_id)
        left = number >= members
        joined = start + rng.random() * days * 86400
        if not left:
            info.members.append(user_id)
        people_rows.append((guild_id, user_id, f"user{number}", f"User {number}", None, None, 0, joined - 86400 * rng.randint(1, 2000), joined,
                            joined + 86400 if left else None, now))
    posters = everyone[:]
    rng.shuffle(posters)
    info.posters = posters[:26_000]
    poster_weights = _zipf_weights(len(info.posters), 1.05)
    channel_weights = _zipf_weights(len(info.text_channels), 1.2)
    bots = [next(next_id) for _ in range(6)]

    await message_db.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?)", channel_rows)
    await message_db.many("INSERT INTO stat_people (guild_id, user_id, name, display_name, nick, avatar, bot, created_at, joined_at, left_at, seen_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", people_rows)

    # messages, a day at a time, growing over the years
    first_slot = int(start // SLOT_SECONDS)
    hour_total = sum(HOUR_WEIGHTS)
    hour_cumulative = []
    running = 0.0
    for weight in HOUR_WEIGHTS:
        running += weight / hour_total
        hour_cumulative.append(running)
    batch: dict = {}
    for day in range(days):
        growth = 0.55 + 0.9 * day / days
        target = int(daily_messages * growth * (0.8 + 0.4 * rng.random()))
        rows_today = max(1, int(target / 6.5))
        users = rng.choices(info.posters, weights=poster_weights, k=rows_today)
        channels = rng.choices(info.text_channels, weights=channel_weights, k=rows_today)
        for user_id, channel_id in zip(users, channels):
            hour = min(bisect.bisect_left(hour_cumulative, rng.random()), 23)
            slot = first_slot + day * DAY_SLOTS + hour * 4 + rng.randrange(4)
            roll = rng.random()
            flags = store.THREAD if roll < 0.06 else store.VOICE_CHAT if roll < 0.07 else 0
            count = 1 + int(-math.log(1 - rng.random()) * 5.5)
            batch[(guild_id, slot, channel_id, user_id, flags)] = batch.get((guild_id, slot, channel_id, user_id, flags), 0) + count
            info.messages += count
        for _ in range(rows_today // 40):  # bots post too
            slot = first_slot + day * DAY_SLOTS + rng.randrange(DAY_SLOTS)
            key = (guild_id, slot, rng.choice(info.text_channels[:20]), rng.choice(bots), store.BOT)
            batch[key] = batch.get(key, 0) + 1
        if len(batch) > 200_000 or day == days - 1:
            await message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)",
                                  [(*key, count) for key, count in batch.items()])
            info.message_rows += len(batch)
            batch = {}

    # voice: a few dozen stretches a day
    voice: dict = {}
    voice_users = info.posters[:3000]
    voice_weights = _zipf_weights(len(voice_users), 1.3)
    for day in range(days):
        for _ in range(rng.randint(20, 60)):
            user_id = rng.choices(voice_users, weights=voice_weights)[0]
            channel_id = rng.choice(info.voice_channels)
            slot = first_slot + day * DAY_SLOTS + rng.randrange(DAY_SLOTS - 8)
            state = rng.choice((store.TALKING, store.TALKING, store.TALKING, store.SELF_MUTED, store.SELF_DEAFENED, store.AFK))
            for step in range(rng.randint(1, 8)):
                key = (guild_id, slot + step, channel_id, user_id, state, 0)
                voice[key] = voice.get(key, 0) + rng.randint(60, 900)
    await message_db.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          [(*key, seconds) for key, seconds in voice.items()])

    # member counts each quarter hour, with joins and leaves
    member_rows = []
    count = members - 30_000
    for slot in range(first_slot, int(now // SLOT_SECONDS) + 1, 1):
        joins = 1 if rng.random() < 0.35 else 0
        leaves = 1 if rng.random() < 0.15 else 0
        count += joins - leaves
        member_rows.append((guild_id, slot, max(count, 1), max(count - 40, 1), 40, joins, leaves))
    await message_db.many("INSERT INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (?, ?, ?, ?, ?, ?, ?)", member_rows)

    # a year of statuses
    status_rows = []
    year_slot = int((now - 365 * 86400) // SLOT_SECONDS)
    for slot in range(year_slot, int(now // SLOT_SECONDS) + 1):
        hour = (slot % DAY_SLOTS) // 4
        base = 4000 + 2500 * HOUR_WEIGHTS[hour] / 7.5
        status_rows.append((guild_id, slot, 15, int(base * 15), int(base * 0.9 * 15), int(base * 0.25 * 15), 60, 0, 0, 0, 0))
    await message_db.many("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", status_rows)

    # a year of emoji: reactions, emoji in text and stickers
    emoji_keys = [store.unicode_key(chr(code)) for code in range(0x1F600, 0x1F640)] + [store.custom_key(800_000_000_000_000_000 + number) for number in range(60)]
    emoji_info = [(guild_id, key, f"emoji_{index}", 0, 0, 0, 1, start, None) for index, key in enumerate(emoji_keys) if key.startswith("c:")]
    await message_db.many("INSERT INTO stat_emoji_info (guild_id, emoji, name, animated, managed, restricted, available, created_at, deleted_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", emoji_info)
    emoji_weights = _zipf_weights(len(emoji_keys), 1.0)
    emoji: dict = {}
    for day in range(365):
        slot0 = year_slot + day * DAY_SLOTS
        picks = rng.choices(emoji_keys, weights=emoji_weights, k=1200)
        users = rng.choices(info.posters, weights=poster_weights, k=1200)
        targets = rng.choices(info.posters, weights=poster_weights, k=1200)
        for key, user_id, target in zip(picks, users, targets):
            use = rng.choice((store.REACTION, store.REACTION, store.REACTION, store.IN_TEXT))
            row = (guild_id, slot0 + rng.randrange(DAY_SLOTS), rng.choice(info.text_channels[:40]), user_id, target if use == store.REACTION else 0, key, use, 0)
            emoji[row] = emoji.get(row, 0) + 1
    await message_db.many("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          [(*key, total) for key, total in emoji.items()])

    # invites and the joins they brought
    codes = [f"code{number:03d}" for number in range(300)]
    inviters = info.posters[:200]
    await message_db.many("INSERT INTO stat_invites (guild_id, code, inviter_id, channel_id, uses, max_uses, max_age, temporary, vanity, created_at, deleted_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          [(guild_id, code, rng.choice(inviters), info.text_channels[0], 0, None, None, 0, 0, start, None) for code in codes])
    code_weights = _zipf_weights(len(codes), 1.4)
    join_rows = []
    for user_id in everyone[: members + departed]:
        at = start + rng.random() * days * 86400
        code = rng.choices(codes, weights=code_weights)[0] if rng.random() < 0.8 else None
        facts = store.JOIN_NO_AVATAR if rng.random() < 0.2 else 0
        join_rows.append((guild_id, user_id, at, code, rng.choice(inviters) if code else None, at - 86400 * rng.randint(0, 2000), facts, None))
    await message_db.many("INSERT INTO stat_joins (guild_id, user_id, at, code, inviter_id, account_created, facts, role_met_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          join_rows)
    gone = everyone[members:]
    await message_db.many("UPDATE stat_joins SET left_at = at + 86400 WHERE guild_id = ? AND user_id = ?", [(guild_id, user_id) for user_id in gone])
    return info
