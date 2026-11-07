"""What keeps a moderation command from doing the wrong thing: who a name means, what a typed length says, and how far a mass action goes."""
from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any
from unittest import mock

import discord
from discord.ext import commands

from captainguild.core.people import OneMember, OnePerson
from captainguild.modules.moderation import ModerationError, Moderation, Someone, meant_a_channel
from captainguild.modules.tags import Tags
from tests.fakes import BotTest, FakeChannel, FakeMember, FakeMessage, _FakeResponse, new_id
from tests.gateway import FakeHTTPResponse, iso, user_json
from tests.test_moderation import ModBase, PurgeChannel
from tests.test_moderation_live import LiveBase


class TypedLengthTest(ModBase):
    UNREADABLE = ("20h45", "1hr30", "2hrs.", "1:30", "5 people complained", "3rd warning", ".5x", "0.5s being rude", "0s")

    async def assert_refused(self, command: Any, *before: Any, words: str) -> None:
        first, _, rest = words.partition(" ")
        ctx = self.context(self.admin)
        await command.callback(self.cog_, ctx, *before, first, reason=rest or None)
        self.assertEqual(len(ctx.fails), 1, words)
        self.assertIn("couldn't read that length", ctx.fails[0], words)

    async def test_a_mute_refuses_a_length_it_cannot_read(self):
        for words in self.UNREADABLE:
            await self.assert_refused(self.cog_.mute, self.member, words=words)
        self.assertEqual(await self.cases(), [])
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertNotIn(self.role_muted, self.member.roles)

    async def test_every_timed_action_refuses_it(self):
        await self.assert_refused(self.cog_.hardmute, self.member, words="20h45 being rude")
        await self.assert_refused(self.cog_.timeout, self.member, words="20h45 being rude")
        await self.assert_refused(self.cog_.tempban, self.member, None, words="20h45 being rude")
        await self.assert_refused(self.cog_.lockdown_channel, self.channel, words="20h45 raid")
        self.assertEqual(await self.cases(), [])
        self.assertEqual(self.guild.banned, [])
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    async def test_a_server_lockdown_refuses_it_before_touching_a_channel(self):
        ctx = self.context(self.admin)
        await self.cog_.lockdown_server.callback(self.cog_, ctx, "20h45", reason="raid")
        self.assertIn("couldn't read that length", ctx.fails[0])
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    async def test_lengths_it_can_read_still_work(self):
        for index, (first, rest, seconds, reason) in enumerate((("20h45m", "being rude", 74700, "being rude"), ("2", "days being rude", 172800, "being rude"), ("1.5h", None, 5400, None))):
            member = FakeMember(self.guild, f"member{index}", self.role_member)
            ctx = self.context(self.admin)
            await self.cog_.mute.callback(self.cog_, ctx, member, first, reason=rest)
            row = (await self.cases())[-1]
            self.assertEqual((row["seconds"], row["reason"]), (seconds, reason))

    async def test_a_reason_that_starts_with_a_word_stays_a_reason(self):
        ctx = self.context(self.admin)
        await self.cog_.mute.callback(self.cog_, ctx, self.member, "being", reason="rude for a while")
        row = (await self.cases())[0]
        self.assertEqual((row["seconds"], row["reason"]), (None, "being rude for a while"))

    async def test_a_tempban_day_count_is_not_mistaken_for_a_length(self):
        ctx = self.context(self.admin)
        await self.cog_.tempban.callback(self.cog_, ctx, self.member, 3, "being", reason="rude")
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.cases())[0]["reason"], "being rude")

    async def test_the_slash_box_refuses_a_length_of_nothing(self):
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.mute.callback(self.cog_, ctx, self.member, "0s", reason=None)
        self.assertTrue(ctx.fails)
        self.assertEqual(await self.cases(), [])


