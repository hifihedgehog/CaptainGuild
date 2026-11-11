import datetime
import time
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.modules import greetings as greetings_module
from captainguild.modules.greetings import Greetings, dates_for, next_run, parse_birthday
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, _FakeResponse


def text(content):
    return {"content": content, "embeds": [], "buttons": []}


def frozen(year=2026, month=11, day=5, hour=12):
    moment = datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc)
    return mock.patch.object(greetings_module.datetime, "datetime", wraps=datetime.datetime, **{"now.return_value": moment})


class GreetingsCase(BotTest):
    modules = [Greetings]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.cog_ = self.cog(Greetings)
        self.cog_.BAN_WAIT = 0
        self.cog_.invites.settle = 0

    async def configure(self, **form):
        await self.bot.settings.save(self.guild.id, "greetings", form)


class WelcomeTest(GreetingsCase):
    async def test_a_join_posts_the_welcome_with_variables_and_only_pings_the_person(self):
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi {mention}, welcome to {server}!"))
        newcomer = FakeMember(self.guild, "newcomer")
        await self.cog_.on_member_join(newcomer)
        sent = self.channel.sent[-1]
        self.assertEqual(sent.content, f"Hi {newcomer.mention}, welcome to {self.guild.name}!")
        mentions = sent.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertTrue(mentions.users)

    async def test_embeds_and_buttons_are_filled_in_too(self):
        await self.configure(welcome_channel=self.channel.id, welcome={"content": "", "embeds": [{"title": "Hi {user}", "color": 5}], "buttons": [{"label": "Rules", "url": "https://example.com"}]})
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))
        sent = self.channel.sent[-1]
        self.assertEqual(sent.embeds[0].title, "Hi newcomer")
        self.assertIn("view", sent.kwargs)

    async def test_nothing_posts_without_a_channel_or_a_message(self):
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))
        await self.configure(welcome_channel=self.channel.id)
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))
        await self.configure(welcome_channel=None, welcome=text("Hi"))
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))
        self.assertEqual(self.channel.sent, [])

    async def test_a_deleted_channel_is_no_crash_and_another_servers_channel_gets_nothing(self):
        other = FakeGuild("Elsewhere")
        self.bot.fake_guilds.append(other)
        elsewhere = FakeChannel(other, "elsewhere")
        await self.configure(welcome_channel=elsewhere.id, welcome=text("Hi"))
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))
        self.assertEqual(elsewhere.sent, [])

    async def test_discord_refusing_the_post_doesnt_raise(self):
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi"))

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Missing Access")

        self.channel.send = refuse
        await self.cog_.on_member_join(FakeMember(self.guild, "newcomer"))

    async def test_the_join_dm_goes_to_people_only(self):
        await self.configure(dm=text("Hi {user}"))
        person = FakeMember(self.guild, "person")
        robot = FakeMember(self.guild, "robot", bot=True)
        await self.cog_.on_member_join(person)
        await self.cog_.on_member_join(robot)
        self.assertEqual(person.dms[0]["content"], "Hi person")
        self.assertEqual(robot.dms, [])

    async def test_closed_dms_dont_raise(self):
        await self.configure(dm=text("Hi"))
        person = FakeMember(self.guild, "person")

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Cannot send messages to this user")

        person.send = refuse
        await self.cog_.on_member_join(person)

    async def test_waiting_for_the_rules_screen(self):
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi"), dm=text("Hello"), wait_for_rules=True)
        person = FakeMember(self.guild, "person", pending=True)
        await self.cog_.on_member_join(person)
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(person.dms, [])
        before = SimpleNamespace(pending=True)
        person.pending = False
        await self.cog_.on_member_update(before, person)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(len(person.dms), 1)
        await self.cog_.on_member_update(SimpleNamespace(pending=False), person)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_without_waiting_the_rules_screen_changes_nothing(self):
        await self.configure(welcome_channel=self.channel.id, welcome=text("Hi"))
        person = FakeMember(self.guild, "person", pending=True)
        await self.cog_.on_member_join(person)
        self.assertEqual(len(self.channel.sent), 1)
        person.pending = False
        await self.cog_.on_member_update(SimpleNamespace(pending=True), person)
        self.assertEqual(len(self.channel.sent), 1)


