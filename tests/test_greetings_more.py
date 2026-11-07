"""Greetings under load and when something's wrong: bursts of joins, messages that can't go out, and birthdays after an outage."""
import asyncio
import datetime
from unittest import mock

import discord

from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.tagscript import Scope, render_payload
from captainguild.modules import greetings as greetings_module
from captainguild.modules.autoroles import Autoroles
from captainguild.modules.greetings import Greetings
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import FakeMember
from tests.test_greetings import GreetingsCase, frozen, text
from tests.test_web_input import ReadOnly


class BurstTest(GreetingsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_.BURST_QUIET = 0.02
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {mention}"), dm=text("Hello"), burst_joins=3)
        self.dms = []

    def join(self, name):
        member = FakeMember(self.guild, name)

        async def send(**kwargs):
            self.dms.append(name)

        member.send = send
        return self.cog_.on_member_join(member)

    async def test_past_the_limit_nobody_more_is_welcomed_and_one_line_says_how_many(self):
        for number in range(10):
            await self.join(f"raider{number}")
        self.assertEqual(len(self.channel.sent), 3)
        self.assertEqual(len(self.dms), 3)
        await asyncio.wait_for(self.cog_._burst_tasks[self.guild.id], 5)
        self.assertEqual(len(self.channel.sent), 4)
        self.assertEqual(self.channel.sent[-1].content, "7 more people joined.")
        self.assertFalse(self.channel.sent[-1].kwargs["allowed_mentions"].users)

    async def test_a_mass_ban_posts_up_to_the_limit_and_one_line_says_how_many(self):
        await self.configure(ban=text("Banned {user}"))
        for number in range(10):
            await self.cog_.on_member_ban(self.guild, FakeMember(self.guild, f"raider{number}"))
        self.assertEqual(len(self.channel.sent), 3)
        await asyncio.wait_for(self.cog_._burst_tasks[(self.guild.id, "leaving")], 5)
        self.assertEqual(len(self.channel.sent), 4)
        self.assertEqual(self.channel.sent[-1].content, "7 more people left or were banned.")

    async def test_a_wave_of_people_leaving_counts_the_same_way(self):
        await self.configure(farewell=text("Bye {user}"), ban={"content": "", "embeds": [], "buttons": []})
        for number in range(6):
            await self.cog_.on_member_remove(FakeMember(self.guild, f"raider{number}"))
        self.assertEqual(len(self.channel.sent), 3)
        await asyncio.wait_for(self.cog_._burst_tasks[(self.guild.id, "leaving")], 5)
        self.assertEqual(self.channel.sent[-1].content, "3 more people left or were banned.")

    async def test_joins_and_leaves_have_separate_counts(self):
        await self.configure(farewell=text("Bye {user}"), ban={"content": "", "embeds": [], "buttons": []})
        for number in range(3):
            await self.join(f"newcomer{number}")
            await self.cog_.on_member_remove(FakeMember(self.guild, f"gone{number}"))
        self.assertEqual(len(self.channel.sent), 6)

    async def test_zero_welcomes_everyone(self):
        await self.configure(burst_joins=0)
        for number in range(10):
            await self.join(f"member{number}")
        self.assertEqual(len(self.channel.sent), 10)
        self.assertEqual(self.cog_._burst_tasks, {})

    async def test_a_normal_day_stays_under_it(self):
        for number in range(5):
            await self.join(f"member{number}")
            joins = self.cog_._joins[self.guild.id]
            for at in range(len(joins)):
                joins[at] -= greetings_module.BURST_SECONDS + 1  # the next one comes well over a minute later
        self.assertEqual(len(self.channel.sent), 5)

    async def test_it_ends_with_the_cog(self):
        for number in range(5):
            await self.join(f"raider{number}")
        task = self.cog_._burst_tasks[self.guild.id]
        self.cog_.cog_unload()
        await asyncio.wait({task})
        self.assertTrue(task.cancelled())


class NameTest(GreetingsCase):
    def test_a_masked_link_in_a_name_doesnt_render(self):
        member = FakeMember(self.guild, "[Verify here](https://a.test)")
        payload = render_payload(text("Hi {user}"), Scope(guild=self.guild, member=member, channel=self.channel))
        self.assertNotIn("](", payload["content"])
        self.assertIn("Verify here", payload["content"])

    async def test_the_combined_birthday_is_the_same(self):
        await self.configure(birthday_channel=self.channel.id, birthday=text("Happy birthday {user}"))
        first = FakeMember(self.guild, "[Verify here](https://a.test)")
        second = FakeMember(self.guild, "someone")
        await self.cog_._post_birthday(self.channel, self.guild, [first, second], self.cog_.cfg(self.guild.id)["birthday"])
        self.assertNotIn("](", self.channel.sent[-1].content)


class ClearingTest(GreetingsCase):
    async def test_clearing_by_command_takes_the_embed_too(self):
        await self.configure(welcome={"content": "hi", "embeds": [{"title": "Welcome"}], "buttons": []})
        context = self.context()
        await self.cog_.greetings_welcome.callback(self.cog_, context, message=None)
        self.assertEqual(context.oks, ["The welcome message is cleared."])
        self.assertEqual(self.cog_.cfg(self.guild.id)["welcome"]["embeds"], [])

    async def test_setting_text_keeps_the_embed(self):
        await self.configure(welcome={"content": "hi", "embeds": [{"title": "Welcome"}], "buttons": []})
        await self.cog_.greetings_welcome.callback(self.cog_, self.context(), message="new")
        stored = self.cog_.cfg(self.guild.id)["welcome"]
        self.assertEqual((stored["content"], len(stored["embeds"])), ("new", 1))


class BirthdayRoleTest(GreetingsCase):
    async def test_the_role_doesnt_wait_on_a_template_that_comes_out_empty(self):
        role = self.guild.role("Birthday", 2)
        await self.configure(birthday_channel=self.channel.id, birthday=text("{if({user}==nobody):x}"), birthday_role=role.id)
        await self.db.run("INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, ?, 11, 5, 1.0)", (self.guild.id, self.member.id))
        with frozen(), mock.patch.object(greetings_module, "render_message", mock.AsyncMock(return_value=text(""))):
            failed = await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.assertFalse(failed)
        self.assertEqual(self.channel.sent, [])
        self.assertIn(role, self.member.roles)

    async def test_a_birthday_role_never_comes_back_through_sticky_roles(self):
        role = self.guild.role("Birthday", 2)
        await self.configure(birthday_role=role.id)
        self.assertEqual(self.cog_.transient_roles(self.guild.id), {role.id})
        self.assertEqual(self.bot.transient_roles(self.guild.id), {role.id})

    async def test_none_set_holds_nothing(self):
        self.assertEqual(self.cog_.transient_roles(self.guild.id), set())


class ProblemsTest(GreetingsCase):
    async def problems(self):
        client = await self.client()
        return await (await client.get(self.path("/greetings/problems"))).json()

    async def test_a_gone_channel_shows_on_the_page(self):
        await self.configure(welcome_channel=900000000000000999, welcome=text("Hi"))
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))
        self.assertEqual(await self.problems(), ["The welcome message's channel is gone. Pick another one above."])

    async def test_a_refused_post_names_the_remedy_and_a_good_one_clears_it(self):
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi"))
        original = self.channel.send

        async def refuse(**kwargs):
            raise discord.Forbidden(mock.Mock(status=403, reason="no"), "no")

        self.channel.send = refuse
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))
        self.assertIn("Give it View Channel, Send Messages", (await self.problems())[0])
        self.channel.send = original
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))
        self.assertEqual(await self.problems(), [])

    async def test_a_role_above_the_bot_says_so(self):
        role = self.guild.role("Too high", 500)
        await self.configure(birthday_role=role.id)
        await self.cog_._give_birthday_role(self.guild, self.member, self.cog_.cfg(self.guild.id))
        self.assertIn("at or above the bot's highest role", (await self.problems())[0])


