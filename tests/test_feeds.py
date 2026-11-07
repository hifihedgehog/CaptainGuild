import datetime
import json
import time
import unittest
import zoneinfo
from unittest import mock

import discord

from captainguild.core.ops import DiscordOps
from captainguild.modules.feeds import Feeds, add_months, clean_zone, next_run, parse_when, repeat_text
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, _FakeResponse, new_id

try:
    zoneinfo.ZoneInfo("America/New_York")
    HAS_ZONES = True
except zoneinfo.ZoneInfoNotFoundError:
    HAS_ZONES = False  # Windows without the tzdata package. Production runs on Linux, which has the system's zone files.


def utc(*parts: int) -> float:
    return datetime.datetime(*parts, tzinfo=datetime.timezone.utc).timestamp()


class ScheduleMathTest(unittest.TestCase):
    def test_months_stop_at_the_end_of_a_short_month(self):
        start = datetime.datetime(2027, 1, 31, 12, tzinfo=datetime.timezone.utc)
        self.assertEqual(add_months(start, 1).day, 28)
        self.assertEqual(add_months(start, 2).day, 31)
        self.assertEqual((add_months(start, 12).year, add_months(start, 13).month), (2028, 2))

    def test_next_run(self):
        start = utc(2027, 1, 1, 18)
        self.assertIsNone(next_run(start, (0, 0, 0, 0), start))
        self.assertEqual(next_run(start, (0, 1, 0, 0), start), utc(2027, 1, 2, 18))
        self.assertEqual(next_run(start, (0, 0, 2, 30), start), utc(2027, 1, 1, 20, 30))
        self.assertEqual(next_run(start, (1, 0, 0, 0), start), utc(2027, 2, 1, 18))

    def test_a_long_outage_skips_ahead_to_one_future_run(self):
        start = utc(2027, 1, 1, 18)
        self.assertEqual(next_run(start, (0, 1, 0, 0), utc(2027, 3, 15, 19)), utc(2027, 3, 16, 18))

    @unittest.skipUnless(HAS_ZONES, "needs time zone data")
    def test_daily_runs_keep_the_local_hour_across_a_clock_change(self):
        new_york = zoneinfo.ZoneInfo("America/New_York")
        start = datetime.datetime(2027, 3, 13, 18, tzinfo=new_york).timestamp()  # clocks go forward on March 14
        after = next_run(start, (0, 1, 0, 0), start, "America/New_York")
        self.assertEqual(datetime.datetime.fromtimestamp(after, new_york).hour, 18)
        self.assertEqual(after - start, 23 * 3600)

    def test_an_unknown_zone_falls_back_to_utc(self):
        start = utc(2027, 1, 1, 18)
        self.assertEqual(next_run(start, (0, 1, 0, 0), start, "Mars/Olympus"), utc(2027, 1, 2, 18))
        self.assertIsNone(clean_zone("Mars/Olympus"))
        self.assertIsNone(clean_zone("../../etc/passwd"))
        self.assertIsNone(clean_zone(5))

    def test_when_reads_a_length_or_a_date(self):
        now = 1_000_000.0
        self.assertEqual(parse_when("18h", now), now + 64800)
        self.assertEqual(parse_when("2027-01-02 03:04", now), utc(2027, 1, 2, 3, 4))
        self.assertIsNone(parse_when("sometime soon", now))

    def test_repeat_text(self):
        self.assertEqual(repeat_text((1, 2, 0, 1)), "1 month, 2 days, 1 minute")
        self.assertEqual(repeat_text((0, 0, 0, 0)), "never")