class LeavingTest(GreetingsCase):
    async def test_a_leave_falls_back_to_the_welcome_channel(self):
        other = FakeChannel(self.guild, "goodbyes")
        await self.configure(welcome_channel=self.channel.id, farewell=text("Bye {user}"))
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual(self.channel.sent[-1].content, "Bye leaver")
        await self.configure(farewell_channel=other.id)
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual(other.sent[-1].content, "Bye leaver")

    async def test_someone_never_welcomed_gets_no_goodbye(self):
        await self.configure(welcome_channel=self.channel.id, farewell=text("Bye"), wait_for_rules=True)
        await self.cog_.on_member_remove(FakeMember(self.guild, "leaver", pending=True))
        self.assertEqual(self.channel.sent, [])

    async def test_a_ban_posts_to_its_channel_with_fallbacks(self):
        bans = FakeChannel(self.guild, "bans")
        banned = FakeMember(self.guild, "banned")
        await self.configure(welcome_channel=self.channel.id, ban=text("{user} is out"))
        await self.cog_.on_member_ban(self.guild, banned)
        self.assertEqual(self.channel.sent[-1].content, "banned is out")
        await self.configure(ban_channel=bans.id)
        await self.cog_.on_member_ban(self.guild, banned)
        self.assertEqual(bans.sent[-1].content, "banned is out")

    async def test_a_banned_person_gets_one_post_whichever_event_comes_first(self):
        await self.configure(welcome_channel=self.channel.id, farewell=text("Bye"), ban=text("Banned"))
        first = FakeMember(self.guild, "first")
        await self.cog_.on_member_ban(self.guild, first)
        await self.cog_.on_member_remove(first)
        self.assertEqual([m.content for m in self.channel.sent], ["Banned"])

    async def test_a_leave_posts_normally_when_no_ban_message_is_set(self):
        await self.configure(welcome_channel=self.channel.id, farewell=text("Bye"))
        leaver = FakeMember(self.guild, "leaver")
        await self.cog_.on_member_ban(self.guild, leaver)
        await self.cog_.on_member_remove(leaver)
        self.assertEqual([m.content for m in self.channel.sent], ["Bye"])


