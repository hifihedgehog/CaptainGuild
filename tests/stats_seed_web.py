"""Counts to read statistics pages against: a small server whose every number a test can work out by hand, and a server of 43,000
members for timing."""
from __future__ import annotations

import itertools
import random
import time
from dataclasses import dataclass, field

from captainguild.core.stats import store
from captainguild.core.stats.timeframe import slot_of

DAY_SLOTS = 96
LEAVER = 555000000000000555


@dataclass
class Small:
    now_slot: int
    general: int
    help: int
    lounge: int
    gone_channel: int
    leaver: int = LEAVER
    expected: dict = field(default_factory=dict)


async def seed_small(bot, guild, *, member, admin, mod, general, help_channel, lounge) -> Small:
    """A server with a few days of counts. Everything sits within the last two weeks except one old row a year back."""
    db = bot.message_db
    gid = guild.id
    now = time.time()
    now_slot = slot_of(now)
    gone = 777000000000000777
    found = Small(now_slot, general.id, help_channel.id, lounge.id, gone)
    messages = [
        (now_slot - 10, general.id, member.id, 0, 5), (now_slot - 100, help_channel.id, member.id, 0, 3), (now_slot - 200, general.id, admin.id, 0, 4),
        (now_slot - 210, general.id, mod.id, store.BOT, 9), (now_slot - 220, general.id, 0, 0, 2), (now_slot - 300, gone, LEAVER, 0, 6),
        (now_slot - 400 * DAY_SLOTS, general.id, member.id, 0, 7),
    ]
    await db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", [(gid, *row) for row in messages])
    voice = [(now_slot - 20, lounge.id, member.id, store.TALKING, 0, 600), (now_slot - 30, lounge.id, admin.id, store.AFK, 0, 300)]
    await db.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)", [(gid, *row) for row in voice])
    people = [(member.id, member.name, "Member Person", None, None, 0, now - 9e6, now - 5e6, None), (admin.id, admin.name, None, "Boss", None, 0, now - 9e6, now - 6e6, None),
              (mod.id, mod.name, None, None, None, 1, now - 9e6, now - 6e6, None), (LEAVER, "leaver", "Gone Person", None, None, 0, now - 9e6, now - 7e6, now - 86400)]
    await db.many("INSERT INTO stat_people (guild_id, user_id, name, display_name, nick, avatar, bot, created_at, joined_at, left_at, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [(gid, *row, now) for row in people])
    channels = [(general.id, "general", 0, None), (help_channel.id, "help", 0, None), (lounge.id, "Lounge", 2, None), (gone, "old-chat", 0, now - 3600)]
    await db.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                  [(gid, channel_id, name, kind, now - 9e6, deleted) for channel_id, name, kind, deleted in channels])
    members = [(now_slot - 3 * DAY_SLOTS, 100, 90, 10, 2, 0), (now_slot - 2 * DAY_SLOTS, 101, 91, 10, 1, 0), (now_slot - DAY_SLOTS, 100, 90, 10, 0, 1)]
    await db.many("INSERT INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (?, ?, ?, ?, ?, ?, ?)", [(gid, *row) for row in members])
    status = [(now_slot - 5, 2, 20, 6, 4, 2, 0, 0, 0, 0), (now_slot - 4, 1, 12, 2, 2, 1, 0, 0, 0, 0)]
    await db.many("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [(gid, *row) for row in status])
    game = store.activity_id(store.PLAYING, 356875570916753438, "Minecraft")
    await db.run("INSERT INTO stat_activity_names (guild_id, activity_id, kind, application_id, name, verified, first_seen) VALUES (?, ?, 0, 356875570916753438, 'Minecraft', 1, ?)",
                 (gid, game, now))
    await db.many("INSERT INTO stat_activities (guild_id, slot, user_id, activity_id, flags, seconds) VALUES (?, ?, ?, ?, 0, ?)",
                  [(gid, now_slot - 3, member.id, game, 1200), (gid, now_slot - 3, admin.id, game, 600)])
    emoji = [(now_slot - 6, general.id, member.id, admin.id, "u:\U0001F44D", store.REACTION, 0, 3), (now_slot - 6, general.id, admin.id, 0, "c:111111111111111111", store.IN_TEXT, 0, 2),
             (now_slot - 7, general.id, member.id, member.id, "u:\U0001F44D", store.SELF_REACTION, store.SUPER, 1)]
    await db.many("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [(gid, *row) for row in emoji])
    info = [("c:111111111111111111", "party", 0, 0, 0, 1, None), ("c:222222222222222222", "unused", 1, 0, 0, 1, None), ("c:333333333333333333", "removed", 0, 0, 0, 1, now - 60)]
    await db.many("INSERT INTO stat_emoji_info (guild_id, emoji, name, animated, managed, restricted, available, created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [(gid, key, name, animated, managed, restricted, available, now - 9e6, deleted) for key, name, animated, managed, restricted, available, deleted in info])
    await db.many("INSERT INTO stat_invites (guild_id, code, inviter_id, channel_id, uses, max_uses, max_age, temporary, vanity, created_at, deleted_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, 0, ?, NULL)",
                  [(gid, "abc", admin.id, general.id, 5, now - 9e6), (gid, "quiet", member.id, general.id, 0, now - 9e6)])
    joins = [(member.id, now - 3600, "abc", admin.id, now - 9e6, 0, None, None), (LEAVER, now - 7200, "abc", admin.id, now - 100, store.JOIN_NO_AVATAR, None, now - 1800),
             (mod.id, now - 7300, None, None, now - 9e6, store.JOIN_BOT, None, None)]
    await db.many("INSERT INTO stat_joins (guild_id, user_id, at, code, inviter_id, account_created, facts, role_met_at, left_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [(gid, *row) for row in joins])
    return found


async def seed_large(db, guild_id: int, *, members: int = 43_000, rows: int = 1_500_000, years: int = 4, seed: int = 7) -> None:
    """A large server: 1.5 million message rows over four years from 43,000 people in 120 channels, a tenth as many voice
    rows, 300,000 emoji rows, a member count every quarter hour and a join for each member. A few people write most of it, as in any
    server, so the rankings have a long tail."""
    rng = random.Random(seed)
    now_slot = slot_of(time.time())
    first = now_slot - years * 365 * DAY_SLOTS
    people = [10 ** 17 + index for index in range(members)]
    channels = [2 * 10 ** 17 + index for index in range(120)]
    # Cumulative weights, worked out once: handing choices() plain weights adds up all 43,000 on every call.
    people_weights = list(itertools.accumulate(1 / (index + 1) ** 1.1 for index in range(members)))
    channel_weights = list(itertools.accumulate(1 / (index + 1) ** 0.9 for index in range(len(channels))))
    slots = now_slot - first
    batch: list[tuple] = []

    async def flush(sql: str) -> None:
        nonlocal batch
        if batch:
            await db.many(sql, batch)
            batch = []

    message_sql = "INSERT OR IGNORE INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)"
    per_slot = rows / slots
    for slot in range(first, now_slot):
        count = int(per_slot) + (1 if rng.random() < per_slot - int(per_slot) else 0)
        for user_id, channel_id in zip(rng.choices(people, cum_weights=people_weights, k=count), rng.choices(channels, cum_weights=channel_weights, k=count)):
            batch.append((guild_id, slot, channel_id, user_id, 0, rng.randint(1, 12)))
        if len(batch) >= 100_000:
            await flush(message_sql)
    await flush(message_sql)
    voice_sql = "INSERT OR IGNORE INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)"
    for _ in range(rows // 10):
        batch.append((guild_id, rng.randrange(first, now_slot), rng.choice(channels[100:]), rng.choices(people, cum_weights=people_weights)[0], rng.randint(1, 6), 0, rng.randint(60, 900)))
        if len(batch) >= 100_000:
            await flush(voice_sql)
    await flush(voice_sql)
    emoji_sql = "INSERT OR IGNORE INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    faces = [f"c:{3 * 10 ** 17 + index}" for index in range(200)] + [f"u:{chr(0x1F600 + index)}" for index in range(60)]
    for _ in range(rows // 5):
        batch.append((guild_id, rng.randrange(first, now_slot), rng.choice(channels), rng.choices(people, cum_weights=people_weights)[0], 0, rng.choice(faces), rng.randint(1, 4), 0, 1))
        if len(batch) >= 100_000:
            await flush(emoji_sql)
    await flush(emoji_sql)
    count = members - 5000
    member_sql = "INSERT OR IGNORE INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (?, ?, ?, ?, ?, ?, ?)"
    for slot in range(first, now_slot):
        count += rng.choice((0, 0, 0, 1, 1, -1)) if rng.random() < 0.3 else 0
        batch.append((guild_id, slot, count, count - 40, 40, 1 if rng.random() < 0.05 else 0, 1 if rng.random() < 0.03 else 0))
        if len(batch) >= 100_000:
            await flush(member_sql)
    await flush(member_sql)
    status_sql = "INSERT OR IGNORE INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 3, ?)"
    for slot in range(first, now_slot):
        batch.append((guild_id, slot, 3 * rng.randint(2500, 4500)))
        if len(batch) >= 100_000:
            await flush(status_sql)
    await flush(status_sql)
    now = time.time()
    people_sql = ("INSERT OR IGNORE INTO stat_people (guild_id, user_id, name, display_name, nick, avatar, bot, created_at, joined_at, left_at, seen_at) "
                  "VALUES (?, ?, ?, ?, NULL, NULL, 0, ?, ?, ?, ?)")
    joins_sql = "INSERT OR IGNORE INTO stat_joins (guild_id, user_id, at, code, inviter_id, account_created, facts, role_met_at, left_at) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)"
    codes = [f"code{index}" for index in range(300)]
    for index, user_id in enumerate(people):
        joined = now - rng.uniform(0, years * 365 * 86400)
        left = joined + 86400 if index % 9 == 0 else None
        batch.append((guild_id, user_id, f"person{index}", f"Person {index}", joined - 9e6, joined, left, now))
    await flush(people_sql)
    for index, user_id in enumerate(people):
        joined = now - rng.uniform(0, years * 365 * 86400)
        batch.append((guild_id, user_id, joined, rng.choice(codes), rng.choice(people[:500]), joined - 9e6, joined + 86400 if index % 9 == 0 else None))
    await flush(joins_sql)
    channel_sql = "INSERT OR IGNORE INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, ?, ?, NULL, ?, NULL)"
    for index, channel_id in enumerate(channels):
        batch.append((guild_id, channel_id, f"channel-{index}", 2 if index >= 100 else 0, now - 9e7))
    await flush(channel_sql)
