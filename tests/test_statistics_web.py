"""Statistics pages: what each route reads from the counts, under the view in its address."""
import asyncio
import re
import unittest

import discord

from captainguild.core.stats import web
from captainguild.core.stats.timeframe import zone
from captainguild.modules.statistics import SCHEMA, Statistics
from tests.fakes import BotTest, FakeChannel
from tests.stats_seed_web import LEAVER, seed_small

LA = zone("America/Los_Angeles")


class Views(unittest.TestCase):
    def setUp(self):
        self.cfg = SCHEMA.defaults()

    def test_an_empty_address_reads_as_the_server_set_it(self):
        view = web.read_view({}, {**self.cfg, "interval": "week", "time_unit": "days", "include": "both", "channels": [5], "channel_mode": "allow"})
        self.assertEqual((view["period"], view["interval"], view["unit"], view["include"], view["channels"], view["channel_mode"], view["stat"]),
                         ("", "week", "days", "both", [5], "allow", "both"))
        self.assertEqual(view.asked, {})

    def test_the_address_wins_and_nonsense_is_ignored(self):
        view = web.read_view({"period": "48H", "interval": "fortnight", "include": "bots", "channels": "123456789012345678,123456789012345678,x"}, self.cfg)
        self.assertEqual(view["period"], "48h")
        self.assertEqual(view["interval"], "day")  # the server's
        self.assertEqual(view["include"], "bots")
        self.assertEqual(view["channels"], [])  # one bad ID throws out the list
        self.assertEqual(view.address(), {"period": "48h", "include": "bots"})

    def test_periods(self):
        for raw, read in (("30d", "30d"), ("2mo", "2mo"), ("4w", "4w"), ("all", "all"), ("since:2025-3-1", "since:2025-03-01"), ("", ""), ("default", ""),
                          ("0d", None), ("since:2025-02-30", None), ("forever", None), ("1201mo", None)):
            self.assertEqual(web.read_field("period", raw), read, raw)

    def test_lists_of_names_keep_their_order_and_refuse_strangers(self):
        self.assertEqual(web.read_field("voice_states", "afk,talking"), ["talking", "afk"])
        self.assertEqual(web.read_field("voice_states", ""), [])
        self.assertIsNone(web.read_field("voice_states", "afk,shouting"))
        self.assertEqual(web.read_field("emoji", "u:\U0001F44D\U0001F3FD,c:111111111111111111"), ["u:\U0001F44D", "c:111111111111111111"])
        self.assertEqual(web.read_field("invites", "abc,vanity:makers"), ["abc", "vanity:makers"])
        self.assertIsNone(web.read_field("invites", "not a code!"))
        self.assertEqual(web.clean_address({"period": "7d", "junk": "1", "include": "nobody"}), {"period": "7d"})

    def test_a_month_back_lands_on_the_same_day_or_the_months_last(self):
        import datetime as dt
        now = dt.datetime(2026, 3, 31, 15, tzinfo=LA).timestamp()
        back = dt.datetime.fromtimestamp(web.months_back(now, 1, LA), LA)
        self.assertEqual((back.month, back.day, back.hour), (2, 28, 15))
        back = dt.datetime.fromtimestamp(web.months_back(now, 14, LA), LA)
        self.assertEqual((back.year, back.month, back.day), (2025, 1, 31))

    def test_role_filters_become_people(self):
        view = web.read_view({"roles_allow": "11", "roles_block": "22", "member_mode": "allow", "members": "1,2,3"}, self.cfg)
        holders = {11: {1, 2, 9}, 22: {2}}
        built, roles_on = web.filters_of(view, lambda ids: set().union(*(holders.get(i, set()) for i in ids)))
        self.assertTrue(roles_on)
        self.assertEqual(built.members_in, frozenset({1, 2}))
        self.assertEqual(built.members_out, frozenset({2}))
        built, roles_on = web.filters_of(view, lambda ids: None)  # members can't be read: role filters do nothing
        self.assertFalse(roles_on)
        self.assertEqual(built.members_in, frozenset({1, 2, 3}))

    def test_moving_averages(self):
        self.assertEqual(web.moving_average([2, 4, 6, 8], 2), [None, 3, 5, 7])
        self.assertEqual(web.moving_average([1, 2], 3), [None, None])


class CacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_read_serves_everyone_asking_in_the_same_minute(self):
        cache = web.Cache(seconds=60)
        calls = []

        async def work():
            calls.append(1)
            await asyncio.sleep(0.01)
            return len(calls)
        answers = await asyncio.gather(*(cache.get((1, "a"), work) for _ in range(5)))
        self.assertEqual(answers, [1] * 5)
        self.assertEqual(await cache.get((1, "a"), work), 1)
        self.assertEqual(len(calls), 1)
        cache.forget(1)
        self.assertEqual(await cache.get((1, "a"), work), 2)

    async def test_a_failure_isnt_kept(self):
        cache = web.Cache()

        async def broken():
            raise RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            await cache.get((1, "b"), broken)

        async def fine():
            return "ok"
        self.assertEqual(await cache.get((1, "b"), fine), "ok")

    async def test_what_expired_leaves_memory(self):
        cache = web.Cache(seconds=0.01)

        async def answer():
            return list(range(1000))
        for number in range(50):
            await cache.get((1, "q", number), answer)
        await asyncio.sleep(0.05)
        await cache.get((1, "another"), answer)
        self.assertEqual(list(cache.kept), [(1, "another")])

    async def test_what_it_keeps_stays_within_its_bytes(self):
        cache = web.Cache(budget=200_000)
        row = [[index, 10 ** 17 + index, index * 3] for index in range(100)]
        weight = web.weight_of(row)
        self.assertGreater(weight, 15_000)

        async def answer():
            return [list(item) for item in row]
        for number in range(20):
            await cache.get((1, "ranking", number), answer)
            self.assertLessEqual(cache.held, 200_000)
        self.assertEqual(cache.held, sum(cache.weights.values()))
        self.assertIn((1, "ranking", 19), cache.kept, "the newest stays")
        self.assertLessEqual(len(cache.kept), 200_000 // weight)

    async def test_an_answer_too_large_to_keep_is_still_answered(self):
        cache = web.Cache(budget=40_000)
        calls = []

        async def small():
            return list(range(50))

        async def large():
            calls.append(1)
            return list(range(600))
        self.assertTrue(40_000 // 4 < web.weight_of(list(range(600))) < 40_000)
        await cache.get((1, "small"), small)
        self.assertEqual(len(await cache.get((1, "large"), large)), 600)
        self.assertEqual(len(await cache.get((1, "large"), large)), 600)
        self.assertEqual(len(calls), 2, "over a quarter of the budget, it's read each time")
        self.assertEqual(list(cache.kept), [(1, "small")], "and it pushes nothing out")

    def test_weights_are_near_what_answers_hold(self):
        import sys

        def deep(value, seen):
            if id(value) in seen:
                return 0
            seen.add(id(value))
            size = sys.getsizeof(value)
            if isinstance(value, dict):
                size += sum(deep(key, seen) + deep(item, seen) for key, item in value.items())
            elif isinstance(value, (list, tuple)):
                size += sum(deep(item, seen) for item in value)
            return size
        ranking = [(10 ** 17 + index, index * 7, index % 40) for index in range(40_000)]
        rows = {"rows": [{"id": index, "name": f"name {index}", "seconds": index * 60.5} for index in range(3_000)], "series": [[0] * 200 for _ in range(10)]}
        for value in (ranking, rows):
            real = deep(value, set())
            self.assertTrue(0.5 <= web.weight_of(value) / real <= 2, (web.weight_of(value), real))

    async def test_a_read_whose_asker_left_still_answers_the_others(self):
        cache = web.Cache()
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(0.05)
            return "counted"
        first = asyncio.ensure_future(cache.get((1, "slow"), slow))
        await started.wait()
        second = asyncio.ensure_future(cache.get((1, "slow"), slow))
        await asyncio.sleep(0)
        first.cancel()
        self.assertEqual(await second, "counted")
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(await cache.get((1, "slow"), slow), "counted")


class HalfHourZones(BotTest):
    """A heatmap's cells are local hours. In a zone half an hour or 45 minutes off UTC, one UTC hour holds parts of two local hours, and
    each quarter hour belongs to the local hour it falls in."""
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)

    async def cells(self, zone_name, kind, rows):
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True, "timezone": zone_name})
        for moment, value in rows:
            if kind == "status":
                await self.bot.message_db.run("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) "
                                              "VALUES (?, ?, 1, ?, 0, 0, 0, 0, 0, 0, 0)", (self.guild.id, web.slot_of(moment.timestamp()), value))
            else:
                await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, ?)",
                                              (self.guild.id, web.slot_of(moment.timestamp()), self.channel.id, self.member.id, value))
        self.stats._web_cache.forget(self.guild.id)
        client = await self.client()
        response = await client.get(self.path(f"/statistics/heatmap?kind={kind}&period=30d"))
        body = await response.json()
        self.assertEqual(response.status, 200, body)
        return {(day, hour): value for day, row in enumerate(body["cells"]) for hour, value in enumerate(row) if value}

    def two_moments(self, zone_name, first, second):
        import datetime as dt
        tz = zone(zone_name)
        day = (dt.datetime.now(tz) - dt.timedelta(days=2)).date()
        return [dt.datetime(day.year, day.month, day.day, *first, tzinfo=tz), dt.datetime(day.year, day.month, day.day, *second, tzinfo=tz)]

    async def test_messages_land_in_their_local_hour_in_india(self):
        first, second = self.two_moments("Asia/Kolkata", (5, 40), (6, 10))  # 00:10 and 00:40 UTC, one UTC hour
        hot = await self.cells("Asia/Kolkata", "messages", [(first, 3), (second, 7)])
        self.assertEqual(set(hot), {(first.weekday(), 5), (second.weekday(), 6)})

    async def test_messages_land_in_their_local_hour_in_nepal(self):
        first, second = self.two_moments("Asia/Kathmandu", (5, 50), (6, 10))  # 00:05 and 00:25 UTC, one UTC hour
        hot = await self.cells("Asia/Kathmandu", "messages", [(first, 3), (second, 7)])
        self.assertEqual(set(hot), {(first.weekday(), 5), (second.weekday(), 6)})

    async def test_status_readings_land_in_their_local_hour(self):
        first, second = self.two_moments("Asia/Kolkata", (5, 40), (6, 10))
        hot = await self.cells("Asia/Kolkata", "status", [(first, 10), (second, 50)])
        self.assertEqual(hot, {(first.weekday(), 5): 10, (second.weekday(), 6): 50})

    def test_a_clock_change_on_a_week_boundary_of_the_walk_lands_on_its_slot(self):
        import datetime as dt
        tz = zone("America/Los_Angeles")
        change = web.slot_of(dt.datetime(2025, 3, 9, 10, tzinfo=dt.timezone.utc).timestamp())  # 2 AM in Los Angeles became 3 AM
        for start in (change - web.WEEK_SLOTS, change - 2 * web.WEEK_SLOTS, change - web.WEEK_SLOTS + 1, change - web.WEEK_SLOTS - 1):
            cell = web.LocalHours(tz, start, change + web.WEEK_SLOTS)
            for slot in (change - 1, change, change + 1):
                local = dt.datetime.fromtimestamp(slot * 900, tz)
                self.assertEqual(cell(slot), (local.weekday(), local.hour), (start, slot))

    def test_every_slot_reads_as_the_zone_says_across_clock_changes(self):
        import datetime as dt
        start = web.slot_of(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp())
        end = web.slot_of(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp())
        for name in ("America/Los_Angeles", "Asia/Kolkata", "Asia/Kathmandu", "Australia/Lord_Howe", "Pacific/Chatham", "America/St_Johns", "UTC"):
            tz = zone(name)
            cell = web.LocalHours(tz, start, end)
            for slot in range(start, end, 37):  # every 37th quarter hour lands on every minute of the hour and every hour of the week
                local = dt.datetime.fromtimestamp(slot * 900, tz)
                self.assertEqual(cell(slot), (local.weekday(), local.hour), (name, local))