class FeedsCase(BotTest):
    modules = [Feeds]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.feeds = self.cog(Feeds)
        self.edits: list[tuple] = []
        self.pinged = self.guild.role("News", 3)
        self.watch_role(self.pinged)

    def watch_role(self, role) -> None:
        async def edit(**kwargs):
            self.edits.append(kwargs.get("mentionable"))
            role.mentionable = kwargs["mentionable"]
        role.edit = edit

    def bot_without_mention_everyone(self) -> None:
        self.role_bot.permissions = type(self.role_bot.permissions)("manage_roles", "send_messages")

    async def make_feed(self, name: str = "news", channel=None) -> None:
        await self.feeds.feeds_create.callback(self.feeds, self.context(channel=channel), name, self.pinged)


class FeedCommandTest(FeedsCase):
    async def test_create_list_move_delete(self):
        ctx = self.context()
        await self.make_feed()
        other = FakeChannel(self.guild, "announcements")
        await self.feeds.feeds_move.callback(self.feeds, ctx, "NEWS", other)
        row = await self.db.one("SELECT * FROM feeds WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual((row["name"], row["role_id"], row["channel_id"]), ("news", self.pinged.id, other.id))
        await self.feeds.feeds.callback(self.feeds, ctx)
        self.assertIn("news", ctx.sent[0]["embed"].description)
        self.assertFalse(ctx.sent[0]["allowed_mentions"].roles)
        await self.feeds.feeds_delete.callback(self.feeds, ctx, "news")
        self.assertIsNone(await self.db.one("SELECT * FROM feeds"))
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "feed.delete")

    async def test_create_refusals(self):
        ctx = self.context()
        await self.make_feed()
        await self.feeds.feeds_create.callback(self.feeds, ctx, "news", self.pinged)
        await self.feeds.feeds_create.callback(self.feeds, ctx, "x" * 51, self.pinged)
        await self.feeds.feeds_create.callback(self.feeds, ctx, "   ", self.pinged)
        await self.feeds.feeds_create.callback(self.feeds, ctx, "all", self.guild.default_role)
        self.assertEqual(len(ctx.fails), 4)

    async def test_unknown_feed_names_are_a_sentence(self):
        ctx = self.context()
        await self.feeds.feeds_announce.callback(self.feeds, ctx, "nope", content="hi")
        await self.feeds.feeds_move.callback(self.feeds, ctx, "nope", self.channel)
        await self.feeds.feeds_delete.callback(self.feeds, ctx, "*nope*")
        self.assertEqual(len(ctx.fails), 3)
        self.assertIn("\\*nope\\*", ctx.fails[2])

    async def test_clear_only_touches_this_server(self):
        await self.make_feed("one")
        await self.make_feed("two")
        await self.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_at) VALUES (?, 'theirs', 1, 1, 1)", (self.guild.id + 1,))
        ctx = self.context()
        await self.feeds.feeds_clear.callback(self.feeds, ctx)
        await ctx.sent[-1]["view"].clear()
        rows = await self.db.all("SELECT guild_id FROM feeds")
        self.assertEqual(rows, [{"guild_id": self.guild.id + 1}])


