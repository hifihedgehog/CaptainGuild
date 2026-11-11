"""The welcome posts a server can tune: switches, lifetimes, who gets pinged, the joined count, the invite, and the picture that rides along.

A server that changes none of it sees what it saw before. tests/test_greetings.py and tests/test_greetings_more.py hold that, unchanged.
"""
import asyncio
import collections
import contextlib
import datetime
import inspect
import json
import random
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core import invitewatch
from captainguild.core.module import Module, required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.core.tagscript import OverBudget, Scope, budget, doublebrace, render_text
from captainguild.core.validate import Field, Schema, boolean, text as text_field
from captainguild.demo import DemoOps
from captainguild.modules import greetings as greetings_module
from captainguild.modules.greetings import DELETE, Greetings
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import NOW, FakeChannel, FakeGuild, FakeMember
from tests.test_greetings import GreetingsCase, text

UTC = datetime.timezone.utc
PNG = b"\x89PNG\r\n\x1a\n" + b"picture"
GIF = b"GIF89a" + b"picture"


class Cards(Module):
    """A stand-in for the module that draws the welcome image. It answers the one call greetings makes of it, and keeps what it was asked."""
    key = "welcomecard"
    category = "Greetings"
    schema = Schema({"enabled": Field(False, boolean), "text": Field("", text_field(2000))})

    def __init__(self, bot):
        super().__init__(bot)
        self.asked: list[tuple] = []
        self.pictures: dict[str, dict | None] = {}
        self.error: Exception | None = None
        self.wait = 0.0

    async def picture_for(self, member, kind, scope):
        self.asked.append((member.id, kind, scope))
        if self.wait:
            await asyncio.sleep(self.wait)
        if self.error is not None:
            raise self.error
        return self.pictures.get(kind)


def a_listed_invite(code, uses, *, inviter=(555, "Grace"), max_uses=0):
    who = SimpleNamespace(id=inviter[0], name=inviter[1].lower(), global_name=inviter[1], discriminator="0") if inviter else None
    return SimpleNamespace(code=code, uses=uses, max_uses=max_uses, max_age=0, temporary=False, inviter=who, channel=SimpleNamespace(id=444), created_at=datetime.datetime(2026, 11, 1, tzinfo=UTC), expires_at=None)


def refusal(status):
    response = SimpleNamespace(status=status, reason="no")
    if status == 404:
        return discord.NotFound(response, "Unknown Message")
    return discord.Forbidden(response, "no") if status == 403 else discord.HTTPException(response, "no")


class Case(GreetingsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.guild.features = []
        self.listed = []
        self.reads = 0

        async def invites():
            self.reads += 1
            return list(self.listed)

        self.guild.invites = invites

    async def asyncTearDown(self):
        await self.settle()  # a count still being written would otherwise meet a closed database
        await super().asyncTearDown()

    async def join(self, name="newcomer", **kwargs):
        member = FakeMember(self.guild, name, **kwargs)
        await self.cog_.on_member_join(member)
        return member

    async def leave(self, member):
        self.guild.members.remove(member)
        await self.cog_.on_raw_member_remove(SimpleNamespace(guild_id=self.guild.id, user=member))
        await self.cog_.on_member_remove(member)

    async def settle(self):
        """Let work nobody waited for finish: counts written in the background and invites read after a save."""
        for _ in range(5):
            waiting = [task for task in self.cog_._tasks if not task.done()]
            if not waiting:
                return
            await asyncio.wait(waiting, timeout=5)

    async def posts(self, **where):
        clause = " AND ".join(f"{name} = ?" for name in where)
        return await self.db.all(f"SELECT * FROM greeting_posts{' WHERE ' + clause if clause else ''} ORDER BY posted_at, message_id", tuple(where.values()))

    async def timers(self, kind="welcome"):
        return await self.bot.timers.list_kind(DELETE[kind] if kind in DELETE else kind, self.guild.id)

    async def retries(self):
        """The timers that try again to delete the welcome of someone who left."""
        return await self.timers(greetings_module.LEAVE_RETRY)

    async def count(self, guild=None):
        return await self.db.scalar("SELECT joined FROM greeting_counts WHERE guild_id = ?", ((guild or self.guild).id,))


class DefaultsTest(Case):
    async def test_every_new_setting_starts_where_nothing_changes(self):
        cfg = self.settings("greetings")
        self.assertEqual({name: cfg[name] for name in ("welcome_on", "dm_on", "farewell_on", "ban_on", "welcome_bots")}, dict.fromkeys(("welcome_on", "dm_on", "farewell_on", "ban_on", "welcome_bots"), True))
        self.assertEqual({name: cfg[name] for name in ("dm_same_as_welcome", "welcome_delete_on_leave")}, {"dm_same_as_welcome": False, "welcome_delete_on_leave": False})
        self.assertEqual([cfg[name] for name in ("welcome_delete_after", "farewell_delete_after", "ban_delete_after", "welcome_again_after")], [0, 0, 0, 0])
        self.assertEqual([cfg[name] for name in ("welcome_mentions", "farewell_mentions", "ban_mentions")], ["member"] * 3)

    async def test_settings_saved_before_these_existed_read_the_same(self):
        await self.db.run("INSERT INTO guild_settings (guild_id, module, data, updated_at) VALUES (?, 'greetings', ?, 1.0)",
                          (self.guild.id, json.dumps({"welcome_channel": self.channel.id, "welcome": text("Hi {user}"), "burst_joins": 10})))
        await self.bot.settings.load()
        self.bot.settings._merged.clear()
        await self.join("ada")
        sent = self.channel.sent[-1]
        self.assertEqual(sent.content, "Hi ada")
        allowed = sent.kwargs["allowed_mentions"]
        self.assertEqual((allowed.everyone, allowed.roles), (False, False), "people, never a role or everyone, as before")
        sent_as = allowed.to_dict()
        self.assertEqual((sent_as["parse"], sent_as["users"]), ([], [self.guild.members[-1].id]), "the member is named outright, so no mention inside a name can ping")
        self.assertNotIn("file", sent.kwargs)
        self.assertEqual(await self.timers(), [])

    async def test_the_limits_of_each_setting(self):
        for form in ({"welcome_delete_after": -1}, {"welcome_delete_after": 2592001}, {"farewell_delete_after": 2592001}, {"ban_delete_after": "soon"}, {"welcome_again_after": 1441},
                     {"welcome_again_after": -1}, {"welcome_mentions": "everyone"}, {"farewell_mentions": "everyone"}, {"ban_mentions": "some"}, {"welcome_on": "yes"},
                     {"welcome_bots": 1}):
            with self.assertRaises(Exception, msg=form):
                await self.configure(**form)
        await self.configure(welcome_delete_after=2592000, farewell_delete_after=0, ban_delete_after=60, welcome_again_after=1440, welcome_mentions="all")
        self.assertEqual(self.settings("greetings")["welcome_delete_after"], 30 * 86400)

    async def test_a_lifetime_past_thirty_days_is_refused_in_days(self):
        client = await self.client()
        for name in ("welcome_delete_after", "farewell_delete_after", "ban_delete_after"):
            response = await client.put(self.path("/settings/greetings"), json={name: 31 * 86400})
            self.assertEqual((response.status, (await response.json())["fields"]), (400, {name: "A post can stay up to 30 days."}), name)
            response = await client.put(self.path("/settings/greetings"), json={name: -5})
            self.assertEqual((response.status, (await response.json())["fields"]), (400, {name: "Must be at least 0."}), name)
            response = await client.put(self.path("/settings/greetings"), json={name: "120"})
            self.assertEqual((response.status, (await response.json())[name]), (200, 120), "a number typed into a box counts")

    async def test_a_lifetime_is_whole_minutes_as_the_page_shows_it(self):
        # The page shows a lifetime in days, hours and minutes. One under a minute would show there as all zeros, which its help says keeps
        # every post, and one with seconds left over as a time it isn't.
        whole = "A post stays up for whole minutes, from 1 minute to 30 days."
        client = await self.client()
        for name in ("welcome_delete_after", "farewell_delete_after", "ban_delete_after"):
            for seconds in (1, 30, 59, 90, 3601, 30 * 86400 - 1):
                response = await client.put(self.path("/settings/greetings"), json={name: seconds})
                self.assertEqual((response.status, (await response.json())["fields"]), (400, {name: whole}), (name, seconds))
            for seconds in (60, 3660, 30 * 86400, 0):
                response = await client.put(self.path("/settings/greetings"), json={name: seconds})
                self.assertEqual((response.status, (await response.json())[name]), (200, seconds), (name, seconds))
        ctx = self.context()
        await self.cog_.greetings_delete_after.callback(self.cog_, ctx, "welcome", 60)
        await self.cog_.greetings_delete_after.callback(self.cog_, ctx, "welcome", 30)
        await self.cog_.greetings_delete_after.callback(self.cog_, ctx, "welcome", 150)
        self.assertEqual((ctx.fails, self.settings("greetings")["welcome_delete_after"]), ([whole, whole], 60))
        self.assertIn("whole minutes", self.cog_.greetings_delete_after.app_command.parameters[1].description)


class SwitchTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {user}"), dm=text("Hello {user}"), farewell=text("Bye {user}"), ban=text("Banned {user}"))

    async def test_a_welcome_switched_off_keeps_its_message_and_posts_nothing(self):
        await self.configure(welcome_on=False)
        member = await self.join()
        self.assertEqual(self.channel.sent, [])
        self.assertEqual([dm["content"] for dm in member.dms], ["Hello newcomer"], "the join DM has its own switch")
        self.assertEqual(self.settings("greetings")["welcome"]["content"], "Hi {user}")
        await self.configure(welcome_on=True)
        await self.join("second")
        self.assertEqual([sent.content for sent in self.channel.sent], ["Hi second"])

    async def test_a_join_dm_switched_off(self):
        await self.configure(dm_on=False)
        member = await self.join()
        self.assertEqual(member.dms, [])
        self.assertEqual(len(self.channel.sent), 1)

    async def test_both_switched_off_does_nothing_but_count(self):
        await self.configure(welcome_on=False, dm_on=False)
        cards = mock.Mock()
        with mock.patch.object(self.bot, "module", return_value=cards):
            member = await self.join()
        self.assertEqual((self.channel.sent, member.dms), ([], []))
        cards.picture_for.assert_not_called()
        self.assertEqual(await self.count(), self.guild.member_count)

    async def test_the_join_dm_can_send_the_welcome_message(self):
        await self.configure(dm_same_as_welcome=True, welcome=text("Hi {user}, meet us in #{channel}"))
        member = await self.join()
        self.assertEqual(member.dms[0]["content"], "Hi newcomer, meet us in #general", "it says what the post says, the channel included")
        self.assertFalse(member.dms[0]["allowed_mentions"].users)
        self.assertEqual(self.settings("greetings")["dm"]["content"], "Hello {user}", "its own message stays saved")
        await self.configure(dm_same_as_welcome=False)
        self.assertEqual((await self.join("second")).dms[0]["content"], "Hello second")

    async def test_the_same_message_goes_by_dm_with_no_welcome_channel_set(self):
        await self.configure(dm_same_as_welcome=True, welcome_channel=None, welcome=text("Hi {user}"))
        self.assertEqual((await self.join()).dms[0]["content"], "Hi newcomer")

    async def test_a_goodbye_switched_off(self):
        await self.configure(farewell_on=False)
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual(self.channel.sent, [])
        await self.configure(farewell_on=True)
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual([sent.content for sent in self.channel.sent], ["Bye leaver"])

    async def test_a_ban_message_switched_off_leaves_the_goodbye_to_say_it(self):
        await self.configure(ban_on=False)
        banned = FakeMember(self.guild, "banned")
        await self.cog_.on_member_ban(self.guild, banned)
        self.assertEqual(self.channel.sent, [])
        await self.cog_.on_member_remove(banned)
        self.assertEqual([sent.content for sent in self.channel.sent], ["Bye banned"], "with no ban message going out, a banned person gets the goodbye, as with none set")

    async def test_a_ban_message_switched_on_still_replaces_the_goodbye(self):
        banned = FakeMember(self.guild, "banned")
        await self.cog_.on_member_ban(self.guild, banned)
        await self.cog_.on_member_remove(banned)
        self.assertEqual([sent.content for sent in self.channel.sent], ["Banned banned"])

    async def test_a_test_still_sends_what_is_switched_off(self):
        await self.configure(welcome_on=False, dm_on=False, farewell_on=False, ban_on=False)
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertEqual([sent["content"] for sent in ctx.sent], ["Hi admin", "Bye admin", "Banned admin"])
        self.assertEqual(self.admin.dms[-1]["content"], "Hello admin")
        client = await self.client()
        response = await client.post(self.path("/greetings/test"), json={"which": "welcome", "payload": text("Hi {user}"), "channel_id": str(self.channel.id)})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.channel.sent[-1].content, "Hi admin")


class BotsTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(welcome_channel=self.channel.id, welcome=text("{if({user(bot)}==true):A bot, {user}, was added.|Welcome, {user}!}"), dm=text("Hello"))

    async def test_a_bot_gets_the_welcome_and_one_message_can_tell_them_apart(self):
        robot = await self.join("robot", bot=True)
        await self.join("person")
        self.assertEqual([sent.content for sent in self.channel.sent], ["A bot, robot, was added.", "Welcome, person!"])
        self.assertEqual(robot.dms, [], "a bot never gets the DM")

    async def test_bots_can_be_left_out(self):
        await self.configure(welcome_bots=False)
        await self.join("robot", bot=True)
        await self.join("person")
        self.assertEqual([sent.content for sent in self.channel.sent], ["Welcome, person!"])
        self.assertEqual(await self.count(), self.guild.member_count, "a bot that isn't welcomed is still counted")


class JoinedCountTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(welcome_channel=self.channel.id, welcome=text("{user} is number {joined}"), farewell=text("{user} left, {joined} have joined"), ban=text("{user} banned, {joined} have joined"))

    async def test_the_first_join_starts_from_the_member_count_with_the_newcomer_in_it(self):
        self.assertIsNone(await self.count())
        before = self.guild.member_count
        await self.join("first")
        self.assertEqual(await self.count(), before + 1)
        self.assertEqual(self.channel.sent[-1].content, f"first is number {before + 1}")

    async def test_every_join_adds_one_and_a_leave_takes_nothing_away(self):
        start = self.guild.member_count
        first = await self.join("first")
        await self.leave(first)
        await self.join("second")
        await self.join("robot", bot=True)
        self.assertEqual(await self.count(), start + 3, "the count goes up for a person and a bot alike, and never comes down")
        self.assertEqual(self.channel.sent[-1].content, f"robot is number {start + 3}")
        self.assertEqual(self.channel.sent[1].content, f"first left, {start + 1} have joined")

    async def test_joins_at_the_same_moment_each_get_their_own_number(self):
        await self.configure(burst_joins=0)
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 1000, 1.0)", (self.guild.id,))
        people = [FakeMember(self.guild, f"person{number}") for number in range(12)]
        await asyncio.gather(*(self.cog_.on_member_join(person) for person in people))
        numbers = sorted(int(sent.content.rpartition(" ")[2]) for sent in self.channel.sent)
        self.assertEqual(numbers, list(range(1001, 1013)))
        self.assertEqual(await self.count(), 1012)

    async def test_the_count_is_kept_in_the_database_so_a_restart_carries_on(self):
        await self.join("first")
        kept = await self.count()
        fresh = Greetings(self.bot)
        self.assertEqual(await fresh._count_join(FakeMember(self.guild, "second")), kept + 1)

    async def test_a_welcome_that_waited_for_the_rules_shows_the_count_at_its_own_join(self):
        await self.configure(wait_for_rules=True)
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 500, 1.0)", (self.guild.id,))
        waiting = await self.join("waiting", pending=True)
        for number in range(3):
            await self.join(f"quick{number}")
        waiting.pending = False
        await self.cog_.on_member_update(SimpleNamespace(pending=True), waiting)
        self.assertEqual(self.channel.sent[-1].content, "waiting is number 501")
        self.assertEqual(await self.count(), 504)
        self.assertEqual(self.cog_._join_numbers, {}, "the number is let go once it's used")

    async def test_someone_who_leaves_from_the_rules_screen_is_forgotten(self):
        await self.configure(wait_for_rules=True)
        waiting = await self.join("waiting", pending=True)
        self.assertEqual(len(self.cog_._join_numbers), 1)
        await self.leave(waiting)
        self.assertEqual(self.cog_._join_numbers, {})

    async def test_a_number_that_was_not_kept_falls_back_to_the_count_as_it_stands(self):
        await self.configure(wait_for_rules=True)
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 500, 1.0)", (self.guild.id,))
        waiting = await self.join("waiting", pending=True)
        self.cog_._join_numbers.clear()  # as after a restart
        await self.join("quick")
        waiting.pending = False
        await self.cog_.on_member_update(SimpleNamespace(pending=True), waiting)
        self.assertEqual(self.channel.sent[-1].content, "waiting is number 502")

    async def test_a_goodbye_and_a_ban_show_the_count_as_it_stands(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 64097, 1.0)", (self.guild.id,))
        gone = FakeMember(self.guild, "gone")
        await self.cog_.on_member_ban(self.guild, gone)
        await self.configure(ban=text(""))
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual([sent.content for sent in self.channel.sent], ["gone banned, 64097 have joined", "leaver left, 64097 have joined"])
        self.assertEqual(await self.count(), 64097)

    async def test_a_server_never_counted_shows_its_member_count(self):
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual(self.channel.sent[-1].content, f"leaver left, {self.guild.member_count} have joined")
        self.assertIsNone(await self.count(), "showing the count makes no row")

    async def test_a_test_and_a_preview_never_count(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 77, 1.0)", (self.guild.id,))
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        await self.cog_.greetings_testgreet.callback(self.cog_, self.context(), member=self.member)
        client = await self.client()
        for _ in range(2):
            await client.post(self.path("/greetings/test"), json={"which": "welcome", "payload": text("{joined}"), "channel_id": str(self.channel.id)})
        scope = await self.cog_.scope_for(self.member, preview=True)
        await self.cog_.scope_for(self.member)
        self.assertEqual(await self.count(), 77)
        self.assertEqual(ctx.sent[0]["content"], "admin is number 77")
        self.assertEqual(self.channel.sent[-1].content, "77")
        self.assertEqual(scope.extra["joined"], "77")

    async def test_a_storage_error_does_not_cost_anyone_their_welcome(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 40, 1.0)", (self.guild.id,))
        with mock.patch.object(self.cog_, "_add_joins", mock.AsyncMock(side_effect=RuntimeError("disk full"))), self.assertLogs(greetings_module.log, "ERROR"):
            await self.join("unlucky")
        self.assertEqual(self.channel.sent[-1].content, "unlucky is number 40", "it shows the count as last read")
        await self.join("next")
        self.assertEqual(self.channel.sent[-1].content, "next is number 42", "and the join that couldn't be counted is counted with the next one")

    async def test_a_count_that_waits_too_long_lets_the_welcome_go(self):
        self.cog_.COUNT_PATIENCE = 0.05
        stuck = asyncio.Event()

        async def never(guild, joins, **_):
            await stuck.wait()

        with mock.patch.object(self.cog_, "_add_joins", never), self.assertLogs(greetings_module.log, "ERROR"):
            await asyncio.wait_for(self.join("patient"), 5)
        self.assertEqual(len(self.channel.sent), 1)
        stuck.set()

    async def test_two_people_who_arrive_before_the_first_count_get_the_next_two_numbers(self):
        await self.configure(burst_joins=0)
        before = self.guild.member_count
        # Discord counts both joins as they arrive, before the bot's handler for the first one runs.
        first = FakeMember(self.guild, "first", joined=NOW)
        second = FakeMember(self.guild, "second", joined=NOW + datetime.timedelta(seconds=1))
        await self.cog_.on_member_join(first)
        await self.cog_.on_member_join(second)
        self.assertEqual([sent.content for sent in self.channel.sent], [f"first is number {before + 1}", f"second is number {before + 2}"])
        self.assertEqual(await self.count(), before + 2)

    async def test_the_first_count_written_for_skipped_joins_leaves_out_who_joined_after_them(self):
        await self.configure(burst_joins=1)
        self.cog_.BURST_QUIET = 30
        self.cog_._joins[self.guild.id] = collections.deque([time.monotonic(), time.monotonic()])  # the minute's limit is already used
        before = self.guild.member_count
        first = FakeMember(self.guild, "first", joined=NOW)
        second = FakeMember(self.guild, "second", joined=NOW + datetime.timedelta(seconds=1))
        await self.cog_.on_member_join(first)
        await asyncio.sleep(0)  # the write for the first starts before the second is handled
        await self.cog_.on_member_join(second)
        await self.settle()
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(await self.count(), before + 2)

    async def test_two_joins_stamped_with_the_same_moment_get_the_next_two_numbers(self):
        await self.configure(burst_joins=0)
        before = self.guild.member_count
        first = FakeMember(self.guild, "first", joined=NOW)
        second = FakeMember(self.guild, "second", joined=NOW)  # Discord gave both joins the same millisecond
        # Both arrived together, so discord.py started a handler for each before either was counted.
        await asyncio.gather(self.cog_.on_member_join(first), self.cog_.on_member_join(second))
        self.assertEqual(sorted(sent.content for sent in self.channel.sent), [f"first is number {before + 1}", f"second is number {before + 2}"])
        self.assertEqual(await self.count(), before + 2)

    async def test_a_second_join_of_the_same_moment_whose_handler_has_not_started_gets_the_next_number(self):
        # discord.py puts a joining member in the member list and raises the member count before it starts any handler, so the second join is
        # already there when the first one's count is written, although nothing has run for it yet.
        await self.configure(burst_joins=0)
        before = self.guild.member_count
        first = FakeMember(self.guild, "first", joined=NOW)
        second = FakeMember(self.guild, "second", joined=NOW)
        await self.cog_.on_member_join(first)
        await self.cog_.on_member_join(second)
        self.assertEqual([sent.content for sent in self.channel.sent], [f"first is number {before + 1}", f"second is number {before + 2}"])
        self.assertEqual(await self.count(), before + 2)

    async def test_the_same_when_the_second_comes_first_in_the_order_of_joins(self):
        # Equal join times are put in order by member ID, which needn't match the order Discord sent the joins in. The first count then counts
        # the second join too, keeps its place, and the second join's own write gives it that place and adds nothing.
        await self.configure(burst_joins=0)
        before = self.guild.member_count
        second = FakeMember(self.guild, "second", joined=NOW)  # the lower ID
        first = FakeMember(self.guild, "first", joined=NOW)
        await self.cog_.on_member_join(first)
        await self.cog_.on_member_join(second)
        self.assertEqual([sent.content for sent in self.channel.sent], [f"first is number {before + 2}", f"second is number {before + 1}"])
        self.assertEqual(await self.count(), before + 2)
        self.assertNotIn(second.id, self.cog_._placed.get(self.guild.id, {}), "a kept place is given once")
        third = FakeMember(self.guild, "third", joined=NOW + datetime.timedelta(seconds=1))
        await self.cog_.on_member_join(third)
        self.assertEqual((self.channel.sent[-1].content, await self.count()), (f"third is number {before + 3}", before + 3))

    async def test_skipped_joins_stamped_with_the_same_moment_are_each_counted_once(self):
        await self.configure(burst_joins=1)
        self.cog_.BURST_QUIET = 30
        self.cog_._joins[self.guild.id] = collections.deque([time.monotonic(), time.monotonic()])  # the minute's limit is already used
        before = self.guild.member_count
        first = FakeMember(self.guild, "first", joined=NOW)
        second = FakeMember(self.guild, "second", joined=NOW)
        third = FakeMember(self.guild, "third", joined=NOW)
        await self.cog_.on_member_join(first)
        await asyncio.sleep(0)  # the write for the first starts before the others are handled
        await self.cog_.on_member_join(second)
        await self.cog_.on_member_join(third)
        await self.settle()
        self.assertEqual(await self.count(), before + 3)

    async def test_a_welcomed_join_counts_the_skipped_ones_of_its_moment_with_it(self):
        await self.configure(burst_joins=0)
        before = self.guild.member_count
        skipped = FakeMember(self.guild, "skipped", joined=NOW)
        welcomed = FakeMember(self.guild, "welcomed", joined=NOW)
        self.cog_._counting.add(self.guild.id)  # a write is under way elsewhere, so the welcomed join's own write takes the skipped one along
        self.cog_._count_later(skipped)
        await self.cog_.on_member_join(welcomed)
        self.assertEqual([sent.content for sent in self.channel.sent], [f"welcomed is number {before + 2}"])
        self.assertEqual(await self.count(), before + 2)

    async def turns(self, count=20):
        for _ in range(count):
            await asyncio.sleep(0)

    def numbers(self):
        return {sent.content.split(" is number ")[0]: int(sent.content.rpartition(" ")[2]) for sent in self.channel.sent if " is number " in (sent.content or "")}

    async def test_a_join_that_leaves_while_its_first_count_waits_keeps_its_place(self):
        await self.configure(burst_joins=0)
        before = self.guild.member_count
        await self.db._write_lock.acquire()  # another feature's write holds the database, so the count's write waits its turn
        try:
            first = FakeMember(self.guild, "first", joined=NOW)
            joining = asyncio.ensure_future(self.cog_.on_member_join(first))
            await self.turns()
            self.guild.members.remove(first)  # the leave comes in, and discord.py takes them off the list and lowers the count
            second = FakeMember(self.guild, "second", joined=NOW + datetime.timedelta(seconds=1))
            joining_too = asyncio.ensure_future(self.cog_.on_member_join(second))
            await self.turns()
        finally:
            self.db._write_lock.release()
        await asyncio.gather(joining, joining_too)
        self.assertEqual((self.numbers(), await self.count()), ({"first": before + 1, "second": before + 2}, before + 2))

    async def test_a_join_whose_leave_came_in_before_its_handler_ran_is_still_counted(self):
        await self.configure(burst_joins=0)
        before = self.guild.member_count
        first = FakeMember(self.guild, "first", joined=NOW)
        self.guild.members.remove(first)  # both events read from the gateway before the join's handler ran
        await self.cog_.on_member_join(first)
        await self.join("second")
        self.assertEqual((self.numbers(), await self.count()), ({"first": before + 1, "second": before + 2}, before + 2))

    async def test_a_bot_welcomed_in_a_rules_screen_raid_gets_its_own_place(self):
        # The rules screen and the welcome keep their own limits for joins in a minute, so in a raid of people on the rules screen a bot in the
        # same moment is welcomed. Its write takes the skipped join before it along, while the write already started for skipped joins finds
        # the one after it and lands first.
        await self.configure(wait_for_rules=True, burst_joins=1)
        self.cog_.BURST_QUIET = 3600
        self.cog_._joins[(self.guild.id, greetings_module.SCREENING)] = collections.deque([time.monotonic(), time.monotonic()])  # the limit is used
        before = self.guild.member_count
        later = NOW + datetime.timedelta(days=1)
        first = FakeMember(self.guild, "first", pending=True, joined=later)
        robot = FakeMember(self.guild, "robot", bot=True, joined=later + datetime.timedelta(milliseconds=5))
        last = FakeMember(self.guild, "last", pending=True, joined=later + datetime.timedelta(milliseconds=9))
        await asyncio.gather(*(self.cog_.on_member_join(person) for person in (first, robot, last)))
        await self.settle()
        self.assertEqual((self.numbers(), await self.count()), ({"robot": before + 2}, before + 3))
        self.assertEqual((self.cog_._pending, self.cog_._placed), ({}, {}), "nothing left held")

    async def test_a_large_server_whose_list_holds_only_some_members_counts_from_its_member_count(self):
        # Discord gives the member count, while the list holds only the members the bot has been sent so far, as while a chunk is coming in.
        await self.configure(burst_joins=0)
        listed = len(self.guild.members)
        self.guild.__class__ = type("PartlyListed", (type(self.guild),), {"member_count": property(lambda guild: 43_500 - listed + len(guild.members))})
        await self.join("first")
        await self.join("second")
        self.assertEqual((self.numbers(), await self.count()), ({"first": 43_501, "second": 43_502}, 43_502))

    async def test_a_first_count_whose_write_failed_is_written_with_the_next_join(self):
        await self.configure(burst_joins=0)
        before = self.guild.member_count
        real = self.cog_._add_joins
        calls = []

        async def breaks_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("the database broke")
            return await real(*args, **kwargs)

        first = FakeMember(self.guild, "first", joined=NOW)
        second = FakeMember(self.guild, "second", joined=NOW + datetime.timedelta(seconds=1))
        with mock.patch.object(self.cog_, "_add_joins", breaks_once), self.assertLogs(level="ERROR"):
            await self.cog_.on_member_join(first)
        await self.cog_.on_member_join(second)
        self.assertEqual((await self.count(), self.numbers()["second"]), (before + 2, before + 2), "the failed join is counted with the next one, once")
        self.assertEqual(self.cog_._pending, {})

    async def test_rounds_of_joins_in_any_order_count_each_join_once(self):
        # Joins on a server with no count: some in one millisecond with IDs in either order, some read before earlier handlers ran, some skipped,
        # some writes held back. Each welcomed join shows its own number among the new ones, and the count ends at the members before plus the joins.
        await self.configure(burst_joins=0)
        self.cog_.BURST_QUIET = 3600
        wrong = []
        for round_number in range(120):
            rng = random.Random(round_number)
            await self.db.run("DELETE FROM greeting_counts")
            self.cog_._placed.clear()
            self.channel.sent.clear()
            for person in [member for member in self.guild.members if member.name.startswith("zz")]:
                self.guild.members.remove(person)
            before = self.guild.member_count
            joins = rng.randint(2, 6)
            moments = sorted(NOW + datetime.timedelta(days=1, milliseconds=rng.choice((0, 0, 1, 2))) for _ in range(joins))
            ids = rng.sample(range(900000000000000000, 900000000000000000 + 50), joins)
            skipped = [rng.random() < 0.35 for _ in range(joins)]
            decisions = list(skipped)
            self.cog_._in_burst = lambda guild_id, limit: decisions.pop(0) if decisions else False
            hold = rng.random() < 0.5
            if hold:
                await self.db._write_lock.acquire()
            tasks = []
            try:
                for index, (moment, member_id) in enumerate(zip(moments, ids)):
                    member = FakeMember(self.guild, f"zz{round_number}x{index}", joined=moment)
                    member.id, member.mention = member_id, f"<@{member_id}>"
                    if rng.random() < 0.5:
                        await self.turns(rng.randint(0, 6))
                    tasks.append(asyncio.ensure_future(self.cog_.on_member_join(member)))
                await self.turns(rng.randint(0, 10))
            finally:
                if hold:
                    self.db._write_lock.release()
            await asyncio.gather(*tasks)
            await self.settle()
            values = sorted(self.numbers().values())
            if (await self.count() != before + joins or len(set(values)) != len(values) or len(values) != joins - sum(skipped)
                    or (values and (values[0] < before + 1 or values[-1] > before + joins))):
                wrong.append(round_number)
        self.assertEqual(wrong, [])

    async def test_a_first_count_with_no_join_time_starts_from_the_member_count(self):
        self.assertEqual(await self.cog_._add_joins(self.guild, 1), self.guild.member_count)
        self.assertEqual(await self.cog_._add_joins(self.guild, 3, newest=(NOW, 1)), self.guild.member_count + 3)

    async def test_a_count_that_gives_up_while_its_write_lands_is_counted_once(self):
        await self.configure(burst_joins=0)
        await self.join("zero")
        start = await self.count()
        self.cog_.COUNT_PATIENCE = 0.2
        commit = self.db.conn.commit
        slow = {"once": True}

        async def slow_commit():
            await commit()  # the write lands on the database's thread
            if slow["once"]:
                slow["once"] = False
                await asyncio.sleep(1)  # and the answer comes back after the welcome stopped waiting for it

        with mock.patch.object(self.db.conn, "commit", slow_commit), self.assertLogs(greetings_module.log, "ERROR"):
            await self.join("one")
        await self.settle()
        await self.join("two")
        await self.settle()
        self.assertEqual(await self.count(), start + 2, "two joins, two counts")
        self.assertEqual(self.channel.sent[-1].content, f"two is number {start + 2}")

    async def test_a_count_that_gives_up_and_then_fails_is_counted_with_the_next_join(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 40, 1.0)", (self.guild.id,))
        self.cog_.COUNT_PATIENCE = 0.05
        real = self.cog_._add_joins

        async def slow_then_broken(guild, joins, **options):
            await asyncio.sleep(0.2)
            raise RuntimeError("disk full")

        with mock.patch.object(self.cog_, "_add_joins", slow_then_broken), self.assertLogs(greetings_module.log, "ERROR"):
            await self.join("unlucky")
            await self.settle()
        self.assertEqual(self.cog_._uncounted, {self.guild.id: 1}, "the failed join waits for the next")
        await self.join("next")
        self.assertEqual(self.channel.sent[-1].content, "next is number 42")
        self.assertIs(self.cog_._add_joins.__func__, real.__func__)

    async def test_the_test_button_fills_in_the_count_and_the_invite_only_where_a_greeting_has_them(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 64097, 1.0)", (self.guild.id,))
        client = await self.client()
        words = text("Party for {mention}! We are {joined}, via {invite}.")
        for which in ("welcome", "farewell", "ban"):
            response = await client.post(self.path("/greetings/test"), json={"which": which, "payload": words, "channel_id": str(self.channel.id)})
            self.assertEqual(response.status, 200, which)
            self.assertEqual(self.channel.sent[-1].content, f"Party for {self.admin.mention}! We are 64097, via sample.", which)
        await client.post(self.path("/greetings/test"), json={"which": "dm", "payload": words})
        self.assertEqual(self.admin.dms[-1]["content"], f"Party for {self.admin.mention}! We are 64097, via sample.")
        response = await client.post(self.path("/greetings/test"), json={"which": "birthday", "payload": words, "channel_id": str(self.channel.id)})
        self.assertEqual(response.status, 200)
        tested = self.channel.sent[-1].content
        await self.configure(birthday=words)
        await self.cog_._post_birthday(self.channel, self.guild, [self.admin], self.settings("greetings")["birthday"])
        self.assertEqual(tested, self.channel.sent[-1].content, "the test shows what the real announcement shows")
        self.assertEqual(tested, f"Party for {self.admin.mention}! We are {{joined}}, via {{invite}}.")

    async def test_without_the_server_at_hand_only_a_greeting_s_test_gets_the_count_and_the_invite(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 64097, 1.0)", (self.guild.id,))
        request = SimpleNamespace(guild_id=self.guild.id, user=SimpleNamespace(id=self.admin.id), ops=self.bot.ops)
        with mock.patch.object(self.bot, "get_guild", return_value=None):
            member, greeting = await self.cog_._test_values(request, "welcome", None)
            _, birthday = await self.cog_._test_values(request, "birthday", None)
        self.assertIsNone(member)
        self.assertEqual((greeting.extra["joined"], render_text("{invite}", greeting)), ("64097", "sample"))
        self.assertEqual(birthday.extra, {})

    async def test_the_page_reads_and_sets_the_count(self):
        client = await self.client()
        self.assertEqual(await (await client.get(self.path("/greetings/joined"))).json(), {"joined": self.guild.member_count, "counted": False})
        response = await client.put(self.path("/greetings/joined"), json={"joined": 64097})
        self.assertEqual((response.status, await response.json()), (200, {"joined": 64097, "counted": True}))
        self.assertEqual(await (await client.get(self.path("/greetings/joined"))).json(), {"joined": 64097, "counted": True})
        await self.join("next")
        self.assertEqual(self.channel.sent[-1].content, "next is number 64098")
        row = (await self.bot.activity_log.recent(self.guild.id))[0]
        self.assertEqual((row["action"], row["detail"], row["user_name"]), ("greetings.joined", "64097", "admin"))

    async def test_the_page_refuses_what_is_not_a_count(self):
        client = await self.client()
        for body in ({}, {"joined": -1}, {"joined": "many"}, {"joined": 1.5}, {"joined": True}, {"joined": 10 ** 12 + 1}, {"joined": None}):
            response = await client.put(self.path("/greetings/joined"), json=body)
            self.assertEqual(response.status, 400, body)
            self.assertIn("whole number from 0", (await response.json())["fields"]["joined"])
        self.assertIsNone(await self.count())
        self.assertEqual((await client.put(self.path("/greetings/joined"), json={"joined": "12"})).status, 200, "a number typed into a text box counts")
        self.assertEqual((await client.put(self.path("/greetings/joined"), json={"joined": 0})).status, 200)
        self.assertEqual(await self.count(), 0)

    async def test_staff_can_not_read_or_set_it_and_another_server_s_count_stays_put(self):
        other = FakeGuild("Elsewhere")
        self.bot.fake_guilds.append(other)
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 9, 1.0)", (other.id,))
        client = await self.client()
        await client.put(self.path("/greetings/joined"), json={"joined": 5})
        self.assertEqual((await self.count(), await self.count(other)), (5, 9))
        staff = await self.client(access="staff")
        self.assertEqual((await staff.get(self.path("/greetings/joined"))).status, 403)
        self.assertEqual((await staff.put(self.path("/greetings/joined"), json={"joined": 1})).status, 403)

    async def test_the_command_shows_and_sets_it(self):
        ctx = self.context()
        await self.cog_.greetings_joined.callback(self.cog_, ctx)
        self.assertIn(f"{self.guild.member_count:,} people have joined so far", ctx.sent[-1]["content"])
        await self.cog_.greetings_joined.callback(self.cog_, ctx, 64097)
        self.assertEqual(ctx.oks, ["The joined count is 64,097. The next person to join makes it 64,098."])
        self.assertEqual(await self.count(), 64097)
        await self.cog_.greetings_joined.callback(self.cog_, ctx)
        self.assertIn("64,097 people have joined so far", ctx.sent[-1]["content"])
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "greetings.joined")
        self.assertEqual(required_permissions(self.cog_.greetings_joined), ("manage_guild",))