class PresentOnly(BotTest):
    """Without the Presence intent the status page has Discord's count of members present, which takes in bots. Offline is then everyone
    less that count, so bots online don't come off the people offline."""
    modules = [Statistics]

    async def test_offline_takes_bots_out_of_both_sides(self):
        import time
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        slot = web.slot_of(time.time()) - 8
        await self.bot.message_db.run("INSERT INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (?, ?, 12, 10, 2, 0, 0)",
                                      (self.guild.id, slot))
        await self.bot.message_db.run("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) "
                                      "VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 1, 7)", (self.guild.id, slot))
        client = await self.client()
        found = {}
        for include in ("humans", "both", "bots"):
            body = await (await client.get(self.path(f"/statistics/status?period=24h&interval=day&include={include}"))).json()
            found[include] = [value for value in body["offline"] if value is not None]
        self.assertEqual(found, {"humans": [5], "both": [5], "bots": []})


class LongPeriods(BotTest):
    """An address can ask for any period back to year 1. A period starts no earlier than the first count, and never before Discord began,
    and what a heatmap costs follows the rows stored, not the hours the period spans."""
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        import datetime as dt
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True, "timezone": "America/Los_Angeles"})
        self.first = web.slot_of(dt.datetime(2016, 3, 1, 12, tzinfo=dt.timezone.utc).timestamp())
        for slot in (self.first, web.slot_of(time_now()) - 8):
            await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 4)",
                                          (self.guild.id, slot, self.channel.id, self.member.id))
        self.web = await self.client()

    async def get(self, tail):
        response = await self.web.get(self.path(tail))
        body = await response.json()
        self.assertEqual(response.status, 200, body)
        return body

    async def test_a_period_from_year_one_starts_at_the_first_count(self):
        for period in ("since:0001-01-02", "876000h", "1200mo", "since:1999-12-31", "all"):
            for path in ("/statistics/heatmap?kind=messages", "/statistics/activity?kind=messages", "/statistics/overview"):
                body = await self.get(f"{path}&period={period}" if "?" in path else f"{path}?period={period}")
                start = body["period"]["start"]
                self.assertLessEqual(start, self.first * 900, (period, path))
                self.assertGreater(start, self.first * 900 - 32 * 86400, (period, path))  # floored to its bucket, a month at most
            self.stats._web_cache.forget(self.guild.id)
        activity = await self.get("/statistics/activity?kind=messages&period=since:0001-01-02")
        self.assertEqual(activity["total"], 8)

    async def test_a_heatmap_reads_each_stored_slot_and_not_every_hour(self):
        calls = []
        real = web.LocalHours.__call__

        def counted(cell, slot):
            calls.append(slot)
            return real(cell, slot)
        web.LocalHours.__call__ = counted
        try:
            body = await self.get("/statistics/heatmap?kind=messages&period=all")
        finally:
            web.LocalHours.__call__ = real
        self.assertEqual(len(calls), 2, "one per stored slot")
        self.assertEqual(sum(1 for row in body["cells"] for value in row if value), 2)

    def test_hours_of_the_week_are_counted_as_a_walk_would(self):
        import random
        chooser = random.Random(5)
        for name in ("America/Los_Angeles", "Asia/Kathmandu", "Australia/Lord_Howe", "UTC"):
            tz = zone(name)
            for _ in range(12):
                start = web.slot_of(1_420_070_400) + chooser.randrange(0, 400_000)
                end = start + chooser.randrange(0, 40_000)
                cell = web.LocalHours(tz, start, end)
                walked = [[0.0] * 24 for _ in range(7)]
                for slot in range(start, end):
                    day, hour = cell(slot)
                    walked[day][hour] += 0.25
                self.assertEqual(web.hours_of_week(start, end, cell), walked, (name, start, end))


