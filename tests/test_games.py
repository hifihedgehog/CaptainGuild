"""The World of Warcraft timers, the game profile lookups, and the small game commands. Nothing here reaches the network."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from unittest import mock

import discord

from captainguild.core.ops import DiscordOps
from captainguild.core.outside import OutsideError
from captainguild.core.shortcuts import SHORTCUTS, TAG_FIRST_SHORTCUTS, expand_line, named_command
from captainguild.modules import load_all
from captainguild.modules.config import Config
from captainguild.modules.fun import Fun
from captainguild.modules.games import (
    CLASSES,
    INCURSION,
    INVASION,
    RESET_HOURS,
    SPECS,
    Games,
    clean,
    draw_snow_globe,
    next_reset,
)
from captainguild.modules.utilities import Utilities
from tests.fake_outside import FakeOutside
from tests.fakes import BotTest
from tests.gateway import GatewayTest

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3600.0
STATS_URL = "https://fortnite-api.com/v2/stats/br/v2"


def utc(*parts: int) -> float:
    return datetime.datetime(*parts, tzinfo=datetime.timezone.utc).timestamp()


class ScheduleTest(BotTest):
    async def test_the_anchors_are_the_ones_written_down(self):
        self.assertEqual(INVASION.anchors["EU"], utc(2017, 5, 26, 22, 30))
        self.assertEqual(INVASION.anchors["NA"], utc(2017, 5, 26, 12))
        self.assertEqual(INCURSION.anchors["EU"], utc(2019, 1, 20, 16))
        self.assertEqual(INCURSION.anchors["NA"], utc(2019, 1, 21, 1))

    async def test_an_invasion_runs_six_hours_in_each_eighteen_and_a_half(self):
        start = utc(2017, 5, 26, 12)
        self.assertEqual(INVASION.state("NA", start), (True, start + 6 * HOUR))
        self.assertEqual(INVASION.state("NA", start + 6 * HOUR - 1), (True, start + 6 * HOUR))
        self.assertEqual(INVASION.state("NA", start + 6 * HOUR), (False, start + 18.5 * HOUR))
        self.assertEqual(INVASION.state("NA", start + 18.5 * HOUR), (True, start + 24.5 * HOUR))

    async def test_an_incursion_runs_seven_hours_in_each_nineteen(self):
        start = utc(2019, 1, 20, 16)
        self.assertEqual(INCURSION.state("EU", start + 3 * HOUR), (True, start + 7 * HOUR))
        self.assertEqual(INCURSION.state("EU", start + 7 * HOUR), (False, start + 19 * HOUR))

    async def test_the_cycle_holds_years_later_and_before_the_anchor(self):
        start = INCURSION.anchors["NA"]
        cycles = 2000
        self.assertEqual(INCURSION.state("NA", start + cycles * 19 * HOUR + 60), (True, start + cycles * 19 * HOUR + 7 * HOUR))
        self.assertEqual(INCURSION.state("NA", start - 60), (False, start))

    async def test_next_start_skips_a_window_already_open(self):
        start = INVASION.anchors["EU"]
        self.assertEqual(INVASION.next_start("EU", start + 60), start + 18.5 * HOUR)
        self.assertEqual(INVASION.next_start("EU", start - 60), start)

    async def test_reset_comes_at_the_written_hour(self):
        self.assertEqual(RESET_HOURS, {"EU": 4, "NA": 15})
        self.assertEqual(next_reset(15, utc(2026, 11, 6, 14, 59)), utc(2026, 11, 6, 15))
        self.assertEqual(next_reset(15, utc(2026, 11, 6, 15)), utc(2026, 11, 7, 15))
        self.assertEqual(next_reset(4, utc(2026, 11, 6, 20)), utc(2026, 11, 7, 4))

    async def test_each_spec_names_a_listed_class(self):
        self.assertEqual(len(SPECS), len(set(SPECS)))
        for spec in SPECS:
            self.assertTrue(any(spec.endswith(name) for name in CLASSES), spec)


class CleanTest(BotTest):
    async def test_remote_text_is_flat_short_and_escaped(self):
        self.assertEqual(clean("**big**\n\tname", 100), "\\*\\*big\\*\\* name")
        self.assertLessEqual(len(clean("x" * 500, 40)), 40)
        self.assertEqual(clean(None, 10), "")
        self.assertEqual(clean(7, 10), "")
        self.assertEqual(clean("a\u200bb\x00c", 10), "abc")


class GamesCommandTest(BotTest):
    modules = load_all()

    STATS = {"status": 200, "data": {
        "account": {"id": "abc", "name": "@everyone **Player**"},
        "battlePass": {"level": 42, "progress": 10},
        "stats": {"all": {"overall": {"wins": 1234, "kills": 5678, "kd": 1.5, "matches": 2000, "winRate": 61.7, "minutesPlayed": 600}}},
    }}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.games = self.cog(Games)
        self.web = FakeOutside()
        self.games.outside = self.web
        self.ctx = self.context(self.member)

    async def run_command(self, line: str, *args, **kwargs) -> None:
        command = self.bot.get_command(line)
        await command.callback(command.cog, self.ctx, *args, **kwargs)

    def embed(self) -> discord.Embed:
        return self.ctx.sent[-1]["embed"]

    def assert_no_pings(self) -> None:
        mentions = self.ctx.sent[-1]["allowed_mentions"]
        self.assertFalse(mentions.everyone or mentions.users or mentions.roles)

    async def test_the_timers_show_both_regions(self):
        for line in ("games incursion", "games invasion", "games reset"):
            await self.run_command(line)
            text = self.embed().description
            self.assertIn("EU", text)
            self.assertIn("NA", text)
            self.assertIn("<t:", text)

    async def test_the_timer_says_which_state_it_is_in(self):
        start = INVASION.anchors["NA"]
        with mock.patch("captainguild.modules.games.time.time", return_value=start + 60):
            await self.run_command("games invasion")
        na = next(line for line in self.embed().description.splitlines() if "NA" in line)
        self.assertIn("ends", na)
        self.assertIn(f"<t:{int(start + 6 * HOUR)}:R>", na)

    async def test_the_pickers_pick_from_their_lists(self):
        await self.run_command("games pickmyclass")
        self.assertIn(self.ctx.sent[-1]["content"], CLASSES)
        await self.run_command("games pickmyspec")
        self.assertIn(self.ctx.sent[-1]["content"], SPECS)

    async def test_the_names_people_type(self):
        for typed, command in (
            ("incursion", "games incursion"), ("assault", "games incursion"), ("assaults", "games incursion"), ("invasion", "games invasion"), ("invasions", "games invasion"),
            ("pickmyclass", "games pickmyclass"), ("pickmyspec", "games pickmyspec"), ("reset", "games reset"), ("whenisthereset", "games reset"),
            ("fortnite playstation", "games fortnite playstation"), ("fn xbox", "games fortnite xbox"), ("roblox", "games roblox"), ("roblox play", "games roblox"), ("shake", "fun shake"), ("wiki", "misc wiki"),
        ):
            found = named_command(self.bot, typed)
            self.assertIsNotNone(found, typed)
            self.assertEqual(found[0].qualified_name, command, typed)

    async def test_a_bare_player_name_is_a_pc_lookup(self):
        self.assertEqual(expand_line(self.bot, "fn Some Player"), "games fortnite pc Some Player")
        self.assertEqual(expand_line(self.bot, "fn xbox Some Player"), "games fortnite xbox Some Player")

    async def test_reset_and_wiki_leave_room_for_a_tag(self):
        for name, target in (("reset", "games reset"), ("wiki", "misc wiki")):
            self.assertIsNone(self.bot.all_commands.get(name))
            self.assertNotIn(name, SHORTCUTS)
            self.assertEqual(TAG_FIRST_SHORTCUTS[name], target)

    async def test_every_lookup_has_a_cooldown(self):
        for line in ("games fortnite pc", "games fortnite xbox", "games fortnite playstation", "games roblox", "fun shake"):
            self.assertIsNotNone(self.bot.get_command(line)._buckets._cooldown, line)

    # ---- Fortnite ----

    async def test_no_key_says_so_and_calls_nobody(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FORTNITE_API_KEY", None)
            await self.run_command("games fortnite pc", name="Someone")
        self.assertEqual(self.ctx.fails, ["The bot's host hasn't added a Fortnite API key."])
        self.assertEqual(self.web.calls, [])

    async def fortnite(self, line: str = "games fortnite pc", name: str = "Someone") -> None:
        with mock.patch.dict(os.environ, {"FORTNITE_API_KEY": "test-key"}):
            await self.run_command(line, name=name)

    async def test_the_lookup_sends_the_key_and_the_platform(self):
        self.web.answer(STATS_URL, self.STATS)
        for line, account in (("games fortnite pc", "epic"), ("games fortnite playstation", "psn"), ("games fortnite xbox", "xbl")):
            await self.fortnite(line)
            call = self.web.calls[-1]
            self.assertEqual(call["headers"], {"Authorization": "test-key"})
            self.assertEqual(call["params"], {"name": "Someone", "accountType": account, "timeWindow": "lifetime"})

    async def test_the_stats_land_in_their_fields(self):
        self.web.answer(STATS_URL, self.STATS)
        await self.fortnite()
        embed = self.embed()
        self.assertEqual({f.name: f.value for f in embed.fields}, {"Matches": "2,000", "Wins": "1,234", "Win rate": "61.7%", "Kills": "5,678", "K/D": "1.50", "Hours played": "10", "Battle pass": "42"})
        self.assertIn("\\*\\*Player\\*\\*", embed.title)
        self.assert_no_pings()

    async def test_wrong_types_never_reach_the_page(self):
        hostile = {"data": {"account": {"name": ["x"]}, "stats": {"all": {"overall": {"wins": True, "kills": "lots", "kd": None, "matches": {}, "winRate": [], "minutesPlayed": "9"}}}}}
        self.web.answer(STATS_URL, hostile)
        await self.fortnite()
        self.assertEqual(self.ctx.fails, [])
        self.assertEqual(self.embed().title, "Someone")
        self.assertEqual(self.embed().fields, [])

    async def test_each_refusal_has_its_own_sentence(self):
        for status, text in ((404, "no player"), (403, "private"), (401, "refused the key"), (500, "didn't answer"), (429, "didn't answer")):
            self.ctx.fails.clear()
            self.web.answer(STATS_URL, {}, status=status)
            await self.fortnite()
            self.assertIn(text, self.ctx.fails[0], status)

    async def test_a_service_that_is_down_is_a_sentence(self):
        self.web.answer(STATS_URL, OutsideError("down"))
        await self.fortnite()
        self.assertIn("didn't answer", self.ctx.fails[0])

    async def test_no_stats_is_a_sentence(self):
        self.web.answer(STATS_URL, {"data": {"account": {"name": "x"}, "stats": None}})
        await self.fortnite()
        self.assertIn("no stats", self.ctx.fails[0])

    async def test_a_very_long_name_is_refused_before_any_call(self):
        await self.fortnite(name="x" * 100)
        self.assertTrue(self.ctx.fails)
        self.assertEqual(self.web.calls, [])

    async def test_the_key_stays_out_of_what_a_person_sees(self):
        self.web.answer(STATS_URL, {}, status=401)
        await self.fortnite()
        self.assertNotIn("test-key", " ".join(self.ctx.fails))

    # ---- Roblox ----

    def roblox(self, *, description="An **about** line", name="Some_User", display="@everyone [x](https://a.example)", avatar="https://tr.rbxcdn.com/face.png"):
        self.web.answer("https://users.roblox.com/v1/usernames/users", {"data": [{"id": 261, "name": name, "displayName": display, "requestedUsername": "some_user", "hasVerifiedBadge": False}]})
        self.web.answer("https://users.roblox.com/v1/users/261", {"id": 261, "name": name, "displayName": display, "description": description, "created": "2006-02-27T21:06:40.3Z", "isBanned": False})
        self.web.answer("https://friends.roblox.com/v1/users/261/friends/count", {"count": 12})
        self.web.answer("https://friends.roblox.com/v1/users/261/followers/count", {"count": 3400})
        self.web.answer("https://friends.roblox.com/v1/users/261/followings/count", {"count": 5})
        self.web.answer("https://thumbnails.roblox.com/v1/users/avatar-headshot", {"data": [{"targetId": 261, "state": "Completed", "imageUrl": avatar}]})

    async def test_the_profile_lookup(self):
        self.roblox()
        await self.run_command("games roblox", name="some_user")
        sent = json.loads(self.web.called("https://users.roblox.com/v1/usernames/users")[0]["data"])
        self.assertEqual(sent, {"usernames": ["some_user"], "excludeBannedUsers": True})
        embed = self.embed()
        self.assertEqual(embed.url, "https://www.roblox.com/users/261/profile")
        self.assertEqual({f.name: f.value for f in embed.fields}, {"Friends": "12", "Followers": "3,400", "Following": "5", "Joined": f"<t:{int(utc(2006, 2, 27, 21, 6, 40))}:D>"})
        self.assertEqual(embed.thumbnail.url, "https://tr.rbxcdn.com/face.png")
        self.assert_no_pings()

    async def test_remote_text_is_escaped_and_short(self):
        self.roblox(description="**bold**\n" + "y" * 5000)
        await self.run_command("games roblox", name="some_user")
        embed = self.embed()
        self.assertIn("\\*\\*bold\\*\\*", embed.description)
        self.assertLessEqual(len(embed.description), 400)
        self.assertNotIn("\n", embed.description)
        self.assertIn("\\[x", embed.title)

    async def test_an_avatar_from_elsewhere_is_left_off(self):
        self.roblox(avatar="http://169.254.169.254/x")
        await self.run_command("games roblox", name="some_user")
        self.assertIsNone(self.embed().thumbnail.url)

    async def test_a_lost_count_costs_only_that_field(self):
        self.roblox()
        self.web.answer("https://friends.roblox.com/v1/users/261/followers/count", OutsideError("down"))
        self.web.answer("https://thumbnails.roblox.com/v1/users/avatar-headshot", OutsideError("down"))
        await self.run_command("games roblox", name="some_user")
        self.assertNotIn("Followers", [f.name for f in self.embed().fields])
        self.assertIn("Friends", [f.name for f in self.embed().fields])

    async def test_nobody_by_that_name(self):
        self.web.answer("https://users.roblox.com/v1/usernames/users", {"data": []})
        await self.run_command("games roblox", name="some_user")
        self.assertIn("no Roblox user", self.ctx.fails[0])

    async def test_an_id_that_isnt_a_number_stops_there(self):
        self.web.answer("https://users.roblox.com/v1/usernames/users", {"data": [{"id": "../../x"}]})
        await self.run_command("games roblox", name="some_user")
        self.assertEqual(len(self.web.calls), 1)
        self.assertTrue(self.ctx.fails)

    async def test_a_name_roblox_cant_hold_is_refused_before_any_call(self):
        await self.run_command("games roblox", name="not a name!")
        self.assertTrue(self.ctx.fails)
        self.assertEqual(self.web.calls, [])

    async def test_roblox_down(self):
        await self.run_command("games roblox", name="some_user")
        self.assertIn("didn't answer", self.ctx.fails[0])

    # ---- games play and list ----

    async def test_games_list_names_each_game_command_that_exists(self):
        await self.run_command("games list")
        text = self.ctx.sent[-1]["content"]
        for name in ("toproll", "leaderboard", "fortnite", "roblox", "incursion"):
            self.assertIn(f"/games {name}", text)
            self.assertIsNotNone(self.bot.get_command(f"games {name}"))

    async def test_games_play_starts_toproll(self):
        toproll = self.bot.get_command("games toproll")
        started = mock.AsyncMock()
        with mock.patch.object(toproll, "_callback", started):
            await self.run_command("games play")
        started.assert_awaited_once()

    # ---- shake and wiki ----

    async def test_shake_posts_a_picture_in_an_embed(self):
        await self.run_command("fun shake")
        sent = self.ctx.sent[-1]
        self.assertEqual(sent["embed"].image.url, "attachment://snow-globe.png")
        self.assertEqual(sent["file"].filename, "snow-globe.png")

    async def test_each_shake_scatters_the_snow(self):
        self.assertTrue(draw_snow_globe(1).startswith(b"\x89PNG"))
        self.assertNotEqual(draw_snow_globe(1), draw_snow_globe(2))

    async def test_wiki_links_this_bots_docs(self):
        await self.run_command("misc wiki")
        self.assertEqual(self.ctx.sent[-1]["content"], "https://bot.example/docs")

    async def test_wiki_with_no_public_address_says_so(self):
        self.bot.public_url = ""
        await self.run_command("misc wiki")
        self.assertEqual(self.ctx.sent, [])
        self.assertIn("no public address", self.ctx.fails[0])

    # ---- reminder incursion ----

    async def test_a_reminder_for_the_next_incursion(self):
        start = INCURSION.anchors["EU"]
        with mock.patch("captainguild.modules.utilities.time.time", return_value=start + 60):
            await self.run_command("reminder incursion", "eu")
        row = await self.db.one("SELECT * FROM reminders WHERE user_id = ?", (self.member.id,))
        self.assertEqual(row["due_at"], start + 19 * HOUR)
        self.assertIn("EU", row["about"])
        self.assertTrue(self.ctx.oks)


class PublishedTest(BotTest):
    async def test_the_key_is_optional_and_listed(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^# Optional\..*\nFORTNITE_API_KEY=$")

    async def test_the_privacy_page_names_what_leaves_the_machine(self):
        page = (ROOT / "captainguild" / "web" / "static" / "privacy.html").read_text(encoding="utf-8")
        self.assertIn("Fortnite-API", page)
        self.assertIn("Roblox", page)


class TypedGamesTest(GatewayTest):
    modules = [Config, Fun, Games, Utilities]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.net.keep_history = True

    def posted(self) -> str:
        return " ".join(json.dumps({key: m.get(key) for key in ("content", "embeds")}) for m in self.net.posted)

    async def typed(self, content: str) -> None:
        self.dispatch_message(user_id=self.member_id, content=content)
        await self.settle(0.4)
        self.assert_no_errors(content)

    async def test_the_short_names_reach_their_commands(self):
        for line, words in (("!assault", "Incursions"), ("!invasions", "Invasions"), ("!whenisthereset", "Next reset"), ("!reset", "Next reset"), ("!wiki", "/docs"), ("!games list", "/games roblox")):
            await self.typed(line)
            self.assertIn(words, self.posted(), line)

    async def test_a_bare_name_on_fortnite_reaches_the_lookup(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FORTNITE_API_KEY", None)
            await self.typed("!fn Some Player")
        self.assertIn("hasn't added a Fortnite API key", self.posted())

    async def test_a_word_nothing_reads_is_refused(self):
        await self.typed("!pickmyclass please")
        self.assertFalse(any(name in self.posted() for name in CLASSES))
        await self.typed("!pickmyclass")
        self.assertTrue(any(name in self.posted() for name in CLASSES))