class RaidTest(Case):
    """A burst of joins past the limit: each one is counted, and that's all it costs."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_.BURST_QUIET = 30
        await self.configure(welcome_channel=self.channel.id, welcome=text("{user} {joined} {invite}"), dm=text("Hello"), burst_joins=3, welcome_again_after=10,
                             welcome_delete_after=60, welcome_delete_on_leave=True)
        await self.settle()

    async def test_a_skipped_join_is_counted_and_does_nothing_else(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 100, 1.0)", (self.guild.id,))
        self.reads = 0
        writes = []
        real = self.cog_._add_joins

        async def counted(guild, joins, **options):
            writes.append(joins)
            return await real(guild, joins, **options)

        reads = []
        read_one, read_all = self.db.scalar, self.db.all

        async def scalar(sql, args=()):
            reads.append(sql)
            return await read_one(sql, args)

        async def every(sql, args=()):
            reads.append(sql)
            return await read_all(sql, args)

        with mock.patch.object(self.cog_, "_add_joins", counted), mock.patch.object(self.db, "scalar", scalar), mock.patch.object(self.db, "all", every):
            for number in range(3):
                await self.join(f"first{number}")
            await self.settle()
            welcomed = len(reads)
            for number in range(297):
                await self.join(f"raider{number}")
            await self.settle()
        self.assertLess(len(reads) - welcomed, 20, "a skipped join reads nothing. Only the few writes that add them up read the count back.")
        self.assertEqual(len(self.channel.sent), 3)
        self.assertEqual(await self.count(), 400, "every one of them is in the count")
        self.assertEqual(self.reads, 3, "invites were read for the three who were welcomed")
        self.assertEqual(sum(writes), 300)
        self.assertLess(len(writes), 20, "the skipped ones were written together, not one write each")
        self.assertEqual(len(await self.posts()), 6, "three welcomes and their three DMs are noted, and nothing for the rest")
        self.assertEqual(len(self.cog_._welcomed), 3)

    async def test_the_join_after_a_burst_takes_the_uncounted_ones_with_it(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 100, 1.0)", (self.guild.id,))
        self.cog_._uncounted[self.guild.id] = 25  # skipped joins whose write hasn't happened yet
        self.cog_._joins.clear()
        await self.join("after")
        self.assertEqual(self.channel.sent[-1].content.split()[1], "126")

    async def test_a_burst_of_people_on_the_rules_screen_is_only_counted_too(self):
        await self.configure(wait_for_rules=True)
        await self.settle()
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 100, 1.0)", (self.guild.id,))
        self.reads = 0
        for number in range(50):
            await self.join(f"raider{number}", pending=True)
        await self.settle()
        self.assertEqual(await self.count(), 150)
        self.assertEqual(len(self.cog_._join_numbers), 3, "numbers are held for the few inside the limit")
        self.assertEqual(self.reads, 3)
        self.assertEqual(self.channel.sent, [])

    async def test_a_skipped_welcome_lets_go_of_the_number_held_for_it(self):
        await self.configure(wait_for_rules=True, burst_joins=2)
        await self.settle()
        waiting = [await self.join(f"waiting{number}", pending=True) for number in range(2)]
        self.assertEqual(len(self.cog_._join_numbers), 2)
        for number in range(3):
            await self.join(f"quick{number}")
        for member in waiting:
            member.pending = False
            await self.cog_.on_member_update(SimpleNamespace(pending=True), member)
        self.assertEqual(len(self.channel.sent), 2, "the two quick ones filled the minute")
        self.assertEqual(self.cog_._join_numbers, {})

    async def test_skipped_joins_a_failed_write_could_not_count_are_counted_with_the_next(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 100, 1.0)", (self.guild.id,))
        real = self.cog_._add_joins
        failures = [RuntimeError("disk full")]

        async def once_broken(guild, joins, **options):
            if failures:
                raise failures.pop()
            return await real(guild, joins, **options)

        self.cog_._joins[self.guild.id] = collections.deque([time.monotonic()] * 4)  # past the minute's limit
        with mock.patch.object(self.cog_, "_add_joins", once_broken), self.assertLogs(greetings_module.log, "ERROR"):
            for number in range(3):
                await self.join(f"skipped{number}")
            await self.settle()
        self.assertEqual(self.cog_._uncounted, {self.guild.id: 3}, "kept for the next write")
        self.assertEqual(await self.count(), 100)
        self.cog_._joins.clear()
        await self.join("next")
        self.assertEqual(await self.count(), 104)
        self.assertEqual(self.channel.sent[-1].content.split()[1], "104")

    async def test_what_memory_holds_has_a_ceiling(self):
        with mock.patch.object(greetings_module, "MAX_HELD", 5):
            for number in range(12):
                self.cog_._hold(self.cog_._join_numbers, (self.guild.id, number), number)
        self.assertEqual(sorted(self.cog_._join_numbers.values()), [7, 8, 9, 10, 11])


class NotedPostsTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.goodbyes = FakeChannel(self.guild, "goodbyes")
        await self.configure(welcome_channel=self.channel.id, farewell_channel=self.goodbyes.id, welcome=text("Hi {user}"), farewell=text("Bye {user}"), ban=text("Banned {user}"))

    async def test_each_post_is_noted_with_who_it_is_about(self):
        member = await self.join()
        await self.cog_.on_member_remove(member)
        banned = FakeMember(self.guild, "banned")
        await self.cog_.on_member_ban(self.guild, banned)
        rows = await self.posts()
        self.assertEqual([(row["kind"], row["user_id"], row["channel_id"], row["message_id"]) for row in rows], [
            ("welcome", member.id, self.channel.id, self.channel.sent[0].id), ("farewell", member.id, self.goodbyes.id, self.goodbyes.sent[0].id),
            ("ban", banned.id, self.goodbyes.id, self.goodbyes.sent[1].id)])
        self.assertTrue(all(abs(row["posted_at"] - time.time()) < 60 and row["guild_id"] == self.guild.id for row in rows))
        self.assertEqual(await self.timers(), [], "with no lifetime set, nothing is scheduled")

    async def test_a_join_dm_is_noted_only_where_a_rejoin_is_measured_from_it(self):
        await self.configure(dm=text("Hello"))
        await self.join("first")
        self.assertEqual([row["kind"] for row in await self.posts()], ["welcome"])
        await self.configure(welcome_again_after=5)
        await self.join("second")
        self.assertEqual([row["kind"] for row in await self.posts()], ["welcome", "welcome", "dm"])

    async def test_a_post_discord_refused_is_not_noted(self):
        async def refuse(**kwargs):
            raise refusal(403)

        self.channel.send = refuse
        with self.assertLogs(greetings_module.log, "WARNING"):
            await self.join()
        self.assertEqual(await self.posts(), [])

    async def test_a_test_post_is_not_noted(self):
        await self.cog_.greetings_testgreet.callback(self.cog_, self.context())
        client = await self.client()
        await client.post(self.path("/greetings/test"), json={"which": "welcome", "payload": text("Hi"), "channel_id": str(self.channel.id)})
        self.assertEqual(await self.posts(), [])

    async def test_noting_never_breaks_a_post_that_went_out(self):
        with mock.patch.object(self.db, "run", mock.AsyncMock(side_effect=RuntimeError("disk full"))), self.assertLogs(greetings_module.log, "ERROR"):
            await self.cog_._note_post(self.guild, 5, "welcome", SimpleNamespace(id=7, channel=self.channel))
        with self.assertNoLogs(greetings_module.log, "ERROR"):
            await self.cog_._note_post(self.guild, 5, "welcome", None)
            await self.cog_._note_post(self.guild, 5, "welcome", SimpleNamespace())
        self.assertEqual(await self.posts(), [])

    async def test_posts_are_kept_thirty_days(self):
        now = time.time()
        for message_id, age in ((1, 31), (2, 29), (3, 0)):
            await self.db.run("INSERT INTO greeting_posts (guild_id, user_id, kind, channel_id, message_id, posted_at) VALUES (?, 5, 'welcome', ?, ?, ?)", (self.guild.id, self.channel.id, message_id, now - age * 86400))
        self.cog_._cleared_at = now
        await self.join("soon")
        self.assertEqual(len(await self.posts()), 4, "it looks once an hour at most")
        self.cog_._cleared_at = now - 3601
        await self.join("later")
        self.assertEqual(sorted(row["message_id"] for row in await self.posts() if row["message_id"] < 10), [2, 3])

    async def test_a_server_s_noted_posts_have_a_ceiling(self):
        now = time.time()
        await self.db.many("INSERT INTO greeting_posts (guild_id, user_id, kind, channel_id, message_id, posted_at) VALUES (?, 5, 'welcome', ?, ?, ?)",
                           [(self.guild.id, self.channel.id, number, now - 1000 + number) for number in range(1, 31)])
        await self.db.run("INSERT INTO greeting_posts (guild_id, user_id, kind, channel_id, message_id, posted_at) VALUES (999, 5, 'welcome', 1, 500, ?)", (now - 5000,))
        self.cog_._cleared_at = 0
        with mock.patch.object(greetings_module, "MAX_POSTS", 10):
            await self.join("newest")
        kept = [row["message_id"] for row in await self.posts(guild_id=self.guild.id)]
        self.assertEqual(len(kept), 10)
        self.assertEqual(sorted(kept)[:9], list(range(22, 31)), "the oldest go, and the newest stay")
        self.assertEqual(len(await self.posts(guild_id=999)), 1, "another server's are its own")
        self.assertEqual(Greetings.row_limits, {"greeting_posts": greetings_module.MAX_POSTS})


class DeleteAfterTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {user}"), farewell=text("Bye {user}"), ban=text("Banned {user}"), welcome_delete_after=600)

    async def test_a_welcome_is_deleted_when_its_lifetime_is_over(self):
        await self.join()
        [timer] = await self.timers()
        [row] = await self.posts()
        self.assertAlmostEqual(timer["due_at"], row["posted_at"] + 600, delta=2)
        self.assertEqual((timer["key"], timer["payload"]), (str(row["message_id"]), {"guild_id": self.guild.id, "channel_id": self.channel.id, "message_id": row["message_id"]}))
        await self.bot.timers.run_due(time.time() + 599)
        self.assertEqual(len(self.channel.sent), 1, "not before its time")
        await self.bot.timers.run_due(time.time() + 601)
        self.assertEqual((self.channel.sent, await self.posts(), await self.timers()), ([], [], []))

    async def test_each_post_has_its_own_timer_and_none_is_left_behind(self):
        for number in range(4):
            await self.join(f"person{number}")
        self.assertEqual(len(await self.timers()), 4)
        await self.bot.timers.run_due(time.time() + 700)
        self.assertEqual((self.channel.sent, await self.posts(), await self.timers()), ([], [], []))

    async def test_a_message_that_is_already_gone_counts_as_deleted(self):
        await self.join()
        await self.channel.sent[0].delete()
        gone = mock.Mock()
        gone.delete = mock.AsyncMock(side_effect=refusal(404))
        with mock.patch.object(self.channel, "get_partial_message", return_value=gone):
            await self.bot.timers.run_due(time.time() + 601)
        gone.delete.assert_awaited_once()
        self.assertEqual((await self.posts(), await self.timers()), ([], []))

    async def test_running_twice_does_no_harm(self):
        await self.join()
        [timer] = await self.timers()
        await self.cog_._delete_due(timer["payload"], timer)
        await self.cog_._delete_due(timer["payload"], timer)
        self.assertEqual((self.channel.sent, await self.posts()), ([], []))

    async def test_a_delete_discord_forbids_is_tried_again_in_an_hour_while_the_post_is_noted(self):
        await self.join()
        post = self.channel.sent[-1]
        blocked = mock.Mock()
        blocked.delete = mock.AsyncMock(side_effect=refusal(403))  # the bot lost View Channel there for a while
        with mock.patch.object(self.channel, "get_partial_message", return_value=blocked):
            await self.bot.timers.run_due(time.time() + 601)
        [timer] = await self.timers()
        self.assertAlmostEqual(timer["due_at"], time.time() + greetings_module.FORBIDDEN_RETRY, delta=5)
        self.assertEqual((timer["tries"], len(await self.posts())), (0, 1), "put back as it was, and still noted")
        self.assertEqual(greetings_module.FORBIDDEN_RETRY, 3600)
        await self.bot.timers.run_due(time.time() + 3601)  # the access is back
        self.assertTrue(post.deleted)
        self.assertEqual((await self.posts(), await self.timers()), ([], []))

    async def test_a_delete_still_forbidden_after_thirty_days_is_given_up(self):
        await self.join()
        await self.db.run("UPDATE greeting_posts SET posted_at = posted_at - ?", (greetings_module.KEEP_POSTS + 1,))
        blocked = mock.Mock()
        blocked.delete = mock.AsyncMock(side_effect=refusal(403))
        with mock.patch.object(self.channel, "get_partial_message", return_value=blocked):
            await self.bot.timers.run_due(time.time() + 601)
        self.assertEqual((await self.posts(), await self.timers()), ([], []))

    async def test_a_forbidden_delete_whose_post_is_no_longer_noted_is_given_up(self):
        await self.join()
        await self.db.run("DELETE FROM greeting_posts")
        blocked = mock.Mock()
        blocked.delete = mock.AsyncMock(side_effect=refusal(403))
        with mock.patch.object(self.channel, "get_partial_message", return_value=blocked):
            await self.bot.timers.run_due(time.time() + 601)
        self.assertEqual(await self.timers(), [])

    async def test_a_delete_discord_refuses_for_good_is_not_asked_again(self):
        await self.join()
        refused = mock.Mock()
        refused.delete = mock.AsyncMock(side_effect=refusal(400))
        with mock.patch.object(self.channel, "get_partial_message", return_value=refused):
            await self.bot.timers.run_due(time.time() + 601)
        refused.delete.assert_awaited_once()
        self.assertEqual((await self.posts(), await self.timers()), ([], []))

    async def test_a_goodbye_s_shorter_lifetime_is_not_held_back_by_a_welcome_s_longer_one(self):
        await self.configure(welcome_delete_after=86400, farewell_delete_after=60, burst_joins=0)
        people = [await self.join(f"person{number}") for number in range(3)]
        await self.cog_.on_member_remove(people[0])
        [timer] = await self.timers("farewell")
        self.assertAlmostEqual(timer["due_at"], time.time() + 60, delta=5)

    async def test_a_delete_discord_rate_limits_goes_back_to_the_timer(self):
        await self.join()
        post = self.channel.sent[-1]
        limited = mock.Mock()
        limited.delete = mock.AsyncMock(side_effect=refusal(429))
        with mock.patch.object(self.channel, "get_partial_message", return_value=limited), self.assertLogs("captainguild.core.timers", "ERROR"):
            await self.bot.timers.run_due(time.time() + 601)
        [timer] = await self.timers()
        self.assertEqual((timer["tries"], len(await self.posts())), (1, 1), "tried again later, and still noted")
        await self.bot.timers.run_due(time.time() + 3600)
        self.assertTrue(post.deleted)
        self.assertEqual((await self.posts(), await self.timers()), ([], []))

    async def test_a_delete_that_fails_for_now_stays_on_the_timer(self):
        await self.join()
        broken = mock.Mock()
        broken.delete = mock.AsyncMock(side_effect=refusal(500))
        with mock.patch.object(self.channel, "get_partial_message", return_value=broken), self.assertLogs("captainguild.core.timers", "ERROR"):
            await self.bot.timers.run_due(time.time() + 601)
        [timer] = await self.timers()
        self.assertEqual(timer["tries"], 1)
        self.assertEqual(len(await self.posts()), 1)
        await self.bot.timers.run_due(time.time() + 3600)
        self.assertEqual((self.channel.sent, await self.posts(), await self.timers()), ([], [], []))

    async def test_a_delete_discord_keeps_waiting_steps_aside_and_comes_back(self):
        self.cog_.DELETE_PATIENCE = 0.05
        await self.join()
        slow = mock.Mock()

        async def wait(**kwargs):
            await asyncio.sleep(30)

        slow.delete = wait
        began = time.monotonic()
        with mock.patch.object(self.channel, "get_partial_message", return_value=slow):
            await self.bot.timers.run_due(time.time() + 601)
        self.assertLess(time.monotonic() - began, 5, "the timers behind it weren't held up")
        [timer] = await self.timers()
        self.assertAlmostEqual(timer["due_at"], time.time() + greetings_module.DELETE_RETRY, delta=5)
        self.assertEqual(timer["tries"], 0)
        self.assertEqual(len(await self.posts()), 1)
        await self.bot.timers.run_due(time.time() + 3600)
        self.assertEqual((self.channel.sent, await self.posts(), await self.timers()), ([], [], []))

    async def test_deletes_discord_keeps_waiting_hold_the_other_timers_a_bounded_time(self):
        await self.configure(burst_joins=0)
        for number in range(12):
            await self.join(f"member{number}")

        asked = []

        async def hangs(*args, **kwargs):
            asked.append(1)
            await asyncio.sleep(3600)

        for sent in self.channel.sent:
            sent.delete = hangs
        self.cog_.DELETE_PATIENCE = 0.25
        self.cog_.DELETE_HOLD = 0.4
        self.assertLess(await self.other_timer_late(), 1.5, "two deletes used the minute's hold, and the other ten stepped aside without waiting")
        self.assertEqual(len(asked), 2, "the ten that stepped aside never asked Discord")
        self.assertEqual(len(self.cog_._delete_waits), 3, "two deletes waited, and the third put the other ten back in one write")
        self.assertLess(sum(seconds for _, seconds in self.cog_._delete_waits), 0.4 + 0.3, "the minute's hold, and the writes that put deletes back")
        timers = await self.timers()
        self.assertEqual(len(timers), 12, "every delete is still on its timer")
        due_at = sorted(timer["due_at"] for timer in timers)
        self.assertTrue(all(later - earlier >= greetings_module.DELETE_SPACING - 0.001 for earlier, later in zip(due_at, due_at[1:])), "and they come back spaced, not all at once")
        self.assertGreater(due_at[0], time.time() + greetings_module.DELETE_RETRY - 5)
        self.assertEqual(Greetings.DELETE_HOLD, 5.0)

    async def other_timer_late(self) -> float:
        """How late another feature's timer, due just after the deletes, runs behind them. Every timer is moved into the past first, so the
        deletes are due by the clock, as the loop finds them."""
        await self.db.run("UPDATE timers SET due_at = due_at - 700")
        ran = {}

        async def other(payload, timer):
            ran.setdefault("at", time.perf_counter())

        self.bot.timers.handler("another.feature", other)
        await self.bot.timers.schedule("another.feature", time.time() - 1, {}, guild_id=self.guild.id, key="one")
        began = time.perf_counter()
        while "at" not in ran:
            await self.bot.timers.run_due()
        return ran["at"] - began

    async def test_a_channel_lookup_discord_keeps_waiting_is_inside_the_patience(self):
        await self.configure(burst_joins=0)
        await self.join("one")
        self.cog_.DELETE_PATIENCE = 0.25

        async def slow_lookup(guild, channel_id):
            await asyncio.sleep(2.0)  # a thread that isn't in the bot's lists, looked up on Discord, which is slow to answer
            return None

        with mock.patch.object(greetings_module.delivery, "find_channel", slow_lookup):
            late = await self.other_timer_late()
        self.assertLess(late, 1.0, "the lookup waited no longer than a delete may")
        self.assertEqual(len(await self.timers()), 1, "and the delete went back on its timer")

    async def test_putting_many_deletes_back_takes_one_write_inside_the_hold(self):
        await self.configure(burst_joins=0)
        for number in range(60):
            await self.join(f"member{number}")

        async def hangs(*args, **kwargs):
            await asyncio.sleep(3600)

        for sent in self.channel.sent:
            sent.delete = hangs
        self.cog_.DELETE_PATIENCE = 0.25
        self.cog_.DELETE_HOLD = 0.4
        late = await self.other_timer_late()
        self.assertLess(late, 1.5, "two deletes used the hold, and the other 58 went back together")
        self.assertEqual(len(self.cog_._delete_waits), 3, "the two that waited, and the one that put the rest back")
        timers = await self.timers()
        self.assertEqual(len(timers), 60, "every delete is still on its timer")
        due_at = sorted(timer["due_at"] for timer in timers)
        self.assertTrue(all(later - earlier >= greetings_module.DELETE_SPACING - 0.001 for earlier, later in zip(due_at, due_at[1:])), "and they come back spaced")

    async def test_with_the_hold_used_the_deletes_of_the_coming_minute_go_back_in_the_same_write(self):
        await self.configure(burst_joins=0)
        for number in range(40):
            await self.join(f"member{number}")
        await self.db.run("UPDATE timers SET due_at = due_at - 620")  # due a second apart, from 20 s ago to 19 s from now
        self.cog_._delete_waits.append((time.monotonic(), self.cog_.DELETE_HOLD))  # the minute's hold is used
        await self.bot.timers.run_due()
        due_at = sorted(timer["due_at"] for timer in await self.timers())
        self.assertEqual(len(due_at), 40)
        self.assertGreater(due_at[0], time.time() + greetings_module.DELETE_RETRY - 5, "the ones not due yet went back with the rest")
        self.assertEqual(len(self.cog_._delete_waits), 2, "in one write, from the first of them the loop handed over")

    async def test_one_server_s_deletes_put_off_never_delay_another_server_s(self):
        await self.configure(burst_joins=0)
        for number in range(30):
            await self.join(f"member{number}")
        await self.db.run("UPDATE timers SET due_at = due_at - 700")
        self.cog_._delete_waits.append((time.monotonic(), self.cog_.DELETE_HOLD))  # the minute's hold is used
        await self.bot.timers.run_due()
        due_at = sorted(timer["due_at"] for timer in await self.timers())
        self.assertAlmostEqual(due_at[-1] - due_at[0], 29 * greetings_module.DELETE_SPACING, delta=0.01, msg="this server's come back spaced")
        another = FakeGuild("Elsewhere")
        self.assertAlmostEqual(self.cog_._put_off_slot(another.id), time.time() + greetings_module.DELETE_RETRY, delta=5,
                               msg="another server's next comes back no later than its own retry")
        self.assertAlmostEqual(self.cog_._put_off_slot(self.guild.id), due_at[-1] + greetings_module.DELETE_SPACING, delta=0.01, msg="and this server's after its own")

    async def test_a_delete_handed_over_while_the_hold_is_used_is_never_dropped(self):
        # The loop hands over what's due, and a delete it hands over goes back on its timer whatever the clock says.
        await self.configure(burst_joins=0)
        await self.join("one")
        before = (await self.timers())[0]
        self.cog_._delete_waits.append((time.monotonic(), self.cog_.DELETE_HOLD))  # the minute's hold is used
        await self.bot.timers.run_due(before["due_at"] + 1)  # as if its time had come
        after = await self.timers()
        self.assertEqual(len(after), 1, "still on its timer")
        self.assertAlmostEqual(after[0]["due_at"], time.time() + greetings_module.DELETE_RETRY, delta=5)

    async def test_the_hold_is_per_minute_and_quick_deletes_use_almost_none_of_it(self):
        await self.configure(burst_joins=0)
        for number in range(5):
            await self.join(f"member{number}")
        self.cog_.DELETE_HOLD = 0.5
        await self.bot.timers.run_due(time.time() + 700)
        self.assertEqual((self.channel.sent, await self.timers()), ([], []))
        now = [time.monotonic()]
        self.cog_._delete_waits.append((now[0], 0.5))  # the minute's hold used up
        self.assertLessEqual(self.cog_._delete_patience(now[0]), 0)
        self.assertEqual(self.cog_._delete_patience(now[0] + greetings_module.DELETE_WINDOW + 1), 0.5, "a minute on, the hold is whole again")

    async def test_a_channel_that_is_gone_ends_it(self):
        await self.join()
        self.guild.channels.remove(self.channel)
        await self.bot.timers.run_due(time.time() + 601)
        self.assertEqual((await self.posts(), await self.timers()), ([], []))

    async def test_a_timer_only_ever_reaches_into_its_own_server(self):
        other = FakeGuild("Elsewhere")
        self.bot.fake_guilds.append(other)
        theirs = FakeChannel(other, "theirs")
        message = await theirs.send("their own post")
        payload = {"guild_id": self.guild.id, "channel_id": theirs.id, "message_id": message.id}
        await self.cog_._delete_due(payload, {"kind": DELETE["welcome"], "key": str(message.id)})
        self.assertEqual(theirs.sent, [message])
        self.assertFalse(message.deleted)

    async def test_goodbye_and_ban_messages_have_lifetimes_of_their_own(self):
        await self.configure(welcome_delete_after=0, farewell_delete_after=300, ban_delete_after=900)
        member = await self.join()
        await self.cog_.on_member_remove(member)
        await self.cog_.on_member_ban(self.guild, FakeMember(self.guild, "banned"))
        self.assertEqual([len(await self.timers(kind)) for kind in ("welcome", "farewell", "ban")], [0, 1, 1])
        await self.bot.timers.run_due(time.time() + 301)
        self.assertEqual([sent.content for sent in self.channel.sent], ["Hi newcomer", "Banned banned"])
        await self.bot.timers.run_due(time.time() + 901)
        self.assertEqual([sent.content for sent in self.channel.sent], ["Hi newcomer"])

    async def test_timers_of_one_server_are_spaced_so_a_burst_of_posts_is_not_due_at_once(self):
        await self.configure(burst_joins=0)
        for number in range(5):
            await self.join(f"person{number}")
        due = [timer["due_at"] for timer in await self.timers()]
        self.assertEqual(due, sorted(due))
        self.assertTrue(all(later - earlier >= greetings_module.DELETE_SPACING - 0.001 for earlier, later in zip(due, due[1:])), due)

    async def test_leave_messages_are_spaced_among_themselves(self):
        await self.configure(burst_joins=0, welcome_delete_after=86400, farewell_delete_after=600, ban=text(""))
        people = [await self.join(f"person{number}") for number in range(4)]
        for person in people:
            await self.cog_.on_member_remove(person)
        due = [timer["due_at"] for timer in await self.timers("farewell")]
        self.assertEqual(len(due), 4)
        self.assertTrue(all(later - earlier >= greetings_module.DELETE_SPACING - 0.001 for earlier, later in zip(due, due[1:])), due)
        self.assertAlmostEqual(due[0], time.time() + 600, delta=5)
        await self.join("after")
        welcome = [timer["due_at"] for timer in await self.timers()]
        self.assertGreaterEqual(welcome[-1] - welcome[-2], greetings_module.DELETE_SPACING - 0.001, "and a welcome is spaced after the welcomes before it, whatever left in between")


class LifetimeChangeTest(Case):
    """Posts already noted follow a lifetime when it changes."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {user}"), farewell=text("Bye {user}"), farewell_delete_after=3600)
        self.now = time.time()

    async def note(self, message_id, age, kind="welcome"):
        await self.db.run("INSERT INTO greeting_posts (guild_id, user_id, kind, channel_id, message_id, posted_at) VALUES (?, 5, ?, ?, ?, ?)", (self.guild.id, kind, self.channel.id, message_id, self.now - age))

    async def due(self, kind="welcome"):
        return {int(timer["key"]): timer["due_at"] for timer in await self.timers(kind)}

    async def test_posts_noted_with_no_lifetime_get_one_when_it_is_set(self):
        await self.note(1, 100)
        await self.note(2, 10)
        self.assertEqual(await self.due(), {})
        await self.configure(welcome_delete_after=600)
        due = await self.due()
        self.assertAlmostEqual(due[1], self.now + 500, delta=3)
        self.assertAlmostEqual(due[2], self.now + 590, delta=3)

    async def test_a_longer_and_a_shorter_lifetime_both_move_them(self):
        await self.note(1, 100)
        await self.configure(welcome_delete_after=600)
        await self.configure(welcome_delete_after=7200)
        self.assertAlmostEqual((await self.due())[1], self.now + 7100, delta=3)
        await self.configure(welcome_delete_after=300)
        self.assertAlmostEqual((await self.due())[1], self.now + 200, delta=3)
        self.assertEqual(len(await self.timers()), 1, "moved, not added again")

    async def test_zero_takes_the_timers_away_and_leaves_the_posts(self):
        await self.note(1, 100)
        message = await self.channel.send("stays")
        await self.note(message.id, 50)
        await self.configure(welcome_delete_after=600)
        self.assertEqual(len(await self.timers()), 2)
        await self.bot.timers.schedule(DELETE["welcome"], self.now + 5, {"guild_id": self.guild.id, "channel_id": self.channel.id, "message_id": message.id + 1}, guild_id=self.guild.id, key="404")
        await self.configure(welcome_delete_after=0)
        self.assertEqual(await self.timers(), [], "one whose post is no longer noted goes too")
        await self.bot.timers.run_due(time.time() + 99999)
        self.assertEqual(self.channel.sent, [message])
        self.assertEqual(len(await self.posts()), 2)

    async def test_a_new_lifetime_leaves_alone_a_timer_whose_post_is_no_longer_noted(self):
        await self.note(1, 100)
        await self.configure(welcome_delete_after=600)
        await self.bot.timers.schedule(DELETE["welcome"], self.now + 5, {"guild_id": self.guild.id, "channel_id": 1, "message_id": 404}, guild_id=self.guild.id, key="404")
        await self.configure(welcome_delete_after=7200)
        due = await self.due()
        self.assertAlmostEqual(due[404], self.now + 5, delta=1)
        self.assertAlmostEqual(due[1], self.now + 7100, delta=3)

    async def test_posts_already_past_the_new_lifetime_go_one_at_a_time_oldest_first(self):
        for number in range(1, 6):
            await self.note(number, 5000 - number)
        await self.note(9, 10)
        await self.configure(welcome_delete_after=600)
        due = await self.due()
        late = [due[number] for number in range(1, 6)]
        self.assertAlmostEqual(late[0], time.time(), delta=3)
        self.assertEqual([round(later - earlier, 3) for earlier, later in zip(late, late[1:])], [greetings_module.BACKLOG_SPACING] * 4)
        self.assertAlmostEqual(due[9], self.now + 590, delta=3)

    async def test_moved_timers_wake_the_loop_that_runs_them(self):
        await self.note(1, 5000)
        self.bot.timers._wake.clear()
        await self.configure(welcome_delete_after=600)
        self.assertTrue(self.bot.timers._wake.is_set(), "a post already past its new lifetime is due now, and the loop may be asleep until some later timer")

    async def test_one_kind_s_change_leaves_the_others_alone(self):
        await self.note(1, 100)
        await self.note(2, 100, "farewell")
        await self.note(3, 100, "ban")
        await self.configure(farewell_delete_after=3660)
        before = await self.due("farewell")
        self.assertEqual(set(before), {2})
        await self.configure(welcome_delete_after=600)
        self.assertEqual(await self.due("farewell"), before)
        self.assertEqual((set(await self.due()), await self.due("ban")), ({1}, {}))
        await self.configure(ban_delete_after=60)
        self.assertEqual(set(await self.due("ban")), {3})
        self.assertEqual(await self.due("farewell"), before)

    async def test_a_save_that_changes_no_lifetime_moves_nothing(self):
        await self.note(1, 100)
        await self.configure(welcome_delete_after=600)
        with mock.patch.object(self.cog_, "_move_delete_timers", mock.AsyncMock()) as moved:
            await self.configure(welcome=text("Hi again"), burst_joins=5, welcome_bots=False)
            moved.assert_not_awaited()
            await self.configure(ban_delete_after=60)
            moved.assert_awaited_once_with(self.guild.id, "ban")

    async def test_a_post_made_after_a_change_gets_the_new_lifetime(self):
        await self.configure(welcome_delete_after=86400, burst_joins=0)
        for number in range(3):
            await self.join(f"early{number}")
        await self.configure(welcome_delete_after=60)
        await self.join("late")
        due = sorted(timer["due_at"] for timer in await self.timers())
        self.assertAlmostEqual(due[-1], time.time() + 60, delta=5, msg="the day-long timers set before don't push a new post's delete back")

    async def test_a_lifetime_change_made_while_a_post_is_being_noted_still_reaches_it(self):
        await self.configure(welcome_delete_after=600)
        real = self.db.run
        changed = []

        async def run(sql, args=()):
            done = await real(sql, args)
            if "INTO greeting_posts" in sql and not changed:
                changed.append(True)
                # The setting is saved and its timers moved by another task while this post's note is still being written.
                saving = asyncio.ensure_future(self.configure(welcome_delete_after=7200))
                await asyncio.sleep(0.05)
                self.assertFalse(saving.done(), "the move waits for the note to finish")
                self.saving = saving
            return done

        with mock.patch.object(self.db, "run", run):
            await self.join()
            await self.saving
        [timer] = await self.timers()
        [row] = await self.posts()
        self.assertAlmostEqual(timer["due_at"], row["posted_at"] + 7200, delta=5)

    async def test_a_move_that_fails_is_logged_and_tried_again_at_the_next_save(self):
        await self.note(1, 100)
        with mock.patch.object(self.cog_, "_move_delete_timers", mock.AsyncMock(side_effect=RuntimeError("disk full"))), self.assertLogs(greetings_module.log, "ERROR"):
            await self.configure(welcome_delete_after=600)
        self.assertEqual((self.settings("greetings")["welcome_delete_after"], await self.due()), (600, {}), "the setting is saved, and the timers aren't there yet")
        await self.configure(welcome_bots=False)
        self.assertEqual(set(await self.due()), {1}, "the next save of the page moves them")

    async def test_another_server_s_posts_and_timers_are_not_touched(self):
        await self.db.run("INSERT INTO greeting_posts (guild_id, user_id, kind, channel_id, message_id, posted_at) VALUES (999, 5, 'welcome', 1, 77, ?)", (self.now,))
        await self.bot.timers.schedule(DELETE["welcome"], self.now + 50, {"guild_id": 999, "channel_id": 1, "message_id": 77}, guild_id=999, key="77")
        await self.note(1, 100)
        await self.configure(welcome_delete_after=600)
        await self.configure(welcome_delete_after=0)
        self.assertEqual([timer["key"] for timer in await self.bot.timers.list_kind(DELETE["welcome"])], ["77"])

    async def test_after_a_restart_a_change_is_measured_from_what_was_saved(self):
        await self.note(1, 100)
        await self.configure(welcome_delete_after=600)
        fresh = Greetings(self.bot)
        with mock.patch.object(self.bot.timers, "handler"), mock.patch.object(self.bot.settings, "listen"):
            await fresh.on_load()
        self.assertEqual(fresh._lifetimes[self.guild.id], (600, 3600, 0))

    async def test_a_restored_backup_s_posts_get_their_timers_again(self):
        await self.configure(welcome_delete_after=600)
        await self.note(1, 100)
        await self.note(2, 100, "farewell")
        await self.bot.timers.schedule(DELETE["welcome"], self.now + 5, {"guild_id": self.guild.id, "channel_id": 1, "message_id": 404}, guild_id=self.guild.id, key="404")
        self.cog_._lifetimes.clear()
        self.cog_._join_numbers[(self.guild.id, 5)] = 9
        await self.cog_.on_guild_restored(self.guild.id)
        due = await self.due()
        self.assertEqual((set(due), set(await self.due("farewell"))), ({1, 404}, {2}))
        self.assertAlmostEqual(due[404], self.now + 5, delta=1, msg="a post made since the backup isn't in it, and its timer still holds a delete that's wanted")
        self.assertAlmostEqual(due[1], self.now + 500, delta=3)
        self.assertEqual(self.cog_._lifetimes[self.guild.id], (600, 3600, 0))
        self.assertEqual(self.cog_._join_numbers, {(self.guild.id, 5): 9}, "what's held for people on the rules screen is still true")

    async def test_a_restore_to_no_lifetime_takes_every_timer_of_that_kind(self):
        await self.note(1, 100)
        await self.bot.timers.schedule(DELETE["welcome"], self.now + 5, {"guild_id": self.guild.id, "channel_id": 1, "message_id": 404}, guild_id=self.guild.id, key="404")
        await self.bot.timers.schedule(DELETE["welcome"], self.now + 5, {"guild_id": self.guild.id, "channel_id": 1, "message_id": 1}, guild_id=self.guild.id, key="1")
        await self.cog_.on_guild_restored(self.guild.id)
        self.assertEqual(await self.due(), {})


class DeleteOnLeaveTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {user}"), farewell=text("Bye {user}"), welcome_delete_on_leave=True)

    async def test_a_member_s_welcome_goes_when_they_leave(self):
        staying = await self.join("staying")
        leaving = await self.join("leaving")
        await self.leave(leaving)
        self.assertEqual([sent.content for sent in self.channel.sent], ["Hi staying", "Bye leaving"])
        self.assertEqual([(row["kind"], row["user_id"]) for row in await self.posts()], [("welcome", staying.id), ("farewell", leaving.id)])

    async def test_every_welcome_of_someone_who_came_back_several_times_goes(self):
        member = FakeMember(self.guild, "returner")
        for _ in range(3):
            await self.cog_.on_member_join(member)
        self.assertEqual(len(self.channel.sent), 3)
        await self.configure(farewell=text(""))
        await self.leave(member)
        self.assertEqual((self.channel.sent, await self.posts()), ([], []))

    async def test_it_does_nothing_where_it_is_switched_off(self):
        await self.configure(welcome_delete_on_leave=False)
        member = await self.join()
        with mock.patch.object(self.cog_, "_delete_welcomes", mock.AsyncMock()) as looked:
            await self.leave(member)
            await self.cog_.on_member_ban(self.guild, member)
        looked.assert_not_awaited()
        self.assertEqual([sent.content for sent in self.channel.sent], ["Hi newcomer", "Bye newcomer"])

    async def test_a_ban_deletes_it_too_even_of_someone_who_had_already_left(self):
        await self.configure(welcome_delete_on_leave=False)
        member = await self.join()
        self.guild.members.remove(member)
        await self.configure(welcome_delete_on_leave=True, farewell=text(""))
        await self.cog_.on_member_ban(self.guild, member)
        self.assertEqual((self.channel.sent, await self.posts()), ([], []))

    async def test_a_ban_message_about_them_stays(self):
        await self.configure(ban=text("Banned {user}"), farewell=text(""))
        member = await self.join()
        self.guild.members.remove(member)
        await self.cog_.on_member_ban(self.guild, member)
        self.assertEqual([sent.content for sent in self.channel.sent], ["Banned newcomer"])
        self.assertEqual([row["kind"] for row in await self.posts()], ["ban"])

    async def test_a_goodbye_from_an_earlier_leave_stays(self):
        member = await self.join()
        await self.leave(member)
        self.guild.members.append(member)
        await self.cog_.on_member_join(member)
        await self.configure(farewell=text(""))
        await self.leave(member)
        self.assertEqual([sent.content for sent in self.channel.sent], ["Bye newcomer"])

    async def test_its_lifetime_timer_goes_with_it(self):
        await self.configure(welcome_delete_after=600, farewell=text(""))
        member = await self.join()
        self.assertEqual(len(await self.timers()), 1)
        await self.leave(member)
        self.assertEqual((self.channel.sent, await self.timers()), ([], []))

    async def test_a_welcome_that_lands_after_they_left_is_deleted_at_once(self):
        member = FakeMember(self.guild, "quick")
        real = self.channel.send

        async def slow(*args, **kwargs):
            self.guild.members.remove(member)  # they leave while the post is on its way, before there's anything noted to delete
            await self.cog_.on_raw_member_remove(SimpleNamespace(guild_id=self.guild.id, user=member))
            return await real(*args, **kwargs)

        self.channel.send = slow
        await self.cog_.on_member_join(member)
        self.assertEqual((self.channel.sent, await self.posts()), ([], []))

    async def test_a_delete_that_fails_for_now_is_tried_again_and_the_rest_still_go(self):
        await self.configure(farewell=text(""))
        member = FakeMember(self.guild, "returner")
        await self.cog_.on_member_join(member)
        await self.cog_.on_member_join(member)
        first, second = self.channel.sent
        real = first.delete
        first.delete = mock.AsyncMock(side_effect=refusal(500))
        await self.leave(member)
        self.assertEqual(self.channel.sent, [first])
        self.assertEqual([row["message_id"] for row in await self.posts()], [first.id])
        [timer] = await self.retries()
        self.assertEqual((timer["key"], timer["payload"]), (str(first.id), {"guild_id": self.guild.id, "channel_id": self.channel.id, "message_id": first.id}))
        self.assertAlmostEqual(timer["due_at"], time.time() + greetings_module.DELETE_RETRY, delta=5, msg="with no lifetime set, nothing else would ever come back for it")
        first.delete = real
        await self.bot.timers.run_due(time.time() + 600)
        self.assertEqual((self.channel.sent, await self.posts(), await self.retries()), ([], [], []))

    async def test_a_delete_discord_keeps_waiting_is_tried_again_too(self):
        self.cog_.DELETE_PATIENCE = 0.05
        await self.configure(farewell=text(""))
        member = await self.join()

        async def wait(**kwargs):
            await asyncio.sleep(30)

        self.channel.sent[0].delete = wait
        await asyncio.wait_for(self.leave(member), 5)
        self.assertEqual([timer["key"] for timer in await self.retries()], [str(self.channel.sent[0].id)])

    async def test_a_retry_survives_a_lifetime_switched_off(self):
        await self.configure(welcome_delete_after=600, farewell=text(""))
        member = await self.join()
        post = self.channel.sent[-1]
        post.delete = mock.AsyncMock(side_effect=refusal(503))
        await self.leave(member)
        self.assertEqual(len(await self.retries()), 1)
        await self.configure(welcome_delete_after=0)
        self.assertEqual(len(await self.retries()), 1, "the save takes the lifetime timers, never the retry")
        del post.delete
        await self.bot.timers.run_due(time.time() + 3600)
        self.assertTrue(post.deleted)
        self.assertEqual((await self.posts(), await self.retries(), await self.timers()), ([], [], []))

    async def test_a_retry_keeps_its_time_when_the_lifetime_changes(self):
        await self.configure(welcome_delete_after=600, farewell=text(""))
        member = await self.join()
        self.channel.sent[-1].delete = mock.AsyncMock(side_effect=refusal(503))
        await self.leave(member)
        [before] = await self.retries()
        await self.configure(welcome_delete_after=86400)
        [after] = await self.retries()
        self.assertEqual(after["due_at"], before["due_at"])
        self.assertAlmostEqual(after["due_at"], time.time() + greetings_module.DELETE_RETRY, delta=5)

    async def test_a_retry_goes_once_a_later_ban_deletes_the_welcome(self):
        await self.configure(farewell=text(""))
        member = await self.join()
        post = self.channel.sent[-1]
        post.delete = mock.AsyncMock(side_effect=refusal(503))
        await self.leave(member)
        self.assertEqual(len(await self.retries()), 1)
        del post.delete
        await self.cog_.on_member_ban(self.guild, member)
        self.assertTrue(post.deleted)
        self.assertEqual((await self.posts(), await self.retries()), ([], []))

    async def test_a_restore_leaves_a_retry_alone(self):
        await self.configure(welcome_delete_after=600, farewell=text(""))
        member = await self.join()
        self.channel.sent[-1].delete = mock.AsyncMock(side_effect=refusal(503))
        await self.leave(member)
        [before] = await self.retries()
        await self.cog_.on_guild_restored(self.guild.id)
        self.assertEqual([timer["due_at"] for timer in await self.retries()], [before["due_at"]])

    async def test_a_storage_error_while_cleaning_up_is_logged_and_goes_no_further(self):
        member = await self.join()
        with mock.patch.object(self.db, "all", mock.AsyncMock(side_effect=RuntimeError("disk full"))), self.assertLogs(greetings_module.log, "ERROR"):
            await self.cog_._delete_welcomes(self.guild, member.id)
        self.assertEqual(len(self.channel.sent), 1)


class RejoinTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {user}"), dm=text("Hello"), welcome_again_after=5)
        self.member = FakeMember(self.guild, "returner")

    async def test_a_rejoin_inside_the_time_gets_no_second_welcome_or_dm(self):
        for _ in range(4):
            await self.cog_.on_member_join(self.member)
        self.assertEqual((len(self.channel.sent), len(self.member.dms)), (1, 1))
        await self.join("someone else")
        self.assertEqual(len(self.channel.sent), 2, "it's each person's own time")

    async def test_the_time_is_in_minutes(self):
        await self.cog_.on_member_join(self.member)
        self.cog_._welcomed[(self.guild.id, self.member.id)] -= 4 * 60
        await self.db.run("UPDATE greeting_posts SET posted_at = posted_at - ?", (4 * 60,))
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.channel.sent), 1, "four minutes into a five-minute wait")
        self.cog_._welcomed.clear()
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.channel.sent), 1, "and the same from the noted posts alone")

    async def test_memory_holds_it_where_no_post_was_noted(self):
        await self.cog_.on_member_join(self.member)
        await self.db.run("DELETE FROM greeting_posts")
        await self.cog_.on_member_join(self.member)
        self.assertEqual((len(self.channel.sent), len(self.member.dms)), (1, 1))

    async def test_a_rejoin_after_the_time_is_welcomed_again(self):
        await self.cog_.on_member_join(self.member)
        key = (self.guild.id, self.member.id)
        self.cog_._welcomed[key] -= 5 * 60 + 1
        await self.db.run("UPDATE greeting_posts SET posted_at = posted_at - ?", (5 * 60 + 1,))
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.channel.sent), 2)

    async def test_it_holds_across_a_restart_from_the_posts_that_were_noted(self):
        await self.cog_.on_member_join(self.member)
        self.cog_._welcomed.clear()
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.channel.sent), 1)
        await self.db.run("UPDATE greeting_posts SET posted_at = posted_at - 301")
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.channel.sent), 2)

    async def test_a_server_that_only_sends_a_dm_measures_from_the_dm(self):
        await self.configure(welcome_channel=None)
        await self.cog_.on_member_join(self.member)
        self.cog_._welcomed.clear()
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.member.dms), 1)
        self.assertEqual([row["kind"] for row in await self.posts()], ["dm"])

    async def test_zero_welcomes_every_join_and_reads_nothing_to_decide(self):
        await self.configure(welcome_again_after=0)
        with mock.patch.object(self.cog_, "_welcomed_lately", mock.AsyncMock(side_effect=AssertionError("it checked for an earlier welcome"))):
            for _ in range(3):
                await self.cog_.on_member_join(self.member)
        self.assertEqual((len(self.channel.sent), len(self.member.dms)), (3, 3))
        self.assertEqual(self.cog_._welcomed, {})

    async def test_a_join_that_sends_nothing_is_not_what_a_rejoin_is_measured_from(self):
        await self.configure(welcome_on=False, dm_on=False)
        await self.cog_.on_member_join(self.member)
        self.assertEqual(self.cog_._welcomed, {})
        await self.configure(welcome_on=True)
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_welcome_discord_refused_is_not_one_a_rejoin_waits_on(self):
        await self.configure(dm=text(""))
        send = self.channel.send

        async def refuse(*args, **kwargs):
            raise refusal(403)

        self.channel.send = refuse
        with self.assertLogs(greetings_module.log, "WARNING"):
            await self.cog_.on_member_join(self.member)
        self.channel.send = send
        await self.cog_.on_member_join(self.member)  # they come back inside the wait, having never been welcomed
        self.assertEqual([sent.content for sent in self.channel.sent], ["Hi returner"])

    async def test_a_join_dm_that_went_out_is_a_welcome_a_rejoin_waits_on(self):
        async def refuse(*args, **kwargs):
            raise refusal(403)

        self.channel.send = refuse
        with self.assertLogs(greetings_module.log, "WARNING"):
            await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.member.dms), 1)
        self.assertIn((self.guild.id, self.member.id), self.cog_._welcomed, "the mark is in memory too, where the noted posts can't stand in for it")
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.member.dms), 1, "the DM reached them, so the rejoin waits")

    async def test_a_posted_welcome_with_no_dm_is_one_a_rejoin_waits_on(self):
        await self.configure(dm=text(""))
        await self.cog_.on_member_join(self.member)
        self.assertIn((self.guild.id, self.member.id), self.cog_._welcomed, "the mark is in memory too, where the noted posts can't stand in for it")
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_refused_welcome_lets_go_of_its_own_mark_and_no_other(self):
        await self.configure(dm=text(""))
        key = (self.guild.id, self.member.id)

        async def refuse(*args, **kwargs):
            self.cog_._welcomed[key] = 12345.0  # a later join of theirs was welcomed meanwhile
            raise refusal(403)

        self.channel.send = refuse
        with self.assertLogs(greetings_module.log, "WARNING"):
            await self.cog_.on_member_join(self.member)
        self.assertEqual(self.cog_._welcomed[key], 12345.0)

    async def test_a_rejoin_while_the_welcome_is_on_its_way_gets_no_second_one(self):
        send = self.channel.send

        async def slow(*args, **kwargs):
            await self.cog_.on_member_join(self.member)  # they leave and come back before the first post lands
            return await send(*args, **kwargs)

        self.channel.send = slow
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_rejoin_is_still_counted(self):
        start = self.guild.member_count
        for _ in range(3):
            await self.cog_.on_member_join(self.member)
        self.assertEqual(await self.count(), start + 2)

    async def test_a_storage_error_welcomes_rather_than_stays_silent(self):
        await self.cog_.on_member_join(self.member)
        self.cog_._welcomed.clear()
        with mock.patch.object(self.db, "scalar", mock.AsyncMock(side_effect=RuntimeError("disk full"))), self.assertLogs(greetings_module.log, "ERROR"):
            self.assertFalse(await self.cog_._welcomed_lately((self.guild.id, self.member.id), 300))


class MentionsTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.crew = self.guild.role("Crew", 3)
        self.helpers = self.guild.role("Helpers", 3)
        self.words = f"Hi {{mention}}, {self.crew.mention} and {self.helpers.mention} will help. {{user}} @here"
        await self.configure(welcome_channel=self.channel.id, welcome=text(self.words))

    async def pings(self, name="newcomer"):
        await self.join(name)
        return self.channel.sent[-1].kwargs["allowed_mentions"]

    async def test_by_default_only_people_are_pinged(self):
        allowed = await self.pings()
        self.assertEqual(([user.id for user in allowed.users], allowed.roles, allowed.everyone), ([self.guild.members[-1].id], False, False))

    async def test_a_name_that_holds_a_mention_pings_nobody(self):
        victim = FakeMember(self.guild, "victim")
        teammate = FakeMember(self.guild, "teammate")
        for mode in ("member", "roles", "all"):
            await self.configure(welcome_mentions=mode, welcome=text(f"Hi {{mention}}, {{user}}! Ask {teammate.mention} or <@!{self.admin.id}>. {teammate.mention} is online now."),
                                 farewell=text(f"{{user}} left. {teammate.mention} knows why."), ban=text("{user} was banned."))
            joiner = await self.join(victim.mention)
            self.assertIn(victim.mention, self.channel.sent[-1].content, "the name is shown as it is")
            allowed = self.channel.sent[-1].kwargs["allowed_mentions"]
            self.assertEqual([user.id for user in allowed.users], [joiner.id, teammate.id, self.admin.id], mode)
            self.assertNotIn("users", allowed.to_dict()["parse"])
        await self.cog_.on_member_ban(self.guild, FakeMember(self.guild, victim.mention))
        self.cog_._recent_bans.clear()
        await self.cog_.on_member_remove(joiner)
        ban, goodbye = (sent.kwargs["allowed_mentions"] for sent in self.channel.sent[-2:])
        self.assertEqual(([user.id for user in ban.users], [user.id for user in goodbye.users]), ([self.guild.members[-1].id], [joiner.id, teammate.id]))
        self.assertNotIn(victim.id, [user.id for user in ban.users] + [user.id for user in goodbye.users])

    async def test_a_name_can_not_set_off_an_everyone_the_template_holds_in_a_branch_not_taken(self):
        await self.configure(welcome_mentions="all", welcome=text("{if({user(bot)}==true):@everyone a bot was added|Welcome {user}}"))
        await self.join("ordinary")
        self.assertFalse(self.channel.sent[-1].kwargs["allowed_mentions"].everyone)
        await self.join("@everyone")
        self.assertEqual(self.channel.sent[-1].content, "Welcome @everyone")
        self.assertFalse(self.channel.sent[-1].kwargs["allowed_mentions"].everyone, "the name's @everyone pings nobody")
        await self.join("robot", bot=True)
        self.assertEqual(self.channel.sent[-1].content, "@everyone a bot was added")
        self.assertTrue(self.channel.sent[-1].kwargs["allowed_mentions"].everyone, "the branch the template takes for a bot still pings")

    async def test_a_name_can_not_set_off_a_role_or_a_person_the_template_holds_in_a_branch_not_taken(self):
        mod = FakeMember(self.guild, "moderator")
        for mode in ("member", "roles", "all"):
            await self.configure(welcome_mentions=mode, welcome=text("{if({user(bot)}==true):" + f"{self.crew.mention} {mod.mention} a bot was added" + "|Welcome {user}}"))
            joiner = await self.join(f"{self.crew.mention} {mod.mention}")
            allowed = self.channel.sent[-1].kwargs["allowed_mentions"]
            self.assertEqual([user.id for user in allowed.users], [joiner.id], mode)
            self.assertIn(allowed.roles, (False, []), mode)
        robot = await self.join("robot", bot=True)
        allowed = self.channel.sent[-1].kwargs["allowed_mentions"]
        self.assertEqual(([user.id for user in allowed.users], [role.id for role in allowed.roles]), ([robot.id, mod.id], [self.crew.id]), "the branch taken pings what it holds")

    async def test_a_name_any_block_shows_can_not_ping(self):
        victim = FakeMember(self.guild, "victim")
        shown = FakeMember(self.guild, f"{victim.mention} {self.crew.mention} @everyone")  # someone in the server whose name a block can show
        await self.configure(welcome_mentions="all", welcome=text("Hi {user}! Say hello to {server(random)}. @here"))
        with mock.patch("captainguild.core.tagscript.objects.random.choice", lambda people: shown):
            joiner = await self.join("newcomer")
        sent = self.channel.sent[-1]
        self.assertIn(victim.mention, sent.content)
        allowed = sent.kwargs["allowed_mentions"]
        self.assertEqual(([user.id for user in allowed.users], allowed.roles), ([joiner.id], []))
        self.assertFalse(allowed.everyone, "the message asked for @here, and the name would have widened it to @everyone")

    async def test_a_mention_the_template_builds_pings_as_it_always_did(self):
        mod = FakeMember(self.guild, "moderator")
        welcome = "{=(mod):" + str(mod.id) + "}Hi {mention}. Ask <@{mod}> for help."
        goodbye = "{=(mod):" + str(mod.id) + "}{user} left. <@!{mod}> knows why."
        await self.configure(welcome=text(welcome), farewell=text(goodbye), ban=text(goodbye))
        joiner = await self.join()
        self.assertEqual(self.channel.sent[-1].content, f"Hi {joiner.mention}. Ask {mod.mention} for help.")
        self.assertEqual([user.id for user in self.channel.sent[-1].kwargs["allowed_mentions"].users], [joiner.id, mod.id])
        banned = FakeMember(self.guild, "banned")
        await self.cog_.on_member_ban(self.guild, banned)
        await self.cog_.on_member_remove(joiner)
        ban, goodbye_sent = self.channel.sent[-2:]
        self.assertEqual([user.id for user in ban.kwargs["allowed_mentions"].users], [banned.id, mod.id])
        self.assertEqual([user.id for user in goodbye_sent.kwargs["allowed_mentions"].users], [joiner.id, mod.id])

    async def test_a_mention_typed_in_a_date_format_pings_as_it_did(self):
        mod = FakeMember(self.guild, "moderator")
        await self.configure(welcome=text("Hi {mention}! {strf({user(joined_at)}):Joined %Y. Ask <@" + str(mod.id) + "> for a tour.}"))
        joiner = await self.join()
        sent = self.channel.sent[-1]
        self.assertIn(f"Ask {mod.mention} for a tour.", sent.content)
        self.assertEqual([user.id for user in sent.kwargs["allowed_mentions"].users], [joiner.id, mod.id])
        await self.configure(welcome_mentions="all", welcome=text("Hi {mention}! {strf({user(joined_at)}):Joined %Y. @here " + self.crew.mention + "}"))
        await self.join()
        allowed = self.channel.sent[-1].kwargs["allowed_mentions"]
        self.assertEqual((allowed.everyone, [role.id for role in allowed.roles]), (True, [self.crew.id]))
        await self.configure(welcome_mentions="member", welcome=text("Hi {mention}! {strf({user(joined_at)}):Joined %Y.} {strf({user(joined_at)}):{user(name)}} {if(1==2):<@" + str(mod.id) + ">}"))
        named = await self.join(f"<@{mod.id}>")
        self.assertEqual([user.id for user in self.channel.sent[-1].kwargs["allowed_mentions"].users], [named.id], "a format from outside pings nobody")

    async def test_a_mention_typed_in_a_date_format_inside_another_pings_as_typed(self):
        mod = FakeMember(self.guild, "moderator")
        await self.configure(welcome=text("Hi {mention}! {strf({user(joined_at)}):{strf({user(created_at)}):Ask <@" + str(mod.id) + "> for a tour,} joined %Y.}"))
        joiner = await self.join()
        sent = self.channel.sent[-1]
        self.assertIn(f"Ask {mod.mention} for a tour, joined ", sent.content)
        self.assertEqual([user.id for user in sent.kwargs["allowed_mentions"].users], [joiner.id, mod.id])

    async def test_a_role_mention_the_template_builds_pings_nobody(self):
        # Saving a role ping takes the saver's own right to send it, and that check reads role mentions typed whole. So only those ping.
        await self.configure(welcome_mentions="roles")
        for built in (f"Hi {{user}} <@&{{=(x):}}{self.crew.id}>", "{=(r):" + str(self.crew.id) + "}Hi {user} <@&{r}>"):
            await self.configure(welcome=text(built))
            self.assertEqual(self.cog_.pinged_channels(self.settings("greetings")), {"welcome_mentions:roles": {self.channel.id}}, built)
            joiner = await self.join()
            sent = self.channel.sent[-1]
            self.assertEqual(sent.content, f"Hi {joiner.name} <@&{self.crew.id}>")
            self.assertEqual(sent.kwargs["allowed_mentions"].roles, [], built)
        await self.configure(farewell=text(f"Bye {{user}} <@&{{=(x):}}{self.crew.id}>"), farewell_mentions="all")
        leaving = await self.join("leaving")
        self.guild.members.remove(leaving)
        await self.cog_.on_member_remove(leaving)
        self.assertEqual(self.channel.sent[-1].kwargs["allowed_mentions"].roles, [], "the leave message the same way")
        await self.configure(welcome=text(f"Hi {{user}} {self.crew.mention}"))
        await self.join()
        self.assertEqual([role.id for role in self.channel.sent[-1].kwargs["allowed_mentions"].roles], [self.crew.id], "typed whole, it pings")

    async def test_a_built_mention_with_the_shortest_id_a_mention_takes_pings_too(self):
        await self.configure(welcome=text("{=(early):123456789012345}{=(late):1234567890123456789012}Hi {mention}. <@{early}> and <@{late}>"))
        joiner = await self.join()
        self.assertEqual([user.id for user in self.channel.sent[-1].kwargs["allowed_mentions"].users], [joiner.id, 123456789012345], "15 digits is a mention, 22 isn't")

    async def test_a_mention_built_from_a_value_outside_the_template_pings_nobody(self):
        stranger = FakeMember(self.guild, "stranger")
        await self.configure(welcome_mentions="all", welcome=text("{=(who):{user(name)}}Hi {mention}. <@{who}> and <@!{user(name)}> and <@&{user(name)}>"))
        joiner = await self.join(str(stranger.id))  # a name that is someone else's ID
        sent = self.channel.sent[-1]
        self.assertEqual(sent.content, f"Hi {joiner.mention}. <@{stranger.id}> and <@!{stranger.id}> and <@&{stranger.id}>")
        allowed = sent.kwargs["allowed_mentions"]
        self.assertEqual(([user.id for user in allowed.users], allowed.roles), ([joiner.id], []))

    async def test_a_mention_the_template_writes_whole_from_digits_it_does_not_hold_pings_nobody(self):
        await self.configure(welcome_mentions="all", welcome=text("{=(p):@every}Hi {mention}. Ask <@{m:trunc(111111111111111*1000)}> or <@&{m:trunc(111111111111111*1000)}>. {p}one"))
        joiner = await self.join()
        self.assertEqual(self.channel.sent[-1].content, f"Hi {joiner.mention}. Ask <@111111111111111008> or <@&111111111111111008>. @everyone")
        allowed = self.channel.sent[-1].kwargs["allowed_mentions"]
        self.assertEqual([user.id for user in allowed.users], [joiner.id], "only IDs the stored text holds can ping")
        self.assertEqual((allowed.roles, allowed.everyone), ([], False), "and only the @everyone or @here it holds")

    async def test_one_post_pings_a_hundred_people_at_most_with_the_member_first(self):
        others = [str(200000000000000 + number) for number in range(101)]  # IDs as short as a mention takes, so all of them fit in one message
        await self.configure(welcome=text("Hi {mention}! " + " ".join(f"<@{other}>" for other in others)))
        joiner = await self.join()
        users = [user.id for user in self.channel.sent[-1].kwargs["allowed_mentions"].users]
        self.assertEqual(len(users), 100, "Discord takes a hundred")
        self.assertEqual(users[:2], [joiner.id, int(others[0])])
        await self.configure(welcome_mentions="roles", welcome=text("Hi {mention}! " + "".join(f"<@&{other}>" for other in others)))
        await self.join()
        roles = [role.id for role in self.channel.sent[-1].kwargs["allowed_mentions"].roles]
        self.assertEqual((len(roles), roles[0]), (100, int(others[0])))

    async def test_a_message_with_no_block_pings_what_it_holds(self):
        mod = FakeMember(self.guild, "moderator")
        await self.configure(welcome_mentions="all", welcome=text(f"Welcome aboard. {mod.mention} and {self.crew.mention} will help. @here"))
        await self.join()
        allowed = self.channel.sent[-1].kwargs["allowed_mentions"]
        self.assertEqual(([user.id for user in allowed.users][1:], [role.id for role in allowed.roles], allowed.everyone), ([mod.id], [self.crew.id], True))

    async def test_a_message_the_engine_could_not_run_pings_what_it_holds_as_written(self):
        mod = FakeMember(self.guild, "moderator")
        await self.configure(welcome=text(f"Welcome {{user}}. {mod.mention} will help."))
        for broken in (mock.patch("captainguild.core.tagscript.budget.budgeted_render", mock.AsyncMock(side_effect=OverBudget)),
                       mock.patch("captainguild.core.tagscript.payload._render_all", side_effect=RuntimeError("the engine broke")),
                       mock.patch("captainguild.core.tagscript.engine.Interpreter.render_or_keep", side_effect=RuntimeError("it broke after the text was filled in"))):
            await self.configure(welcome={"content": f"Welcome {{user}}. {mod.mention} will help.", "embeds": [{"title": "Hi {user}"}], "buttons": []})
            with broken, self.assertLogs(level="INFO"):
                joiner = await self.join()
            self.assertEqual(self.channel.sent[-1].content, f"Welcome {{user}}. {mod.mention} will help.")
            self.assertEqual([user.id for user in self.channel.sent[-1].kwargs["allowed_mentions"].users], [joiner.id, mod.id])

    async def test_a_text_that_went_out_as_written_pings_no_role_and_not_everyone(self):
        # Past the server's template time, past the engine's own limits, or when the engine broke, the text goes out as written. None of its
        # blocks ran, so no branch of it was taken: it pings the member and the people typed in it, and never a role or everyone.
        mod = FakeMember(self.guild, "moderator")
        template = "{if({user(bot)}==true):@everyone " + self.crew.mention + " a bot was added|Welcome {user}} " + mod.mention
        doubling = "{=(a):xx}" + "{=(a):{a}{a}}" * 23  # past the engine's work limit
        for mode in ("roles", "all"):
            await self.configure(welcome_mentions=mode, welcome=text(template))
            joiner = await self.join()
            allowed = self.channel.sent[-1].kwargs["allowed_mentions"]
            self.assertEqual((allowed.everyone, allowed.roles, [user.id for user in allowed.users]), (False, [], [joiner.id, mod.id]), "control: the template ran")
            ways = [("past the engine's limits", text(template + doubling), contextlib.nullcontext()),
                    ("the engine broke", text(template), mock.patch("captainguild.core.tagscript.payload._render_all", side_effect=RuntimeError("the engine broke"))),
                    ("the engine broke after the text was filled in", {"content": template, "embeds": [{"title": "Hi {user}"}], "buttons": []},
                     mock.patch("captainguild.core.tagscript.engine.Interpreter.render_or_keep", side_effect=RuntimeError("it broke on the embed")))]
            for how, stored, broken in ways:
                await self.configure(welcome=stored)
                with broken, (self.assertLogs(level="INFO") if how.startswith("the engine broke") else contextlib.nullcontext()):
                    joiner = await self.join()
                sent = self.channel.sent[-1]
                self.assertTrue(sent.content.startswith("{if({user(bot)}==true):@everyone"), how)
                allowed = sent.kwargs["allowed_mentions"]
                self.assertEqual((allowed.everyone, allowed.roles, [user.id for user in allowed.users]), (False, False, [joiner.id, mod.id]), (mode, how))
        await self.configure(welcome=text(template))
        budget.charge(self.guild.id, budget.MESSAGES, None, budget.SERVER_SECONDS)  # the server's message templates used this minute's time
        with self.assertLogs(level="INFO"):
            joiner = await self.join()
        sent = self.channel.sent[-1]
        self.assertEqual(sent.content, template)
        allowed = sent.kwargs["allowed_mentions"]
        self.assertEqual((allowed.everyone, allowed.roles, [user.id for user in allowed.users]), (False, False, [joiner.id, mod.id]), "past the server's time")

    async def test_spaces_and_line_breaks_around_the_text_do_not_upset_it(self):
        mod = FakeMember(self.guild, "moderator")
        await self.configure(welcome=text(f"\n  {{user}} {mod.mention}  \n"))
        joiner = await self.join("  name with spaces  ")
        self.assertEqual(self.channel.sent[-1].content, f"name with spaces   {mod.mention}")
        self.assertEqual([user.id for user in self.channel.sent[-1].kwargs["allowed_mentions"].users], [joiner.id, mod.id])

    async def test_a_mention_in_text_cut_to_fit_still_pings(self):
        mod = FakeMember(self.guild, "moderator")
        await self.configure(welcome=text(f"{{user}} {mod.mention} " + "x" * 1800))
        joiner = await self.join("n" * 300)
        self.assertEqual(len(self.channel.sent[-1].content), 2000)
        self.assertEqual([user.id for user in self.channel.sent[-1].kwargs["allowed_mentions"].users], [joiner.id, mod.id])

    def test_text_that_does_not_line_up_counts_as_nobody_s(self):
        shown = "<@111111111111111111> @everyone"
        self.assertEqual(greetings_module._template_mentions({"content": shown}, shown, shown), ([111111111111111111], [], {"@everyone"}, set()))
        self.assertEqual(greetings_module._template_mentions({"content": shown}, shown, "x" + shown[1:]), ([], [], set(), {"@everyone"}))

    async def test_roles_adds_the_roles_the_stored_text_names_and_never_everyone(self):
        await self.configure(welcome_mentions="roles")
        allowed = await self.pings()
        self.assertEqual(([role.id for role in allowed.roles], allowed.everyone, [user.id for user in allowed.users]), ([self.crew.id, self.helpers.id], False, [self.guild.members[-1].id]))

    async def test_all_adds_everyone_only_when_the_stored_text_asks_for_it(self):
        await self.configure(welcome_mentions="all")
        allowed = await self.pings()
        self.assertEqual(([role.id for role in allowed.roles], allowed.everyone), ([self.crew.id, self.helpers.id], True))
        await self.configure(welcome=text(f"Hi {{user}}, {self.crew.mention}"))
        allowed = await self.pings("@everyone")
        self.assertEqual(([role.id for role in allowed.roles], allowed.everyone), ([self.crew.id], False), "a name that says @everyone asks for nothing")

    async def test_a_name_can_not_add_a_role_or_widen_here_to_everyone(self):
        other = self.guild.role("Staff", 4)
        await self.configure(welcome_mentions="all")
        allowed = await self.pings(f"{other.mention} @everyone")
        self.assertEqual([role.id for role in allowed.roles], [self.crew.id, self.helpers.id], "only the roles written in the stored message")
        self.assertFalse(allowed.everyone, "the message asked for @here, and the name would have made it @everyone")
        allowed = await self.pings("plain @here name")
        self.assertTrue(allowed.everyone, "a name that says no more than the message does changes nothing")
        await self.configure(welcome=text("@everyone welcome {user}"))
        self.assertTrue((await self.pings("@here @everyone")).everyone)

    async def test_a_typed_mention_whose_number_is_no_discord_id_stays_out_of_the_lists(self):
        too_long = str(1 << 63)  # one past what a database column holds, and more than any real ID: the codebase's ids_from stops here too
        largest = str((1 << 63) - 1)
        await self.configure(welcome_mentions="roles", welcome=text(f"Hi {{user}} <@{too_long}> <@&{too_long}> <@{largest}> <@&{largest}> <@{'9' * 21}> {self.crew.mention} and again {self.crew.mention}"))
        allowed = await self.pings()
        self.assertEqual([user.id for user in allowed.users], [self.guild.members[-1].id, int(largest)])
        self.assertEqual([role.id for role in allowed.roles], [int(largest), self.crew.id])
        self.assertEqual(greetings_module._BIGGEST_ID, 2 ** 63 - 1)
        self.assertIn(too_long, self.channel.sent[-1].content, "the text goes out as written")

    async def test_roles_in_an_embed_or_made_by_a_block_are_not_pings(self):
        await self.configure(welcome_mentions="roles", welcome={"content": "Hi {user} <@&{user(id)}>", "embeds": [{"description": self.crew.mention}], "buttons": []})
        self.assertEqual((await self.pings()).roles, [])

    async def test_goodbye_and_ban_messages_keep_their_pings_whatever_the_welcome_is_set_to(self):
        await self.configure(welcome_mentions="all", farewell=text(f"Bye {{user}} {self.crew.mention} @everyone"), ban=text("Banned {user} @everyone"))
        member = await self.join()
        await self.cog_.on_member_ban(self.guild, FakeMember(self.guild, "banned"))
        self.guild.members.remove(member)
        self.cog_._recent_bans.clear()
        await self.cog_.on_member_remove(member)
        for sent in self.channel.sent[1:]:
            allowed = sent.kwargs["allowed_mentions"]
            self.assertEqual((allowed.everyone, allowed.roles), (False, False), sent.content)
            self.assertEqual(len(allowed.users), 1, "the person the post is about, and no role or everyone whatever the welcome is set to")

    async def farewell_and_ban(self):
        """A leave post for one member and a ban post for another, as the pings each allowed."""
        self.channel.sent.clear()
        leaving = await self.join("leaving")
        self.guild.members.remove(leaving)
        await self.cog_.on_member_remove(leaving)
        await self.cog_.on_member_ban(self.guild, FakeMember(self.guild, "banned"))
        self.assertEqual(len(self.channel.sent), 3)
        return [sent.kwargs["allowed_mentions"] for sent in self.channel.sent[1:]]

    async def test_the_leave_and_ban_messages_ping_as_their_own_choices_say(self):
        # The other bot pinged the roles and the @everyone written in a leave message by the same rule as in a welcome.
        await self.configure(farewell=text(f"Bye {{mention}} {self.crew.mention} @everyone"), ban=text(f"Banned {{user}} {self.helpers.mention} @here"))
        farewell, ban = await self.farewell_and_ban()
        self.assertEqual((farewell.roles, farewell.everyone, ban.roles, ban.everyone), (False, False, False, False), "member, the default, is what they did before")
        await self.configure(farewell_mentions="roles", ban_mentions="all")
        farewell, ban = await self.farewell_and_ban()
        self.assertEqual(([role.id for role in farewell.roles], farewell.everyone), ([self.crew.id], False))
        self.assertEqual(([role.id for role in ban.roles], ban.everyone), ([self.helpers.id], True))
        await self.configure(farewell_mentions="all", ban_mentions="member")
        farewell, ban = await self.farewell_and_ban()
        self.assertEqual(([role.id for role in farewell.roles], farewell.everyone, ban.roles, ban.everyone), ([self.crew.id], True, False, False))
        welcome = self.channel.sent[0].kwargs["allowed_mentions"]
        self.assertEqual((welcome.roles, welcome.everyone), (False, False), "and the welcome keeps its own choice")

    async def test_a_leave_or_ban_message_pings_where_it_posts(self):
        await self.configure(farewell=text(f"Bye {{user}} {self.crew.mention} @here"), ban=text("Banned {user} @everyone"), farewell_mentions="all", ban_mentions="all")
        here = {self.channel.id}
        self.assertEqual(self.cog_.pinged_channels(self.settings("greetings")),
                         {"farewell_mentions:all": here, f"farewell:{self.crew.id}": here, "farewell:@here": here, "ban_mentions:all": here, "ban:@everyone": here})
        leave_room, ban_room = FakeChannel(self.guild, "leaving"), FakeChannel(self.guild, "bans")
        await self.configure(farewell_channel=leave_room.id)
        found = self.cog_.pinged_channels(self.settings("greetings"))
        self.assertEqual((found["farewell_mentions:all"], found["ban_mentions:all"]), ({leave_room.id}, {leave_room.id}), "a ban message posts where the leave message does")
        await self.configure(ban_channel=ban_room.id, farewell_mentions="member")
        self.assertEqual(self.cog_.pinged_channels(self.settings("greetings")), {"ban_mentions:all": {ban_room.id}, "ban:@everyone": {ban_room.id}})
        await self.configure(welcome_channel=None, farewell_channel=None, ban_channel=None)
        self.assertEqual(self.cog_.pinged_channels(self.settings("greetings")), {}, "with no channel there is nowhere to ping")

    async def test_saving_a_wider_leave_or_ban_ping_takes_the_right_to_send_it_there(self):
        await self.configure(farewell=text(f"Bye {{user}} {self.crew.mention}"), ban=text("Banned {user} @everyone"))
        client = await self.site(self.manager())
        for kind in ("farewell", "ban"):
            for mode in ("roles", "all"):
                response = await client.put(self.path("/settings/greetings"), json={f"{kind}_mentions": mode})
                self.assertEqual((response.status, (await response.json())["fields"]), (400, {f"{kind}_mentions:{mode}": "You need Mention Everyone in that channel to ping through the bot."}), (kind, mode))
        cfg = self.settings("greetings")
        self.assertEqual((cfg["farewell_mentions"], cfg["ban_mentions"]), ("member", "member"))
        able = await self.site(self.manager("mention_everyone"))
        self.assertEqual((await able.put(self.path("/settings/greetings"), json={"farewell_mentions": "all", "ban_mentions": "roles"})).status, 200)
        self.assertEqual((await client.put(self.path("/settings/greetings"), json={"farewell_mentions": "member", "ban_mentions": "member"})).status, 200, "anyone with the page can narrow them")
        await self.configure(farewell_mentions="all")
        response = await client.put(self.path("/settings/greetings"), json={"farewell": text(f"Bye {{user}} {self.crew.mention} {self.helpers.mention}")})
        self.assertEqual((response.status, list((await response.json())["fields"])), (400, [f"farewell:{self.helpers.id}"]), "a new role in the text takes the right too")

    async def test_the_mentions_command_sets_the_leave_and_ban_choices_with_the_same_right(self):
        await self.configure(farewell=text(f"Bye {{user}} {self.crew.mention}"), ban=text("Banned {user} @everyone"))
        ctx = self.context(self.manager())
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "roles", "farewell")
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "all", "ban")
        self.assertEqual(ctx.fails, ["You need Mention Everyone in that channel to ping through the bot."] * 2)
        ctx = self.context()
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "roles", "farewell")
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "greetings.farewell_mentions")
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "all", "ban")
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "greetings.ban_mentions")
        self.assertEqual(ctx.oks, ["The leave message pings the member who left and the roles its text mentions.",
                                   "The ban message pings the person who was banned, the roles its text mentions, and @everyone or @here when its text has them."])
        cfg = self.settings("greetings")
        self.assertEqual((cfg["welcome_mentions"], cfg["farewell_mentions"], cfg["ban_mentions"]), ("member", "roles", "all"), "the welcome's choice stays")
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "all")
        self.assertEqual((self.settings("greetings")["welcome_mentions"], ctx.oks[-1]), ("all", greetings_module.PINGS["all"]), "left out, the message is the welcome")
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "greetings.mentions")
        self.assertEqual([parameter.name for parameter in self.cog_.greetings_mentions.app_command.parameters], ["who", "message"])

    async def test_a_leave_or_ban_ping_the_bot_can_not_send_is_said_on_the_page(self):
        client = await self.client()

        async def said():
            return await (await client.get(self.path("/greetings/problems"))).json()

        await self.configure(farewell=text("Bye {user} @everyone"), ban=text(f"Banned {{user}} {self.crew.mention}"), farewell_mentions="all", ban_mentions="roles")
        self.assertEqual(await said(), [], "the bot has the permission")
        self.role_bot.permissions.names.discard("administrator")
        self.role_bot.permissions.names.discard("mention_everyone")
        self.assertEqual(await said(), ["The leave message pings a role or everyone, and the bot can't send that ping in #general. Give it Mention Everyone there.",
                                        "The ban message pings a role or everyone, and the bot can't send that ping in #general. Give it Mention Everyone there."])
        await self.configure(farewell_on=False)
        self.assertEqual(await said(), ["The ban message pings a role or everyone, and the bot can't send that ping in #general. Give it Mention Everyone there."],
                         "a leave message that's switched off pings nobody")

    async def test_the_inviter_s_mention_names_them_without_a_ping(self):
        teammate = FakeMember(self.guild, "teammate")
        await self.configure(welcome=text(f"Hi {{mention}}, invited by {{invite(inviter_mention)}}. Ask {teammate.mention}."))
        await self.settle()
        member = await self.join()
        allowed = self.channel.sent[-1].kwargs["allowed_mentions"]
        self.assertEqual([user.id for user in allowed.users], [member.id, teammate.id], "the member and the people the message itself names")
        self.assertEqual((allowed.roles, allowed.everyone), (False, False))

    def manager(self, *extra):
        role = self.guild.role("Manager", 4, "manage_guild", "view_channel", "send_messages", *extra)
        return FakeMember(self.guild, "manager", role)

    async def site(self, person):
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(person.id, person.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def test_saving_a_wider_ping_takes_the_right_to_send_it_there(self):
        client = await self.site(self.manager())
        for mode in ("roles", "all"):
            response = await client.put(self.path("/settings/greetings"), json={"welcome_mentions": mode})
            self.assertEqual(response.status, 400, mode)
            self.assertEqual((await response.json())["fields"], {f"welcome_mentions:{mode}": "You need Mention Everyone in that channel to ping through the bot."})
        self.assertEqual(self.settings("greetings")["welcome_mentions"], "member")
        able = await self.site(self.manager("mention_everyone"))
        self.assertEqual((await able.put(self.path("/settings/greetings"), json={"welcome_mentions": "all"})).status, 200)
        self.assertEqual((await client.put(self.path("/settings/greetings"), json={"welcome_mentions": "member"})).status, 200, "anyone with the page can narrow it")
        admin = await self.client()
        self.assertEqual((await admin.put(self.path("/settings/greetings"), json={"welcome_mentions": "roles"})).status, 200)

    async def test_with_a_wider_ping_set_a_new_ping_takes_the_right_too(self):
        await self.configure(welcome_mentions="all", welcome=text(f"Hi {{user}} {self.crew.mention}"))
        client = await self.site(self.manager())
        self.assertEqual((await client.put(self.path("/settings/greetings"), json={"welcome": text(f"Hello there {{user}}, {self.crew.mention}!")})).status, 200, "new words around a ping that was already there")
        for added in (f"{self.crew.mention} {self.helpers.mention}", f"{self.crew.mention} @everyone", f"{self.crew.mention} @here"):
            response = await client.put(self.path("/settings/greetings"), json={"welcome": text(f"Hello {{user}} {added}")})
            self.assertEqual(response.status, 400, added)
        other = FakeChannel(self.guild, "lobby")
        self.assertEqual((await client.put(self.path("/settings/greetings"), json={"welcome_channel": str(other.id)})).status, 400, "the same ping in another channel is a new ping")
        self.helpers.mentionable = True
        self.assertEqual((await client.put(self.path("/settings/greetings"), json={"welcome": text(f"Hello {{user}} {self.crew.mention} {self.helpers.mention}")})).status, 200,
                         "a role anyone can mention takes no right")

    async def test_the_commands_ask_for_the_same_right(self):
        manager = self.manager()
        ctx = self.context(manager)
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "roles")
        self.assertEqual(ctx.fails, ["You need Mention Everyone in that channel to ping through the bot."])
        self.assertEqual(self.settings("greetings")["welcome_mentions"], "member")
        ctx = self.context()
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "all")
        self.assertEqual(ctx.oks, [greetings_module.PINGS["all"]])
        ctx = self.context(manager)
        await self.cog_.greetings_welcome.callback(self.cog_, ctx, message="@everyone hello {user}")
        other = FakeChannel(self.guild, "lobby")
        await self.cog_.greetings_welcome_channel.callback(self.cog_, ctx, other)
        await self.cog_.greetings_set_welcome.callback(self.cog_, ctx, other)
        self.assertEqual(ctx.fails, ["You need Mention Everyone in that channel to ping through the bot."] * 3)
        cfg = self.settings("greetings")
        self.assertEqual((cfg["welcome"]["content"], cfg["welcome_channel"]), (self.words, self.channel.id))
        await self.cog_.greetings_welcome.callback(self.cog_, ctx, message="hello {user} @here")
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "member")
        self.assertEqual(ctx.oks, ["The welcome message is set.", greetings_module.PINGS["member"]], "the ping that was already there, and narrowing, take nothing")
        self.assertEqual(required_permissions(self.cog_.greetings_mentions), ("manage_guild",))

    async def test_a_ping_the_bot_can_not_send_is_said_on_the_page_with_the_remedy(self):
        note = ["The welcome message pings a role or everyone, and the bot can't send that ping in #general. Give it Mention Everyone there."]
        client = await self.client()

        async def said():
            return await (await client.get(self.path("/greetings/problems"))).json()

        await self.configure(welcome_mentions="all")
        self.assertEqual(await said(), [], "the bot has the permission")
        self.role_bot.permissions.names.discard("administrator")
        self.role_bot.permissions.names.discard("mention_everyone")
        self.assertEqual(await said(), note)
        await self.configure(welcome=text(f"Hi {{user}}, {self.crew.mention} will help."), welcome_mentions="roles")
        self.assertEqual(await said(), note, "a role that isn't open to mention takes the permission too")
        self.crew.mentionable = True
        self.assertEqual(await said(), [], "a role anyone can mention takes none")
        await self.configure(welcome=text("Hi {user} @here"))
        self.assertEqual(await said(), [], "and in this choice @here is not a ping")
        await self.configure(welcome_mentions="all")
        self.assertEqual(await said(), note)
        await self.configure(welcome_on=False)
        self.assertEqual(await said(), [], "a welcome that's switched off pings nobody")
        await self.configure(welcome_on=True, welcome_mentions="member")
        self.assertEqual(await said(), [])
        await self.configure(welcome_mentions="all", welcome_channel=None)
        self.assertEqual(await said(), [])
        await self.configure(welcome_channel=self.channel.id)
        self.role_bot.permissions.names.add("mention_everyone")
        self.assertEqual(await said(), [])

    async def test_with_no_welcome_channel_there_is_nowhere_to_need_the_right(self):
        await self.configure(welcome_channel=None)
        ctx = self.context(self.manager())
        await self.cog_.greetings_mentions.callback(self.cog_, ctx, "all")
        self.assertEqual(ctx.fails, [])
        self.assertEqual(self.cog_.pinged_channels(self.settings("greetings")), {})
        client = await self.site(self.manager())
        self.assertEqual((await client.put(self.path("/settings/greetings"), json={"welcome_mentions": "roles"})).status, 200)
        await self.cog_.greetings_welcome_channel.callback(self.cog_, ctx, self.channel)
        self.assertEqual(ctx.fails, ["You need Mention Everyone in that channel to ping through the bot."], "naming the channel later is when the ping becomes real")


class InviteTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.listed = [a_listed_invite("alpha", 3), a_listed_invite("beta", 7, inviter=(777, "Hopper"))]
        await self.configure(welcome_channel=self.channel.id, welcome=text("{user} came by {invite} from {invite(inviter)}, use {invite(uses)}"), dm=text("You used {invite}"))
        await self.settle()
        self.reads = 0

    def use(self, code):
        self.listed = [a_listed_invite(item.code, item.uses + (item.code == code), inviter=(item.inviter.id, item.inviter.global_name)) for item in self.listed]

    async def test_a_welcome_and_its_dm_show_the_invite_that_was_used(self):
        self.use("beta")
        member = await self.join("ada")
        self.assertEqual(self.channel.sent[-1].content, "ada came by beta from Hopper, use 8")
        self.assertEqual(member.dms[0]["content"], "You used beta")
        self.assertEqual(self.reads, 1, "one read serves the post and the DM")
        self.use("alpha")
        await self.join("grace")
        self.assertEqual(self.channel.sent[-1].content, "grace came by alpha from Grace, use 4")

    async def test_a_join_with_no_invite_just_ahead_of_another_reads_unknown(self):
        # Ada came in through Server Discovery. Grace used beta a moment later, so beta's count rose before Ada's read, and Grace's event came
        # in while Ada's read waited for joins to settle. Neither welcome names beta, since the rise could be either's.
        self.cog_.invites.settle = 0.05
        reading = asyncio.Event()
        real = self.cog_.invites.used_by

        async def used_by(member):
            reading.set()
            return await real(member)

        self.use("beta")
        with mock.patch.object(self.cog_.invites, "used_by", used_by):
            welcoming = asyncio.ensure_future(self.cog_.on_member_join(FakeMember(self.guild, "ada")))
            await reading.wait()
            await self.join("grace")
            await welcoming
        self.assertEqual(sorted(sent.content for sent in self.channel.sent), ["ada came by Unknown from Unknown, use 0", "grace came by Unknown from Unknown, use 0"])

    async def test_it_works_with_the_statistics_pack_not_even_loaded(self):
        self.assertIsNone(self.bot.module("statistics"))
        self.use("alpha")
        await self.join()
        self.assertIn("came by alpha", self.channel.sent[-1].content)

    async def test_when_it_can_not_be_told_the_invite_reads_unknown(self):
        await self.join("ada")
        self.assertEqual(self.channel.sent[-1].content, "ada came by Unknown from Unknown, use 0")
        self.use("alpha")
        self.use("beta")
        await self.join("grace")
        self.assertEqual(self.channel.sent[-1].content, "grace came by Unknown from Unknown, use 0", "two counts rose, so it's neither")

    async def test_a_server_that_shows_no_invite_is_never_read(self):
        await self.configure(welcome=text("Hi {user}"), dm=text("Hello"))
        await self.settle()
        self.assertFalse(self.cog_.invites.watching(self.guild.id))
        self.reads = 0
        self.use("alpha")
        await self.join()
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.cog_.invites._answers, {}, "and nothing is held for the join")
        await self.configure(wait_for_rules=True)
        await self.join("waiting", pending=True)
        self.assertEqual((self.reads, self.cog_.invites._answers), (0, {}))

    async def test_what_decides_whether_a_server_s_invites_are_read(self):
        for form, wanted in [
            ({"welcome": text("Hi"), "dm": text("Hello")}, False),
            ({"welcome": text("via {invite}")}, True),
            ({"welcome_on": False}, False),
            ({"dm_same_as_welcome": True}, True),
            ({"dm_on": False}, False),
            ({"dm_on": True, "dm_same_as_welcome": False, "dm": {"content": "", "embeds": [{"footer": {"text": "by {invite(inviter)}"}}], "buttons": []}}, True),
            ({"dm": text("Hello"), "farewell": text("left, {invite}")}, False),
        ]:
            await self.configure(**form)
            await self.settle()
            self.assertEqual((self.guild.id in self.cog_._invite_servers, self.cog_.invites.watching(self.guild.id)), (wanted, wanted), form)

    async def test_a_welcome_that_waited_for_the_rules_shows_the_invite_noted_at_the_join(self):
        await self.configure(wait_for_rules=True)
        self.use("beta")
        waiting = await self.join("waiting", pending=True)
        self.assertEqual(self.reads, 1)
        self.use("alpha")
        await self.join("quick")
        waiting.pending = False
        await self.cog_.on_member_update(SimpleNamespace(pending=True), waiting)
        self.assertEqual(self.channel.sent[-1].content, "waiting came by beta from Hopper, use 8")
        self.assertEqual(waiting.dms[0]["content"], "You used beta")
        self.assertEqual(self.reads, 2, "accepting the rules reads nothing: nobody joined then")

    async def test_someone_who_leaves_from_the_rules_screen_has_their_invite_forgotten(self):
        await self.configure(wait_for_rules=True)
        self.use("beta")
        waiting = await self.join("waiting", pending=True)
        self.assertEqual(self.cog_.invites.kept(self.guild.id, waiting.id)[1].code, "beta")
        await self.leave(waiting)
        self.assertEqual(self.cog_.invites.kept(self.guild.id, waiting.id), (False, None))

    async def test_a_test_shows_a_sample_invite_and_reads_none(self):
        self.use("alpha")
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertEqual(ctx.sent[0]["content"], "admin came by sample from Bot, use 1")
        self.assertEqual(self.admin.dms[-1]["content"], "You used sample")
        client = await self.client()
        await client.post(self.path("/greetings/test"), json={"which": "welcome", "payload": text("{invite} #{invite(channel)}"), "channel_id": str(self.channel.id)})
        self.assertEqual(self.channel.sent[-1].content, f"sample #{self.channel.id}")
        self.assertEqual(self.reads, 0)
        await self.join("ada")
        self.assertEqual(self.channel.sent[-1].content, "ada came by alpha from Grace, use 4", "and the use it left unread is still there for the join that made it")

    async def test_ten_minutes_on_the_rules_screen_is_as_long_as_the_invite_is_kept(self):
        await self.configure(wait_for_rules=True)
        self.use("beta")
        waiting = await self.join("waiting", pending=True)
        now = [time.monotonic()]
        self.cog_.invites.clock = lambda: now[0]
        now[0] += invitewatch.KEEP_SECONDS + 1
        waiting.pending = False
        await self.cog_.on_member_update(SimpleNamespace(pending=True), waiting)
        self.assertEqual(self.channel.sent[-1].content, "waiting came by Unknown from Unknown, use 0")

    async def test_a_slow_read_holds_the_welcome_five_seconds_at_most(self):
        self.assertEqual(Greetings.INVITE_WAIT, 5.0)
        self.cog_.INVITE_WAIT = 0.05
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return list(self.listed)

        self.guild.invites = slow
        self.use("alpha")
        member = await asyncio.wait_for(self.join("ada"), 5)
        self.assertEqual(self.channel.sent[-1].content, "ada came by Unknown from Unknown, use 0")
        self.assertEqual(member.dms[0]["content"], "You used Unknown", "the DM agrees with the post")
        gate.set()
        await self.settle()
        self.assertEqual(self.cog_.invites.kept(self.guild.id, member.id)[1].code, "alpha", "the answer still arrives, for whatever asks next")

    async def test_a_read_that_breaks_costs_the_invite_and_nothing_else(self):
        async def broken():
            raise RuntimeError("something else broke")

        self.guild.invites = broken
        with self.assertLogs(greetings_module.log, "ERROR"):
            await self.join("ada")
        self.assertEqual(self.channel.sent[-1].content, "ada came by Unknown from Unknown, use 0")

    async def test_without_manage_server_the_page_says_so_with_the_remedy(self):
        self.role_bot.permissions.names.discard("manage_guild")
        self.role_bot.permissions.names.discard("administrator")
        client = await self.client()
        await self.join("ada")
        self.assertEqual(self.channel.sent[-1].content, "ada came by Unknown from Unknown, use 0")
        problems = await (await client.get(self.path("/greetings/problems"))).json()
        self.assertEqual(problems, ["A greeting here shows the invite someone joined by, and the bot can't read this server's invites, so it reads Unknown. Give the bot Manage Server."])
        self.role_bot.permissions.names.add("manage_guild")
        self.assertEqual(await (await client.get(self.path("/greetings/problems"))).json(), [])

    async def test_a_join_that_got_no_welcome_does_not_lend_its_invite_to_the_next_person(self):
        await self.configure(welcome_again_after=10)
        self.use("beta")
        first = await self.join("first")
        self.assertEqual(self.channel.sent[-1].content, "first came by beta from Hopper, use 8")
        await self.leave(first)
        self.use("beta")  # they come straight back by the same invite, inside the rejoin wait, and nothing is read for them
        again = FakeMember(self.guild, "first")
        again.id = first.id
        await self.cog_.on_member_join(again)
        self.assertEqual(len(self.channel.sent), 1, "no second welcome")
        await self.join("second")  # with no invite of their own, as from the server's listing
        self.assertEqual(self.channel.sent[-1].content, "second came by Unknown from Unknown, use 0")
        self.use("alpha")
        await self.join("third")
        self.assertEqual(self.channel.sent[-1].content, "third came by alpha from Grace, use 4", "and the join after that is told again")

    async def test_joins_a_raid_skipped_leave_the_next_welcome_s_invite_unknown(self):
        await self.configure(burst_joins=1)
        self.cog_.BURST_QUIET = 30
        self.use("alpha")
        await self.join("first")
        self.use("alpha")
        self.reads = 0
        await self.join("skipped")
        self.assertEqual(self.reads, 0, "a skipped join costs no read")
        self.cog_._joins.clear()  # the minute is over
        await self.join("next")
        self.assertEqual(self.channel.sent[-1].content, "next came by Unknown from Unknown, use 0")

    async def test_a_bot_left_unwelcomed_does_not_spoil_the_next_person_s_invite(self):
        await self.configure(welcome_bots=False)
        await self.join("robot", bot=True)
        self.use("beta")
        await self.join("person")
        self.assertEqual(self.channel.sent[-1].content, "person came by beta from Hopper, use 8")

    async def test_an_inviter_s_name_can_not_widen_here_to_everyone(self):
        await self.configure(welcome_mentions="all", welcome=text("@here {user} came by {invite} from {invite(inviter)}"))
        await self.settle()
        self.listed = [a_listed_invite("alpha", 3, inviter=(555, "@everyone")), a_listed_invite("beta", 7, inviter=(777, "Hopper"))]
        await self.cog_.invites.watch(self.guild)
        self.use("alpha")
        await self.join("ada")
        self.assertEqual(self.channel.sent[-1].content, "@here ada came by alpha from @everyone")
        self.assertFalse(self.channel.sent[-1].kwargs["allowed_mentions"].everyone)
        self.use("beta")
        await self.join("grace")
        self.assertTrue(self.channel.sent[-1].kwargs["allowed_mentions"].everyone, "with no outside @everyone, the template's @here goes through")

    async def test_a_server_that_shows_no_invite_gets_no_such_note(self):
        await self.configure(welcome=text("Hi"), dm=text("Hello"))
        await self.settle()
        self.role_bot.permissions.names.clear()
        client = await self.client()
        self.assertEqual(await (await client.get(self.path("/greetings/problems"))).json(), [])

    async def test_a_new_invite_and_a_reconnect_keep_the_counts_right(self):
        fresh = a_listed_invite("fresh", 0, max_uses=1)
        fresh.guild = self.guild
        await self.cog_.on_invite_create(fresh)
        await self.join("ada")
        self.assertIn("came by fresh", self.channel.sent[-1].content, "a one-use invite made and used between two reads")
        self.use("alpha")
        self.reads = 0
        await self.cog_.on_ready()
        self.assertEqual(self.reads, 1, "after a reconnect the counts start from what Discord says now")
        await self.join("grace")
        self.assertIn("came by Unknown", self.channel.sent[-1].content, "a use from while the bot was away isn't pinned on the next person")

    async def test_leaving_the_server_lets_the_counts_go_and_coming_back_reads_them_again(self):
        await self.cog_.on_guild_remove(self.guild)
        self.assertFalse(self.cog_.invites.watching(self.guild.id))
        await self.cog_.on_guild_join(self.guild)
        self.assertTrue(self.cog_.invites.watching(self.guild.id))
        other = FakeGuild("Elsewhere")
        other.invites = mock.AsyncMock(return_value=[])
        await self.cog_.on_guild_join(other)
        other.invites.assert_not_awaited()
        self.assertFalse(self.cog_.invites.watching(other.id), "a server that shows no invite isn't read")

    async def test_a_goodbye_and_a_ban_read_unknown_and_read_no_invites(self):
        await self.configure(farewell=text("{user} left, came by {invite}"), ban=text("{user} banned, came by {invite(inviter)}"))
        await self.settle()
        self.reads = 0
        member = FakeMember(self.guild, "gone")
        await self.cog_.on_member_ban(self.guild, member)
        await self.configure(ban=text(""))
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual([sent.content for sent in self.channel.sent], ["gone banned, came by Unknown", "leaver left, came by Unknown"])
        self.assertEqual(self.reads, 0)


class WithCards(Case):
    modules = [Greetings, Cards]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cards = self.cog(Cards)
        self.cards.pictures = {"welcome": {"filename": "welcome.png", "data": PNG, "in_embed": True}}
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {user}"))

    def file_of(self, sent):
        file = (sent.kwargs if hasattr(sent, "kwargs") else sent)["file"]
        return file.filename, file.fp.read()


class PictureTest(WithCards):
    async def test_inside_the_embed_the_picture_becomes_the_first_embed_s_image(self):
        await self.configure(welcome={"content": "Hi {user}", "embeds": [{"title": "Welcome", "image": {"url": "https://example.com/old.png"}}, {"description": "second"}], "buttons": []})
        await self.join()
        sent = self.channel.sent[-1]
        self.assertEqual(self.file_of(sent), ("welcome.png", PNG))
        self.assertEqual([embed.to_dict().get("image", {}).get("url") for embed in sent.embeds], ["attachment://welcome.png", None])
        self.assertEqual((sent.content, sent.embeds[0].title), ("Hi newcomer", "Welcome"))

    async def test_a_message_with_no_embed_gets_one_that_holds_only_the_picture(self):
        await self.join()
        sent = self.channel.sent[-1]
        self.assertEqual([{name: value for name, value in embed.to_dict().items() if name not in ("type", "flags")} for embed in sent.embeds], [{"image": {"url": "attachment://welcome.png"}}])
        self.assertEqual((sent.content, self.file_of(sent)), ("Hi newcomer", ("welcome.png", PNG)))

    async def test_outside_the_embed_it_is_a_plain_attachment(self):
        self.cards.pictures["welcome"] = {"filename": "welcome.gif", "data": GIF, "in_embed": False}
        await self.join()
        sent = self.channel.sent[-1]
        self.assertEqual((sent.content, sent.embeds, self.file_of(sent)), ("Hi newcomer", [], ("welcome.gif", GIF)))

    async def test_a_picture_alone_is_a_welcome(self):
        await self.configure(welcome=text(""))
        member = await self.join()
        sent = self.channel.sent[-1]
        self.assertEqual((sent.content, self.file_of(sent)), ("", ("welcome.png", PNG)))
        self.assertEqual([(row["kind"], row["user_id"]) for row in await self.posts()], [("welcome", member.id)], "and it's noted like any welcome")

    async def test_the_picture_is_asked_for_with_the_member_and_the_values_the_message_shows(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 500, 1.0)", (self.guild.id,))
        await self.configure(welcome=text("{user} is {joined}"))
        member = await self.join("ada")
        self.assertEqual([(asked, kind) for asked, kind, _ in self.cards.asked], [(member.id, "welcome"), (member.id, "dm")], "one for the post, and one for a DM that can be a picture alone")
        asked_id, kind, scope = self.cards.asked[0]
        self.assertIsInstance(scope, Scope)
        self.assertEqual((scope.member, scope.guild, scope.channel, scope.extra["joined"]), (member, self.guild, self.channel, "501"))
        self.assertEqual(self.channel.sent[-1].content, "ada is 501")

    async def test_no_picture_means_the_message_as_it_always_was(self):
        self.cards.pictures = {}
        await self.join()
        sent = self.channel.sent[-1]
        self.assertEqual((sent.content, sent.embeds), ("Hi newcomer", []))
        self.assertNotIn("file", sent.kwargs)
        await self.configure(welcome=text(""))
        await self.join("second")
        self.assertEqual(len(self.channel.sent), 1, "no words and no picture is nothing to post")

    async def test_whatever_goes_wrong_with_the_picture_the_welcome_still_goes_out(self):
        for broken in (RuntimeError("drawing failed"), asyncio.TimeoutError()):
            self.cards.error = broken
            with self.assertLogs(greetings_module.log, "WARNING"):
                await self.join()
            self.assertEqual(self.channel.sent[-1].content, "Hi newcomer")
            self.assertNotIn("file", self.channel.sent[-1].kwargs)
        self.cards.error = None
        for odd in ("a string", {"filename": "welcome.png"}, {"filename": "welcome.png", "data": b""}, {"filename": "welcome.png", "data": "text"}, [], 5):
            self.cards.pictures["welcome"] = odd
            await self.join()
            self.assertNotIn("file", self.channel.sent[-1].kwargs, odd)

    async def test_a_picture_that_never_comes_is_not_waited_for(self):
        self.cog_.PICTURE_WAIT = 0.05
        self.cards.wait = 30
        with self.assertLogs(greetings_module.log, "WARNING"):
            await asyncio.wait_for(self.join(), 5)
        self.assertEqual(self.channel.sent[-1].content, "Hi newcomer")
        self.assertEqual(Greetings.PICTURE_WAIT, 20.0)

    async def test_a_file_name_that_is_not_a_plain_picture_name_is_replaced(self):
        for name, data, wanted in [("../../etc/passwd.png", PNG, "welcome.png"), ("a b.png", PNG, "welcome.png"), ("welcome.exe", GIF, "welcome.gif"), (None, PNG, "welcome.png"),
                                   ("card-1_final.gif", GIF, "card-1_final.gif"), ("x" * 80 + ".png", PNG, "welcome.png")]:
            self.cards.pictures["welcome"] = {"filename": name, "data": data, "in_embed": True}
            await self.join()
            sent = self.channel.sent[-1]
            self.assertEqual((self.file_of(sent)[0], sent.embeds[0].image.url), (wanted, f"attachment://{wanted}"), name)

    async def test_where_the_bot_can_not_attach_files_the_words_go_alone_and_the_page_says_why(self):
        self.role_bot.permissions.names.discard("administrator")
        self.role_bot.permissions.names.discard("attach_files")
        await self.join()
        self.assertNotIn("file", self.channel.sent[-1].kwargs)
        self.assertEqual(self.channel.sent[-1].embeds, [])
        client = await self.client()
        self.assertEqual(await (await client.get(self.path("/greetings/problems"))).json(), ["The bot can't attach the welcome image in #general. Give it Attach Files there."])
        self.role_bot.permissions.names.add("attach_files")
        await self.join("second")
        self.assertIn("file", self.channel.sent[-1].kwargs)
        self.assertEqual(await (await client.get(self.path("/greetings/problems"))).json(), [])

    async def test_a_refused_post_with_a_picture_names_attach_files_too(self):
        async def refuse(**kwargs):
            raise refusal(403)

        self.channel.send = refuse
        with self.assertLogs(greetings_module.log, "WARNING"):
            await self.join()
        self.assertIn("Give it View Channel, Send Messages, Embed Links and Attach Files there.", self.cog_._problems[(self.guild.id, "welcome")])

    async def test_the_join_dm_carries_its_own_picture(self):
        self.cards.pictures["dm"] = {"filename": "hello.png", "data": PNG, "in_embed": False}
        await self.configure(dm=text("Hello {user}"))
        member = await self.join()
        self.assertEqual([kind for _, kind, _ in self.cards.asked], ["welcome", "dm"])
        self.assertIs(self.cards.asked[0][2].extra, self.cards.asked[1][2].extra, "both pictures draw from the same values")
        self.assertEqual((member.dms[0]["content"], self.file_of(member.dms[0])), ("Hello newcomer", ("hello.png", PNG)))
        self.assertIsNone(self.cards.asked[1][2].channel)

    async def test_a_dm_can_be_a_picture_alone_and_a_bot_gets_neither(self):
        self.cards.pictures["dm"] = {"filename": "hello.png", "data": PNG, "in_embed": True}
        member = await self.join()
        self.assertEqual((member.dms[0]["content"], self.file_of(member.dms[0])[0]), (None, "hello.png"))
        robot = await self.join("robot", bot=True)
        self.assertEqual(robot.dms, [])
        self.assertEqual([(asked, kind) for asked, kind, _ in self.cards.asked if asked == robot.id], [(robot.id, "welcome")], "no DM picture is drawn for a bot")

    async def test_goodbye_and_ban_messages_carry_no_picture(self):
        self.cards.pictures.update(farewell={"filename": "bye.png", "data": PNG, "in_embed": True}, ban={"filename": "ban.png", "data": PNG, "in_embed": True})
        await self.configure(farewell=text("Bye"), ban=text("Banned"))
        await self.cog_.on_member_ban(self.guild, FakeMember(self.guild, "banned"))
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual(self.cards.asked, [])
        self.assertTrue(all("file" not in sent.kwargs for sent in self.channel.sent))

    async def test_the_test_command_attaches_it_for_the_member_it_is_about(self):
        self.cards.pictures["dm"] = {"filename": "hello.png", "data": PNG, "in_embed": False}
        await self.configure(dm=text("Hello {user}"), farewell=text("Bye {user}"))
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx, member=self.member)
        self.assertEqual([sent["content"] for sent in ctx.sent], ["Hi member", "Bye member"])
        self.assertEqual(self.file_of(ctx.sent[0]), ("welcome.png", PNG))
        self.assertNotIn("file", ctx.sent[1])
        self.assertEqual((self.admin.dms[-1]["content"], self.file_of(self.admin.dms[-1])[0]), ("Hello member", "hello.png"), "the DM goes to whoever asked, about the member they chose")
        self.assertEqual(self.member.dms, [])
        self.assertEqual([(asked, kind) for asked, kind, _ in self.cards.asked], [(self.member.id, "welcome"), (self.member.id, "dm")])
        self.assertEqual(ctx.oks, ["Sent 3 test messages."])

    async def test_the_test_command_sends_a_picture_alone(self):
        await self.configure(welcome=text(""))
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertEqual((ctx.sent[0]["content"], self.file_of(ctx.sent[0])[0], ctx.oks), (None, "welcome.png", ["Sent 1 test message."]))
        self.cards.pictures["dm"] = {"filename": "hello.png", "data": PNG, "in_embed": False}
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertEqual((self.file_of(self.admin.dms[-1])[0], ctx.oks), ("hello.png", ["Sent 2 test messages."]), "the join DM can be a picture alone too")
        self.cards.pictures = {}
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertEqual((ctx.sent, ctx.fails), ([], ["No greeting messages are set yet."]))

    async def test_the_test_command_leaves_the_picture_out_where_the_bot_can_not_attach_files(self):
        self.role_bot.permissions.names.discard("administrator")
        self.role_bot.permissions.names.discard("attach_files")
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertEqual(ctx.sent[0]["content"], "Hi admin")
        self.assertNotIn("file", ctx.sent[0])

    async def test_the_page_s_test_button_attaches_it(self):
        self.cards.pictures["dm"] = {"filename": "hello.gif", "data": GIF, "in_embed": True}
        client = await self.client()
        response = await client.post(self.path("/greetings/test"), json={"which": "welcome", "payload": text("Hi {user}"), "channel_id": str(self.channel.id)})
        self.assertEqual(response.status, 200)
        sent = self.channel.sent[-1]
        self.assertEqual((sent.content, sent.kwargs["files"][0].filename, sent.embeds[0].image.url), ("Hi admin", "welcome.png", "attachment://welcome.png"))
        self.assertFalse(sent.kwargs["allowed_mentions"].users)
        response = await client.post(self.path("/greetings/test"), json={"which": "dm", "payload": text("Hello {user}")})
        self.assertEqual(response.status, 200)
        dm = self.admin.dms[-1]
        self.assertEqual((dm["content"], dm["files"][0].filename, dm["embeds"][0].image.url), ("Hello admin", "hello.gif", "attachment://hello.gif"))
        self.assertEqual([(asked, kind) for asked, kind, _ in self.cards.asked], [(self.admin.id, "welcome"), (self.admin.id, "dm")])
        await client.post(self.path("/greetings/test"), json={"which": "ban", "payload": text("Banned"), "channel_id": str(self.channel.id)})
        self.assertEqual(len(self.cards.asked), 2, "a ban message has no picture to ask for")

    async def test_the_picture_s_own_text_can_make_a_server_s_invites_be_read(self):
        await self.settle()
        self.assertFalse(self.cog_.invites.watching(self.guild.id))
        await self.bot.settings.save(self.guild.id, "welcomecard", {"text": "Invited by {invite(inviter)}"})
        await self.settle()
        self.assertTrue(self.cog_.invites.watching(self.guild.id))
        self.listed = [a_listed_invite("alpha", 4)]
        await self.cog_.invites.watch(self.guild)
        self.listed = [a_listed_invite("alpha", 5)]
        await self.join("ada")
        self.assertEqual(render_text("{invite} by {invite(inviter)}", self.cards.asked[-1][2]), "alpha by Grace", "the picture's text reads the invite from the scope it was handed")
        await self.bot.settings.save(self.guild.id, "welcomecard", {"text": "Welcome"})
        await self.settle()
        self.assertFalse(self.cog_.invites.watching(self.guild.id))

    async def test_a_restart_finds_the_servers_whose_picture_shows_the_invite(self):
        await self.bot.settings.save(self.guild.id, "welcomecard", {"text": "via {invite}"})
        other = FakeGuild("Elsewhere")
        await self.bot.settings.save(other.id, "welcomecard", {"text": "by {invite(inviter)}"})  # a server that saved nothing on the Greetings page
        quiet = FakeGuild("Quiet")
        await self.bot.settings.save(quiet.id, "welcomecard", {"text": "Welcome"})
        fresh = Greetings(self.bot)
        with mock.patch.object(self.bot.timers, "handler"), mock.patch.object(self.bot.settings, "listen"):
            await fresh.on_load()
        self.assertEqual(fresh._invite_servers, {self.guild.id, other.id})