def time_now() -> float:
    import time
    return time.time()


class Seeded(BotTest):
    """The small seed (tests/stats_seed_web.py) and a signed-in owner."""
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        self.help = FakeChannel(self.guild, "help")
        self.lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.seed = await seed_small(self.bot, self.guild, member=self.member, admin=self.admin, mod=self.mod, general=self.channel, help_channel=self.help,
                                     lounge=self.lounge)
        self.web = await self.client()

    async def get(self, tail, status=200):
        response = await self.web.get(self.path(tail))
        body = await response.json()
        self.assertEqual(response.status, status, body)
        return body


class Pages(Seeded):
    async def test_messages_plot_counts_people_and_channels(self):
        data = await self.get("/statistics/activity?kind=messages")
        self.assertEqual(data["total"], 5 + 3 + 4 + 2 + 6)  # the bot's 9 and the year-old 7 are out
        self.assertEqual(data["unique_members"], 3)  # privacy's nobody isn't someone
        self.assertEqual(data["unique_channels"], 3)
        self.assertEqual(sum(data["count"]), data["total"])
        self.assertEqual(len(data["count"]), len(data["period"]["buckets"]))
        self.assertEqual(data["period"]["label"], "Last 14 days")
        everything = await self.get("/statistics/activity?kind=messages&period=all&include=both")
        self.assertEqual(everything["total"], 20 + 9 + 7)
        self.assertEqual(everything["period"]["label"], "All time")
        voice = await self.get("/statistics/activity?kind=voice&voice_states=talking")
        self.assertEqual(voice["total"], 600)

    async def test_long_periods_move_to_longer_buckets(self):
        data = await self.get("/statistics/activity?kind=messages&period=10000d&interval=hour")
        self.assertEqual(data["period"]["asked_interval"], "hour")
        self.assertIn(data["period"]["interval"], ("day", "week", "month"))  # the period starts at the first count, about a year back
        self.assertLessEqual(len(data["period"]["buckets"]), 1000)
        self.assertGreater(data["period"]["start"], time_now() - 430 * 86400)

    async def test_rankings_name_everyone_and_say_who_left(self):
        members = await self.get("/statistics/top?kind=messages&by=members")
        rows = [dict(zip(members["columns"], row)) for row in members["rows"]]
        self.assertEqual([(row["rank"], row["id"], row["value"]) for row in rows], [(1, str(self.member.id), 8), (2, str(LEAVER), 6), (3, str(self.admin.id), 4)])
        self.assertEqual(rows[1]["display_name"], "Gone Person")
        self.assertFalse(rows[1]["in_server"])
        self.assertTrue(rows[0]["in_server"])
        channels = await self.get("/statistics/top?kind=messages&by=channels")
        rows = [dict(zip(channels["columns"], row)) for row in channels["rows"]]
        self.assertEqual([(row["name"], row["value"], row["members"]) for row in rows], [("general", 11, 2), ("old-chat", 6, 1), ("help", 3, 1)])
        self.assertTrue(rows[1]["deleted"])

    async def test_filters_from_the_address(self):
        blocked = await self.get(f"/statistics/activity?kind=messages&channel_mode=block&channels={self.channel.id}")
        self.assertEqual(blocked["total"], 3 + 6)
        allowed = await self.get(f"/statistics/activity?kind=messages&member_mode=allow&members={self.member.id}")
        self.assertEqual(allowed["total"], 8)
        roles = await self.get(f"/statistics/activity?kind=messages&roles_allow={self.role_member.id}")
        self.assertEqual(roles["total"], 8)
        kept_out = await self.get(f"/statistics/activity?kind=messages&roles_block={self.role_admin.id}")
        self.assertEqual(kept_out["total"], 5 + 3 + 2 + 6)

    async def test_a_server_filter_applies_until_the_address_changes_it(self):
        await self.bot.settings.save(self.guild.id, "statistics", {"channels": [self.channel.id], "channel_mode": "block"})
        self.assertEqual((await self.get("/statistics/activity?kind=messages"))["total"], 9)
        self.assertEqual((await self.get("/statistics/activity?kind=messages&channels="))["total"], 20)

    async def test_heatmap(self):
        data = await self.get("/statistics/heatmap?kind=messages&period=all")
        self.assertEqual(len(data["cells"]), 7)
        self.assertTrue(all(len(row) == 24 for row in data["cells"]))
        self.assertGreater(sum(sum(row) for row in data["cells"]), 0)
        status = await self.get("/statistics/heatmap?kind=status")
        self.assertGreater(sum(sum(row) for row in status["cells"]), 0)

    async def test_overview(self):
        data = await self.get("/statistics/overview")
        totals = data["totals"]
        self.assertEqual(totals["messages_now"], 20)
        self.assertEqual(totals["voice_now"], 900)
        self.assertEqual(totals["contributors_now"], 3)
        self.assertEqual((totals["joins_now"], totals["leaves_now"]), (3, 1))
        self.assertEqual(totals["members_now"], 90)  # humans, as the view counts them
        self.assertIsNotNone(totals["online_now"])
        self.assertEqual(len(data["series"]["members"]), len(data["period"]["buckets"]))

    async def test_status(self):
        data = await self.get("/statistics/status")
        self.assertTrue(data["breakdown"])
        self.assertFalse(data["present_only"])
        online = [value for value in data["online"] if value is not None]
        self.assertEqual(online, [(20 / 2 + 12) / 2])  # both readings fall in one day: the average of each quarter hour's average

    async def test_apps(self):
        data = await self.get("/statistics/apps")
        self.assertEqual([(row["name"], row["seconds"], row["people"], row["verified"]) for row in data["rows"]], [("Minecraft", 1800, 2, True)])
        self.assertEqual(sum(data["series"][0]["values"]), 1800)
        self.assertFalse(data["tracking"])

    async def test_invites_and_their_fake_reasons(self):
        await self.bot.settings.save(self.guild.id, "statistics", {"invite_labels": {"abc": "Website"}})
        data = await self.get("/statistics/invites")
        self.assertEqual(data["total"], 2)  # the bot's join is out for a view of people
        joins = {row[1]: dict(zip(data["joins"]["columns"], row)) for row in data["joins"]["rows"]}
        self.assertEqual(joins[str(LEAVER)]["reasons"], ["no_avatar", "left"])
        self.assertEqual(joins[str(self.member.id)]["reasons"], [])
        self.assertEqual(data["labels"], {"abc": "Website"})
        self.assertEqual(data["people"][str(LEAVER)]["display_name"], "Gone Person")
        invites = {row["code"]: row for row in data["invites"]}
        self.assertEqual((invites["abc"]["joins"], invites["abc"]["real"], invites["abc"]["fake"]), (2, 1, 1))
        self.assertEqual(data["people"][str(invites["abc"]["inviter_id"])]["name"], self.admin.name)
        self.assertEqual(invites["quiet"]["joins"], 0)
        self.assertEqual([(row["user_id"], row["real"]) for row in data["inviters"]], [(str(self.admin.id), 1)])
        real = await self.get("/statistics/invites?fake_reasons=left,no_avatar&fake_mode=block&fake_match=any")
        self.assertEqual(real["total"], 1)
        only = await self.get("/statistics/invites?invites=quiet&invite_mode=allow")
        self.assertEqual(only["total"], 0)

    async def test_emoji_states_and_types(self):
        data = await self.get("/statistics/emoji")
        rows = {row["key"]: row for row in data["rows"]}
        self.assertEqual((rows["u:\U0001F44D"]["uses"], rows["u:\U0001F44D"]["reactions"], rows["u:\U0001F44D"]["self_reactions"], rows["u:\U0001F44D"]["super"]), (4, 3, 1, 1))
        self.assertEqual(rows["c:111111111111111111"]["state"], "used")
        self.assertEqual(rows["c:222222222222222222"]["state"], "unused")
        self.assertEqual(rows["c:333333333333333333"]["state"], "deleted")
        self.assertEqual(rows["c:111111111111111111"]["name"], "party")
        self.assertIn("cdn.discordapp.com/emojis/111111111111111111", rows["c:111111111111111111"]["image"])
        used_only = await self.get("/statistics/emoji?emoji_states=used")
        self.assertEqual(sorted(row["key"] for row in used_only["rows"]), ["c:111111111111111111", "u:\U0001F44D"])
        no_super = await self.get("/statistics/emoji?emoji_types=super&emoji_type_mode=block")
        self.assertEqual({row["key"]: row["uses"] for row in no_super["rows"] if row["uses"]}, {"u:\U0001F44D": 3, "c:111111111111111111": 2})
        server_only = await self.get("/statistics/emoji?emoji_types=server&emoji_type_mode=allow&emoji_states=used")
        self.assertEqual([row["key"] for row in server_only["rows"]], ["c:111111111111111111"])
        reactions = await self.get("/statistics/emoji?emoji_uses=messages&emoji_states=used")
        self.assertEqual([(row["key"], row["uses"]) for row in reactions["rows"]], [("c:111111111111111111", 2)])

    async def test_analytics_and_the_share_from_people_who_left(self):
        data = await self.get("/statistics/analytics")
        self.assertEqual(sum(data["messages"]["count"]), 20)
        self.assertEqual(sum(data["messages"]["not_in_server"]), 6)
        self.assertEqual(sum(data["voice"]["count"]), 900)
        roles = await self.get(f"/statistics/analytics?roles_allow={self.role_member.id}")
        self.assertIsNone(roles["messages"]["not_in_server"])
        self.assertTrue(roles["roles_on"])
        unique = await self.get("/statistics/analytics/unique")
        self.assertEqual(max(unique["members"]), 2)  # the member wrote and talked in the same bucket: counted once
        people = await self.get("/statistics/analytics/members")
        rows = {row[0]: dict(zip(people["columns"], row)) for row in people["rows"]}
        self.assertEqual((rows[str(self.member.id)]["messages"], rows[str(self.member.id)]["voice"], rows[str(self.member.id)]["message_channels"]), (8, 600, 2))
        self.assertEqual(rows[str(self.admin.id)]["voice"], 300)
        places = await self.get("/statistics/analytics/channels")
        rows = {row[1]: dict(zip(places["columns"], row)) for row in places["rows"]}
        self.assertEqual((rows["Lounge"]["voice"], rows["Lounge"]["voice_members"]), (900, 2))

    async def test_member_drilldown(self):
        data = await self.get(f"/statistics/drilldown/members?ids={self.member.id},{LEAVER}")
        mine, gone = data["members"]
        self.assertEqual(mine["messages"], 8)
        self.assertEqual(mine["last_message"]["channel"]["name"], "general")
        self.assertEqual(mine["last_voice"]["channel"]["name"], "Lounge")
        self.assertEqual([(row["channel"]["name"], row["messages"], row["voice"]) for row in mine["channels"]], [("general", 5, 0), ("help", 3, 0), ("Lounge", 0, 600)])
        self.assertAlmostEqual(mine["averages"]["messages"]["day"] * (data["period"]["end"] - data["period"]["start"]) / 86400, 8, places=6)
        self.assertEqual(mine["invited"], 0)
        self.assertEqual(gone["person"]["display_name"], "Gone Person")
        self.assertIsNotNone(gone["left_at"])
        admin = (await self.get(f"/statistics/drilldown/members?ids={self.admin.id}"))["members"][0]
        self.assertEqual((admin["invited"], admin["invited_left"]), (2, 1))
        self.assertEqual(sum(admin["series"]["voice"]), 300)

    async def test_channel_drilldown_and_categories(self):
        category = FakeChannel(self.guild, "Community", discord.ChannelType.category)
        await self.bot.message_db.run("UPDATE stat_channels SET parent_id = ? WHERE guild_id = ? AND channel_id IN (?, ?)", (category.id, self.guild.id, self.channel.id, self.help.id))
        await self.bot.message_db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type) VALUES (?, ?, 'Community', 4)", (self.guild.id, category.id))
        data = await self.get(f"/statistics/drilldown/channels?ids={self.channel.id},{category.id}")
        general, whole = data["channels"]
        self.assertEqual(general["messages"], 11)  # privacy's 2 count toward the channel and no one's name
        self.assertEqual([(row[0], row[1]) for row in general["members"]], [(str(self.member.id), 5), (str(self.admin.id), 4)])
        self.assertEqual(data["people"][str(self.member.id)]["name"], self.member.name)
        self.assertEqual(general["members_total"], 2)
        self.assertEqual(general["last_message"]["person"]["id"], str(self.member.id))
        self.assertEqual(whole["messages"], 14)
        self.assertEqual(whole["channel"]["name"], "Community")

    async def test_presets(self):
        saved = await self.web.post(self.path("/statistics/presets"), json={"name": "Two months", "query": {"period": "2mo", "interval": "week", "junk": "x"}})
        self.assertEqual(saved.status, 200, await saved.text())
        preset = await saved.json()
        self.assertEqual(preset["query"], {"period": "2mo", "interval": "week"})
        again = await self.web.post(self.path("/statistics/presets"), json={"name": "two MONTHS", "query": {}})
        self.assertEqual(again.status, 400)
        long_name = await self.web.post(self.path("/statistics/presets"), json={"name": "x" * 20, "query": {}})
        self.assertEqual(long_name.status, 400)
        drill = await self.web.post(self.path("/statistics/presets"), json={"name": "Helpers", "kind": "members", "query": {"period": "30d"}, "picks": [str(self.member.id)]})
        self.assertEqual(drill.status, 200)
        listed = await self.get("/statistics/presets")
        self.assertEqual([row["name"] for row in listed["filters"]], ["Two months"])
        self.assertEqual(listed["members"][0]["picks"], [str(self.member.id)])
        self.assertTrue(listed["can_edit"])
        gone = await self.web.delete(self.path(f"/statistics/presets/{preset['id']}"))
        self.assertEqual(gone.status, 200)
        self.assertEqual((await self.get("/statistics/presets"))["filters"], [])
        activity = await self.bot.db.all("SELECT action FROM activity WHERE guild_id = ? ORDER BY id", (self.guild.id,))
        self.assertIn("statistics.preset.create", [row["action"] for row in activity])

    async def test_panels_are_each_persons_own_and_have_no_cap_of_four(self):
        panels = [{"stat": "messages", "value": "sum", "period": "7d"}, {"stat": "voice", "value": "average", "period": "2w", "interval": "day"},
                  {"stat": "members", "value": "change", "period": "4w", "percent": True}, {"stat": "messages", "value": "unique", "unique": "channels", "period": "all"},
                  {"stat": "messages", "value": "change", "period": "7d"}, {"stat": "members", "value": "count", "period": "7d", "title": "Members now"}]
        saved = await self.web.put(self.path("/statistics/panels"), json={"panels": panels})
        self.assertEqual(saved.status, 200, await saved.text())
        self.assertEqual(len((await self.get("/statistics/panels"))["panels"]), 6)
        values = (await self.get("/statistics/panels/values"))["values"]
        self.assertEqual(values[0]["number"], 20)
        self.assertEqual(values[3]["number"], 3)
        self.assertEqual(values[5]["number"], 90)
        self.assertAlmostEqual(values[2]["number"], 0.0)  # 90 at both ends of four weeks
        bad = await self.web.put(self.path("/statistics/panels"), json={"panels": [{"stat": "members", "value": "unique"}]})
        self.assertEqual(bad.status, 400)
        too_many = await self.web.put(self.path("/statistics/panels"), json={"panels": [panels[0]] * (web.PANEL_LIMIT + 1)})
        self.assertEqual(too_many.status, 400)
        stored = await self.bot.db.all("SELECT user_id FROM stat_panels WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual([row["user_id"] for row in stored], [self.admin.id])

    async def test_settings_save_through_the_dashboards_checks(self):
        data = await self.get("/statistics/settings")
        self.assertTrue(data["all"])
        from captainguild.modules.statistics_web import AREAS
        self.assertEqual(sorted(data["areas"]), sorted(AREAS))
        saved = await self.web.put(self.path("/statistics/settings"), json={"timezone": "America/Los_Angeles", "smooth_charts": True})
        self.assertEqual(saved.status, 200, await saved.text())
        self.assertEqual(self.settings("statistics")["timezone"], "America/Los_Angeles")
        refused = await self.web.put(self.path("/statistics/settings"), json={"timezone": "Mars/Base"})
        self.assertEqual(refused.status, 400)

    async def test_context(self):
        data = await self.get("/statistics/context")
        self.assertEqual(data["level"], "full")
        self.assertTrue(data["can_drill"] and data["can_presets"] and data["can_settings"])
        self.assertEqual(data["defaults"]["include"], "humans")
        self.assertEqual([row["name"] for row in data["activities"]], ["Minecraft"])
        self.assertEqual(sorted(row["code"] for row in data["invites"]), ["abc", "quiet"])
        self.assertIn("c:222222222222222222", [row["key"] for row in data["emoji"]])
        self.assertEqual(data["fake_reasons"][0], "self")

    async def test_answers_are_kept_a_minute(self):
        first = await self.get("/statistics/activity?kind=messages")
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 50)",
                                      (self.guild.id, self.seed.now_slot - 1, self.channel.id, self.member.id))
        self.assertEqual((await self.get("/statistics/activity?kind=messages"))["total"], first["total"])
        self.stats._web_cache.forget(self.guild.id)
        self.assertEqual((await self.get("/statistics/activity?kind=messages"))["total"], first["total"] + 50)

    async def test_counts_waiting_in_memory_are_written_before_a_read(self):
        self.stats.recorder.message(self.guild.id, self.seed.now_slot, self.channel.id, self.member.id, 0, 40)
        self.assertEqual((await self.get("/statistics/activity?kind=messages"))["total"], 60)

    async def test_bad_kinds_are_refused(self):
        await self.get("/statistics/activity?kind=reactions", 400)
        await self.get("/statistics/top?kind=messages&by=roles", 400)
        await self.get("/statistics/drilldown/members?ids=bob", 400)