class CatchingUpTest(GreetingsCase):
    async def birthday(self, member, month, day):
        await self.db.run("INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, ?, ?, ?, 1.0)", (self.guild.id, member.id, month, day))

    async def test_days_the_bot_missed_are_announced_once(self):
        await self.configure(birthday_channel=self.channel.id, birthday=text("Happy birthday {user}"))
        await self.birthday(self.member, 11, 3)
        await self.birthday(self.admin, 11, 5)
        since = datetime.datetime(2026, 11, 2, 12, tzinfo=datetime.timezone.utc).timestamp()
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id), since=since)
            self.assertEqual(len(self.channel.sent), 2)
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id), since=since)
        self.assertEqual(len(self.channel.sent), 2)

    async def test_it_looks_back_a_week_at_most(self):
        await self.configure(birthday_channel=self.channel.id, birthday=text("Happy birthday"))
        await self.birthday(self.member, 10, 1)
        since = datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.timezone.utc).timestamp()
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id), since=since)
        self.assertEqual(self.channel.sent, [])

    async def test_a_late_new_years_eve_counts_for_its_own_year(self):
        await self.configure(birthday_channel=self.channel.id, birthday=text("Happy birthday"))
        await self.birthday(self.member, 12, 31)
        since = datetime.datetime(2026, 12, 30, 12, tzinfo=datetime.timezone.utc).timestamp()
        with frozen(2027, 1, 2):
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id), since=since)
        self.assertEqual(await self.db.scalar("SELECT announced_year FROM birthdays WHERE user_id = ?", (self.member.id,)), 2026)

    async def test_with_a_whole_member_list_someone_who_left_costs_no_call(self):
        await self.configure(birthday_channel=self.channel.id, birthday=text("Happy birthday"))
        await self.db.run("INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, 424242, 11, 5, 1.0)", (self.guild.id,))
        self.guild.chunked = True
        self.guild.fetch_member = mock.AsyncMock(side_effect=AssertionError("asked Discord"))
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.guild.fetch_member.assert_not_called()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays WHERE user_id = 424242"), 1)

    async def test_many_combined_birthdays_split_so_no_mention_is_cut(self):
        await self.configure(birthday_channel=self.channel.id, birthday=text("Happy birthday {mention}"), birthday_combine=True)
        people = [FakeMember(self.guild, f"person{number}") for number in range(120)]
        for person in people:
            await self.birthday(person, 11, 5)
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.assertEqual(len(self.channel.sent), 3)
        self.assertTrue(all(person.mention in " ".join(sent.content for sent in self.channel.sent) for person in people))