class BanGuardTest(ModBase):
    def rude_bulk(self, error: Exception) -> None:
        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise error
        self.guild.bulk_ban = refuse

    async def test_a_softban_refuses_someone_already_banned(self):
        outsider = Someone(new_id(), "banned")
        await self.cog_.punish(self.guild, outsider, "ban", moderator=self.admin)
        with self.assertRaises(ModerationError) as caught:
            await self.cog_.punish(self.guild, outsider, "softban", moderator=self.admin)
        self.assertIn("already banned", str(caught.exception))
        self.assertIn(outsider.id, self.guild.banned)
        self.assertEqual(len(await self.cases()), 1)

    async def test_a_softban_refuses_someone_on_a_temporary_ban(self):
        outsider = Someone(new_id(), "banned")
        await self.cog_.punish(self.guild, outsider, "tempban", moderator=self.admin, seconds=3600)
        with self.assertRaises(ModerationError):
            await self.cog_.punish(self.guild, outsider, "softban", moderator=self.admin)
        self.assertIn(outsider.id, self.guild.banned)
        self.assertIsNotNone(await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:{outsider.id}"))

    async def test_a_softban_on_someone_who_left_still_clears_them(self):
        outsider = Someone(new_id(), "left")
        await self.cog_.punish(self.guild, outsider, "softban", moderator=self.admin)
        self.assertEqual(self.guild.banned, [])
        self.assertEqual((await self.cases())[0]["action"], "softban")

    async def test_massban_turns_a_temporary_ban_permanent(self):
        outsider = Someone(new_id(), "tempbanned")
        fresh = new_id()
        await self.cog_.punish(self.guild, outsider, "tempban", moderator=self.admin, seconds=3600)
        ctx = self.context(self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, f"{outsider.id} {fresh}", reason="for good")
        self.assertIn("Banned 2 of 2", ctx.oks[0])
        self.assertIsNone(await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:{outsider.id}"))
        await self.bot.timers.run_due(time.time() + 7200)
        self.assertIn(outsider.id, self.guild.banned)
        self.assertEqual([row["action"] for row in await self.cases()], ["tempban", "ban", "ban"])

    async def test_massban_records_nothing_when_nobody_could_be_banned(self):
        target = new_id()
        self.guild.banned.append(target)
        self.rude_bulk(discord.HTTPException(_FakeResponse(400), {"code": 500000, "message": "Failed to ban users"}))
        ctx = self.context(self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, str(target), reason=None)
        self.assertIn("Banned 0 of 1", ctx.oks[0])
        self.assertEqual(await self.cases(), [])
        self.assertEqual(self.cog_._expected, {})

    async def test_massban_one_at_a_time_skips_someone_already_banned(self):
        old, new = new_id(), new_id()
        self.guild.banned.append(old)
        self.rude_bulk(discord.Forbidden(_FakeResponse(403), "needs manage server"))
        ctx = self.context(self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, f"{old} {new}", reason=None)
        self.assertIn("Banned 1 of 2", ctx.oks[0])
        self.assertEqual([row["user_id"] for row in await self.cases()], [new])

    async def test_massban_asks_discord_about_someone_missing_from_the_member_list(self):
        self.role_mod.permissions = type(self.role_mod.permissions)("ban_members")
        self.guild.members.remove(self.admin)
        self.guild.chunked = False
        self.guild.query_members = mock.AsyncMock(return_value=[self.admin])
        ctx = self.context(self.mod)
        await self.cog_.massban.callback(self.cog_, ctx, str(self.admin.id), reason="junior against senior")
        self.assertEqual(self.guild.banned, [])
        self.assertIn("at or above yours", ctx.oks[0])
        self.guild.query_members.assert_awaited_once()

    async def test_massban_bans_nobody_when_discord_does_not_answer(self):
        self.guild.chunked = False
        self.guild.query_members = mock.AsyncMock(side_effect=TimeoutError)
        ctx = self.context(self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, str(new_id()), reason=None)
        self.assertTrue(ctx.fails)
        self.assertEqual(self.guild.banned, [])

    async def test_a_finished_member_list_is_not_asked(self):
        self.guild.query_members = mock.AsyncMock()
        await self.cog_.massban.callback(self.cog_, self.context(self.admin), str(new_id()), reason=None)
        self.guild.query_members.assert_not_awaited()

    async def test_a_single_ban_asks_too(self):
        self.guild.members.remove(self.admin)
        self.guild.chunked = False
        self.guild.query_members = mock.AsyncMock(return_value=[self.admin])
        with self.assertRaises(ModerationError) as caught:
            await self.cog_.punish(self.guild, Someone(self.admin.id), "ban", moderator=self.mod)
        self.assertIn("at or above yours", str(caught.exception))
        self.assertEqual(self.guild.banned, [])

    async def test_two_moderators_banning_at_once_make_one_case(self):
        results = await asyncio.gather(
            self.cog_.punish(self.guild, self.member, "ban", moderator=self.admin, reason="first"),
            self.cog_.punish(self.guild, self.member, "ban", moderator=self.owner, reason="second"),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, ModerationError) for result in results), 1)
        self.assertEqual(len(await self.cases()), 1)

    async def test_a_second_ban_right_after_is_refused_while_they_are_still_listed(self):
        await self.cog_.punish(self.guild, self.member, "ban", moderator=self.admin)
        self.assertIn(self.member, self.guild.members)
        with self.assertRaises(ModerationError):
            await self.cog_.punish(self.guild, self.member, "ban", moderator=self.owner)
        self.assertEqual(len(await self.cases()), 1)

    async def test_a_temporary_ban_can_still_turn_permanent_right_after(self):
        await self.cog_.punish(self.guild, self.member, "tempban", moderator=self.admin, seconds=3600)
        await self.cog_.punish(self.guild, self.member, "ban", moderator=self.owner)
        self.assertIsNone(await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:{self.member.id}"))
        self.assertEqual([row["action"] for row in await self.cases()], ["tempban", "ban"])

    async def test_banning_again_after_an_unban_works(self):
        await self.cog_.punish(self.guild, self.member, "ban", moderator=self.admin)
        await self.guild.unban(self.member)
        await self.cog_.punish(self.guild, self.member, "ban", moderator=self.admin)
        self.assertIn(self.member.id, self.guild.banned)


class HardmuteRankTest(ModBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.senior = FakeMember(self.guild, "senior", self.guild.role("Senior", 7))
        await self.cog_.punish(self.guild, self.senior, "hardmute", moderator=self.owner, seconds=None)

    async def test_a_junior_can_not_act_on_someone_stripped_of_a_higher_role(self):
        self.assertLess(self.senior.top_role.position, self.mod.top_role.position)
        cases = len(await self.cases())
        for action in ("unmute", "ban", "kick", "warn"):
            with self.assertRaises(ModerationError) as caught:
                await self.cog_.punish(self.guild, self.senior, action, moderator=self.mod)
            self.assertIn("before the hardmute", str(caught.exception), action)
        self.assertEqual(len(await self.cases()), cases)
        self.assertEqual(self.senior.top_role, self.role_muted)
        self.assertEqual(self.guild.banned, [])

    async def test_massban_counts_the_role_it_took_too(self):
        self.role_mod.permissions = type(self.role_mod.permissions)("ban_members")
        ctx = self.context(self.mod)
        await self.cog_.massban.callback(self.cog_, ctx, str(self.senior.id), reason=None)
        self.assertEqual(self.guild.banned, [])
        self.assertIn("before the hardmute", ctx.oks[0])

    async def test_someone_above_that_role_can_lift_it(self):
        await self.cog_.punish(self.guild, self.senior, "unmute", moderator=self.admin)
        self.assertEqual(self.senior.top_role.name, "Senior")

    async def test_the_owner_can_lift_it(self):
        await self.cog_.punish(self.guild, self.senior, "unmute", moderator=self.owner)
        self.assertEqual(self.senior.top_role.name, "Senior")

    async def test_automod_has_no_rank_to_check(self):
        row = await self.cog_.punish(self.guild, self.senior, "warn", moderator=None, source="automod")
        self.assertEqual(row["action"], "warn")


class ServerLockdownReachTest(ModBase):
    def overwrite(self, channel: Any, target: Any, **values: Any) -> None:
        channel.overwrites[target] = discord.PermissionOverwrite(**values)

    def gated(self, name: str, *roles: Any) -> FakeChannel:
        """A channel @everyone can't see, opened to these roles, as a server with a verify gate has."""
        channel = FakeChannel(self.guild, name)
        self.overwrite(channel, self.guild.default_role, view_channel=False)
        for role in roles:
            self.overwrite(channel, role, view_channel=True)
        return channel

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.regular = self.guild.role("Regular", 3)
        for index in range(40):
            FakeMember(self.guild, f"regular{index}", self.regular)
        self.few = self.guild.role("Translators", 4)
        FakeMember(self.guild, "translator", self.few)  # about 2% of this server

    def denied(self, channel: Any) -> Any:
        return channel.overwrites_for(self.guild.default_role).send_messages

    async def lock(self) -> list[str]:
        ctx = self.context(self.admin)
        await self.cog_.lockdown_server.callback(self.cog_, ctx, None, reason="raid")
        self.assertEqual(ctx.fails, [])
        return ctx.oks

    async def test_channels_opened_to_the_member_role_are_locked(self):
        talk = self.gated("talk", self.regular)
        staff = self.gated("staff", self.role_mod)
        small = self.gated("translation", self.few)
        replies = await self.lock()
        self.assertIs(self.denied(talk), False)
        self.assertIs(self.denied(self.channel), False)
        self.assertIsNone(self.denied(staff))
        self.assertIsNone(self.denied(small))
        self.assertIn("Left alone", replies[0])
        self.assertIn(staff.mention, replies[0])
        self.assertIn(small.mention, replies[0])
        self.assertNotIn(talk.mention, replies[0])

    async def test_a_common_role_with_moderator_power_is_still_staff(self):
        helpers = self.guild.role("Helpers", 6, "manage_messages")
        for member in self.guild.members:
            member.roles.append(helpers)
        room = self.gated("helpers", helpers)
        await self.lock()
        self.assertIsNone(self.denied(room))

    async def test_a_role_only_bots_hold_is_not_the_public(self):
        bots = self.guild.role("Bots", 6)
        for index in range(30):
            FakeMember(self.guild, f"bot{index}", bots, bot=True)
        room = self.gated("bot-room", bots)
        await self.lock()
        self.assertIsNone(self.denied(room))

    async def test_a_role_that_opens_a_channel_with_a_server_permission_counts(self):
        self.guild.default_role.permissions = type(self.guild.default_role.permissions)("send_messages")
        self.regular.permissions = type(self.regular.permissions)("view_channel", "send_messages")
        plain = FakeChannel(self.guild, "plain")
        hidden = self.gated("hidden")
        await self.lock()
        self.assertIs(self.denied(plain), False)
        self.assertIsNone(self.denied(hidden))

    async def test_a_channel_closed_to_the_member_role_is_left_alone(self):
        room = FakeChannel(self.guild, "room")
        self.guild.default_role.permissions = type(self.guild.default_role.permissions)()
        self.overwrite(room, self.regular, view_channel=False)
        await self.lock()
        self.assertIsNone(self.denied(room))

    async def test_it_says_where_members_can_still_post_when_clearing_their_allow_was_refused(self):
        loud = self.gated("loud", self.regular)
        self.overwrite(loud, self.regular, view_channel=True, send_messages=True)
        quiet = self.gated("quiet", self.regular)
        real = loud.set_permissions

        async def refuse_the_role(target: Any, **kwargs: Any) -> None:
            if target is self.regular:
                raise discord.Forbidden(_FakeResponse(403), "no")
            await real(target, **kwargs)
        loud.set_permissions = refuse_the_role
        replies = await self.lock()
        self.assertIn("can still post there", replies[0])
        self.assertIn(loud.mention, replies[0])
        self.assertNotIn(quiet.mention, replies[0])

    async def test_a_member_role_allow_comes_off_for_a_server_lock(self):
        loud = self.gated("loud", self.regular)
        self.overwrite(loud, self.regular, view_channel=True, send_messages=True)
        replies = await self.lock()
        self.assertNotIn("can still post there", replies[0])
        self.assertIsNone(loud.overwrites_for(self.regular).send_messages)
        await self.cog_.unlockdown_server.callback(self.cog_, self.context(self.admin))
        self.assertIs(loud.overwrites_for(self.regular).send_messages, True)

    async def test_a_long_list_of_skipped_channels_stays_within_a_message(self):
        hidden = [self.gated(f"staff{index}", self.role_mod) for index in range(100)]
        replies = await self.lock()
        self.assertIn(hidden[0].mention, replies[0])
        self.assertIn("and 70 more", replies[0])
        self.assertLessEqual(len(replies[0]), 2000)

    async def test_nothing_skipped_means_nothing_said(self):
        replies = await self.lock()
        self.assertNotIn("Left alone", replies[0])

    async def test_unlocking_the_server_puts_them_all_back(self):
        talk = self.gated("talk", self.regular)
        await self.lock()
        await self.cog_.unlockdown_server.callback(self.cog_, self.context(self.admin))
        self.assertIsNone(self.denied(talk))
        self.assertIs(talk.overwrites_for(self.guild.default_role).view_channel, False)
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))