class Flushes(Seeded):
    """Each read that misses the cache wants the counts waiting in memory written first. Each write is a transaction on disk, so a burst
    of different questions writes once."""

    async def test_a_burst_of_reads_writes_what_waits_once(self):
        calls = []
        real = self.stats.up_to_date

        async def counted():
            calls.append(1)
            await real()
        self.stats.up_to_date = counted
        for days in range(1, 21):
            await self.get(f"/statistics/activity?kind=messages&period={days}d")
        self.assertEqual(len(calls), 1)


class NamedOnce(Seeded):
    """A ranking of 40,000 people kept from the last minute comes back named too, so a page read again names nobody again."""

    async def test_a_ranking_read_again_names_nobody_again(self):
        calls = []
        real = self.stats._web_person

        def counted(viewer, user_id, known):
            calls.append(user_id)
            return real(viewer, user_id, known)
        self.stats._web_person = counted
        for path in ("/statistics/top?kind=messages&by=members&period=all", "/statistics/analytics/members?period=all"):
            calls.clear()
            first = await self.get(path)
            named = len(calls)
            self.assertGreater(named, 0, path)
            again = await self.get(path)
            self.assertEqual(len(calls), named, path)
            self.assertEqual(first, again, path)

    async def test_a_ranking_read_again_is_not_encoded_again(self):
        from captainguild.modules import statistics_web
        calls = []
        real = statistics_web.safe_json

        def counted(value):
            calls.append(1)
            return real(value)
        statistics_web.safe_json = counted
        try:
            for path in ("/statistics/top?kind=messages&by=members&period=all", "/statistics/analytics/members?period=all"):
                calls.clear()
                await self.get(path)
                await self.get(path)
                self.assertEqual(len(calls), 1, path)
        finally:
            statistics_web.safe_json = real


