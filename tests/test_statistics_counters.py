"""Counter channels: what each counter reads, how a name is made, the clock they update on, Discord's rename limit, taking over channels,
export and import, and the follower readers."""
import asyncio
import datetime as dt
import json
import re
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import discord

from captainguild.core.ops import DiscordOps
from captainguild.core.stats import counters, query, social, store
from captainguild.core.stats import timeframe as tf
from captainguild.core.validate import Invalid
from captainguild.modules.statistics import Statistics
from captainguild.modules.statistics_tools import COUNTER_PRESETS, counter_from_row, parse_end
from tests.fakes import NOW, BotTest, FakeChannel, FakeMember, FakePermissions

LA = tf.zone("America/Los_Angeles")
UTC = tf.zone("UTC")


def moment(*parts, tz=dt.timezone.utc):
    return dt.datetime(*parts, tzinfo=tz)


class Numbers(unittest.TestCase):
    def test_styles(self):
        self.assertEqual(counters.format_number(13742), "13742")
        self.assertEqual(counters.format_number(13742, "separated"), "13,742")
        self.assertEqual(counters.format_number(13742, "short"), "13.7k")
        self.assertEqual(counters.format_number(999, "short"), "999")
        self.assertEqual(counters.format_number(99_960, "short"), "100k")
        self.assertEqual(counters.format_number(999_960, "short"), "1M")
        self.assertEqual(counters.format_number(46_700_000, "short"), "46.7M")
        self.assertEqual(counters.format_number(2.345, "plain", 1), "2.3")
        self.assertEqual(counters.format_number(-1500, "short"), "-1.5k")

    def test_counts_written_the_short_way(self):
        self.assertEqual(counters.short_count("246K subscribers"), 246_000)
        self.assertEqual(counters.short_count("46.7M"), 46_700_000)
        self.assertEqual(counters.short_count("1,234"), 1234)
        self.assertEqual(counters.short_count("8637"), 8637)
        self.assertEqual(counters.short_count("1.234"), 1234)  # a count with no letter has no decimals, so a dot separates thousands
        self.assertEqual(counters.short_count("about 5K"), 5000)
        self.assertEqual(counters.short_count("1,5M"), 1_500_000)
        self.assertEqual(counters.short_count("246\u00a0K"), 246_000)
        self.assertIsNone(counters.short_count("none"))


class Dates(unittest.TestCase):
    """Each expected string is what dayjs with the advanced-format, week-of-year, week-year and ISO-week plugins writes for that moment."""

    def test_a_long_day_name_and_an_ordinal(self):
        self.assertEqual(counters.format_date("dddd MMM Do", moment(2026, 11, 8, 15, 4, 5)), "Sunday Nov 8th")

    def test_core_tokens(self):
        when = moment(2026, 1, 2, 3, 4, 5, 678000)
        self.assertEqual(counters.format_date("YYYY-MM-DD HH:mm:ss.SSS", when), "2026-01-02 03:04:05.678")
        self.assertEqual(counters.format_date("YY M D d dd ddd dddd", when), "26 1 2 5 Fr Fri Friday")
        self.assertEqual(counters.format_date("h hh a A", when), "3 03 am AM")
        self.assertEqual(counters.format_date("MMM MMMM", when), "Jan January")
        self.assertEqual(counters.format_date("Z ZZ", when), "+00:00 +0000")

    def test_escapes_and_letters_that_are_not_tokens(self):
        when = moment(2026, 11, 8, 15, 4)
        self.assertEqual(counters.format_date("[Today is] dddd", when), "Today is Sunday")
        self.assertEqual(counters.format_date("[]", when), "[]")
        self.assertEqual(counters.format_date("YYY", when), "26Y")  # the core reads YY and leaves the last Y

    def test_advanced_tokens(self):
        when = moment(2026, 11, 8, 0, 30)
        self.assertEqual(counters.format_date("Q Do k kk", when), "4 8th 24 24")
        self.assertEqual(counters.format_date("X x", moment(2026, 11, 8, 15, 4, 5)), "1794150245 1794150245000")
        self.assertEqual(counters.format_date("Wo", when), "45o")  # the plugin's pattern has no Wo, so W reads and o stays
        self.assertEqual(counters.format_date("wo", when), "46th")

    def test_ordinals(self):
        self.assertEqual([counters.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111, 0, 10)],
                         ["[1st]", "[2nd]", "[3rd]", "[4th]", "[11th]", "[12th]", "[13th]", "[21st]", "[22nd]", "[23rd]", "[101st]", "[111th]", "[0th]", "[10th]"])

    def test_weeks_around_new_year(self):
        # 2026-12-27 is a Sunday, and January 1, 2027 falls in its week, so the en week is 1 of 2027. ISO counts it in week 52 of 2026.
        self.assertEqual(counters.format_date("w gggg W GGGG", moment(2026, 12, 27, 12)), "1 2027 52 2026")
        # 2027-01-01 is a Friday: en week 1, ISO week 53 of 2026.
        self.assertEqual(counters.format_date("ww gggg WW GGGG", moment(2027, 1, 1, 12)), "01 2027 53 2026")
        self.assertEqual(counters.format_date("w", moment(2026, 1, 3, 12)), "1")
        self.assertEqual(counters.format_date("w", moment(2026, 1, 4, 12)), "2")

    def test_zones(self):
        stamp = moment(2026, 7, 1, 12).timestamp()
        self.assertEqual(counters.format_date("HH:mm z", dt.datetime.fromtimestamp(stamp, LA)), "05:00 PDT")
        fixed = counters.zone_for("UTC+05:45", UTC)
        self.assertEqual(counters.format_date("HH:mm Z", dt.datetime.fromtimestamp(stamp, fixed)), "17:45 +05:45")
        self.assertEqual(counters.zone_for("server", LA), LA)

    def test_zone_settings(self):
        self.assertEqual(counters.clean_zone("UTC+5:30"), "UTC+05:30")
        self.assertEqual(counters.clean_zone(""), "server")
        self.assertEqual(counters.clean_zone("Europe/Paris"), "Europe/Paris")
        for bad in ("UTC+15:00", "UTC+05:10", "Mars/Base"):
            with self.assertRaises(Invalid, msg=bad):
                counters.clean_zone(bad)


class Countdowns(unittest.TestCase):
    def counter(self, units):
        return counters.clean_counter({"type": "countdown", "end": 1_000_000.0, "units": units, "end_text": "Launched"})

    def test_units_left_out_roll_into_smaller_ones(self):
        left = 2 * 86400 + 5 * 3600 + 7 * 60 + 30
        now = 1_000_000.0 - left
        self.assertEqual(counters.countdown_text(self.counter({"days": "d ", "hours": "h ", "minutes": "m"}), now), ("2d 5h 7m", False))
        self.assertEqual(counters.countdown_text(self.counter({"hours": " hours"}), now), ("53 hours", False))
        self.assertEqual(counters.countdown_text(self.counter({"minutes": "m"}), now), ("3187m", False))

    def test_the_end_text_takes_the_whole_name(self):
        counter = self.counter({"days": "d"})
        setup = counters.clean_setup({"text": "Launch in {1}!", "counters": [counter], "frequency": 10})
        reading = counters.Reading(words="Launched", ended=True)
        self.assertEqual(counters.render(setup, [reading]), "Launched")
        self.assertEqual(counters.countdown_text(counter, 1_000_001.0), ("Launched", True))