class ScopeForTest(WithCards):
    async def test_a_preview_gets_the_count_as_it_stands_and_a_sample_invite(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 64097, 1.0)", (self.guild.id,))
        scope = await self.cog_.scope_for(self.member, preview=True)
        self.assertEqual((scope.guild, scope.member, scope.channel, scope.extra["joined"]), (self.guild, self.member, None, "64097"))
        self.assertEqual(render_text("{invite} by {invite(inviter)}, {invite(uses)} use, #{invite(channel)}", scope), "sample by Bot, 1 use, #0")
        scope = await self.cog_.scope_for(self.member, self.channel, True)
        self.assertEqual((scope.channel, render_text("{invite(channel)} {channel}", scope)), (self.channel, f"{self.channel.id} general"))
        self.assertEqual(await self.count(), 64097)

    async def test_outside_a_preview_it_gives_what_was_noted_for_that_member(self):
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 300, 1.0)", (self.guild.id,))
        scope = await self.cog_.scope_for(self.member)
        self.assertEqual(render_text("{joined} {invite} {invite(uses)}", scope), "300 Unknown 0")
        self.cog_._join_numbers[(self.guild.id, self.member.id)] = 250
        self.cog_.invites._keep((self.guild.id, self.member.id), invitewatch.Invite(code="alpha", uses=4))
        scope = await self.cog_.scope_for(self.member)
        self.assertEqual(render_text("{joined} {invite} {invite(uses)}", scope), "250 alpha 4")
        self.assertEqual(self.cog_._join_numbers, {(self.guild.id, self.member.id): 250}, "reading it uses nothing up")

    async def test_it_reads_no_invites_from_discord(self):
        await self.configure(welcome=text("{invite}"))
        await self.settle()
        self.reads = 0
        await self.cog_.scope_for(self.member, preview=True)
        await self.cog_.scope_for(self.member)
        self.assertEqual(self.reads, 0)


class CommandTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {user}"), dm=text("Hello"), farewell=text("Bye"), ban=text("Banned"))

    async def test_switch_turns_each_message_and_option_on_and_off(self):
        for what, (field, turned_on, turned_off) in greetings_module.SWITCHES.items():
            before = self.settings("greetings")[field]
            for on in (not before, before):
                ctx = self.context()
                await self.cog_.greetings_switch.callback(self.cog_, ctx, what, on)
                self.assertEqual(self.settings("greetings")[field], on, what)
                self.assertEqual(ctx.oks, [turned_on if on else turned_off], what)
                self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], f"greetings.{what}_{'on' if on else 'off'}")
        self.assertEqual(set(greetings_module.SWITCHES), set(greetings_module.Switch.__args__), "every choice the command offers is one it knows")
        self.assertEqual({field for field, _, _ in greetings_module.SWITCHES.values()},
                         {"welcome_on", "dm_on", "farewell_on", "ban_on", "dm_same_as_welcome", "welcome_bots", "welcome_delete_on_leave"})
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["user_name"], "admin")

    async def test_a_switch_keeps_the_message(self):
        await self.cog_.greetings_switch.callback(self.cog_, self.context(), "welcome", False)
        self.assertEqual(self.settings("greetings")["welcome"]["content"], "Hi {user}")
        await self.join()
        self.assertEqual(self.channel.sent, [])

    async def test_delete_after_sets_a_lifetime_and_leaving_it_out_clears_it(self):
        ctx = self.context()
        await self.cog_.greetings_delete_after.callback(self.cog_, ctx, "welcome", 600)
        await self.cog_.greetings_delete_after.callback(self.cog_, ctx, "farewell", 2 * 86400)
        await self.cog_.greetings_delete_after.callback(self.cog_, ctx, "ban", 30 * 86400)
        cfg = self.settings("greetings")
        self.assertEqual((cfg["welcome_delete_after"], cfg["farewell_delete_after"], cfg["ban_delete_after"]), (600, 172800, 2592000))
        self.assertEqual(ctx.oks[0], "Welcome messages are deleted 10 minutes after they go out. Posts already made follow.")
        self.assertEqual(ctx.oks[1], "Leave messages are deleted 2 days after they go out. Posts already made follow.")
        await self.cog_.greetings_delete_after.callback(self.cog_, ctx, "welcome")
        self.assertEqual((self.settings("greetings")["welcome_delete_after"], ctx.oks[-1]), (0, "Welcome messages stay until someone deletes them."))
        await self.cog_.greetings_delete_after.callback(self.cog_, ctx, "ban", 30 * 86400 + 1)
        self.assertEqual((ctx.fails, self.settings("greetings")["ban_delete_after"]), (["A post can stay up to 30 days."], 2592000))

    async def test_delete_after_by_command_moves_the_posts_already_made(self):
        await self.join()
        self.assertEqual(await self.timers(), [])
        await self.cog_.greetings_delete_after.callback(self.cog_, self.context(), "welcome", 600)
        self.assertEqual(len(await self.timers()), 1)

    async def test_show_reports_what_is_set(self):
        ctx = self.context()
        await self.cog_.greetings.callback(self.cog_, ctx)
        said = ctx.sent[-1]["content"]
        self.assertEqual(said.splitlines()[:6], [
            f"**Welcome:** <#{self.channel.id}>, set", f"**Farewell:** <#{self.channel.id}>, set", f"**Ban:** <#{self.channel.id}>, set", "**Join DM:** set",
            "The welcome message pings the member it greets.", f"**Joined count:** {self.guild.member_count:,}"])
        self.assertEqual(len(said.splitlines()), 7)
        self.assertFalse(ctx.sent[-1]["allowed_mentions"].everyone)
        await self.configure(welcome_on=False, welcome_delete_after=5400, farewell_delete_after=86400, ban_on=False, dm_on=False, dm_same_as_welcome=True, welcome_mentions="all",
                             welcome_again_after=90, welcome_bots=False, welcome_delete_on_leave=True, farewell_mentions="roles", ban_mentions="all")
        await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, 64097, 1.0)", (self.guild.id,))
        await self.cog_.greetings.callback(self.cog_, ctx)
        self.assertEqual(ctx.sent[-1]["content"].splitlines()[:-1], [
            f"**Welcome:** <#{self.channel.id}>, set, switched off, deleted after 1 hour and 30 minutes",
            f"**Farewell:** <#{self.channel.id}>, set, deleted after 1 day",
            f"**Ban:** <#{self.channel.id}>, set, switched off",
            "**Join DM:** sends the welcome message, switched off",
            "The welcome message pings the member it greets, the roles its text mentions, and @everyone or @here when its text has them.",
            "The leave message pings the member who left and the roles its text mentions.",
            "The ban message pings the person who was banned, the roles its text mentions, and @everyone or @here when its text has them.",
            "**Joined count:** 64,097",
            "Someone who rejoins within 1 hour and 30 minutes of a welcome gets no second one.",
            "A bot that joins gets no welcome message.",
            "A member's welcome message is deleted when they leave or are banned.",
        ])

    async def test_testgreet_can_be_about_a_chosen_member(self):
        robot = FakeMember(self.guild, "robot", bot=True)
        await self.configure(welcome=text("{user} {mention} {if({user(bot)}==true):bot|person}"), dm=text("Hello {user}"))
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx, member=robot)
        self.assertEqual(ctx.sent[0]["content"], f"robot {robot.mention} bot")
        self.assertFalse(ctx.sent[0]["allowed_mentions"].users, "a test pings nobody, whoever it's about")
        self.assertEqual(self.admin.dms[-1]["content"], "Hello robot", "the DM comes to whoever asked")
        self.assertEqual(robot.dms, [])
        self.assertEqual(ctx.oks, ["Sent 4 test messages."])

    async def test_testgreet_with_the_dm_sending_the_welcome_message(self):
        await self.configure(dm_same_as_welcome=True, welcome=text("Hi {user} in #{channel}"), farewell=text(""), ban=text(""))
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertEqual((ctx.sent[0]["content"], self.admin.dms[-1]["content"], ctx.oks), ("Hi admin in #general", "Hi admin in #general", ["Sent 2 test messages."]))

    async def test_every_new_command_needs_manage_server(self):
        for command in (self.cog_.greetings_switch, self.cog_.greetings_delete_after, self.cog_.greetings_joined, self.cog_.greetings_mentions, self.cog_.greetings_testgreet):
            self.assertEqual(required_permissions(command), ("manage_guild",), command.name)
        names = {command.name for command in self.cog_.greetings.commands if command.with_app_command} | {self.cog_.greetings.fallback}
        self.assertEqual(names, {"show", "welcome_channel", "welcome", "joindm", "farewell_channel", "farewell", "ban_channel", "banmessage", "testgreet",
                                 "switch", "delete_after", "joined", "mentions"}, "nine there were and four are added, which leaves room under Discord's 25 for the picture's three")
        self.assertFalse([name for name in names if name.startswith("image")], "names that start with image are the picture's own")


class OpsTest(unittest.TestCase):
    def test_the_demo_site_takes_a_dm_s_files_as_the_real_one_does(self):
        self.assertEqual(inspect.signature(DemoOps.send_dm), inspect.signature(DiscordOps.send_dm))
        self.assertIn("files", inspect.signature(DiscordOps.send_dm).parameters, "the page's Test button sends the welcome image with the join DM")


class ConvertRouteTest(Case):
    async def convert(self, body, access="full"):
        client = await self.client(access=access)
        response = await client.post(self.path("/greetings/convert"), json=body)
        return response.status, await response.json()

    async def test_pasted_text_comes_back_as_a_message_with_its_notes(self):
        status, answer = await self.convert({"text": "{{User.Mention}} just landed in **{{Guild.Name}}** as its {{Ordinal(Guild.Members)}} arrival. {{Server.Motto}}"})
        self.assertEqual(status, 200)
        self.assertEqual(answer["message"], {"content": "{mention} just landed in **{server}** as its {ord(plain):{server(members)}} arrival. {{Server.Motto}}", "embeds": [], "buttons": []})
        self.assertEqual(answer["notes"], ["`{{Server.Motto}}` has no match in the template language, so it stays as written."])
        status, answer = await self.convert({"text": "You are the {{Ordinal(Guild.Members)}} of {{FormatNumber(Guild.Members)}}", "number_format": "dots"})
        self.assertEqual((status, answer["message"]["content"]), (200, "You are the {ord(dots):{server(members)}} of {num(dots):{server(members)}}"))

    async def test_pasted_text_keeps_the_line_breaks_a_message_had(self):
        status, answer = await self.convert({"text": "Hi {{User.Mention}}!\n{{#User.Bot}}\nA machine.\n{{/User.Bot}}\n{{! note }}\nSee you around."})
        self.assertEqual((status, answer["message"]["content"]), (200, "Hi {mention}!\n{if({user(bot)}==true):\nA machine.\n}\n\nSee you around."))

    async def test_pasted_text_reads_text_in_quotes_as_a_message_did(self):
        status, answer = await self.convert({"text": "{{'C:\\temp'}} {{User.Name == \"John\"}}"})
        self.assertEqual((status, answer["message"]["content"]), (200, "C:\\temp {if({user(profile_name)}==John):true|false}"))
        self.assertEqual(len(answer["notes"]), 1)
        self.assertIn("holds text in double quotes", answer["notes"][0])

    async def test_a_message_that_conversion_made_too_long_still_comes_back_for_the_editor(self):
        name = "{{User.Name}}"
        pasted = "a" * (1990 - 3 * len(name)) + name * 3
        note = "Message text: once converted this is 2,011 characters, and it can hold 2,000. Save refuses the message until it is 11 characters shorter."
        for text in (pasted, json.dumps({"content": pasted})):
            status, answer = await self.convert({"text": text})
            self.assertEqual((status, len(answer["message"]["content"]), answer["notes"]), (200, 2011, [note]))
        client = await self.client()
        response = await client.put(self.path("/settings/greetings"), json={"welcome": answer["message"]})
        self.assertEqual(response.status, 400, "Save refuses it until it is shortened")
        response = await client.put(self.path("/settings/greetings"), json={"welcome": {**answer["message"], "content": answer["message"]["content"][11:]}})
        self.assertEqual(response.status, 200)

    async def test_a_paste_is_never_answered_with_a_server_error(self):
        status, answer = await self.convert({"text": json.dumps({"content": "hi", "note": "text" + "9" * 4400})})
        self.assertEqual((status, answer["message"]["content"]), (200, "hi"))
        for broken in (RuntimeError("a fault in the converter"), RecursionError("nested too deeply"), ValueError("a number too long to read")):
            with mock.patch.object(greetings_module.doublebrace, "convert_message", side_effect=broken), self.assertLogs(level="ERROR"):
                status, answer = await self.convert({"text": json.dumps({"content": "hi"})})
            self.assertEqual((status, answer["fields"]), (400, {"text": "This paste couldn't be converted. Paste the message's text, or its JSON, on its own."}), broken)

    async def test_a_pasted_message_as_json_keeps_its_embed(self):
        stored = '{"embeds": [{"title": "Welcome {{User.Name}}", "thumbnail": {"url": "{{Guild.Icon}}"}, "description": "Arrival {{Ordinal(Guild.Members)}}"}]}'
        status, answer = await self.convert({"text": "  " + stored + "\n", "number_format": "commas"})
        self.assertEqual((status, answer["notes"]), (200, []))
        self.assertEqual(answer["message"], {"content": "", "buttons": [], "embeds": [{"title": "Welcome {user(profile_name)}", "thumbnail": {"url": "{server(icon)}"},
                                                                                       "description": "Arrival {ord:{server(members)}}"}]})

    async def test_what_only_looks_like_json_is_read_as_text(self):
        for pasted, wanted in [("{{User.Name}} joined", "{user(profile_name)} joined"), ('{"a": 1} is not a message', '{"a": 1} is not a message'),
                               ('{"note": "keep {{Guild.Name}}"}', doublebrace.OPEN_BRACE + '"note": "keep {server}"' + doublebrace.CLOSE_BRACE),
                               ("[1, 2]", "[1, 2]"), ('[{"title": "Hi"}, 5]', '[{"title": "Hi"}, 5]'), ("[]", "[]"), ("{broken", "{broken"), ('"just {{Guild.Name}}"', '"just {server}"')]:
            status, answer = await self.convert({"text": pasted})
            self.assertEqual((status, answer["message"]["embeds"]), (200, []), pasted)
            self.assertEqual(answer["message"]["content"], wanted, pasted)

    async def test_a_pasted_list_of_embeds_is_a_message(self):
        status, answer = await self.convert({"text": '[{"title": "Hi {{User.Username}}"}, {"description": "{{Guild.Name}}"}]'})
        self.assertEqual((status, answer["message"]), (200, {"content": "", "buttons": [], "embeds": [{"title": "Hi {user(name)}"}, {"description": "{server}"}]}))

    async def test_nothing_is_saved_by_converting(self):
        before = self.settings("greetings")
        await self.convert({"text": "Hi {{User.Name}}"})
        self.assertEqual(self.settings("greetings"), before)
        self.assertEqual(await self.bot.activity_log.recent(self.guild.id), [])

    async def test_what_can_not_be_converted_is_refused_with_a_sentence(self):
        for body, field, said in [({}, "text", "Paste the message's text"), ({"text": "   "}, "text", "Paste the message's text"), ({"text": 5}, "text", "Paste the message's text"),
                                  ({"text": "x" * 50001}, "text", "This takes up to 50,000"), ({"text": "hi", "number_format": "roman"}, "number_format", "Pick one of the listed"),
                                  ({"text": "hi", "number_format": ["plain"]}, "number_format", "Pick one of the listed"), ({"text": "x" * 2001}, "text", "2000"),
                                  ({"text": '{"embeds": [{"title": "' + "x" * 300 + '"}]}'}, "text", "Title can be at most 256"), ({"text": "{{User.Name}}" * 2001}, "text", "can be at most 2000 characters. This is 26013.")]:
            status, answer = await self.convert(body)
            self.assertEqual(status, 400, str(body)[:60])
            self.assertIn(said, answer["fields"][field], str(body)[:60])

    async def test_a_paste_built_to_be_slow_is_answered_at_once(self):
        value = "{" * 400 + "x" * 1198 + "}" * 400
        slow = json.dumps({"embeds": [{"fields": [{"name": "n", "value": value} for _ in range(24)]}]})
        self.assertLessEqual(len(slow), greetings_module.CONVERT_MAX)
        long_text = "{" * 400 + "x" * 1201 + "}" * 400
        for body, sentence in [(slow, "Embed 1: Field 1 value can be at most 1024 characters. This is 1998."), (long_text, "The message text can be at most 2000 characters. This is 2001.")]:
            with mock.patch.object(doublebrace, "_stays_plain", mock.Mock(side_effect=AssertionError("it did the work before it looked at the lengths"))) as asked:
                status, answer = await self.convert({"text": body})
            self.assertEqual((status, answer["fields"]), (400, {"text": sentence}))
            asked.assert_not_called()

    async def test_it_takes_manage_server(self):
        self.assertEqual((await self.convert({"text": "hi"}, access="staff"))[0], 403)

    async def client_as(self, person):
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(person.id, person.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def test_one_paste_is_converted_at_a_time_across_the_bot(self):
        # Two people send two pastes each, all at once. One conversion runs at a time, so a paste never holds both of the bot's template slots
        # and greetings, tags and triggers always have one. Every paste is still answered.
        running, most = [0], [0]
        guard = threading.Lock()
        real = greetings_module._converted

        def slow(text, number_format):
            with guard:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.05)
            with guard:
                running[0] -= 1
            return real(text, number_format)

        clients = [await self.client(), await self.client_as(FakeMember(self.guild, "co-admin"))]
        with mock.patch.object(greetings_module, "_converted", slow):
            answers = await asyncio.gather(*(client.post(self.path("/greetings/convert"), json={"text": "Hi {{User.Name}}"}) for client in clients for _ in range(2)))
        self.assertEqual([response.status for response in answers], [200] * 4)
        self.assertEqual(most[0], 1)

    async def test_a_paste_is_converted_in_the_person_s_share_of_template_time(self):
        # As tag and trigger previews run: away from the event loop, two runs at most across the bot, and each person a share of each minute.
        with mock.patch.object(greetings_module, "budgeted_render", wraps=greetings_module.budgeted_render) as ran:
            status, answer = await self.convert({"text": "Hi {{User.Name}}"})
        self.assertEqual((status, answer["message"]["content"]), (200, "Hi {user(profile_name)}"))
        self.assertEqual((ran.call_args.args[1], ran.call_args.kwargs), (self.guild.id, {"pool": budget.PREVIEWS, "member_id": self.admin.id}))
        budget.charge(self.guild.id, None, self.admin.id, budget.MEMBER_SECONDS)  # this person's share of the minute is used up
        status, answer = await self.convert({"text": "Hi {{User.Name}}"})
        self.assertEqual((status, answer["fields"]), (400, {"text": "Too much template work this minute. Try again in a minute."}))