class CommandChecksTest(GreetingsCase):
    async def test_testgreet_counts_only_what_went_out(self):
        await self.configure(dm=text("Hello"))
        context = self.context()

        async def closed(**kwargs):
            raise discord.Forbidden(mock.Mock(status=403, reason="no"), "closed")

        context.author.send = closed
        await self.cog_.greetings_testgreet.callback(self.cog_, context)
        self.assertEqual(context.oks, ["Sent 0 test messages. Your DMs are closed, so the join DM didn't reach you."])

    async def test_a_channel_command_needs_post_there(self):
        role = self.guild.role("Manager", 4, "manage_guild", "view_channel")
        manager = FakeMember(self.guild, "manager", role)
        news = ReadOnly(self.guild, "news", manager)
        for command, field in ((self.cog_.greetings_welcome_channel, "welcome_channel"), (self.cog_.greetings_farewell_channel, "farewell_channel"), (self.cog_.greetings_ban_channel, "ban_channel"), (self.cog_.birthday_channel, "birthday_channel")):
            context = self.context(manager)
            await command.callback(self.cog_, context, news)
            self.assertEqual(context.fails, ["You can't post in that channel."], field)
            self.assertIsNone(self.cog_.cfg(self.guild.id)[field])

    async def test_the_dashboard_form_checks_it_too(self):
        self.assertEqual(self.cog_.posted_channels({"welcome_channel": 5, "farewell_channel": None, "ban_channel": 6, "birthday_channel": None}), {"welcome_channel": {5}, "farewell_channel": set(), "ban_channel": {6}, "birthday_channel": set()})

    async def test_the_test_route_needs_post_there(self):
        role = self.guild.role("Manager", 4, "manage_guild", "view_channel")
        manager = FakeMember(self.guild, "manager", role)
        news = ReadOnly(self.guild, "news", manager)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(manager.id, manager.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        response = await client.post(self.path("/greetings/test"), json={"which": "welcome", "payload": text("hi"), "channel_id": str(news.id)})
        self.assertEqual(response.status, 400)
        self.assertEqual(news.sent, [])


class StickyRolesConsumerTest(GreetingsCase):
    modules = [Greetings, Autoroles]

    async def test_autoroles_reads_the_provider(self):
        role = self.guild.role("Birthday", 2)
        await self.configure(birthday_role=role.id)
        self.assertIn(role.id, self.bot.transient_roles(self.guild.id))