class LockdownSetupRankTest(ModBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.guild.default_role.permissions = type(self.guild.default_role.permissions)("view_channel", "send_messages")

    async def test_it_only_touches_roles_below_the_person_running_it(self):
        talker = self.guild.role("Talker", 2, "send_messages")
        senior = self.guild.role("Senior", 7, "send_messages")
        ctx = self.context(self.mod)
        await self.cog_.lockdown_setup.callback(self.cog_, ctx, False)
        self.assertIn("Talker", ctx.oks[0])
        self.assertNotIn("Senior", ctx.oks[0])
        await self.cog_.lockdown_setup.callback(self.cog_, self.context(self.mod), True)
        self.assertFalse(talker.permissions.send_messages)
        self.assertTrue(senior.permissions.send_messages)

    async def test_the_owner_reaches_every_role_below_the_bot(self):
        senior = self.guild.role("Senior", 7, "send_messages")
        await self.cog_.lockdown_setup.callback(self.cog_, self.context(self.owner), True)
        self.assertFalse(senior.permissions.send_messages)


class StartupMuteTest(ModBase):
    def joined(self, member: Any, *, before_now: bool) -> None:
        moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=-1 if before_now else 1)
        member.joined_at = moment

    async def test_a_mute_taken_off_by_hand_stays_off(self):
        self.joined(self.member, before_now=True)
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=3600)
        await self.member.remove_roles(self.role_muted)
        await self.cog_.reconcile(self.guild)
        self.assertNotIn(self.role_muted, self.member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertIsNone(await self.bot.timers.get_key("moderation.unmute", f"{self.guild.id}:{self.member.id}"))
        row = (await self.cases())[-1]
        self.assertEqual((row["action"], row["source"]), ("unmute", "external"))

    async def test_a_hardmute_taken_off_by_hand_gives_the_roles_back(self):
        self.joined(self.member, before_now=True)
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.admin)
        await self.member.remove_roles(self.role_muted)
        self.assertNotIn(self.role_member, self.member.roles)
        await self.cog_.reconcile(self.guild)
        self.assertIn(self.role_member, self.member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_roles_that_will_not_go_back_leave_the_mute_on_record(self):
        self.joined(self.member, before_now=True)
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.admin)
        await self.member.remove_roles(self.role_muted)
        cases = len(await self.cases())

        async def refuse(**kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.member.edit = refuse
        await self.cog_.reconcile(self.guild)
        self.assertIsNotNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertEqual(len(await self.cases()), cases)

    async def test_someone_who_left_and_came_back_is_muted_again(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod)
        await self.member.remove_roles(self.role_muted)
        self.joined(self.member, before_now=False)
        await self.cog_.reconcile(self.guild)
        self.assertIn(self.role_muted, self.member.roles)
        self.assertIsNotNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_a_member_still_wearing_the_role_keeps_the_mute(self):
        self.joined(self.member, before_now=True)
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod)
        await self.cog_.reconcile(self.guild)
        self.assertIn(self.role_muted, self.member.roles)
        self.assertIsNotNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertEqual(len(await self.cases()), 1)

    async def test_a_new_mute_role_does_not_read_as_taken_off(self):
        self.joined(self.member, before_now=True)
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod)
        new_role = self.guild.role("Silenced", 1)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": new_role.id})
        await self.cog_.reconcile(self.guild)
        self.assertIn(self.role_muted, self.member.roles)
        self.assertIsNotNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_no_join_date_counts_as_coming_back(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod)
        await self.member.remove_roles(self.role_muted)
        self.member.joined_at = None
        await self.cog_.reconcile(self.guild)
        self.assertIn(self.role_muted, self.member.roles)