class MentionTest(FeedsCase):
    async def announce(self, content: str = "Patch notes are out"):
        ctx = self.context()
        await self.feeds.feeds_announce.callback(self.feeds, ctx, "news", content=content)
        return ctx

    async def test_a_role_is_made_mentionable_only_for_the_send(self):
        self.bot_without_mention_everyone()
        await self.make_feed()
        order = []
        original = self.channel.send

        async def watching_send(content=None, **kwargs):
            order.append(("send", self.pinged.mentionable))
            return await original(content, **kwargs)
        self.channel.send = watching_send
        ctx = await self.announce()
        self.assertEqual(self.edits, [True, False])
        self.assertEqual(order, [("send", True)])
        sent = self.channel.sent[0]
        self.assertEqual(sent.content, f"{self.pinged.mention} Patch notes are out")
        mentions = sent.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertEqual(mentions.roles, [self.pinged])
        self.assertEqual(ctx.fails, [])
        self.assertIn("Announced", ctx.oks[0])
        self.assertNotIn("wasn't pinged", ctx.oks[0])

    async def test_with_mention_everyone_the_role_is_left_alone(self):
        await self.make_feed()
        await self.announce()
        self.assertEqual(self.edits, [])
        self.assertEqual(self.channel.sent[0].kwargs["allowed_mentions"].roles, [self.pinged])

    async def test_a_role_already_mentionable_is_left_alone(self):
        self.bot_without_mention_everyone()
        self.pinged.mentionable = True
        await self.make_feed()
        await self.announce()
        self.assertEqual(self.edits, [])

    async def test_mentions_go_back_off_when_the_send_fails(self):
        self.bot_without_mention_everyone()
        await self.make_feed()

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.send = refuse
        ctx = await self.announce()
        self.assertEqual(self.edits, [True, False])
        self.assertEqual(self.pinged.mentionable, False)
        self.assertEqual(len(ctx.fails), 1)

    async def test_a_role_above_the_bot_posts_without_the_ping_and_says_so(self):
        self.bot_without_mention_everyone()
        self.pinged.position = 50
        await self.make_feed()
        ctx = await self.announce()
        self.assertEqual(self.edits, [])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("wasn't pinged", ctx.oks[0])

    async def test_a_refused_role_edit_posts_without_the_ping(self):
        self.bot_without_mention_everyone()
        await self.make_feed()

        async def refuse(**kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.pinged.edit = refuse
        ctx = await self.announce()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("wasn't pinged", ctx.oks[0])

    async def test_text_cannot_ping_anything_else(self):
        await self.make_feed()
        await self.announce(f"@everyone and {self.role_mod.mention}")
        mentions = self.channel.sent[0].kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertEqual(mentions.roles, [self.pinged])

    async def test_a_deleted_channel_or_role_is_handled(self):
        await self.make_feed()
        self.guild.roles.remove(self.pinged)
        ctx = await self.announce()
        self.assertEqual(self.channel.sent[0].content, "Patch notes are out")
        self.assertIn("wasn't pinged", ctx.oks[0])
        self.guild.channels.remove(self.channel)
        ctx = await self.announce()
        self.assertEqual(len(ctx.fails), 1)


class AutofeedTest(FeedsCase):
    async def create(self, kind: str = "silent", when: str = "1h", message: str = "Reset on {server} in {channel} at {unix}", **kwargs):
        ctx = self.context()
        if kind == "role":
            await self.feeds.autofeeds_create.callback(self.feeds, ctx, kwargs.get("role", self.pinged), when, message=message)
        else:
            await getattr(self.feeds, f"autofeeds_{kind}").callback(self.feeds, ctx, when, message=message)
        return ctx

    async def row(self) -> dict:
        return await self.db.one("SELECT * FROM autofeeds WHERE guild_id = ?", (self.guild.id,))

    async def run_timers(self, seconds: float = 3700) -> int:
        return await self.bot.timers.run_due(time.time() + seconds)

    async def test_silent_autofeed_posts_when_due_and_only_then(self):
        await self.create()
        self.assertEqual(await self.run_timers(10), 0)
        self.assertEqual(self.channel.sent, [])
        await self.run_timers()
        sent = self.channel.sent[0]
        self.assertTrue(sent.content.startswith(f"Reset on {self.guild.name} in {self.channel.name} at "))
        self.assertTrue(sent.content.rsplit(" ", 1)[1].isdigit())
        mentions = sent.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone or mentions.roles)

    async def test_a_one_time_autofeed_is_gone_afterward(self):
        await self.create()
        await self.run_timers()
        self.assertIsNone(await self.row())
        self.assertEqual(await self.run_timers(), 0)

    async def test_role_everyone_and_here_mentions(self):
        await self.create("role")
        await self.run_timers()
        self.assertEqual(self.channel.sent[0].kwargs["allowed_mentions"].roles, [self.pinged])
        self.assertTrue(self.channel.sent[0].content.startswith(self.pinged.mention))
        await self.create("everyone")
        await self.run_timers()
        self.assertTrue(self.channel.sent[1].content.startswith("@everyone "))
        self.assertTrue(self.channel.sent[1].kwargs["allowed_mentions"].everyone)
        await self.create("here")
        await self.run_timers()
        self.assertTrue(self.channel.sent[2].content.startswith("@here "))

    async def test_a_repeating_autofeed_comes_back(self):
        await self.create(when="1h")
        row = await self.row()
        ctx = self.context()
        await self.feeds.autofeeds_repeat.callback(self.feeds, ctx, row["id"], "1 day")
        await self.run_timers()
        self.assertEqual(len(self.channel.sent), 1)
        again = await self.row()
        self.assertEqual(again["repeat_days"], 1)
        self.assertAlmostEqual(again["next_at"], row["next_at"] + 86400, places=3)
        self.assertEqual((await self.bot.timers.get_key("autofeed", str(row["id"])))["due_at"], again["next_at"])
        await self.run_timers(3700 + 86400)
        self.assertEqual(len(self.channel.sent), 2)

    async def test_it_survives_a_restart(self):
        await self.create()
        await self.bot.remove_cog(self.feeds.qualified_name)
        self.bot.modules.remove(self.feeds)
        self.bot.timers.handlers.pop("autofeed")
        await self.bot.prepare([Feeds])
        await self.run_timers()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_startup_arms_an_autofeed_whose_timer_is_missing(self):
        await self.create()
        row = await self.row()
        await self.db.run("DELETE FROM timers WHERE kind = 'autofeed'")
        await self.feeds.on_load()
        self.assertIsNotNone(await self.bot.timers.get_key("autofeed", str(row["id"])))

    async def test_a_restored_backup_arms_its_autofeeds(self):
        await self.create()
        row = await self.row()
        await self.db.run("DELETE FROM timers WHERE kind = 'autofeed'")
        await self.feeds.on_guild_restored(self.guild.id)
        self.assertIsNotNone(await self.bot.timers.get_key("autofeed", str(row["id"])))

    async def test_a_channel_that_is_gone_keeps_the_autofeed_with_a_reason(self):
        await self.create()
        self.guild.channels.remove(self.channel)
        await self.run_timers()
        row = await self.row()
        self.assertEqual(row["last_error"], "The channel no longer exists.")

    async def test_a_refused_post_is_noted_and_a_repeat_keeps_going(self):
        await self.create()
        row = await self.row()
        await self.feeds.autofeeds_repeat.callback(self.feeds, self.context(), row["id"], "2h")

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.send = refuse
        await self.run_timers()
        row = await self.row()
        self.assertEqual(row["last_error"], "The bot can't post in that channel.")
        self.assertIsNotNone(await self.bot.timers.get_key("autofeed", str(row["id"])))

    async def test_a_message_discord_refuses_is_noted_instead_of_retried(self):
        await self.create()

        async def refuse(*args, **kwargs):
            raise discord.HTTPException(_FakeResponse(400), "Invalid Form Body")
        self.channel.send = refuse
        await self.run_timers()
        self.assertIn("Discord refused", (await self.row())["last_error"])
        self.assertEqual(await self.db.all("SELECT * FROM timers WHERE kind = 'autofeed'"), [])

    async def test_a_passing_discord_error_retries_through_the_timer(self):
        await self.create()

        async def fail_once(*args, **kwargs):
            raise discord.HTTPException(_FakeResponse(500), "busy")
        self.channel.send = fail_once
        await self.run_timers()
        timer = (await self.db.all("SELECT * FROM timers WHERE kind = 'autofeed'"))[0]
        self.assertEqual(timer["tries"], 1)
        self.assertIsNotNone(await self.row())

    async def test_a_server_the_bot_is_not_in_waits(self):
        await self.create()
        self.bot.fake_guilds.remove(self.guild)
        await self.run_timers()
        self.assertIsNotNone(await self.row())
        self.assertIsNotNone((await self.db.all("SELECT * FROM timers WHERE kind = 'autofeed'"))[0])

    async def test_command_refusals(self):
        for when, phrase in (("nope", "isn't a time"), ("10s", "at least a minute"), ("99y", "five years")):
            ctx = await self.create(when=when)
            self.assertIn(phrase, ctx.fails[0])
        ctx = await self.create(message="x" * 2001)
        self.assertEqual(len(ctx.fails), 1)
        self.assertIsNone(await self.row())

    async def test_silence_move_repeat_delete_and_list(self):
        await self.create("role")
        row = await self.row()
        ctx = self.context()
        await self.feeds.autofeeds.callback(self.feeds, ctx)
        self.assertIn(str(row["id"]), ctx.sent[0]["embed"].description)
        await self.feeds.autofeeds_silence.callback(self.feeds, ctx, row["id"])
        self.assertEqual(((await self.row())["mention"], (await self.row())["role_id"]), ("silent", None))
        other = FakeChannel(self.guild, "elsewhere")
        await self.feeds.autofeeds_move.callback(self.feeds, ctx, row["id"], other)
        self.assertEqual((await self.row())["channel_id"], other.id)
        await self.feeds.autofeeds_repeat.callback(self.feeds, ctx, row["id"], "30m")
        self.assertIn("once an hour", ctx.fails[0])
        await self.feeds.autofeeds_repeat.callback(self.feeds, ctx, row["id"], "whenever")
        self.assertEqual(len(ctx.fails), 2)
        await self.feeds.autofeeds_repeat.callback(self.feeds, ctx, row["id"], "90m")
        self.assertEqual((await self.row())["repeat_hours"], 1)
        self.assertEqual((await self.row())["repeat_minutes"], 30)
        await self.feeds.autofeeds_repeat.callback(self.feeds, ctx, row["id"], "off")
        self.assertEqual((await self.row())["repeat_hours"], 0)
        await self.feeds.autofeeds_delete.callback(self.feeds, ctx, row["id"])
        self.assertIsNone(await self.row())
        self.assertIsNone(await self.bot.timers.get_key("autofeed", str(row["id"])))

    async def test_clear_and_foreign_ids(self):
        await self.create()
        await self.create()
        row = await self.row()
        ctx = self.context()
        foreign = FakeGuild("Other")
        self.bot.fake_guilds.append(foreign)
        foreign_ctx = self.context()
        foreign_ctx.guild = foreign
        for command, args in ((self.feeds.autofeeds_delete, (row["id"],)), (self.feeds.autofeeds_silence, (row["id"],)), (self.feeds.autofeeds_move, (row["id"], self.channel)), (self.feeds.autofeeds_repeat, (row["id"], "1d"))):
            await command.callback(self.feeds, foreign_ctx, *args)
        self.assertEqual(len(foreign_ctx.fails), 4)
        self.assertIsNotNone(await self.row())
        await self.feeds.autofeeds_clear.callback(self.feeds, ctx)
        self.assertIsNotNone(await self.row(), "nothing goes before the button")
        await ctx.sent[-1]["view"].clear()
        self.assertIsNone(await self.row())
        self.assertEqual(await self.bot.timers.list_kind("autofeed"), [])


