"""How long each statistics route takes on a large server: 1.5 million message rows over four years from 43,000 members, with voice,
emoji, member counts, statuses and joins beside them. Seeding takes a few minutes, so it runs only when asked:

    set CG_STATS_TIMING=C:\\some\\local\\folder
    python -m unittest tests.test_statistics_timing

CG_STATS_TIMING names a folder on a local disk for the database and SQLite's temporary files, so a synced folder's uploads and locks
don't land in the numbers. Any other value uses the system's temporary folder.

Each route runs cold (no answer kept from an earlier read) three times, for the server's default view and for all time, read by someone
with full access and at the members' level, where every read names the channels the reader may see. The middle of the three must be under
two seconds. So must every request
of a dozen panels over all time, and a ranking read again from the cache must not hold the event loop. The times print as a table."""
from __future__ import annotations

import asyncio
import os
import statistics as numbers
import tempfile
import time
import unittest

import discord

from captainguild.core.stats import web
from captainguild.modules.statistics import Statistics
from tests.fakes import BotTest, FakeChannel, FakeMember
from tests.stats_seed_web import seed_large

LIMIT = 2.0
STALL = 0.1  # the longest a cached ranking may hold the event loop
RUNS = 3
ROUTES = [
    "/statistics/context", "/statistics/overview", "/statistics/activity?kind=messages", "/statistics/activity?kind=voice",
    "/statistics/top?kind=messages&by=members", "/statistics/top?kind=messages&by=channels", "/statistics/top?kind=voice&by=members",
    "/statistics/top?kind=voice&by=channels", "/statistics/heatmap?kind=messages", "/statistics/heatmap?kind=voice", "/statistics/heatmap?kind=status",
    "/statistics/status", "/statistics/apps", "/statistics/invites", "/statistics/emoji", "/statistics/analytics", "/statistics/analytics/unique",
    "/statistics/analytics/members", "/statistics/analytics/channels", "/statistics/drilldown/members?ids={people}", "/statistics/drilldown/channels?ids={channels}",
]
MEMBER_ROUTES = ROUTES[:15]  # a member without the drill role reads no analytics or drilldowns
FOLDER = os.environ.get("CG_STATS_TIMING", "")


class LoopLag:
    """The longest stretch the event loop went without running a 5 ms ticker."""

    def __init__(self) -> None:
        self.worst = 0.0
        self.task: asyncio.Task | None = None

    async def _run(self) -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            self.worst = max(self.worst, now - last - 0.005)
            last = now

    def __enter__(self) -> "LoopLag":
        self.task = asyncio.ensure_future(self._run())
        return self

    def __exit__(self, *exc) -> None:
        self.task.cancel()


@unittest.skipUnless(FOLDER, "seeds a large server for minutes: set CG_STATS_TIMING to a local folder to run it")
class LargeServer(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        self.saved = (tempfile.tempdir, os.environ.get("TEMP"), os.environ.get("TMP"))
        if os.path.isdir(FOLDER):
            tempfile.tempdir = FOLDER
            os.environ["TEMP"] = os.environ["TMP"] = FOLDER
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        started = time.perf_counter()
        await seed_large(self.bot.message_db, self.guild.id)
        self.seeded = time.perf_counter() - started
        # Discord's member list is a lookup by ID. The fake's is a list, so it gets an index here to stand in for 43,000 members.
        for index in range(0, 43_000, 2):
            FakeMember(self.guild, f"person{index}").id = 10 ** 17 + index
        found = {member.id: member for member in self.guild.members}
        self.guild.get_member = found.get
        for index in range(120):
            FakeChannel(self.guild, f"channel-{index}", discord.ChannelType.voice if index >= 100 else discord.ChannelType.text).id = 2 * 10 ** 17 + index
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True, "timezone": "America/Los_Angeles", "dashboard_access": "members",
                                                                     "viewer_filters": True, "lookback": {"mode": "days", "days": 30, "since": None}})

    async def asyncTearDown(self):
        await super().asyncTearDown()
        tempfile.tempdir, temp, tmp = self.saved
        for name, value in (("TEMP", temp), ("TMP", tmp)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    async def timed(self, client, path):
        self.stats._web_cache.forget(self.guild.id)
        started = time.perf_counter()
        response = await client.get(self.path(path))
        body = await response.read()
        took = time.perf_counter() - started
        self.assertEqual(response.status, 200, (path, body[:300]))
        return took, len(body)

    async def test_every_route_answers_in_under_two_seconds(self):
        people = ",".join(str(10 ** 17 + index) for index in range(5))
        channels = ",".join(str(2 * 10 ** 17 + index) for index in (0, 1, 2, 100, 101))
        rows, slow = [], []
        # At the members' level every query names the channels the reader may see.
        for who, access, routes in (("full", "full", ROUTES), ("member", "viewer", MEMBER_ROUTES)):
            client = await self.client(access=access)
            for route in routes:
                path = route.format(people=people, channels=channels)
                for label, extra in (("default", ""), ("all time", ("&" if "?" in path else "?") + "period=all")):
                    runs = [await self.timed(client, path + extra) for _ in range(RUNS)]
                    middle = numbers.median(took for took, _ in runs)
                    rows.append((who, path, label, middle, max(took for took, _ in runs), runs[0][1]))
                    if middle >= LIMIT:
                        slow.append((who, path, label, round(middle, 2)))

        # A dozen panels over all time, the most a person keeps, each request reading for its share of time.
        admin = await self.client(access="full")
        panels = [{"stat": stat, "value": "unique" if index % 3 == 0 else "sum", "unique": "members" if index % 2 else "channels", "period": "all",
                   "interval": "month", "title": f"panel {index}"} for index, stat in enumerate(["messages", "voice"] * (web.PANEL_LIMIT // 2))]
        saved = await admin.put(self.path("/statistics/panels"), json={"panels": panels})
        self.assertEqual(saved.status, 200, await saved.text())
        self.stats._web_cache.forget(self.guild.id)
        start, requests = 0, []
        while start is not None:
            began = time.perf_counter()
            answer = await (await admin.get(self.path(f"/statistics/panels/values?from={start}"))).json()
            requests.append(time.perf_counter() - began)
            start = answer["next"]
        if max(requests) >= LIMIT:
            slow.append(("full", "/statistics/panels/values", "a dozen all-time panels", round(max(requests), 2)))

        # A ranking of 40,000 people read again within the minute: from the cache, named once.
        path = self.path("/statistics/top?kind=messages&by=members&period=all")
        await (await admin.get(path)).read()
        stalls = []
        for _ in range(RUNS):
            with LoopLag() as lag:
                await (await admin.get(path)).read()
                await asyncio.sleep(0.02)
            stalls.append(lag.worst)

        print(f"\nSeeded in {self.seeded:.0f} s, temporary files in {tempfile.gettempdir()}. Route times, cold, middle and slowest of {RUNS}:")
        for who, path, label, middle, slowest, size in rows:
            print(f"  {middle:6.2f} s {slowest:6.2f} s  {size / 1024:8.0f} KiB  {who:6} {label:9} {path}")
        print(f"  panels: {len(requests)} requests, slowest {max(requests):.2f} s, all {sum(requests):.1f} s")
        print(f"  a cached ranking of people held the event loop at most {max(stalls) * 1000:.0f} ms")
        self.assertEqual(slow, [])
        self.assertLess(max(stalls), STALL)


if __name__ == "__main__":
    unittest.main()