class CleanupReachTest(ModBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.room = PurgeChannel(self.guild, "room")
        await self.bot.settings.save(self.guild.id, "core", {"prefixes": ["."]})

    def post(self, author: Any, content: str) -> Any:
        message = FakeMessage(self.room, author, content)
        self.room.sent.append(message)
        return message

    async def test_it_takes_commands_and_leaves_chat(self):
        commands_typed = [self.post(self.member, text) for text in (".warn someone", ".WARN someone", ". warn someone", ".coinflip")]
        mine = self.post(self.me, "a reply")
        chat = [self.post(self.member, text) for text in ("...i think it's clogged", ".3mf files open fine", ".4mm or .6mm?", "plain chat", ".")]
        await self.cog_.cleanup.callback(self.cog_, self.context(self.admin, self.room), 50)
        self.assertEqual(self.room.sent, chat)
        for message in (*commands_typed, mine):
            self.assertNotIn(message, self.room.sent)

    def test_runs_a_command(self):
        names = {"coinflip"}
        for text in (".warn x", ". warn x", ".coinflip", ".COINFLIP now"):
            self.assertTrue(self.cog_._runs_a_command(text, (".",), names), text)
        for text in ("...", ".", "", "warn x", ".nothing here", "..warn"):
            self.assertFalse(self.cog_._runs_a_command(text, (".",), names), text)
        self.assertFalse(self.cog_._runs_a_command(".warn x", (), names))


class CleanupTagTest(BotTest):
    modules = [Moderation, Tags]

    async def test_a_typed_tag_counts_as_a_command(self):
        tags = self.cog(Tags)
        moderation = self.cog(Moderation)
        await self.bot.settings.save(self.guild.id, "core", {"prefixes": ["."]})
        await tags.tag_create.callback(tags, self.context(self.admin), "support", content="Read the guide")
        names = await tags.all_names(self.guild.id)
        self.assertEqual(names, {"support"})
        self.assertTrue(moderation._runs_a_command(".Support please", (".",), names))
        self.assertFalse(moderation._runs_a_command(".supported files", (".",), names))


class MeantAChannelTest(ModBase):
    def test_it_tells_a_missed_channel_from_a_reason(self):
        for word, rest in (("gneral", "2h raid"), ("gneral", "2 hours raid"), ("<#123456789012345678>", None), ("123456789012345678", "raid")):
            self.assertTrue(meant_a_channel(word, rest), word)
        for word, rest in ((None, None), ("raid", None), ("raid", "in progress"), ("2h", "raid"), ("2", "days raid")):
            self.assertFalse(meant_a_channel(word, rest), word)


def discord_finds_no_such_member(test: Any) -> None:
    """Over the network, the library's member lookups come back empty. These are the calls it makes for a name or an ID it doesn't have cached."""
    for name in ("query_member_by_id", "query_member_named"):
        patcher = mock.patch.object(commands.MemberConverter, name, mock.AsyncMock(return_value=None))
        patcher.start()
        test.addCleanup(patcher.stop)


def member_data(user_id: int, name: str, nick: str | None, roles: list[str]) -> dict:
    return {"user": user_json(user_id, name), "roles": roles, "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": nick, "premium_since": None, "avatar": None, "communication_disabled_until": None}


class TypedNameTest(LiveBase):
    def add_member(self, name: str, nick: str | None = None) -> int:
        user_id = new_id()
        data = member_data(user_id, name, nick, [self.role_member["id"]])
        self.member_json[user_id] = data
        self.bot._connection.parse_guild_member_add({"guild_id": str(self.guild_id), **data})
        return user_id

    async def test_a_name_that_fits_two_members_is_refused(self):
        first = self.add_member("first_alex", "Alex")
        second = self.add_member("second_alex", "Alex")
        for typed in ("!ban Alex scam", "!tempban Alex 2h scam", "!softban Alex", "!kick Alex", "!mute Alex 2h", "!hardmute Alex", "!unmute Alex", "!timeout Alex 2h", "!untimeout Alex", "!warn Alex", "!setnick Alex other",
                      "!purge user Alex", "!clearwarns Alex", "!setnote Alex hello", "!clearnotes Alex"):
            await self.typed(typed)
            reply = self.replies()[-1]
            self.assertIn("More than one member goes by Alex", reply, typed)
            self.assertIn(str(first), reply, typed)
            self.assertIn(str(second), reply, typed)
            self.assertIn("mention or an ID", reply, typed)
        self.assertEqual(await self.cases(), [])
        self.assertFalse(any(c[0] in ("PUT", "PATCH") and "/bans/" in c[1] for c in self.net.calls))

    async def test_a_nickname_that_copies_a_username_is_refused_too(self):
        target = self.add_member("real_name")
        self.add_member("copycat", "real_name")
        await self.typed("!warn real_name stop")
        self.assertIn("More than one member", self.replies()[-1])
        self.assertEqual(await self.cases(), [])
        await self.typed(f"!warn <@{target}> stop")
        self.assertEqual((await self.cases())[0]["user_id"], target)

    async def test_a_long_list_of_matches_is_cut_short(self):
        for index in range(7):
            self.add_member(f"alex{index}", "Alex")
        await self.typed("!warn Alex stop")
        self.assertIn("and 2 more", self.replies()[-1])

    async def test_a_name_that_fits_one_member_works(self):
        target = self.add_member("only_alex", "Alex")
        await self.typed("!warn Alex stop")
        self.assertEqual((await self.cases())[0]["user_id"], target)

    async def test_an_id_or_a_mention_is_exact_whoever_shares_the_nickname(self):
        first = self.add_member("first_alex", "Alex")
        self.add_member("second_alex", "Alex")
        await self.typed(f"!warn <@{first}> one")
        await self.typed(f"!warn {first} two")
        self.assertEqual([row["user_id"] for row in await self.cases()], [first, first])

    async def test_a_name_only_ever_means_a_member(self):
        self.bot._connection.store_user(user_json(new_id(), "stranger"))
        discord_finds_no_such_member(self)
        await self.typed("!ban stranger scam")
        self.assertIn("I can't find a member called", self.replies()[-1])
        self.assertEqual(await self.cases(), [])

    async def test_someone_outside_the_server_can_be_banned_by_id(self):
        outsider = new_id()
        discord_finds_no_such_member(self)
        await self.typed(f"!ban {outsider} scam")
        self.assertEqual([row["user_id"] for row in await self.cases()], [outsider])
        await self.typed(f"!tempban <@{new_id()}> 2h scam")
        self.assertEqual((await self.cases())[-1]["action"], "tempban")

    async def test_slash_options_stay_user_pickers(self):
        for name, option in (("ban", "user"), ("tempban", "user"), ("softban", "user"), ("kick", "member"), ("mute", "member"), ("warn", "member"), ("clearwarnings", "member")):
            payload = next(c for c in self.bot.tree.get_commands() if c.name == name).to_dict(self.bot.tree)
            found = next(o for o in payload["options"] if o["name"] == option)
            self.assertEqual(found["type"], discord.AppCommandOptionType.user.value, name)

    async def test_the_slash_path_takes_the_picked_person(self):
        option, resolved = self.member_option()
        self.slash("warn", [option, {"name": "reason", "type": 3, "value": "from the box"}], resolved=resolved)
        await self.settle()
        self.assert_no_errors("/warn")
        row = (await self.cases())[0]
        self.assertEqual((row["user_id"], row["reason"]), (self.member_id, "from the box"))

    async def test_the_slash_path_bans_someone_outside_the_server(self):
        outsider = new_id()
        resolved = {"users": {str(outsider): user_json(outsider, "outsider")}}
        self.slash("ban", [{"name": "user", "type": 6, "value": str(outsider)}], resolved=resolved)
        await self.settle()
        self.assert_no_errors("/ban")
        self.assertEqual((await self.cases())[0]["user_id"], outsider)

    def test_both_kinds_are_transformers_and_converters(self):
        for kind in (OneMember, OnePerson):
            self.assertTrue(issubclass(kind, commands.Converter))
            self.assertEqual(kind().type, discord.AppCommandOptionType.user)


class TypedChannelTest(LiveBase):
    async def rows(self) -> list[dict]:
        return await self.db.all("SELECT channel_id, reason, expires_at FROM lockdowns WHERE guild_id = ? ORDER BY channel_id", (self.guild_id,))

    async def test_a_forum_named_by_mention_is_the_one_locked(self):
        await self.typed(f"!lockdown channel <#{self.forum['id']}> 2h raid")
        rows = await self.rows()
        self.assertEqual([row["channel_id"] for row in rows], [int(self.forum["id"])])
        self.assertEqual(rows[0]["reason"], "raid")
        self.assertIsNotNone(rows[0]["expires_at"])

    async def test_a_voice_channel_works_on_the_group_alone(self):
        await self.typed(f"!lockdown <#{self.voice['id']}> 2h raid")
        self.assertEqual([row["channel_id"] for row in await self.rows()], [int(self.voice["id"])])

    async def test_a_channel_it_cannot_find_locks_nothing(self):
        for typed in ("!lockdown gneral 2h raid", "!lockdown channel gneral 2h raid", f"!lockdown channel <#{new_id()}> raid", f"!lockdown {new_id()}"):
            await self.typed(typed)
            self.assertIn("can't find a channel", self.replies()[-1], typed)
            self.assertEqual(await self.rows(), [], typed)

    async def test_no_channel_named_still_locks_this_one(self):
        await self.typed("!lockdown 2h raid")
        rows = await self.rows()
        self.assertEqual([(row["channel_id"], row["reason"]) for row in rows], [(int(self.text["id"]), "raid")])
        self.assertIsNotNone(rows[0]["expires_at"])
        await self.typed("!unlockdown")
        await self.typed("!lockdown raid in progress")
        self.assertEqual((await self.rows())[0]["reason"], "raid in progress")

    async def test_a_channel_it_cannot_find_unlocks_nothing(self):
        await self.typed("!lockdown")
        for typed in ("!unlockdown gneral", "!unlockdown channel gneral", f"!unlockdown {new_id()}"):
            await self.typed(typed)
            self.assertIn("can't find a channel", self.replies()[-1], typed)
            self.assertEqual(len(await self.rows()), 1, typed)
        await self.typed("!unlockdown")
        self.assertEqual(await self.rows(), [])

    async def test_autopurge_remove_needs_a_real_channel_or_none_at_all(self):
        purge = next(m for m in self.bot.modules if m.key == "autopurge")
        await purge.set_channel(self.guild_id, int(self.text["id"]), 3600, "all")
        await purge.set_channel(self.guild_id, int(self.log_channel["id"]), 3600, "all")
        count = lambda: self.db.scalar("SELECT COUNT(*) FROM autopurge_channels")
        for typed in ("!autopurge remove gneral", f"!autopurge remove {new_id()}", f"!autopurge remove <#{self.forum['id']}>"):
            await self.typed(typed)
            self.assertIn("can't find a channel", self.replies()[-1], typed)
            self.assertEqual(await count(), 2, typed)
        await self.typed(f"!autopurge remove <#{self.text['id']}>")
        self.assertEqual(await count(), 1)
        await self.typed("!autopurge remove")
        self.assertEqual(await count(), 0)


class PurgePermissionTest(LiveBase):
    async def test_purge_and_cleanup_need_manage_server(self):
        for typed in ("!purge all 5", "!purge 5", f"!purge user <@{self.member_id}> 5", "!purge contains hello", "!purge reactions", "!cleanup"):
            await self.typed(typed, user_id=self.mod_id)
            self.assertIn("Manage Server", self.replies()[-1], typed)
        self.assertFalse(any(c[1] == "/channels/{channel_id}/messages" and c[0] == "GET" for c in self.net.calls))

    async def test_a_bypass_role_still_works(self):
        await self.bot.settings.save(self.guild_id, "commands", {"overwrites": {"purge": {"bypass_roles": [int(self.role_mod["id"])]}}, })
        await self.typed("!purge all 5", user_id=self.mod_id)
        self.assertTrue(any(c[1] == "/channels/{channel_id}/messages" and c[0] == "GET" for c in self.net.calls))


class ConcurrentBanLiveTest(LiveBase):
    async def test_two_moderators_ban_at_once(self):
        real = self.bot.http.request

        async def discord_remembers(route: Any, **kwargs: Any) -> Any:
            answer = await real(route, **kwargs)
            if route.method == "PUT" and route.path == "/guilds/{guild_id}/bans/{user_id}":
                self.net.bans[self.member_id] = self.member_json[self.member_id]["user"]
            return answer
        self.bot.http.request = discord_remembers
        self.dispatch_message(user_id=self.owner_id, content=f"!ban <@{self.member_id}> first")
        self.dispatch_message(user_id=self.admin_id, content=f"!ban <@{self.member_id}> second")
        await self.settle()
        self.assert_no_errors("two bans")
        self.assertEqual(len(await self.cases()), 1)
        self.assertTrue(any("already banned" in reply for reply in self.replies()))

    async def test_softban_on_someone_banned_makes_no_unban_call(self):
        outsider = new_id()
        self.net.bans[outsider] = user_json(outsider, "banned")
        discord_finds_no_such_member(self)
        await self.typed(f"!softban {outsider} cleanup")
        self.assertIn("already banned", self.replies()[-1])
        self.assertFalse(any(c[0] == "DELETE" and c[1] == "/guilds/{guild_id}/bans/{user_id}" for c in self.net.calls))


class MassbanRouteLiveTest(LiveBase):
    async def test_the_bulk_route_gets_only_people_not_on_a_timer(self):
        timed, fresh = new_id(), new_id()
        self.net.bans[timed] = user_json(timed, "tempbanned")
        await self.bot.timers.schedule("moderation.unban", time.time() + 3600, {"guild_id": self.guild_id, "user_id": timed, "user_name": "tempbanned", "moderator_id": None, "moderator_name": None, "started_at": time.time()}, guild_id=self.guild_id, key=f"{self.guild_id}:{timed}")
        await self.typed(f"!massban {timed} {fresh} for good")
        bulk = next(c for c in self.net.calls if c[1] == "/guilds/{guild_id}/bulk-ban")
        self.assertEqual([int(user) for user in bulk[2]["user_ids"]], [fresh])
        self.assertTrue(any(c[0] == "PUT" and c[1] == "/guilds/{guild_id}/bans/{user_id}" for c in self.net.calls))
        self.assertIsNone(await self.bot.timers.get_key("moderation.unban", f"{self.guild_id}:{timed}"))
        self.assertEqual(sorted(row["user_id"] for row in await self.cases()), sorted([timed, fresh]))

    async def test_nobody_could_be_banned(self):
        target = new_id()
        real = self.bot.http.request

        async def refuse(route: Any, **kwargs: Any) -> Any:
            if route.path == "/guilds/{guild_id}/bulk-ban":
                raise discord.HTTPException(FakeHTTPResponse(400), {"code": 500000, "message": "Failed to ban users"})
            return await real(route, **kwargs)
        self.bot.http.request = refuse
        await self.typed(f"!massban {target} raid")
        self.assertIn("Banned 0 of 1", self.replies()[-1])
        self.assertEqual(await self.cases(), [])
        self.assertFalse(any(c[0] == "PUT" and c[1] == "/guilds/{guild_id}/bans/{user_id}" for c in self.net.calls))