class DashboardTest(FeedsCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)

    def form(self, **changes) -> dict:
        return {
            "channel_id": str(self.channel.id), "message": {"content": "Hello from {server}", "embeds": [], "buttons": []},
            "when": time.time() + 7200, "mention": str(self.pinged.id), "repeat": [0, 1, 0, 0], "timezone": "UTC", **changes,
        }

    async def test_create_list_edit_delete(self):
        client = await self.client()
        response = await client.post(self.path("/autofeeds"), json=self.form())
        self.assertEqual(response.status, 200)
        autofeed_id = (await response.json())["id"]
        data = await (await client.get(self.path("/autofeeds"))).json()
        row = data["autofeeds"][0]
        self.assertEqual((row["id"], row["mention"], row["role_id"], row["channel_id"], row["repeat"], row["message"]["content"]), (autofeed_id, "role", str(self.pinged.id), str(self.channel.id), [0, 1, 0, 0], "Hello from {server}"))
        timer = await self.bot.timers.get_key("autofeed", str(autofeed_id))
        self.assertAlmostEqual(timer["due_at"], row["next_at"])
        later = time.time() + 90000
        response = await client.put(self.path(f"/autofeeds/{autofeed_id}"), json=self.form(when=later, mention="here", message={"content": "", "embeds": [{"title": "Card"}]}))
        self.assertEqual(response.status, 200)
        row = (await (await client.get(self.path("/autofeeds"))).json())["autofeeds"][0]
        self.assertEqual((row["mention"], row["role_id"], row["next_at"], row["message"]["embeds"][0]["title"]), ("here", None, later, "Card"))
        self.assertAlmostEqual((await self.bot.timers.get_key("autofeed", str(autofeed_id)))["due_at"], later)
        self.assertEqual((await client.delete(self.path(f"/autofeeds/{autofeed_id}"))).status, 200)
        self.assertEqual((await (await client.get(self.path("/autofeeds"))).json())["autofeeds"], [])
        self.assertIsNone(await self.bot.timers.get_key("autofeed", str(autofeed_id)))
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertEqual(actions[:3], ["autofeed.delete", "autofeed.edit", "autofeed.create"])

    async def test_bad_forms_name_the_field(self):
        client = await self.client()
        foreign = new_id()
        for changes, field in (
            ({"channel_id": None}, "channel_id"),
            ({"channel_id": "nonsense"}, "channel_id"),
            ({"channel_id": str(foreign)}, "channel_id"),
            ({"mention": str(foreign)}, "mention"),
            ({"mention": "nonsense"}, "mention"),
            ({"message": {"content": "", "embeds": []}}, "message"),
            ({"message": {"content": "x" * 2001}}, "message"),
            ({"message": "text"}, "message"),
            ({"when": None}, "when"),
            ({"when": time.time() + 5}, "when"),
            ({"when": time.time() + 10 ** 10}, "when"),
            ({"repeat": [0, 0, 0, 30]}, "repeat"),
            ({"repeat": [0, 0, 0]}, "repeat"),
            ({"repeat": [0, -1, 0, 0]}, "repeat"),
            ({"repeat": [0, 99999, 0, 0]}, "repeat"),
        ):
            response = await client.post(self.path("/autofeeds"), json=self.form(**changes))
            self.assertEqual(response.status, 400, changes)
            self.assertIn(field, (await response.json())["fields"], changes)
        self.assertEqual((await client.post(self.path("/autofeeds"), json=[1])).status, 400)
        self.assertEqual((await (await client.get(self.path("/autofeeds"))).json())["autofeeds"], [])

    async def test_a_channel_in_another_server_is_refused(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = FakeChannel(other, "their-channel")
        client = await self.client()
        response = await client.post(self.path("/autofeeds"), json=self.form(channel_id=str(theirs.id)))
        self.assertEqual(response.status, 400)

    async def test_ids_from_another_server_reach_nothing(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = FakeChannel(other, "their-channel")
        cog = self.feeds
        autofeed_id = await cog._insert(other.id, theirs.id, "silent", None, {"content": "x", "embeds": [], "buttons": []}, time.time() + 900, (0, 0, 0, 0), None, None)
        client = await self.client()
        self.assertEqual((await (await client.get(self.path("/autofeeds"))).json())["autofeeds"], [])
        self.assertEqual((await client.put(self.path(f"/autofeeds/{autofeed_id}"), json=self.form())).status, 404)
        self.assertEqual((await client.delete(self.path(f"/autofeeds/{autofeed_id}"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/autofeeds/{autofeed_id}/test"))).status, 404)
        self.assertIsNotNone(await self.db.one("SELECT 1 FROM autofeeds WHERE id = ?", (autofeed_id,)))

    async def test_an_edit_may_keep_a_time_already_passed_and_rearms_the_post(self):
        client = await self.client()
        response = await client.post(self.path("/autofeeds"), json=self.form(repeat=[0, 0, 0, 0]))
        autofeed_id = (await response.json())["id"]
        self.guild.channels.remove(self.channel)
        await self.bot.timers.run_due(time.time() + 8000)
        row = (await (await client.get(self.path("/autofeeds"))).json())["autofeeds"][0]
        self.assertIsNotNone(row["last_error"])
        self.assertIsNone(await self.bot.timers.get_key("autofeed", str(autofeed_id)))
        self.guild.channels.append(self.channel)
        response = await client.put(self.path(f"/autofeeds/{autofeed_id}"), json=self.form(when=row["next_at"], repeat=[0, 0, 0, 0]))
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(await self.bot.timers.get_key("autofeed", str(autofeed_id)))
        await self.bot.timers.run_due(time.time() + 8000)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_test_post_skips_the_ping_and_the_schedule(self):
        client = await self.client()
        autofeed_id = (await (await client.post(self.path("/autofeeds"), json=self.form())).json())["id"]
        before = await self.db.one("SELECT next_at FROM autofeeds WHERE id = ?", (autofeed_id,))
        response = await client.post(self.path(f"/autofeeds/{autofeed_id}/test"))
        self.assertEqual(response.status, 200)
        sent = self.channel.sent[0]
        self.assertEqual(sent.content, f"Hello from {self.guild.name}")
        self.assertFalse(sent.kwargs["allowed_mentions"].roles)
        self.assertEqual(await self.db.one("SELECT next_at FROM autofeeds WHERE id = ?", (autofeed_id,)), before)

    async def test_feeds_through_the_dashboard(self):
        client = await self.client()
        body = {"name": "My News", "role_id": str(self.pinged.id), "channel_id": str(self.channel.id)}
        self.assertEqual((await client.post(self.path("/feeds"), json=body)).status, 200)
        self.assertEqual((await client.post(self.path("/feeds"), json=body)).status, 400)
        for changes in ({"name": ""}, {"name": "a/b"}, {"name": "x" * 51}, {"role_id": "x"}, {"role_id": str(new_id())}, {"channel_id": "x"}, {"channel_id": str(new_id())}):
            self.assertEqual((await client.post(self.path("/feeds"), json={**body, "name": "other", **changes})).status, 400, changes)
        other = FakeChannel(self.guild, "other")
        self.assertEqual((await client.put(self.path("/feeds/My%20News"), json={"role_id": str(self.role_member.id), "channel_id": str(other.id)})).status, 200)
        rows = (await (await client.get(self.path("/autofeeds"))).json())["feeds"]
        self.assertEqual(rows, [{"name": "My News", "role_id": str(self.role_member.id), "channel_id": str(other.id)}])
        self.assertEqual((await client.put(self.path("/feeds/nope"), json={"role_id": str(self.role_member.id), "channel_id": str(other.id)})).status, 404)
        self.assertEqual((await client.delete(self.path("/feeds/nope"))).status, 404)
        self.assertEqual((await client.delete(self.path("/feeds/my%20news"))).status, 200)
        self.assertEqual((await (await client.get(self.path("/autofeeds"))).json())["feeds"], [])

    async def test_staff_cannot_reach_it(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/autofeeds"))).status, 403)
        self.assertEqual((await client.post(self.path("/autofeeds"), json=self.form())).status, 403)
        self.assertEqual((await client.post(self.path("/feeds"), json={})).status, 403)