class Schedule(unittest.TestCase):
    def test_ten_minutes_line_up_with_the_clock(self):
        now = moment(2026, 11, 8, 15, 27, 40).timestamp()
        self.assertEqual(counters.last_boundary(10, now, UTC), moment(2026, 11, 8, 15, 20).timestamp())
        self.assertEqual(counters.next_boundary(10, now, UTC), moment(2026, 11, 8, 15, 30).timestamp())
        self.assertEqual(counters.last_boundary(45, now, UTC), moment(2026, 11, 8, 15, 0).timestamp())

    def test_a_daily_counter_turns_at_local_midnight_through_a_clock_change(self):
        # Clocks in Los Angeles went forward on March 8, 2026, so that day has 23 hours, and 22 hours after 00:30 is 23:30.
        after_change = dt.datetime(2026, 3, 8, 0, 30, tzinfo=LA).timestamp() + 3600 * 22
        self.assertEqual(counters.last_boundary(1440, after_change, LA), dt.datetime(2026, 3, 8, tzinfo=LA).timestamp())
        self.assertEqual(counters.next_boundary(1440, after_change, LA), dt.datetime(2026, 3, 9, tzinfo=LA).timestamp())
        for hours in range(0, 72, 5):
            now = dt.datetime(2026, 3, 7, tzinfo=LA).timestamp() + hours * 3600 + 120
            found = dt.datetime.fromtimestamp(counters.last_boundary(1440, now, LA), LA)
            self.assertEqual((found.hour, found.minute), (0, 0), hours)

    def test_a_date_counters_zone_sets_the_schedule(self):
        setup = counters.clean_setup({"text": "{1}", "counters": [{"type": "datetime", "format": "MMM D", "zone": "UTC+05:45"}], "frequency": 1440})
        tz = counters.schedule_zone(setup, LA)
        now = moment(2026, 11, 8, 20, 0).timestamp()
        self.assertEqual(dt.datetime.fromtimestamp(counters.last_boundary(1440, now, tz), tz).hour, 0)

    def test_two_renames_in_ten_minutes(self):
        now = 10_000.0
        self.assertEqual(counters.rename_allowed_at([], now), now)
        self.assertEqual(counters.rename_allowed_at([now - 100], now), now)
        self.assertEqual(counters.rename_allowed_at([now - 100, now - 50], now), now - 100 + 600)
        self.assertEqual(counters.rename_allowed_at([now - 700, now - 50], now), now)


class Setups(unittest.TestCase):
    def test_the_page_offers_a_goal_on_every_counter_that_takes_one(self):
        page = (Path(__file__).resolve().parent.parent / "captainguild" / "web" / "static" / "js" / "pages" / "counters.js").read_text(encoding="utf-8")
        offered = set(re.findall(r"'(\w+)'", re.search(r"const GOALS = new Set\(\[([^\]]*)\]\)", page).group(1)))
        self.assertEqual(offered, set(counters.GOAL_TYPES))

    def test_a_channel_holds_as_many_counters_as_its_text_can_name(self):
        many = [{"type": "members"}] * 50
        text = "".join(f"{{{index}}}" for index in range(1, 51))
        self.assertEqual(len(counters.clean_setup({"text": text, "counters": many, "frequency": 10})["counters"]), 50)
        with self.assertRaises(Invalid):
            counters.clean_setup({"text": text + "{51}", "counters": [*many, {"type": "members"}], "frequency": 10})

    def test_every_counter_type_fills_its_defaults(self):
        examples = {
            "members": {}, "channels": {}, "statuses": {"statuses": ["online"]}, "role_members": {"roles": [111111111111111111]}, "roles": {},
            "voice": {}, "countdown": {"end": 2_000_000_000}, "datetime": {}, "stat_count": {"stat": {"kind": "messages"}},
            "stat_average": {"stat": {"kind": "statuses"}}, "stat_change": {"stat": {"kind": "members"}}, "stat_top": {"stat": {"kind": "voice"}},
            "invites": {}, "youtube": {"account": "https://www.youtube.com/@BigChannel"}, "instagram": {"account": "@some.one"}, "tiktok": {"account": "tiktok.com/@someone"},
        }
        self.assertEqual(sorted(examples), sorted(counters.TYPES))
        for kind, options in examples.items():
            cleaned = counters.clean_counter({"type": kind, **options})
            self.assertEqual(counters.clean_counter(json.loads(json.dumps(cleaned))), cleaned, kind)
        self.assertEqual(counters.clean_counter({"type": "youtube", "account": "https://www.youtube.com/@BigChannel"})["account"], "@BigChannel")
        self.assertEqual(counters.clean_counter({"type": "tiktok", "account": "tiktok.com/@someone"})["account"], "someone")

    def test_a_text_and_its_counters_must_agree(self):
        good = {"text": "Members: {1}", "counters": [{"type": "members"}], "frequency": 10}
        self.assertEqual(counters.clean_setup(good)["text"], "Members: {1}")
        for bad in ({**good, "text": "Members"}, {**good, "text": "{1} {2}"}, {**good, "frequency": 7}, {**good, "counters": []},
                    {**good, "counters": [{"type": "members"}] * 26}, {**good, "text": "a\nb {1}"}):
            with self.assertRaises(Invalid, msg=str(bad)[:80]):
                counters.clean_setup(bad)

    def test_a_change_needs_a_length(self):
        with self.assertRaises(Invalid):
            counters.clean_counter({"type": "stat_change", "stat": {"kind": "messages", "period": {"all": True}}})

    def test_rendering(self):
        setup = counters.clean_setup({"text": "\N{LARGE GREEN CIRCLE}{1} \N{NO ENTRY}{2} goal {3}", "frequency": 10,
                                      "counters": [{"type": "statuses", "statuses": ["online"]}, {"type": "statuses", "statuses": ["dnd"], "style": "separated"},
                                                   {"type": "members", "goal": 50_000}]})
        name = counters.render(setup, [counters.Reading(1116), counters.Reading(1772), counters.Reading(43_600)])
        self.assertEqual(name, "\N{LARGE GREEN CIRCLE}1116 \N{NO ENTRY}1,772 goal 6400")
        self.assertEqual(counters.render(setup, [counters.Reading(), counters.Reading(1), counters.Reading(60_000)]), "\N{LARGE GREEN CIRCLE}? \N{NO ENTRY}1 goal 0")
        long = counters.clean_setup({"text": "x" * 150 + " {1}", "counters": [{"type": "members"}], "frequency": 10})
        self.assertEqual(len(counters.render(long, [counters.Reading(5)])), 100)

    def test_changes(self):
        counter = counters.clean_counter({"type": "stat_change", "stat": {"kind": "messages"}, "as": "percent", "decimals": 1, "plus": True})
        self.assertEqual(counters.shown(counter, counters.Reading(12.345)), "+12.3%")
        self.assertEqual(counters.shown(counter, counters.Reading(-4.0)), "-4.0%")
        plain = counters.clean_counter({"type": "stat_change", "stat": {"kind": "members"}, "plus": True, "decimals": 0})
        self.assertEqual(counters.shown(plain, counters.Reading(90)), "+90")

    def test_values_read_back_from_a_name(self):
        setup = counters.clean_setup({"text": "\N{LARGE GREEN CIRCLE}{1} \N{NO ENTRY}{2} \N{CRESCENT MOON}{3}", "frequency": 10,
                                      "counters": [{"type": "statuses", "statuses": [status]} for status in ("online", "dnd", "idle")]})
        self.assertEqual(counters.values_from_name(setup, "\N{LARGE GREEN CIRCLE}1116 \N{NO ENTRY}772 \N{CRESCENT MOON}626"), [1116.0, 772.0, 626.0])
        self.assertIsNone(counters.values_from_name(setup, "something else"))
        side_by_side = counters.clean_setup({"text": "{1} {2}", "frequency": 10, "counters": [{"type": "members"}, {"type": "roles"}]})
        self.assertIsNone(counters.values_from_name(side_by_side, "12 34 56"), "two counters with only a space between can't be told apart")