class BirthdayDateTest(BotTest):
    async def test_parse_reads_day_first_and_refuses_dates_that_dont_exist(self):
        for written, wanted in {"25/12": (12, 25), "5-11": (11, 5), "05.11": (11, 5), "25 December": (12, 25), "Dec 25": (12, 25), "december 25th": (12, 25), "29/2": (2, 29)}.items():
            self.assertEqual(parse_birthday(written), wanted, written)
        for written in ("31/2", "32/1", "1/13", "0/5", "soon", "", "25", "Smarch 3"):
            self.assertIsNone(parse_birthday(written), written)

    async def test_a_leap_day_birthday_falls_on_the_28th_in_other_years(self):
        self.assertEqual(dates_for(datetime.datetime(2026, 2, 28)), [(2, 28), (2, 29)])
        self.assertEqual(dates_for(datetime.datetime(2028, 2, 28)), [(2, 28)])
        self.assertEqual(dates_for(datetime.datetime(2028, 2, 29)), [(2, 29)])

    async def test_next_run_is_the_next_time_that_hour_comes_round(self):
        noon = datetime.datetime(2026, 11, 5, 12, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(next_run(13, noon), noon + 3600)
        self.assertEqual(next_run(12, noon), noon + 86400)
        self.assertEqual(next_run(11, noon), noon + 86400 - 3600)


class BirthdayTest(GreetingsCase):
    async def set_birthday(self, member, month=11, day=5):
        await self.cog_._set_birthday(self.guild.id, member.id, month, day)

    async def test_saving_a_channel_schedules_the_daily_run_and_clearing_it_cancels(self):
        await self.configure(birthday_channel=self.channel.id, birthday_hour=9)
        row = await self.bot.timers.get_key(greetings_module.TICK, str(self.guild.id))
        self.assertEqual(datetime.datetime.fromtimestamp(row["due_at"], datetime.timezone.utc).hour, 9)
        await self.configure(birthday_channel=None)
        self.assertIsNone(await self.bot.timers.get_key(greetings_module.TICK, str(self.guild.id)))

    async def test_a_birthday_is_announced_once_with_the_role_on_and_later_off(self):
        role = self.guild.role("Birthday", 3)
        await self.configure(birthday_channel=self.channel.id, birthday_role=role.id)
        await self.set_birthday(self.member)
        with frozen():
            await self.bot.timers.run_due(time.time() + 2 * 86400)
            self.assertEqual(self.channel.sent[-1].content, f"\N{PARTY POPPER} Happy Birthday {self.member.mention}!")
            self.assertTrue(self.channel.sent[-1].kwargs["allowed_mentions"].users)
            self.assertIn(role, self.member.roles)
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.assertEqual(len(self.channel.sent), 1)
        await self.bot.timers.run_due(time.time() + 3 * 86400)
        self.assertNotIn(role, self.member.roles)

    async def test_a_refused_role_removal_stops_there_and_a_passing_failure_retries(self):
        role = self.guild.role("Birthday", 3)
        payload = {"guild_id": self.guild.id, "user_id": self.member.id, "role_id": role.id}
        self.member.roles.append(role)

        async def refused(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Missing Permissions")

        self.member.remove_roles = refused
        await self.cog_._role_ends_due(payload, {})

        async def passing(*args, **kwargs):
            raise discord.HTTPException(_FakeResponse(500), "broken")

        self.member.remove_roles = passing
        with self.assertRaises(discord.HTTPException):
            await self.cog_._role_ends_due(payload, {})

    async def test_the_daily_run_schedules_the_next_one(self):
        await self.configure(birthday_channel=self.channel.id)
        first = await self.bot.timers.get_key(greetings_module.TICK, str(self.guild.id))
        await self.bot.timers.run_due(first["due_at"] + 1)
        second = await self.bot.timers.get_key(greetings_module.TICK, str(self.guild.id))
        self.assertIsNotNone(second)
        self.assertGreater(second["due_at"], time.time())

    async def test_other_days_and_people_who_left_are_left_out(self):
        await self.configure(birthday_channel=self.channel.id)
        await self.set_birthday(self.member, 1, 1)
        gone = FakeMember(self.guild, "gone")
        await self.set_birthday(gone)
        self.guild.members.remove(gone)
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.assertEqual(self.channel.sent, [])

    async def test_combining_names_everyone_in_one_post(self):
        other = FakeMember(self.guild, "{server} other")
        await self.configure(birthday_channel=self.channel.id, birthday_combine=True, birthday=text("Happy Birthday {mention}! ({user})"))
        await self.set_birthday(self.member)
        await self.set_birthday(other)
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.assertEqual(len(self.channel.sent), 1)
        content = self.channel.sent[0].content
        self.assertIn(self.member.mention, content)
        self.assertIn(other.mention, content)
        self.assertIn("(server) other", content)  # a member's name can't run as part of the template

    async def test_without_combining_each_person_gets_a_post(self):
        other = FakeMember(self.guild, "other")
        await self.configure(birthday_channel=self.channel.id)
        await self.set_birthday(self.member)
        await self.set_birthday(other)
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.assertEqual(len(self.channel.sent), 2)

    async def test_a_failed_post_tries_again_the_same_day(self):
        await self.configure(birthday_channel=self.channel.id)
        await self.set_birthday(self.member)
        real = self.channel.send

        async def refuse(*args, **kwargs):
            raise discord.HTTPException(_FakeResponse(500), "broken")

        self.channel.send = refuse
        with frozen():
            await self.cog_._tick_due({"guild_id": self.guild.id}, {})
        row = await self.bot.timers.get_key(greetings_module.TICK, str(self.guild.id))
        self.assertLess(row["due_at"], time.time() + 3700)
        self.channel.send = real
        with frozen():
            await self.cog_._tick_due({"guild_id": self.guild.id}, {})
        self.assertEqual(len(self.channel.sent), 1)

    async def test_changing_a_birthday_cant_earn_a_second_announcement(self):
        await self.configure(birthday_channel=self.channel.id)
        await self.set_birthday(self.member)
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
            await self.cog_._set_birthday(self.guild.id, self.member.id, 11, 5)
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_role_above_the_bot_or_with_power_isnt_handed_out(self):
        high = self.guild.role("High", 20)
        powerful = self.guild.role("Powerful", 3, "ban_members")
        for role in (high, powerful):
            await self.configure(birthday_role=role.id)
            await self.cog_._give_birthday_role(self.guild, self.member, self.cog_.cfg(self.guild.id))
            self.assertNotIn(role, self.member.roles)

    async def test_the_role_works_without_a_channel(self):
        role = self.guild.role("Birthday", 3)
        await self.configure(birthday_role=role.id)
        await self.set_birthday(self.member)
        with frozen():
            await self.cog_._announce(self.guild, self.cog_.cfg(self.guild.id))
        self.assertIn(role, self.member.roles)

    async def test_commands_set_view_and_remove_your_own(self):
        ctx = self.context(author=self.member)
        await self.cog_.birthday_set.callback(self.cog_, ctx, "25/12")
        self.assertIn("25 December", ctx.oks[-1])
        await self.cog_.birthday.callback(self.cog_, ctx)
        self.assertIn("25 December", ctx.sent[-1]["content"])
        await self.cog_.birthday_remove.callback(self.cog_, ctx)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays"), 0)
        await self.cog_.birthday.callback(self.cog_, ctx)
        self.assertIn("no birthday", ctx.sent[-1]["content"])

    async def test_someone_elses_birthday_takes_manage_server(self):
        ctx = self.context(author=self.member)
        await self.cog_.birthday_set.callback(self.cog_, ctx, "25/12", self.mod)
        await self.cog_.birthday_remove.callback(self.cog_, ctx, self.mod)
        self.assertEqual(len(ctx.fails), 2)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays"), 0)
        admin = self.context()
        await self.cog_.birthday_set.callback(self.cog_, admin, "25/12", self.mod)
        self.assertEqual(await self.db.scalar("SELECT user_id FROM birthdays"), self.mod.id)

    async def test_a_bad_date_is_refused(self):
        ctx = self.context(author=self.member)
        await self.cog_.birthday_set.callback(self.cog_, ctx, "31/02")
        self.assertTrue(ctx.fails)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays"), 0)

    async def test_list_shows_soonest_first_without_pinging_and_uses_a_file_when_long(self):
        ctx = self.context()
        await self.cog_.birthday_list.callback(self.cog_, ctx)
        self.assertIn("No birthdays", ctx.sent[-1]["content"])
        await self.set_birthday(self.member, 1, 1)
        await self.set_birthday(self.mod, 12, 31)
        with frozen(month=6, day=1):
            await self.cog_.birthday_list.callback(self.cog_, ctx)
        lines = ctx.sent[-1]["content"].split("\n")
        self.assertIn("31 December", lines[0])
        self.assertIn("1 January", lines[1])
        self.assertFalse(ctx.sent[-1]["allowed_mentions"].users)
        for number in range(120):
            await self.cog_._set_birthday(self.guild.id, 100000000000000000 + number, 3, 3)
        await self.cog_.birthday_list.callback(self.cog_, ctx)
        self.assertEqual(ctx.sent[-1]["file"].filename, "birthdays.txt")

    async def test_channel_command_and_config(self):
        ctx = self.context()
        await self.cog_.birthday_channel.callback(self.cog_, ctx, self.channel)
        self.assertEqual(self.settings("greetings")["birthday_channel"], self.channel.id)
        await self.cog_.birthday_config.callback(self.cog_, ctx)
        self.assertIn(self.channel.mention, ctx.sent[-1]["content"])
        await self.cog_.birthday_channel.callback(self.cog_, ctx)
        self.assertIsNone(self.settings("greetings")["birthday_channel"])
        self.assertEqual(required_permissions(self.cog_.birthday_channel), ("manage_guild",))
        self.assertEqual(required_permissions(self.cog_.birthday_set), ())


class GreetingsCommandTest(GreetingsCase):
    async def test_setting_text_keeps_the_embed_and_leaving_it_out_clears_it(self):
        ctx = self.context()
        await self.configure(welcome={"content": "old", "embeds": [{"title": "Kept"}], "buttons": []})
        await self.cog_.greetings_welcome.callback(self.cog_, ctx, message="new")
        stored = self.settings("greetings")["welcome"]
        self.assertEqual((stored["content"], stored["embeds"][0]["title"]), ("new", "Kept"))
        await self.cog_.greetings_welcome.callback(self.cog_, ctx)
        self.assertEqual(self.settings("greetings")["welcome"]["content"], "")
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["user_name"], "admin")

    async def test_every_message_and_channel_command_saves(self):
        ctx = self.context()
        for command, field in ((self.cog_.greetings_joindm, "dm"), (self.cog_.greetings_farewell, "farewell"), (self.cog_.greetings_banmessage, "ban")):
            await command.callback(self.cog_, ctx, message="hello")
            self.assertEqual(self.settings("greetings")[field]["content"], "hello")
        for command, field in ((self.cog_.greetings_welcome_channel, "welcome_channel"), (self.cog_.greetings_farewell_channel, "farewell_channel"), (self.cog_.greetings_ban_channel, "ban_channel")):
            await command.callback(self.cog_, ctx, self.channel)
            self.assertEqual(self.settings("greetings")[field], self.channel.id)

    async def test_too_long_is_refused_with_a_reason(self):
        ctx = self.context()
        await self.cog_.greetings_welcome.callback(self.cog_, ctx, message="x" * 2001)
        self.assertIn("2000", ctx.fails[0])

    async def test_show_and_test_commands(self):
        ctx = self.context()
        await self.cog_.greetings.callback(self.cog_, ctx)
        self.assertIn("no channel", ctx.sent[-1]["content"])
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertIn("No greeting", ctx.fails[0])
        await self.configure(welcome=text("Welcome {user}"), farewell=text("Bye {user}"), ban=text("Banned {user}"), dm=text("DM {user}"))
        ctx = self.context()
        await self.cog_.greetings_testgreet.callback(self.cog_, ctx)
        self.assertEqual([m["content"] for m in ctx.sent], ["Welcome admin", "Bye admin", "Banned admin"])
        self.assertFalse(ctx.sent[0]["allowed_mentions"].users)
        self.assertEqual(self.admin.dms[-1]["content"], "DM admin")
        self.assertIn("4", ctx.oks[0])

    async def test_every_settings_command_needs_manage_server(self):
        for command in (self.cog_.greetings, self.cog_.greetings_welcome, self.cog_.greetings_welcome_channel, self.cog_.greetings_joindm, self.cog_.greetings_farewell,
                        self.cog_.greetings_farewell_channel, self.cog_.greetings_banmessage, self.cog_.greetings_ban_channel, self.cog_.greetings_testgreet):
            self.assertEqual(required_permissions(command), ("manage_guild",), command.name)


class GreetingsEndpointTest(GreetingsCase):
    async def test_settings_save_through_the_generic_endpoint_with_field_errors(self):
        client = await self.client()
        response = await client.put(self.path("/settings/greetings"), json={"welcome_channel": str(self.channel.id), "welcome": text("Hi")})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["welcome_channel"], str(self.channel.id))
        response = await client.put(self.path("/settings/greetings"), json={"welcome": {"content": "x" * 2001}})
        self.assertEqual(response.status, 400)
        self.assertIn("welcome", (await response.json())["fields"])
        response = await client.put(self.path("/settings/greetings"), json={"birthday_hour": 24})
        self.assertEqual(response.status, 400)

    async def test_the_test_button_posts_with_variables_and_pings_nobody(self):
        client = await self.client()
        template = "Hi {mention} {user} {user(proper)} in {server}, {server(members)} here, #{channel}"
        response = await client.post(self.path("/greetings/test"), json={"which": "welcome", "payload": text(template), "channel_id": str(self.channel.id)})
        self.assertEqual(response.status, 200)
        sent = self.channel.sent[-1]
        self.assertEqual(sent.content, f"Hi {self.admin.mention} admin admin in {self.guild.name}, {self.guild.member_count} here, #general")
        self.assertFalse(sent.kwargs["allowed_mentions"].users)
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "greetings.test")

    async def test_the_dm_test_goes_to_whoever_pressed_it(self):
        client = await self.client()
        response = await client.post(self.path("/greetings/test"), json={"which": "dm", "payload": text("Hi {user}")})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.admin.dms[-1]["content"], "Hi admin")

    async def test_the_test_button_checks_what_it_gets(self):
        other = FakeGuild("Elsewhere")
        self.bot.fake_guilds.append(other)
        elsewhere = FakeChannel(other, "elsewhere")
        client = await self.client()
        self.assertEqual((await client.post(self.path("/greetings/test"), json={"which": "nope", "payload": text("Hi"), "channel_id": str(self.channel.id)})).status, 400)
        self.assertEqual((await client.post(self.path("/greetings/test"), json={"which": "ban", "payload": text(""), "channel_id": str(self.channel.id)})).status, 400)
        self.assertEqual((await client.post(self.path("/greetings/test"), json={"which": "ban", "payload": text("Hi"), "channel_id": str(elsewhere.id)})).status, 400)
        self.assertEqual(elsewhere.sent, [])

    async def test_the_birthday_list_and_changes(self):
        client = await self.client()
        response = await client.put(self.path(f"/greetings/birthdays/{self.member.id}"), json={"birthday": "25/12"})
        self.assertEqual(response.status, 200)
        rows = await (await client.get(self.path("/greetings/birthdays"))).json()
        self.assertEqual([(r["user_id"], r["name"], r["date"]) for r in rows], [(str(self.member.id), "member", "25 December")])
        self.assertEqual((await client.put(self.path(f"/greetings/birthdays/{self.member.id}"), json={"birthday": "31/2"})).status, 400)
        self.assertEqual((await client.put(self.path("/greetings/birthdays/123456789012345678"), json={"birthday": "1/1"})).status, 400)
        self.assertEqual((await client.delete(self.path(f"/greetings/birthdays/{self.member.id}"))).status, 200)
        self.assertEqual((await client.delete(self.path(f"/greetings/birthdays/{self.member.id}"))).status, 404)

    async def test_birthdays_stay_in_their_server(self):
        await self.cog_._set_birthday(999, self.member.id, 1, 1)
        client = await self.client()
        self.assertEqual(await (await client.get(self.path("/greetings/birthdays"))).json(), [])
        self.assertEqual((await client.delete(self.path(f"/greetings/birthdays/{self.member.id}"))).status, 404)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays"), 1)

    async def test_staff_can_read_settings_but_not_change_anything(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/settings/greetings"))).status, 200)
        self.assertEqual((await client.put(self.path("/settings/greetings"), json={"wait_for_rules": True})).status, 403)
        self.assertEqual((await client.post(self.path("/greetings/test"), json={})).status, 403)
        self.assertEqual((await client.get(self.path("/greetings/birthdays"))).status, 403)