class PanelReads(Seeded):
    """A person's panels are bounded twice: how many they keep, and how long one request reads before it answers with what it has."""

    async def save_panels(self, panels):
        saved = await self.web.put(self.path("/statistics/panels"), json={"panels": panels})
        self.assertEqual(saved.status, 200, await saved.text())

    async def test_a_person_keeps_up_to_the_limit(self):
        panel = {"stat": "messages", "value": "sum", "period": "7d"}
        await self.save_panels([panel] * web.PANEL_LIMIT)
        refused = await self.web.put(self.path("/statistics/panels"), json={"panels": [panel] * (web.PANEL_LIMIT + 1)})
        self.assertEqual(refused.status, 400)
        self.assertLessEqual(web.PANEL_LIMIT, 12)

    async def test_one_request_reads_for_its_time_and_the_next_carries_on(self):
        from captainguild.modules import statistics_web
        real = web.panel_value

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.15)
            return await real(*args, **kwargs)
        web.panel_value = slow
        try:
            await self.save_panels([{"stat": "messages", "value": "sum", "period": f"{days}d"} for days in range(1, 9)])
            budget = statistics_web.PANEL_READ_SECONDS
            statistics_web.PANEL_READ_SECONDS = 0.4
            try:
                values, start, rounds = [], 0, 0
                while start is not None:
                    began = time_now()
                    answer = await self.get(f"/statistics/panels/values?from={start}")
                    self.assertLess(time_now() - began, 0.4 + 0.15 + 0.3)
                    self.assertEqual(answer["from"], start)
                    values += answer["values"]
                    start = answer["next"]
                    rounds += 1
            finally:
                statistics_web.PANEL_READ_SECONDS = budget
        finally:
            web.panel_value = real
        self.assertEqual(len(values), 8)
        self.assertGreater(rounds, 2)
        self.assertEqual(values[6]["number"], 20)

    async def test_each_panel_is_read_once_a_minute(self):
        calls = []
        real = web.panel_value

        async def counted(*args, **kwargs):
            calls.append(1)
            return await real(*args, **kwargs)
        web.panel_value = counted
        try:
            panel = {"stat": "messages", "value": "unique", "unique": "members", "period": "all"}
            await self.save_panels([panel, {"stat": "voice", "value": "sum", "period": "7d"}])
            first = await self.get("/statistics/panels/values")
            await self.save_panels([{"stat": "voice", "value": "sum", "period": "7d"}, panel, panel])
            second = await self.get("/statistics/panels/values")
        finally:
            web.panel_value = real
        self.assertEqual(len(calls), 2, "a changed set reads only what it hasn't read")
        self.assertEqual([value["number"] for value in second["values"]], [first["values"][1]["number"], first["values"][0]["number"], first["values"][0]["number"]])

    async def test_a_panel_reads_what_the_page_reads(self):
        await self.save_panels([{"stat": "messages", "value": value, "unique": unique, "period": "all", "interval": "month"}
                                for value, unique in (("sum", "members"), ("unique", "members"), ("unique", "channels"), ("average", "members"), ("count", "members"))])
        values = (await self.get("/statistics/panels/values"))["values"]
        page = await self.get("/statistics/activity?kind=messages&period=all&interval=month")
        self.assertEqual([value["number"] for value in values[:3]], [page["total"], page["unique_members"], page["unique_channels"]])
        self.assertAlmostEqual(values[3]["number"], page["average"])
        self.assertEqual(values[4]["number"], page["count"][-1])
        self.assertEqual(values[0]["series"], page["count"])
        self.assertEqual(values[1]["series"], page["members"])
        self.assertEqual(values[2]["series"], page["channels"])