class Readers(unittest.TestCase):
    def test_social_pages(self):
        youtube = ('<script>var ytInitialData = {"header":{"pageHeaderRenderer":{"content":{"pageHeaderViewModel":{"metadata":{"contentMetadataViewModel":'
                   '{"metadataRows":[{"metadataParts":[{"text":{"content":"@BigChannel"}}]},{"metadataParts":[{"text":{"content":"246K subscribers"}},'
                   '{"text":{"content":"1.2K videos"}}]}]}}}}}}};</script>')
        self.assertEqual(social.youtube_count(youtube), 246_000)
        self.assertEqual(social.youtube_count('<script>var ytInitialData = {"header":{"c4TabbedHeaderRenderer":{"subscriberCountText":{"simpleText":"No subscribers"}}}};</script>'), 0)
        self.assertIsNone(social.youtube_count("<html></html>"))
        instagram = ('<script type="application/json" data-sjs>{"require":[["ScheduledServerJS","handle",null,[{"__bbox":{"require":[["RelayPrefetchedStreamCache",'
                     '"next",[],["adp",{"__bbox":{"complete":true,"result":{"data":{"xig_user_by_username":{"pk":"1","username":"nasa","follower_count":104379507}}}}}]]]}}]]]}</script>')
        self.assertEqual(social.instagram_count(instagram), 104_379_507)
        self.assertEqual(social.instagram_count('<meta property="og:description" content="686M Followers, 170 Following, 8,639 Posts" />'), 686_000_000)
        self.assertIsNone(social.instagram_count("<html></html>"))
        tiktok = ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{"__DEFAULT_SCOPE__":{"webapp.user-detail":{"statusCode":0,'
                  '"userInfo":{"stats":{"followerCount":96700000},"statsV2":{"followerCount":"96659441"}}}}}</script>')
        self.assertEqual(social.tiktok_count(tiktok), (96_659_441, 0))
        self.assertEqual(social.tiktok_count('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{"__DEFAULT_SCOPE__":{"webapp.user-detail":{"statusCode":10221}}}</script>'), (None, 10221))


class FakeOutside:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    async def get(self, url, **kwargs):
        self.asked.append(url)
        found = self.pages.get(url)
        if isinstance(found, Exception):
            raise found
        if found is None:
            return types.SimpleNamespace(status=404, text=lambda: "")
        return types.SimpleNamespace(status=200, text=lambda: found)

    async def close(self):
        pass


class Followers(unittest.IsolatedAsyncioTestCase):
    async def test_a_count_is_kept_for_an_hour_and_a_failure_keeps_the_last_one(self):
        page = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{"__DEFAULT_SCOPE__":{"webapp.user-detail":{"statusCode":0,"userInfo":{"statsV2":{"followerCount":"183"}}}}}</script>'
        outside = FakeOutside({"https://www.tiktok.com/@someone": page})
        reader = social.Followers(outside, gap=0)
        first = await reader.count("tiktok", "someone", now=1000.0)
        self.assertEqual((first.value, first.problem), (183, None))
        await reader.count("tiktok", "someone", now=1000.0 + 3000)
        self.assertEqual(len(outside.asked), 1, "read once within the hour")
        outside.pages["https://www.tiktok.com/@someone"] = "<html>a challenge page</html>"
        later = await reader.count("tiktok", "SomeOne", now=1000.0 + 4000)
        self.assertEqual(later.value, 183)
        self.assertIn("earlier", later.problem)
        await reader.count("tiktok", "someone", now=1000.0 + 4100)
        self.assertEqual(len(outside.asked), 2, "a failure waits before the next try")

    async def test_an_unknown_account_says_so(self):
        reader = social.Followers(FakeOutside({}), gap=0)
        found = await reader.count("instagram", "nobody", now=1.0)
        self.assertIsNone(found.value)
        self.assertEqual(found.problem, social.NOT_FOUND)


def editable(channel):
    """A fake channel that can be renamed, recording each rename."""
    channel.renames = []

    async def edit(*, name=None, reason=None, **kwargs):
        if name is not None:
            channel.renames.append(name)
            channel.name = name
    channel.edit = edit
    return channel


class Module(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.guild.default_role.permissions = FakePermissions("view_channel")
        self.bot.fetch_guild = mock.AsyncMock(return_value=types.SimpleNamespace(approximate_member_count=100, approximate_presence_count=40))
        self.made = []

        async def create_voice_channel(name, *, overwrites=None, category=None, position=None, reason=None):
            channel = editable(FakeChannel(self.guild, name, discord.ChannelType.voice, category=category))
            channel.overwrites = overwrites or {}
            self.made.append(channel)
            return channel
        self.guild.create_voice_channel = create_voice_channel
        self.guild.voice_channels = []
        self.guild.stage_channels = []

    async def add(self, text, counter, **kwargs):
        setup = counters.clean_setup({"text": text, "counters": [counter], "frequency": kwargs.pop("frequency", 10)})
        number, problem = await self.stats.add_counter(self.guild.id, setup, guild=self.guild, **kwargs)
        return await self.stats._counter(self.guild.id, number), problem

    async def test_a_new_counter_makes_a_locked_channel_named_from_live_values(self):
        found, problem = await self.add("Members: {1}", {"type": "members"}, enabled=True)
        self.assertIsNone(problem)
        channel = self.made[0]
        self.assertEqual(channel.name, f"Members: {len(self.guild.members)}")
        everyone = channel.overwrites[self.guild.default_role]
        mine = channel.overwrites[self.me]
        self.assertFalse(everyone.connect)
        self.assertTrue(mine.connect and mine.view_channel and mine.manage_channels)
        self.assertEqual(found["channel_id"], channel.id)

    async def test_a_rename_happens_only_when_the_name_changes_and_twice_in_ten_minutes_at_most(self):
        found, _ = await self.add("Members: {1}", {"type": "members"}, enabled=True)
        channel = self.made[0]
        now = time.time()
        place = await self.stats.counter_place(self.guild, now, [found["setup"]])
        await self.stats.refresh_counter(self.guild, await self.stats._counter(self.guild.id, found["number"]), place)
        self.assertEqual(channel.renames, [], "the same name isn't sent again")
        for name in ("a", "b", "c"):
            FakeMember(self.guild, name)
            found = await self.stats._counter(self.guild.id, found["number"])
            result = await self.stats.refresh_counter(self.guild, found, await self.stats.counter_place(self.guild, now, [found["setup"]]))
        self.assertEqual(len(channel.renames), 2)
        self.assertGreater(result["waiting_until"], now)
        later = now + counters.RENAME_WINDOW + 1
        found = await self.stats._counter(self.guild.id, found["number"])
        await self.stats.refresh_counter(self.guild, found, await self.stats.counter_place(self.guild, later, [found["setup"]]))
        self.assertEqual(channel.renames[-1], f"Members: {len(self.guild.members)}")

    async def test_a_refused_rename_says_what_the_bot_needs(self):
        found, _ = await self.add("Members: {1}", {"type": "members"}, enabled=True)
        channel = self.made[0]

        async def refuse(**kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Missing Permissions")
        channel.edit = refuse
        FakeMember(self.guild, "newcomer")
        result = await self.stats.refresh_counter(self.guild, found, await self.stats.counter_place(self.guild, time.time(), [found["setup"]]))
        self.assertEqual(result["problem"], "The bot can't rename this channel. Give it View Channel, Manage Channels and Connect there.")
        self.assertEqual((await self.stats._counter(self.guild.id, found["number"]))["problem"], result["problem"])

    async def test_off_deletes_the_channel_and_keeps_the_setup_and_on_makes_a_new_one(self):
        category = FakeChannel(self.guild, "Stats", discord.ChannelType.category)
        found, _ = await self.add("Members: {1}", {"type": "members"}, enabled=True, parent_id=category.id)
        channel = self.made[0]
        self.assertIsNone(await self.stats.counter_off(self.guild, found))
        self.assertEqual(channel.calls[-1][0], "delete")
        off = await self.stats._counter(self.guild.id, found["number"])
        self.assertFalse(off["enabled"])
        self.assertIsNone(off["channel_id"])
        self.assertEqual(off["parent_id"], category.id)
        on, problem = await self.stats.counter_on(self.guild, off)
        self.assertTrue(on, problem)
        self.assertEqual(self.made[1].category, category)

    async def test_a_rename_discord_holds_back_waits_for_the_next_window_and_is_left_to_finish(self):
        found, _ = await self.add("Members: {1}", {"type": "members"}, enabled=True)
        channel = self.made[0]
        released = asyncio.Event()
        renamed = channel.edit

        async def held(**kwargs):
            await released.wait()
            await renamed(**kwargs)
        channel.edit = held
        FakeMember(self.guild, "newcomer")
        now = time.time()
        with mock.patch("captainguild.modules.statistics_tools.RENAME_SECONDS", 0.05):
            result = await self.stats.refresh_counter(self.guild, found, await self.stats.counter_place(self.guild, now, [found["setup"]]))
        self.assertAlmostEqual(result["waiting_until"], now + counters.RENAME_WINDOW, delta=5)
        held = (await self.stats._counter(self.guild.id, found["number"]))["renames"]
        self.assertEqual(len(held), 2)
        self.assertTrue(all(abs(moment - now) < 5 for moment in held), "the window counts as used from when Discord held the rename back")
        released.set()
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(channel.name, f"Members: {len(self.guild.members)}", "the request wasn't canceled, so it lands once Discord lets it")

    async def test_two_switches_at_once_make_one_channel(self):
        found, _ = await self.add("Members: {1}", {"type": "members"})
        results = await asyncio.gather(self.stats.counter_on(self.guild, found), self.stats.counter_on(self.guild, found))
        self.assertEqual([on for on, _ in results], [True, True])
        self.assertEqual(len(self.made), 1)
        await asyncio.gather(self.stats.counter_off(self.guild, found), self.stats.counter_off(self.guild, found))
        self.assertEqual([call[0] for call in self.made[0].calls].count("delete"), 1)

    async def test_deleting_the_channel_by_hand_switches_the_counter_off(self):
        found, _ = await self.add("Members: {1}", {"type": "members"}, enabled=True)
        await self.stats._counter_channel_deleted(self.made[0])
        gone = await self.stats._counter(self.guild.id, found["number"])
        self.assertFalse(gone["enabled"])
        self.assertEqual(gone["problem"], "The channel was deleted, so this counter is off. Turn it on to make a new channel.")
        self.assertEqual(gone["setup"], found["setup"])

    async def test_taking_over_a_channel_keeps_it_in_place_and_reads_its_numbers(self):
        existing = editable(FakeChannel(self.guild, "\N{LARGE GREEN CIRCLE}1116 \N{NO ENTRY}772 \N{CRESCENT MOON}626", discord.ChannelType.voice))
        existing.position = 4
        text = "\N{LARGE GREEN CIRCLE}{1} \N{NO ENTRY}{2} \N{CRESCENT MOON}{3}"
        setup = counters.clean_setup({"text": text, "frequency": 10, "counters": [{"type": "statuses", "statuses": [status]} for status in ("online", "dnd", "idle")]})
        self.bot.fetch_guild = mock.AsyncMock(return_value=types.SimpleNamespace(approximate_member_count=43_600, approximate_presence_count=5000))
        number, problem = await self.stats.add_counter(self.guild.id, setup, guild=self.guild, enabled=True, channel_id=existing.id)
        found = await self.stats._counter(self.guild.id, number)
        self.assertEqual((found["channel_id"], found["position"]), (existing.id, 4))
        self.assertEqual(self.made, [], "no channel was made")
        # Without each member's status, online reads Discord's count of members present, and idle and do not disturb keep what the channel showed.
        self.assertEqual(existing.name, "\N{LARGE GREEN CIRCLE}5000 \N{NO ENTRY}772 \N{CRESCENT MOON}626")
        self.assertTrue(found["readings"] is not None)

    async def test_the_bot_adds_its_own_overwrite_to_a_channel_it_takes_over(self):
        existing = editable(FakeChannel(self.guild, "Users: 10", discord.ChannelType.voice))
        number, problem = await self.stats.add_counter(self.guild.id, counters.clean_setup({"text": "Users: {1}", "counters": [{"type": "members", "include": "humans"}], "frequency": 10}),
                                                       guild=self.guild, enabled=True, channel_id=existing.id)
        self.assertIsNone(problem)
        mine = existing.overwrites[self.me]
        self.assertTrue(mine.view_channel and mine.connect and mine.manage_channels)

    async def test_the_minute_tick_updates_what_is_due(self):
        found, _ = await self.add("Members: {1}", {"type": "members"}, enabled=True, frequency=10)
        channel = self.made[0]
        FakeMember(self.guild, "newcomer")
        boundary = counters.next_boundary(10, time.time(), tf.zone("UTC"))
        await self.stats.tools_tick(boundary - 30)
        await asyncio.gather(*self.stats._tools_tasks)
        self.assertEqual(channel.renames, [], "made this minute, so it waits for its next turn")
        await self.stats.tools_tick(boundary + 5)
        await asyncio.gather(*self.stats._tools_tasks)
        self.assertEqual(channel.renames, [f"Members: {len(self.guild.members)}"])

    async def test_switched_off_statistics_stops_updates(self):
        await self.add("Members: {1}", {"type": "members"}, enabled=True)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": False})
        FakeMember(self.guild, "newcomer")
        await self.stats.tools_tick(time.time() + 3600)
        self.assertEqual(self.stats._tools_tasks, set())

    async def test_quick_setup_makes_three_counters_in_a_new_category(self):
        made, problem = await self.stats.quick_counters(self.guild)
        self.assertEqual((len(made), problem), (3, None))
        categories = [channel for channel in self.guild.channels if channel.type == discord.ChannelType.category]
        self.assertEqual([channel.category for channel in self.made], [categories[-1]] * 3)
        self.assertTrue(self.made[2].name.startswith("\N{CALENDAR} "))

    async def test_the_cap(self):
        setup = counters.clean_setup({"text": "M {1}", "counters": [{"type": "members"}], "frequency": 10})
        with mock.patch("captainguild.modules.statistics_tools.MAX_COUNTER_CHANNELS", 2):
            await self.stats.add_counter(self.guild.id, setup)
            await self.stats.add_counter(self.guild.id, setup)
            with self.assertRaises(Invalid):
                await self.stats.add_counter(self.guild.id, setup)

    async def test_a_new_channel_stays_locked_for_roles_the_category_lets_in(self):
        category = FakeChannel(self.guild, "Voice", discord.ChannelType.category)
        category.overwrites = {self.role_member: discord.PermissionOverwrite(view_channel=True, connect=True), self.mod: discord.PermissionOverwrite(connect=True)}
        await self.add("Members: {1}", {"type": "members"}, enabled=True, parent_id=category.id)
        overwrites = self.made[0].overwrites
        self.assertFalse(overwrites[self.guild.default_role].connect)
        self.assertFalse(overwrites[self.role_member].connect, "a role the category lets connect can't join a counter channel")
        self.assertTrue(overwrites[self.role_member].view_channel, "and still sees it")
        self.assertFalse(overwrites[self.mod].connect)
        self.assertTrue(overwrites[self.me].connect)

    async def test_a_server_has_one_counter_update_at_a_time_and_each_reads_its_row_afresh(self):
        setup = counters.clean_setup({"text": "Members: {1}", "counters": [{"type": "members"}], "frequency": 6})
        await self.stats.add_counter(self.guild.id, setup, guild=self.guild, enabled=True)
        second, _ = await self.stats.add_counter(self.guild.id, setup, guild=self.guild, enabled=True)
        a, b = self.made
        await self.bot.db.run("UPDATE stat_counters SET checked_at = NULL WHERE guild_id = ?", (self.guild.id,))  # both due at the next tick
        release = asyncio.Event()
        renamed_a = a.edit

        async def held(**kwargs):  # Discord holds the first counter's rename back
            await release.wait()
            await renamed_a(**kwargs)
        a.edit = held
        FakeMember(self.guild, "newcomer 1")
        started = time.time()
        with mock.patch("captainguild.modules.statistics_tools.RENAME_SECONDS", 30.0):
            await self.stats.counters_due(started)  # the first update waits on the first counter
            await asyncio.sleep(0.05)
            await self.stats.counters_due(started + 60)  # a minute later, the server's update is still going, so this tick passes it by
            await asyncio.sleep(0.05)
            self.assertEqual(b.renames, [])
            edited = counters.clean_setup({"text": "People: {1}", "counters": [{"type": "members"}], "frequency": 6})
            await self.stats.edit_counter(self.guild, await self.stats._counter(self.guild.id, second), edited)  # renames b now
            self.assertEqual(len(b.renames), 1)
            release.set()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if not self.stats._tools_tasks:
                    break
        self.assertEqual(b.renames, [f"People: {len(self.guild.members)}"], "the update read b's new text, so it had nothing to rename")
        stored = (await self.stats._counter(self.guild.id, second))["renames"]
        self.assertEqual(len(stored), 1)
        self.assertGreaterEqual(stored[0], started)

    async def test_a_counter_everyone_sees_counts_only_channels_everyone_sees(self):
        everyone = self.guild.default_role
        hidden = FakeChannel(self.guild, "staff-room")
        public = FakeChannel(self.guild, "general-chat")
        hidden.permissions_for = lambda target: FakePermissions() if target is everyone else target.guild_permissions
        slot = tf.slot_of(time.time())
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, ?)",
                                       [(self.guild.id, slot - 1, hidden.id, self.mod.id, 500), (self.guild.id, slot - 1, public.id, self.member.id, 5)])
        day = {"amount": 1, "unit": "days"}
        top = {"type": "stat_top", "of": "member", "name": "username", "stat": {"kind": "messages", "period": day}}
        count = {"type": "stat_count", "stat": {"kind": "messages", "period": day}}
        setup = counters.clean_setup({"text": "Top chatter: {1} of {2}", "counters": [top, count], "frequency": 10})
        number, _ = await self.stats.add_counter(self.guild.id, setup, guild=self.guild, enabled=True)
        channel = self.made[0]
        self.assertEqual(channel.name, "Top chatter: member of 5")
        staff_only = editable(FakeChannel(self.guild, "staff stats", discord.ChannelType.voice))
        staff_only.permissions_for = hidden.permissions_for
        private, _ = await self.stats.add_counter(self.guild.id, setup, guild=self.guild, enabled=True, channel_id=staff_only.id)
        await self.bot.db.run("UPDATE stat_counters SET checked_at = NULL WHERE guild_id = ?", (self.guild.id,))
        await self.bot.message_db.run("UPDATE stat_messages SET count = count + 1 WHERE guild_id = ?", (self.guild.id,))
        await self.stats.counters_due(time.time())
        await asyncio.gather(*self.stats._tools_tasks)
        self.assertEqual(channel.name, "Top chatter: member of 6", "the minute's update reads only channels everyone can see")
        self.assertEqual(staff_only.name, "Top chatter: mod of 507", "a channel only staff see can show what staff see")

    async def test_every_status_counter_in_a_server_costs_one_read_an_hour(self):
        calls = []
        original = query.status_shares

        async def counted(db, guild_id, tz):
            calls.append(guild_id)
            return await original(db, guild_id, tz)
        setups = [counters.clean_setup({"text": f"S{number}: {{1}}", "counters": [{"type": "statuses", "statuses": ["online"]}], "frequency": 10}) for number in range(10)]
        now = time.time()
        with mock.patch.object(query, "status_shares", counted):
            place = await self.stats.counter_place(self.guild, now, setups)
            for setup in setups:
                await self.stats.counter_readings(setup, place)
            await self.stats.counter_place(self.guild, now + 1800, setups)
            self.assertEqual(calls, [self.guild.id])
            await self.stats.counter_place(self.guild, now + 3700, setups)
            self.assertEqual(len(calls), 2, "an hour later it reads the split again")

    async def test_a_status_channel_splits_discords_present_count_by_the_servers_history(self):
        hour = tf.slot_of(time.time()) // 4 * 4
        rows = [(self.guild.id, hour - week * 7 * 96 + quarter, 1, 20, 10, 10) for week in range(1, 5) for quarter in range(4)]
        await self.bot.message_db.many("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd) VALUES (?, ?, ?, ?, ?, ?)", rows)
        text = "🟢{1} ⛔{2} 🌙{3}"
        setup = counters.clean_setup({"text": text, "frequency": 10, "counters": [{"type": "statuses", "statuses": [status]} for status in ("online", "dnd", "idle")]})
        await self.stats.add_counter(self.guild.id, setup, guild=self.guild, enabled=True)
        self.assertEqual(self.made[0].name, "🟢20 ⛔10 🌙10", "40 present, split a half, a quarter and a quarter")

    async def test_a_bad_stored_row_is_passed_over(self):
        await self.bot.db.run("INSERT INTO stat_counters (guild_id, number, name, setup, enabled, channel_id, created_at, updated_at) VALUES (?, 1, 'x', 'not json', 1, 5, 1, 1)", (self.guild.id,))
        self.assertEqual(await self.stats._counter_rows(self.guild.id), [])
        await self.stats.tools_tick(time.time())
        self.assertIsNone(counter_from_row({"guild_id": 1, "number": 1, "setup": '{"text": "x"}', "name": ""}))


class Values(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.now = moment(2026, 11, 8, 12, 0).timestamp()
        self.slot = tf.slot_of(self.now)
        self.mdb = self.bot.message_db

    def place(self, **kwargs):
        return counters.Place(guild=self.guild, guild_id=self.guild.id, now=self.now, tz=UTC, db=self.mdb, settings=self.stats.cfg(self.guild.id), **kwargs)

    async def read(self, counter, **kwargs):
        return await counters.read(counters.clean_counter(counter), self.place(**kwargs))

    async def test_discord_counts(self):
        voice = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        voice.members = [self.member, self.me]
        self.guild.voice_channels = [voice]
        self.guild.stage_channels = []
        self.member.status, self.admin.status, self.me.status = "online", "dnd", "online"
        self.assertEqual((await self.read({"type": "members", "include": "bots"})).value, 1)
        self.assertEqual((await self.read({"type": "members", "include": "humans"})).value, len(self.guild.members) - 1)
        self.assertEqual((await self.read({"type": "channels", "channel_types": [2]})).value, 1)
        self.assertEqual((await self.read({"type": "roles", "managed": False, "unmanaged": True})).value, 4)
        self.assertEqual((await self.read({"type": "voice", "include": "humans"})).value, 1)
        self.assertEqual((await self.read({"type": "voice", "channel_mode": "block", "channels": [voice.id]})).value, 0)
        self.assertEqual((await self.read({"type": "statuses", "statuses": ["online"]}, presences=True)).value, 2)
        self.assertEqual((await self.read({"type": "role_members", "roles": [self.role_admin.id]})).value, 2)
        self.assertEqual((await self.read({"type": "role_members", "roles": [self.role_admin.id], "statuses": ["dnd"]}, presences=True)).value, 1)

    async def real_statuses(self, weeks, online, idle, dnd, *, present_only=False):
        """Rows for this hour of the week, `weeks` weeks back, one in each quarter of the hour: real statuses, or only Discord's present count."""
        hour = tf.slot_of(self.now) // 4 * 4
        rows = [(self.guild.id, hour - week * 7 * 96 + quarter, 0 if present_only else 1, 0 if present_only else online, 0 if present_only else idle,
                 0 if present_only else dnd, 1 if present_only else 0, online + idle + dnd if present_only else 0) for week in weeks for quarter in range(4)]
        await self.mdb.many("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, present_samples, present) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    async def split(self):
        return await query.status_shares(self.mdb, self.guild.id, UTC)

    async def test_without_statuses_the_present_count_splits_as_the_server_did_at_this_hour(self):
        await self.real_statuses(range(1, 5), 60, 30, 10)
        await self.real_statuses([0, 5], 999, 0, 0, present_only=True)  # rows with only Discord's count hold no split
        await self.mdb.run("UPDATE stat_status SET bot_online = 6, bot_idle = 3, bot_dnd = 1 WHERE samples > 0")  # bots count too, in the same proportions
        counts = (43_600, 1000)
        expected = {("online",): 600, ("idle",): 300, ("dnd",): 100, ("online", "idle"): 900, ("idle", "dnd"): 400, ("online", "offline"): 42_600 + 600}
        for statuses, value in expected.items():
            reading = await self.read({"type": "statuses", "statuses": list(statuses)}, present=counts, shares=await self.split())
            self.assertEqual((reading.value, reading.note), (value, counters.SPLIT), statuses)
        everyone_present = await self.read({"type": "statuses", "statuses": ["online", "idle", "dnd"]}, present=counts, shares=await self.split())
        self.assertEqual(everyone_present.value, 1000, "not offline stays Discord's present count itself")

    async def test_the_split_reads_only_the_latest_four_weeks_of_real_statuses(self):
        await self.real_statuses(range(1, 5), 60, 30, 10)
        await self.real_statuses(range(6, 10), 10, 10, 80)  # more than 28 days before the latest real statuses
        online = await self.read({"type": "statuses", "statuses": ["online"]}, present=(43_600, 1000), shares=await self.split())
        self.assertEqual(online.value, 600)
        await self.mdb.run("DELETE FROM stat_status")
        await self.real_statuses(range(20, 24), 50, 25, 25)  # imported history that ends months ago still serves, until real statuses come again
        await self.real_statuses(range(1, 20), 500, 0, 0, present_only=True)  # since then, only Discord's present count
        dnd = await self.read({"type": "statuses", "statuses": ["dnd"]}, present=(43_600, 1000), shares=await self.split())
        self.assertEqual(dnd.value, 250)

    async def test_with_no_real_statuses_online_shows_the_present_count_and_the_rest_keep_their_value(self):
        await self.real_statuses(range(1, 5), 700, 200, 100, present_only=True)
        self.assertEqual(await self.split(), {})
        online = await self.read({"type": "statuses", "statuses": ["online"]}, present=(43_600, 1000), shares=await self.split())
        self.assertEqual((online.value, online.note), (1000, counters.APPROXIMATE))
        idle = await self.read({"type": "statuses", "statuses": ["idle"]}, present=(43_600, 1000), shares=await self.split())
        self.assertTrue(idle.kept)

    async def test_with_each_members_status_the_counters_read_it_and_ignore_the_split(self):
        await self.real_statuses(range(1, 5), 60, 30, 10)
        self.member.status, self.admin.status, self.mod.status = "online", "idle", "idle"
        idle = await self.read({"type": "statuses", "statuses": ["idle"]}, presences=True, present=(43_600, 1000), shares=await self.split())
        self.assertEqual((idle.value, idle.note), (2, None))

    async def test_statuses_without_each_members_status(self):
        counts = (43_600, 5000)
        self.assertEqual((await self.read({"type": "statuses", "statuses": ["online", "idle", "dnd"]}, present=counts)).value, 5000)
        self.assertEqual((await self.read({"type": "statuses", "statuses": ["offline"]}, present=counts)).value, 38_600)
        approximate = await self.read({"type": "statuses", "statuses": ["online"]}, present=counts)
        self.assertEqual((approximate.value, approximate.note), (5000, counters.APPROXIMATE))
        kept = await self.read({"type": "statuses", "statuses": ["dnd"]}, present=counts)
        self.assertTrue(kept.kept and kept.value is None)
        self.assertEqual(kept.note, counters.NO_PRESENCE)
        self.assertTrue((await self.read({"type": "role_members", "roles": [self.role_admin.id], "statuses": ["online"]})).kept)

    async def seed(self):
        rows = [  # slot, channel, user, flags, count
            (self.slot - 2, 10, self.member.id, 0, 30), (self.slot - 2, 11, self.admin.id, 0, 10), (self.slot - 100, 10, self.member.id, 0, 5),
            (self.slot - 100, 10, self.admin.id, 0, 7), (self.slot - 2, 10, self.me.id, store.BOT, 99), (self.slot - 2, 10, 0, 0, 4),
        ]
        await self.mdb.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", [(self.guild.id, *row) for row in rows])
        await self.mdb.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, ?, 0, ?)",
                           [(self.guild.id, self.slot - 3, 20, self.member.id, store.TALKING, 5400), (self.guild.id, self.slot - 3, 20, self.admin.id, store.AFK, 1800)])

    async def test_stat_counts_averages_changes_and_tops(self):
        await self.seed()
        day = {"amount": 1, "unit": "days"}
        self.assertEqual((await self.read({"type": "stat_count", "stat": {"kind": "messages", "period": day}})).value, 44)
        self.assertEqual((await self.read({"type": "stat_count", "stat": {"kind": "messages", "period": day, "include": "both"}})).value, 143)
        self.assertEqual((await self.read({"type": "stat_count", "stat": {"kind": "messages", "period": day, "members": [self.admin.id], "member_mode": "block"}})).value, 34)
        self.assertEqual((await self.read({"type": "stat_count", "stat": {"kind": "messages", "period": day, "roles_allow": [self.role_member.id]}})).value, 30)
        self.assertEqual((await self.read({"type": "stat_count", "unit": "hours", "decimals": 1, "stat": {"kind": "voice", "period": day}})).value, 2.0)
        self.assertEqual((await self.read({"type": "stat_count", "unit": "minutes", "stat": {"kind": "voice", "period": day, "states": ["talking"]}})).value, 90)
        average = await self.read({"type": "stat_average", "per": "hour", "stat": {"kind": "messages", "period": day}})
        self.assertAlmostEqual(average.value, 44 / 24, places=2)
        # The day before held 12 messages, and this one 44.
        two_days = {"amount": 1, "unit": "days"}
        change = await self.read({"type": "stat_change", "as": "value", "stat": {"kind": "messages", "period": two_days}})
        self.assertEqual(change.value, 32)
        percent = await self.read({"type": "stat_change", "as": "percent", "stat": {"kind": "messages", "period": two_days}})
        self.assertAlmostEqual(percent.value, 32 / 12 * 100)
        top = await self.read({"type": "stat_top", "of": "member", "rank": 1, "name": "username", "stat": {"kind": "messages", "period": day}})
        self.assertEqual(top.words, "member")
        second = await self.read({"type": "stat_top", "of": "member", "rank": 2, "show_value": True, "stat": {"kind": "messages", "period": day}})
        self.assertEqual(second.words, "admin (10)")
        nobody = await self.read({"type": "stat_top", "of": "member", "rank": 5, "stat": {"kind": "messages", "period": day}})
        self.assertEqual(nobody.words, "Nobody yet")

    async def test_a_top_channel_shows_only_channels_everyone_sees(self):
        public = FakeChannel(self.guild, "general-chat")
        hidden = FakeChannel(self.guild, "staff-room")
        await self.mdb.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, ?)",
                           [(self.guild.id, self.slot - 1, hidden.id, self.admin.id, 50), (self.guild.id, self.slot - 1, public.id, self.member.id, 5)])
        visible = {public.id}
        found = await counters.read(counters.clean_counter({"type": "stat_top", "of": "channel", "rank": 1, "stat": {"kind": "messages", "period": {"amount": 1, "unit": "days"}}}),
                                    self.place(visible=lambda channel_id: channel_id in visible))
        self.assertEqual(found.words, "general-chat")

    async def test_the_top_three_in_one_update_read_the_rows_once(self):
        await self.mdb.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 10, ?, 0, ?)",
                           [(self.guild.id, self.slot - 1, person.id, count) for person, count in ((self.member, 9), (self.admin, 7), (self.owner, 5))])
        stat = {"kind": "messages", "include": "both", "period": {"amount": 1, "unit": "days"}}
        place = self.place()
        reads = []
        original = counters.stat_rows

        async def counted(*args, **kwargs):
            reads.append(args[-1])
            return await original(*args, **kwargs)
        with mock.patch.object(counters, "stat_rows", counted):
            found = [await counters.read(counters.clean_counter({"type": "stat_top", "of": "member", "rank": rank, "stat": stat}), place) for rank in (1, 2, 3)]
            total = await counters.read(counters.clean_counter({"type": "stat_count", "stat": stat}), place)
        self.assertEqual([reading.value for reading in found], [9, 7, 5])
        self.assertEqual(total.value, 21)
        self.assertEqual(reads, [["user_id"], []], "one grouped read for the three places, one for the total")

    async def test_member_count_change(self):
        await self.mdb.many("INSERT INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (?, ?, ?, ?, ?, 0, 0)",
                           [(self.guild.id, self.slot - 7 * 96 - 5, 1000, 990, 10), (self.guild.id, self.slot - 1, 1090, 1080, 10)])
        found = await self.read({"type": "stat_change", "plus": True, "stat": {"kind": "members", "period": {"amount": 7, "unit": "days"}}})
        self.assertEqual(found.value, 90)
        self.assertEqual(counters.shown(counters.clean_counter({"type": "stat_change", "plus": True, "stat": {"kind": "members", "period": {"amount": 7, "unit": "days"}}}), found), "+90")

    async def test_invite_joins_with_fake_reasons(self):
        joins = [  # user, at, code, inviter, facts, left
            (1, self.now - 3600, "abc", self.member.id, 0, None), (2, self.now - 3600, "abc", self.member.id, store.JOIN_NO_AVATAR, None),
            (3, self.now - 3600, "def", self.admin.id, 0, self.now - 60), (4, self.now - 90 * 86400, "abc", self.member.id, 0, None),
        ]
        await self.mdb.many("INSERT INTO stat_joins (guild_id, user_id, at, code, inviter_id, account_created, facts, left_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                           [(self.guild.id, *row) for row in joins])
        for user in (1, 2, 4):
            FakeMember(self.guild, f"joined {user}").id = user
        everything = await self.read({"type": "invites"})
        self.assertEqual(everything.value, 4)
        recent = await self.read({"type": "invites", "stat": {"period": {"amount": 1, "unit": "days"}}})
        self.assertEqual(recent.value, 3)
        kept = await self.read({"type": "invites", "stat": {"fake_block": {"types": ["no_avatar", "left"], "match": "any"}}})
        self.assertEqual(kept.value, 2)
        one_code = await self.read({"type": "invites", "stat": {"invite_mode": "allow", "invites": ["def"]}})
        self.assertEqual(one_code.value, 1)


class Commands(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.made = []

        async def create_voice_channel(name, *, overwrites=None, category=None, position=None, reason=None):
            channel = editable(FakeChannel(self.guild, name, discord.ChannelType.voice, category=category))
            channel.overwrites = overwrites or {}
            self.made.append(channel)
            return channel
        self.guild.create_voice_channel = create_voice_channel

    async def test_add_list_edit_off_on_copy_and_remove(self):
        ctx = self.context()
        await self.stats.counter_add.callback(self.stats, ctx, "people", text="People: {1}", frequency=30)
        self.assertIn("Counter 1 is", ctx.oks[0])
        self.assertEqual(self.made[0].name, f"People: {sum(1 for m in self.guild.members if not m.bot)}")
        await self.stats.counter.callback(self.stats, ctx)
        self.assertIn("People: {1}", ctx.sent[-1]["embed"].description)
        await self.stats.counter_edit.callback(self.stats, ctx, 1, text="Humans: {1}")
        self.assertTrue(self.made[0].name.startswith("Humans: "))
        await self.stats.counter_off_command.callback(self.stats, ctx, 1)
        self.assertFalse((await self.stats._counter(self.guild.id, 1))["enabled"])
        await self.stats.counter_on_command.callback(self.stats, ctx, 1)
        self.assertEqual(len(self.made), 2)
        await self.stats.counter_copy.callback(self.stats, ctx, 1)
        copy_row = await self.stats._counter(self.guild.id, 2)
        self.assertFalse(copy_row["enabled"])
        await self.stats.counter_remove.callback(self.stats, ctx, 2)
        self.assertIsNone(await self.stats._counter(self.guild.id, 2))
        self.assertEqual(ctx.fails, [])

    async def test_presets_that_need_more_say_so(self):
        ctx = self.context()
        await self.stats.counter_add.callback(self.stats, ctx, "countdown")
        await self.stats.counter_add.callback(self.stats, ctx, "youtube")
        await self.stats.counter_add.callback(self.stats, ctx, "in_roles", roles="Nonexistent")
        self.assertEqual(len(ctx.fails), 3)
        await self.stats.counter_add.callback(self.stats, ctx, "in_roles", roles=self.role_member.mention)
        self.assertEqual(len(ctx.oks), 1)

    async def test_a_role_picked_for_counters_runs_the_commands_and_nobody_else_does(self):
        keepers = self.guild.role("Counter keepers", 3)
        keeper = FakeMember(self.guild, "keeper", keepers)  # no Manage Server, no Manage Channels
        original = self.stats.cfg
        self.stats.cfg = lambda guild_id: {**original(guild_id), "area_roles": {"counters": [keepers.id]}}
        ctx = self.context(keeper)
        await self.stats.counter_add.callback(self.stats, ctx, "people", text="People: {1}")
        await self.stats.counter.callback(self.stats, ctx)
        self.assertEqual(ctx.fails, [])
        await self.stats.statrole.callback(self.stats, ctx)
        self.assertIn("stat roles", ctx.fails[-1], "the counters area doesn't open stat roles")
        for person in (self.member, FakeMember(self.guild, "channel mod", self.guild.role("Channel mods", 4, "manage_channels"))):
            refused = self.context(person)
            await self.stats.counter.callback(self.stats, refused)
            await self.stats.counter_add.callback(self.stats, refused, "people")
            self.assertEqual(len(refused.fails), 2, person.name)
            self.assertIn("role picked for counter channels", refused.fails[0])
        self.assertEqual(len(self.made), 1)

    async def test_discord_refusing_a_command_answers_in_a_sentence(self):
        ctx = self.context()

        async def refused(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Missing Permissions")
        self.guild.create_voice_channel = refused
        await self.stats.counter_add.callback(self.stats, ctx, "people")
        self.assertIn("couldn't make the channel", ctx.fails[-1])
        await self.stats.add_counter(self.guild.id, counters.clean_setup({"text": "M {1}", "counters": [{"type": "members"}], "frequency": 10}))
        await self.bot.db.run("UPDATE stat_counters SET enabled = 1, channel_id = ? WHERE number = 2", (self.channel.id,))

        async def failing(*args, **kwargs):
            raise discord.HTTPException(types.SimpleNamespace(status=503, reason="down"), "unavailable")
        self.stats.refresh_counter = failing
        await self.stats.counter_update.callback(self.stats, ctx, 2)
        self.assertIn("Discord didn't take that", ctx.fails[-1])

    def test_every_preset_reads(self):
        for key, (label, counter, text) in COUNTER_PRESETS.items():
            filled = dict(counter)
            if filled["type"] in counters.SOCIAL_TYPES:
                filled["account"] = "someone"
            if filled["type"] == "countdown":
                filled["end"] = 2_000_000_000
            if filled["type"] == "role_members":
                filled["roles"] = [111111111111111111]
            counters.clean_setup({"text": text, "counters": [filled], "frequency": 10})
            self.assertLessEqual(len(label), 100)

    def test_ends(self):
        now = 1_800_000_000.0
        self.assertEqual(parse_end("<t:1800003600:R>", UTC, now), 1_800_003_600)
        self.assertEqual(parse_end("3d", UTC, now), now + 3 * 86400)
        self.assertEqual(parse_end("2027-01-15 09:30", UTC, now), moment(2027, 1, 15, 9, 30).timestamp())
        for bad in ("", "yesterday", "<t:1:R>"):
            with self.assertRaises(Invalid):
                parse_end(bad, UTC, now)


class Routes(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)  # the page's channel checks read the fake server
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.made = []

        async def create_voice_channel(name, *, overwrites=None, category=None, position=None, reason=None):
            channel = editable(FakeChannel(self.guild, name, discord.ChannelType.voice, category=category))
            channel.overwrites = overwrites or {}
            self.made.append(channel)
            return channel
        self.guild.create_voice_channel = create_voice_channel

    async def test_the_page_adds_previews_and_lists(self):
        client = await self.client()
        body = {"name": "Members", "text": "Members: {1}", "counters": [{"type": "members"}], "frequency": 10, "enabled": True}
        preview = await client.post(self.path("/counters/preview"), json=body)
        self.assertEqual((await preview.json())["name"], f"Members: {len(self.guild.members)}")
        made = await client.post(self.path("/counters"), json=body)
        self.assertEqual(made.status, 200, await made.text())
        listed = await (await client.get(self.path("/counters"))).json()
        self.assertEqual([(row["number"], row["enabled"]) for row in listed["counters"]], [(1, True)])
        self.assertEqual(listed["frequencies"][0], {"value": 6, "label": "6 minutes"})
        bad = await client.post(self.path("/counters"), json={**body, "text": "Members"})
        self.assertEqual(bad.status, 400)

    async def test_an_export_imports_back_and_takes_over_its_channels_in_place(self):
        client = await self.client()
        self.bot.fetch_guild = mock.AsyncMock(return_value=types.SimpleNamespace(approximate_member_count=100, approximate_presence_count=40))
        existing = editable(FakeChannel(self.guild, "Users: 1", discord.ChannelType.voice))
        category = FakeChannel(self.guild, "Stats", discord.ChannelType.category)
        people = {"name": "People", "text": "Users: {1}", "counters": [{"type": "members", "include": "humans"}], "frequency": 30}
        made = await client.post(self.path("/counters"), json={**people, "enabled": True, "channel_id": str(existing.id)})
        self.assertEqual(made.status, 200, await made.text())
        await client.post(self.path("/counters"), json={"text": "Date: {1}", "counters": [{"type": "datetime", "format": "MMMM D"}], "frequency": 1440,
                                                        "enabled": False, "parent_id": str(category.id)})
        exported = await (await client.get(self.path("/counters/export"))).json()
        self.assertEqual([(entry["text"], entry["on"], entry["channel_id"]) for entry in exported], [("Users: {1}", True, str(existing.id)), ("Date: {1}", False, None)])
        self.assertEqual((exported[0]["name"], exported[0]["frequency"], exported[0]["counters"][0]["include"]), ("People", 30, "humans"))
        for number in (1, 2):
            await client.delete(self.path(f"/counters/{number}?keep_channel=1"))
        self.assertEqual(existing.name, f"Users: {sum(1 for member in self.guild.members if not member.bot)}")
        checked = await (await client.post(self.path("/counters/import"), json={"data": json.dumps(exported)})).json()
        self.assertEqual([entry["action"] for entry in checked["setups"]], ["adopt", "off"])
        self.assertEqual(await self.bot.db.scalar("SELECT COUNT(*) FROM stat_counters"), 0, "checking changes nothing")
        applied = await client.post(self.path("/counters/import"), json={"data": exported, "apply": True})
        self.assertEqual(applied.status, 200, await applied.text())
        rows = await self.stats._counter_rows(self.guild.id)
        self.assertEqual([(row["setup"]["text"], row["enabled"], row["channel_id"]) for row in rows],
                         [("Users: {1}", True, existing.id), ("Date: {1}", False, None)])
        self.assertEqual(self.made, [], "the channel was taken over where it is")
        again = await (await client.post(self.path("/counters/import"), json={"data": exported})).json()
        self.assertEqual(again["setups"][0]["action"], "already")
        refused = await client.post(self.path("/counters/import"), json={"data": "not json"})
        self.assertEqual(refused.status, 400)

    async def test_an_import_leaves_out_a_channel_the_importer_cant_see(self):
        client = await self.client()
        hidden = editable(FakeChannel(self.guild, "staff-voice", discord.ChannelType.voice))
        seen = editable(FakeChannel(self.guild, "lounge", discord.ChannelType.voice))
        setups = [{"text": "Users: {1}", "counters": [{"type": "members"}], "frequency": 10, "on": True, "channel_id": str(hidden.id)},
                  {"text": "Staff: {1}", "counters": [{"type": "stat_count", "stat": {"kind": "messages", "channel_mode": "allow", "channels": [str(hidden.id)]}}],
                   "frequency": 10, "on": False},
                  {"text": "Users: {1}", "counters": [{"type": "members"}], "frequency": 10, "on": True, "channel_id": str(seen.id)}]
        with mock.patch("captainguild.web.api.Req.can_see", lambda req, channel_id: channel_id != hidden.id):
            answer = await client.post(self.path("/counters/import"), json={"data": setups, "apply": True})
        self.assertEqual(answer.status, 200, await answer.text())
        report = (await answer.json())["setups"]
        self.assertEqual([entry["action"] for entry in report], ["skip", "skip", "adopt"])
        self.assertTrue(all(entry["problems"] for entry in report[:2]))
        self.assertIsNone(report[0]["channel_id"], "the report doesn't name a channel they can't see")
        self.assertEqual(hidden.name, "staff-voice")
        self.assertNotIn(self.me, hidden.overwrites)
        self.assertTrue(seen.name.startswith("Users: "), "positive control: a channel they can see is taken over")
        self.assertEqual(await self.bot.db.scalar("SELECT COUNT(*) FROM stat_counters"), 1)

    async def test_the_page_switches_a_counter_back_on_after_its_category_is_gone(self):
        client = await self.client()
        category = FakeChannel(self.guild, "Stats", discord.ChannelType.category)
        setup = counters.clean_setup({"text": "Members: {1}", "counters": [{"type": "members"}], "frequency": 10})
        number, _ = await self.stats.add_counter(self.guild.id, setup, guild=self.guild, enabled=True, parent_id=category.id)
        await self.stats.counter_off(self.guild, await self.stats._counter(self.guild.id, number))
        await category.delete()
        listed = await (await client.get(self.path("/counters"))).json()
        answer = await client.post(self.path(f"/counters/{number}/on"), json={"parent_id": listed["counters"][0]["parent_id"]})  # what the page sends
        self.assertEqual(answer.status, 200, await answer.text())
        self.assertIsNone(self.made[-1].category)

    async def test_quick_setup_answers_discords_refusal_in_a_sentence(self):
        client = await self.client()
        for error in (discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Missing Permissions"),
                      discord.HTTPException(types.SimpleNamespace(status=503, reason="down"), "unavailable")):
            async def refused(*args, _error=error, **kwargs):
                raise _error
            self.guild.create_category = refused
            answer = await client.post(self.path("/counters/quick"))
            self.assertEqual(answer.status, 409, await answer.text())
            self.assertIn("couldn't make the", (await answer.json())["error"])
        del self.guild.create_category
        self.guild.create_voice_channel = type(self.guild).create_voice_channel.__get__(self.guild)  # the fake server's own, as the access tests use it
        made = await client.post(self.path("/counters/quick"))
        self.assertEqual(made.status, 200, await made.text())
        self.assertEqual(len((await made.json())["made"]), 3)

    async def test_a_recount_or_merge_applying_holds_counter_updates(self):
        client = await self.client()
        setup = counters.clean_setup({"text": "Members: {1}", "counters": [{"type": "members"}], "frequency": 10})
        number, _ = await self.stats.add_counter(self.guild.id, setup, guild=self.guild, enabled=True)
        channel = self.made[0]
        FakeMember(self.guild, "newcomer")
        await self.bot.db.run("UPDATE stat_counters SET checked_at = NULL")
        self.stats.stats_applying = lambda guild_id: True
        await self.stats.counters_due(time.time())
        self.assertEqual((self.stats._tools_tasks, channel.renames), (set(), []))
        answer = await client.post(self.path(f"/counters/{number}/update"))
        self.assertEqual(answer.status, 409)
        self.assertIn("recount or merge", (await answer.json())["error"])
        del self.stats.stats_applying
        await self.stats.counters_due(time.time())
        await asyncio.gather(*self.stats._tools_tasks)
        self.assertEqual(channel.renames, [f"Members: {len(self.guild.members)}"], "the next update after it catches up")

    async def test_the_preview_needs_manage_channels(self):
        client = await self.client()
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 3, "manage_guild")]
        answer = await client.post(self.path("/counters/preview"), json={"text": "{1}", "frequency": 10, "counters": [{"type": "members"}]})
        self.assertEqual(answer.status, 403)

    async def test_a_preview_counts_what_its_channel_would_show(self):
        client = await self.client()
        everyone = self.guild.default_role
        everyone.permissions = FakePermissions("view_channel")
        hidden = FakeChannel(self.guild, "staff-room")
        hidden.permissions_for = lambda target: FakePermissions() if target is everyone else target.guild_permissions
        staff_category = FakeChannel(self.guild, "Staff", discord.ChannelType.category)
        staff_category.permissions_for = hidden.permissions_for
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, ?)",
                                       [(self.guild.id, tf.slot_of(time.time()) - 1, hidden.id, self.mod.id, 500)])
        body = {"text": "Top: {1}", "frequency": 10, "counters": [{"type": "stat_top", "of": "member", "name": "username", "stat": {"kind": "messages", "period": {"amount": 1, "unit": "days"}}}]}
        self.assertEqual((await (await client.post(self.path("/counters/preview"), json=body)).json())["name"], "Top: Nobody yet")
        in_staff = await client.post(self.path("/counters/preview"), json={**body, "parent_id": str(staff_category.id)})
        self.assertEqual((await in_staff.json())["name"], "Top: mod", "a channel in a category only staff see shows what staff see")

    async def test_counter_changes_need_manage_channels(self):
        client = await self.client()
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 3, "manage_guild")]
        made = await client.post(self.path("/counters"), json={"text": "Members: {1}", "counters": [{"type": "members"}], "frequency": 10})
        self.assertEqual(made.status, 403)
        self.assertIn("Manage Channels", (await made.json())["error"])

    async def test_a_hidden_channel_cant_be_counted(self):
        client = await self.client()
        hidden = FakeChannel(self.guild, "secret")
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 3, "manage_guild", "manage_channels")]
        with mock.patch("captainguild.web.api.Req.can_see", lambda req, channel_id: channel_id != hidden.id):
            made = await client.post(self.path("/counters"), json={"text": "N {1}", "frequency": 10, "enabled": False,
                                                                   "counters": [{"type": "stat_count", "stat": {"kind": "messages", "channel_mode": "allow", "channels": [str(hidden.id)]}}]})
        self.assertEqual(made.status, 400)
        self.assertIn("can't see", (await made.json())["error"])


if __name__ == "__main__":
    unittest.main()
