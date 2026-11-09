"""How long each statistics card takes on a server with four years behind about ten million messages, a million and a half count rows
and 43,600 members. Not part of the suite, since seeding takes about half a minute.

    python -m tests.statistics_bench                  seeds a temporary database, then times every card at 30 days, a year and all time
    python -m tests.statistics_bench --keep DIR       keeps the seeded database in DIR and reuses it on the next run

Each card is gathered and drawn the way /server and the rest do it, minus Discord. It fails when any card takes over three seconds.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
import tempfile
import time
import types
from pathlib import Path

import discord

from captainguild.core.stats import cards, store, views
from captainguild.db import Database
from captainguild.modules.statistics_commands import COMMANDS_MESSAGE_MIGRATIONS
from tests.stats_seed import seed

KEY = "5" * 64
LIMIT_SECONDS = 3.0
NOW = 1_794_139_200.0  # Nov 8, 2026, noon UTC, where the seed ends


class _Allowed:
    def __getattr__(self, name: str) -> bool:
        return name in ("view_channel", "read_message_history", "send_messages")


def guild_from(people: list[int], rows: list[dict]) -> types.SimpleNamespace:
    """A guild with the seeded members and channels, every channel open to everyone."""
    when = datetime.datetime(2022, 5, 1, tzinfo=datetime.timezone.utc)
    members = [types.SimpleNamespace(id=user_id, bot=False, _roles=[], display_name=f"user {user_id % 100000}", name="user", joined_at=when, created_at=when,
                                     guild_permissions=_Allowed()) for user_id in people]
    by_id = {member.id: member for member in members}
    channels = {}
    for row in rows:
        channel = types.SimpleNamespace(id=row["channel_id"], name=row["name"], type=discord.ChannelType(row["type"]), _overwrites=[], category_id=row["parent_id"],
                                        created_at=when)
        channel.permissions_for = lambda member: _Allowed()
        channels[channel.id] = channel
    everyone = types.SimpleNamespace(id=1, permissions=types.SimpleNamespace(view_channel=True, administrator=False), is_default=lambda: True)
    guild = types.SimpleNamespace(id=1, name="Bench", members=members, default_role=everyone, roles=[everyone], owner_id=0, channels=list(channels.values()), created_at=when,
                                  member_count=len(members), icon=None, stickers=[], me=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_guild=True)))
    guild.get_member = by_id.get
    guild.get_channel = channels.get
    return guild


async def run(folder: Path) -> int:
    path = folder / "messages.db"
    fresh = not path.exists()
    db = Database(path, KEY)
    await db.open()
    try:
        if fresh:
            await db.migrate("module:statistics", [*store.MESSAGE_MIGRATIONS, *COMMANDS_MESSAGE_MIGRATIONS])
            started = time.perf_counter()
            info = await seed(None, db, now=NOW)
            print(f"Seeded {info.message_rows:,} message rows for {info.messages:,} messages in {time.perf_counter() - started:.0f} s")
        people = [row["user_id"] for row in await db.all("SELECT user_id FROM stat_people WHERE guild_id = 1 AND left_at IS NULL")]
        guild = guild_from(people, await db.all("SELECT * FROM stat_channels WHERE guild_id = 1"))
        busiest = await db.scalar("SELECT user_id FROM stat_messages INDEXED BY stat_messages_user WHERE guild_id = 1 GROUP BY user_id ORDER BY SUM(count) DESC LIMIT 1")
        member = guild.get_member(busiest) or types.SimpleNamespace(id=busiest, display_name="someone who left", name="gone", joined_at=None, created_at=None, bot=False)
        text = next(channel for channel in guild.channels if channel.name == "text-0")
        category = next(channel for channel in guild.channels if channel.type == discord.ChannelType.category)
        cfg = {"timezone": "America/Los_Angeles", "interval": "day", "time_unit": "hours", "include": "humans", "voice_states": [], "emoji_uses": [],
               "emoji_states": ["used", "unused", "deleted", "unavailable"], "invite_labels": {}, "lookback": {"mode": "days", "days": 30}, "track_activities": True}
        slowest = 0.0
        print(f"{'card':28} {'period':>8} {'numbers':>9} {'drawing':>9} {'total':>8}")
        for code in ("d30", "d365", "a"):
            for view, target in (("server", None), ("member", member), ("channel", text), ("channel", category), ("messages", None), ("voice", None),
                                 ("activity", None), ("emoji", None), ("invites", None), ("chart_messages", None), ("chart_voice", None), ("chart_members", None),
                                 ("chart_status", None), ("chart_activity", None), ("chart_emoji", None), ("chart_invites", None), ("chart_member", member),
                                 ("chart_channel", text), ("top", None)):
                scope = views.Scope(db=db, guild=guild, cfg=cfg, lang="en-US", now=NOW, lookback=views.lookback_of(code, cfg), filters=views.filters_for(cfg, guild),
                                    visibility=views.Visibility(guild, None, text, True))
                began = time.perf_counter()
                data = await views.gather(scope, view, target, page_kind="members_messages", page=4)
                gathered = time.perf_counter() - began
                began = time.perf_counter()
                await asyncio.to_thread(cards.render, data, "en-US", {})
                drawn = time.perf_counter() - began
                slowest = max(slowest, gathered + drawn)
                label = view + (" (category)" if target is category else "")
                print(f"{label:28} {code:>8} {gathered:8.2f}s {drawn:8.2f}s {gathered + drawn:7.2f}s")
        print(f"Slowest card: {slowest:.2f} s (limit {LIMIT_SECONDS:.0f} s)")
        return 0 if slowest <= LIMIT_SECONDS else 1
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Time every statistics card on a large server's worth of counts.")
    parser.add_argument("--keep", help="keep the seeded database in this folder and reuse it")
    options = parser.parse_args()
    if options.keep:
        folder = Path(options.keep)
        folder.mkdir(parents=True, exist_ok=True)
        return asyncio.run(run(folder))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as name:
        return asyncio.run(run(Path(name)))


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # the database's worker thread can outlive a closed loop on some platforms