class ShippedText(unittest.TestCase):
    """No invisible characters in what ships: soft hyphens, zero-width characters, the word joiner and invisible operators, bidirectional
    controls, variation selectors, byte order marks and Hangul fillers. Where a page means one, it writes an escape."""

    INVISIBLE = re.compile("[\u00ad\u115f\u1160\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u3164\ufe00-\ufe0f\ufeff\uffa0]")

    def test_no_invisible_characters_in_shipped_files(self):
        from pathlib import Path
        package = Path(web.__file__).resolve().parents[2]
        found = []
        for path in sorted([*package.rglob("*"), *Path(__file__).resolve().parent.glob("*.py")]):
            if path.suffix not in (".py", ".js", ".css", ".html", ".svg") or "vendor" in path.parts or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            found += [f"{path.name}:{text.count(chr(10), 0, match.start()) + 1} U+{ord(match.group()):04X}" for match in self.INVISIBLE.finditer(text)]
        self.assertEqual(found, [])


class LightTheme(unittest.TestCase):
    """The light theme darkens button text for light panels. A button drawn over a picture, such as Personalize's Edit banner on its dark
    backdrop, keeps white text."""

    def rules(self):
        from pathlib import Path
        css = (Path(web.__file__).resolve().parents[2] / "web" / "static" / "css" / "app.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for block in css.split("}"):
            if "{" not in block:
                continue
            selectors, _, body = block.partition("{")
            yield [part.strip() for part in selectors.split(",")], {name.strip(): value.strip() for name, _, value in
                                                                    (line.partition(":") for line in body.split(";")) if value.strip()}

    def test_the_edit_banner_button_keeps_white_text(self):
        found = [body.get("color") for selectors, body in self.rules() if ':root[data-theme="light"] .btn.profile-edit' in selectors]
        self.assertEqual(found, ["#fff"])
        backdrop = [body.get("background") for selectors, body in self.rules() if ".profile-edit" in selectors]
        self.assertEqual(backdrop, ["rgb(0 0 0 / 65%)"])

if __name__ == "__main__":
    unittest.main()
