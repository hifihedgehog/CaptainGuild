"""How long stat roles and stat counters take on the largest server the bot serves: 43,600 members, about 1.4 million message rows and
420,000 voice rows over four years, in the encrypted message database. Not part of the suite, since seeding takes minutes.

    python -m tests.statistics_tools_bench                 seeds a temporary database, then times each rule and counter
    python -m tests.statistics_tools_bench --keep FILE     keeps the seeded database in FILE and reuses it on the next run

It fails when any rule or counter takes over three seconds.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import random
import sys
import tempfile
import time
from pathlib import Path

from captainguild.core.stats import counters, statroles
from captainguild.core.stats import timeframe as tf
from captainguild.db import Database
from captainguild.modules.statistics import Statistics

GUILD = 970000000000000001
MEMBERS = 43_600
YEARS = 4
KEY = "5" * 64
LIMIT = 3.0
BATCH = 50_000

RULES = {
    "messages >= 100 in 30 days": [{"type": "message", "limit": {"kind": "threshold", "min": 100, "period": {"amount": 30, "unit": "days"}}}],
    "messages >= 1000 all time": [{"type": "message", "limit": {"kind": "threshold", "min": 1000, "period": {"all": True}}}],
    "top 10% by messages all time": [{"type": "message", "limit": {"kind": "top", "by": "percent", "from": 0, "to": 10, "period": {"all": True}}}],
    "5 a day on 5 of 7 days": [{"type": "message", "limit": {"kind": "days", "per_day": 5, "days": 5, "of": 7}}],
    "5 a day on 60 of 90 days": [{"type": "message", "limit": {"kind": "days", "per_day": 5, "days": 60, "of": 90}}],
    "voice >= 600 min in 30 days": [{"type": "voice", "limit": {"kind": "threshold", "min": 600, "period": {"amount": 30, "unit": "days"}}}],
    "compare messages vs voice all time": [{"type": "compare", "operator": "greater", "left": [{"kind": "message", "period": {"all": True}}],
                                            "right": [{"kind": "voice", "period": {"all": True}}]}],
    "inactive (0 messages in 90 days)": [{"type": "message", "limit": {"kind": "threshold", "min": 0, "max": 0, "period": {"amount": 90, "unit": "days"}}}],
}
COUNTERS = {
    "messages all time": {"type": "stat_count", "stat": {"kind": "messages", "period": {"all": True}}},
    "messages last day": {"type": "stat_count", "stat": {"kind": "messages", "period": {"amount": 1, "unit": "days"}}},
    "voice hours per day, 30 days": {"type": "stat_average", "stat": {"kind": "voice", "period": {"amount": 30, "unit": "days"}}},
    "messages change, 7 days": {"type": "stat_change", "stat": {"kind": "messages", "period": {"amount": 7, "unit": "days"}}},
    "top member all time": {"type": "stat_top", "of": "member", "stat": {"kind": "messages", "period": {"all": True}}},
    "top channel all time": {"type": "stat_top", "of": "channel", "stat": {"kind": "messages", "period": {"all": True}}},
}


async def seed(db: Database, now: float) -> None:
    """Four years of 15-minute slots, each with 8 to 12 message rows and 2 to 4 voice rows. A few people write most of it."""
    if await db.scalar("SELECT COUNT(*) FROM stat_messages") > 0:
        return
    rng = random.Random(7)
    end = tf.slot_of(now)
    start = end - int(YEARS * 365 * 96)
    people = [100_000_000_000_000_000 + index for index in range(MEMBERS)]
    weights = list(itertools.accumulate(1 / (rank + 1) ** 1.1 for rank in range(MEMBERS)))
    channels = [200_000_000_000_000_000 + index for index in range(60)]
    voice_channels = [300_000_000_000_000_000 + index for index in range(12)]
    messages, voice = [], []
    for slot in range(start, end):
        for person in rng.choices(people, cum_weights=weights, k=rng.choice((8, 10, 12))):
            messages.append((GUILD, slot, rng.choice(channels), person, 0, rng.randint(1, 3)))
        for person in rng.choices(people, cum_weights=weights, k=rng.choice((2, 3, 4))):
            voice.append((GUILD, slot, rng.choice(voice_channels), person, rng.choice((1, 1, 1, 3, 4)), 0, rng.randint(60, 900)))
        if len(messages) >= BATCH:
            await db.many("INSERT OR IGNORE INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", messages)
            messages = []
        if len(voice) >= BATCH:
            await db.many("INSERT OR IGNORE INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)", voice)
            voice = []
    if messages:
        await db.many("INSERT OR IGNORE INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", messages)
    if voice:
        await db.many("INSERT OR IGNORE INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)", voice)


async def run(path: Path) -> bool:
    db = Database(path, KEY)
    await db.open()
    try:
        await db.migrate("module:statistics", list(Statistics.message_migrations))
        now = time.time()
        started = time.perf_counter()
        await seed(db, now)
        print(f"{await db.scalar('SELECT COUNT(*) FROM stat_messages'):,} message rows and {await db.scalar('SELECT COUNT(*) FROM stat_voice'):,} voice rows, "
              f"ready in {time.perf_counter() - started:.0f}s", flush=True)
        people = {user_id: statroles.Person(user_id, False, frozenset({GUILD}), now - 300 * 86400, now - 900 * 86400)
                  for user_id in range(100_000_000_000_000_000, 100_000_000_000_000_000 + MEMBERS)}
        snap = statroles.Snapshot(GUILD, now, tf.zone("America/Los_Angeles"), people)
        slow = []
        for label, conditions in RULES.items():
            rule = statroles.clean_rule({"conditions": conditions})
            started = time.perf_counter()
            evaluation = await statroles.Evaluator(db, snap, {}).evaluate(rule)
            took = time.perf_counter() - started
            print(f"stat role  {label:40} {took:6.2f}s  {len(evaluation.passing):,} pass", flush=True)
            if took > LIMIT:
                slow.append(label)
        for label, counter in COUNTERS.items():
            place = counters.Place(guild=None, guild_id=GUILD, now=now, tz=tf.zone("America/Los_Angeles"), db=db)
            started = time.perf_counter()
            reading = await counters.read(counters.clean_counter(counter), place)
            took = time.perf_counter() - started
            print(f"counter    {label:40} {took:6.2f}s  {reading.value if reading.words is None else reading.words}", flush=True)
            if took > LIMIT:
                slow.append(label)
        if slow:
            print(f"Over {LIMIT:.0f} seconds: {', '.join(slow)}")
        return not slow
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Time stat roles and stat counters on a large seeded server.")
    parser.add_argument("--keep", type=Path, help="keep the seeded database in this file and reuse it next time")
    args = parser.parse_args()
    if args.keep:
        ok = asyncio.run(run(args.keep))
    else:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            ok = asyncio.run(run(Path(folder) / "bench.db"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
