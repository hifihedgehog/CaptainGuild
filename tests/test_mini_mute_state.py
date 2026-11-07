"""What a mini mute leaves behind: nothing on a member's record, the role it put on and no other, and nothing that outlasts it."""
from __future__ import annotations

import copy
import time
from unittest import mock

import discord

from captainguild.modules.autoroles import Autoroles
from captainguild.modules.moderation import Moderation
from captainguild.modules.voice import VOTE_FOR_ID, Voice
from captainguild.modules.voicelink import VoiceLink
from tests.fakes import BotTest, FakeChannel, FakeLogSink, _FakeResponse
from tests.test_voice import VoiceTest


class RecordBase(VoiceTest):
    modules = [Voice, Moderation]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.modlog = self.add_channel("modlog")
        await self.bot.settings.save(self.guild_id, "moderation", {"modlog_channel": int(self.modlog["id"])})

    async def case_actions(self) -> list[str]:
        return [row["action"] for row in await self.db.all("SELECT action FROM cases WHERE guild_id = ? ORDER BY number", (self.guild_id,))]

    async def run_to_the_end(self) -> None:
        await self.age_everything()
        await self.bot.timers.run_due()
        await self.settle()


class NoMarkTest(RecordBase):
    async def test_a_mini_mute_makes_no_case_from_start_to_finish(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id), reason="spam")
        self.assertIsNotNone(await self.db.one("SELECT * FROM mini_mutes WHERE user_id = ?", (self.member_id,)))
        await self.run_to_the_end()
        self.assertEqual(await self.case_actions(), [])
        self.assertEqual(self.posts_in(self.modlog["id"]), [])

    async def test_a_lift_makes_no_case_and_still_logs_to_its_own_channel(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id), reason="spam")
        self.member_json[self.member_id]["roles"] = [self.mute_role["id"]]  # the faked network sends no member update
        await self.slash("mini_mute_lift", self.staff_id, user=self.member(self.member_id))
        self.assertEqual(await self.case_actions(), [])
        self.assertEqual([embed["title"] for embed in self.logged(self.mini_log)], ["Mini Mute Applied", "Mini Mute Lifted"])

    async def test_a_server_that_wants_cases_turns_them_on(self):
        await self.configure(mini_mute_cases=True)
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id), reason="spam")
        await self.run_to_the_end()
        self.assertEqual(await self.case_actions(), ["minimute", "unminimute"])


class TransientTest(BotTest):
    modules = [Autoroles, Voice, VoiceLink, Moderation]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.role = self.guild.role("MiniMuted", 1)
        self.linked = self.guild.role("InVoice", 1)
        self.lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": self.role.id})
        await self.bot.settings.save(self.guild.id, "voicelink", {"links": [{"channel_id": self.lounge.id, "roles": [self.linked.id]}]})
        await self.cog(VoiceLink).settle()

    async def asyncTearDown(self) -> None:
        await self.cog(VoiceLink).cog_unload()
        await super().asyncTearDown()

    async def test_the_mini_mute_role_and_linked_roles_are_passing_roles(self):
        self.assertLessEqual({self.role.id, self.linked.id}, self.bot.transient_roles(self.guild.id))

    async def test_no_setting_means_no_role(self):
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": None})
        self.assertEqual(self.cog(Voice).transient_roles(self.guild.id), set())

    async def test_role_timers_are_named(self):
        self.assertLessEqual({"mini_mute", "voice_action"}, set(Voice.role_timers))


class StickyTest(BotTest):
    modules = [Autoroles, Voice, Moderation]

    async def test_a_mute_that_ended_while_they_were_gone_doesnt_come_back(self):
        self.bot.logs = FakeLogSink()
        voice, autoroles = self.cog(Voice), self.cog(Autoroles)
        role = self.guild.role("MiniMuted", 1)
        kept = self.role_member
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": role.id})
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True})
        await voice.apply_mini_mute(self.member, 10, "r", "a", self.admin)
        await autoroles.on_member_remove(self.member)
        self.guild.members.remove(self.member)
        await self.db.run("UPDATE mini_mutes SET expires_at = ?", (time.time() - 1,))
        await voice._mini_mute_due({"guild_id": self.guild.id, "user_id": self.member.id}, {})
        self.guild.members.append(self.member)
        self.member.roles = [self.guild.default_role]
        await autoroles.on_member_join(self.member)
        await voice.on_member_join(self.member)
        self.assertNotIn(role, self.member.roles)
        self.assertIn(kept, self.member.roles)


class OutOfReachTest(BotTest):
    modules = [Voice]

    async def test_a_role_moved_above_the_bot_tells_staff_once(self):
        self.bot.logs = FakeLogSink()
        logs = mock.Mock()
        real = self.bot.module
        self.bot.module = lambda key: logs if key == "logging" else real(key)
        voice = self.cog(Voice)
        role = self.guild.role("Voice Access", 1)
        await self.bot.settings.save(self.guild.id, "voice", {"access_role": role.id})
        role.position = 50
        for _ in range(2):
            _, refusal = voice._access_role(self.guild)
            self.assertIn("below mine", refusal)
        self.assertEqual(logs.staff_notice.call_count, 1)


class ChangedRoleTest(BotTest):
    modules = [Voice, Moderation]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.logs = FakeLogSink()
        self.voice = self.cog(Voice)
        self.role = self.guild.role("MiniMuted", 1)
        self.other = self.guild.role("Other", 1)
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": self.role.id})
        ok, why, _ = await self.voice.apply_mini_mute(self.member, 10, "r", "a", self.admin)
        self.assertTrue(ok, why)
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": self.other.id})

    async def test_the_role_it_put_on_comes_off_when_it_ends(self):
        await self.db.run("UPDATE mini_mutes SET expires_at = ?", (time.time() - 1,))
        await self.voice._mini_mute_due({"guild_id": self.guild.id, "user_id": self.member.id}, {})
        self.assertNotIn(self.role, self.member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM mini_mutes"), 0)

    async def test_a_lift_takes_off_the_role_it_put_on(self):
        ctx = self.context(author=self.admin)
        await self.voice.mini_mute_lift.callback(self.voice, ctx, self.member)
        self.assertNotIn(self.role, self.member.roles)

    async def test_a_rejoin_puts_back_the_role_it_put_on(self):
        self.member.roles.remove(self.role)
        await self.voice._restore_mini_mute(self.member)
        self.assertIn(self.role, self.member.roles)
        self.assertNotIn(self.other, self.member.roles)

    async def test_it_holds_the_role_it_put_on(self):
        self.assertTrue(await self.voice.mini_mute_holds(self.guild.id, self.member.id, self.role.id))
        self.assertFalse(await self.voice.mini_mute_holds(self.guild.id, self.member.id, self.other.id))

    async def test_a_row_from_before_this_was_stored_reads_the_setting(self):
        await self.db.run("UPDATE mini_mutes SET role_id = NULL")
        self.assertTrue(await self.voice.mini_mute_holds(self.guild.id, self.member.id, self.other.id))

    async def test_taking_the_old_role_off_by_hand_ends_it(self):
        before = copy.copy(self.member)
        before.roles = list(self.member.roles)
        self.member.roles.remove(self.role)
        await self.voice.on_member_update(before, self.member)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM mini_mutes"), 0)


class CacheGapTest(BotTest):
    modules = [Voice]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.logs = FakeLogSink()
        self.voice = self.cog(Voice)
        self.role = self.guild.role("MiniMuted", 1)
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": self.role.id})
        await self.voice.apply_mini_mute(self.member, 10, "r", "a", self.admin)
        await self.db.run("UPDATE mini_mutes SET expires_at = ?", (time.time() - 1,))
        self.guild.members.remove(self.member)  # only off the list, unless a test says they left

    async def test_a_member_missing_from_the_cache_still_loses_the_role(self):
        async def found(user_id):
            return self.member
        self.guild.fetch_member = found
        await self.voice._mini_mute_due({"guild_id": self.guild.id, "user_id": self.member.id}, {})
        self.assertNotIn(self.role, self.member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM mini_mutes"), 0)

    async def test_discord_failing_to_answer_keeps_the_row_and_tries_again(self):
        async def down(user_id):
            raise discord.HTTPException(_FakeResponse(503), "down")
        self.guild.fetch_member = down
        await self.voice._mini_mute_due({"guild_id": self.guild.id, "user_id": self.member.id}, {})
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM mini_mutes"), 1)
        timer = await self.bot.timers.get_key("mini_mute", f"{self.guild.id}:{self.member.id}")
        self.assertAlmostEqual(timer["due_at"], time.time() + 60, delta=5)

    async def test_someone_who_truly_left_ends_the_row(self):
        await self.voice._mini_mute_due({"guild_id": self.guild.id, "user_id": self.member.id}, {})
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM mini_mutes"), 0)


class VoteCacheGapTest(VoiceTest):
    async def test_a_gap_in_the_cache_doesnt_close_a_vote(self):
        await self.configure(mini_mute_votes_required=3)
        await self.slash("mini_mute", self.server_vet_id, user=self.member(self.member_id))
        post = self.posts_in(self.mini_log["id"])[-1]
        self.guild._members.pop(self.member_id)
        await self.press(VOTE_FOR_ID, post, self.server_vet_two)
        self.assertEqual(await self.db.scalar("SELECT status FROM mini_mute_votes"), "open")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM mini_mute_ballots"), 2)
        self.assert_no_errors()

    async def test_a_ballot_from_another_server_isnt_counted(self):
        await self.configure(mini_mute_votes_required=3)
        await self.slash("mini_mute", self.server_vet_id, user=self.member(self.member_id))
        vote_id = await self.db.scalar("SELECT vote_message_id FROM mini_mute_votes")
        await self.db.run("INSERT INTO mini_mute_ballots (vote_message_id, guild_id, voter_id, approve) VALUES (?, 5, 77, 1)", (vote_id,))
        self.assertEqual(await self.voice_module._ballots(vote_id), (1, 0))


class LiftWordingTest(BotTest):
    modules = [Voice, Moderation]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.logs = FakeLogSink()
        self.voice = self.cog(Voice)
        self.role = self.guild.role("MiniMuted", 1)
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": self.role.id})
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": self.role.id})
        self.member.roles.append(self.role)
        await self.db.run("INSERT INTO active_mutes (guild_id, user_id, kind, role_id, removed_roles, started_at, expires_at) VALUES (?, ?, 'mute', ?, '[]', ?, NULL)", (self.guild.id, self.member.id, self.role.id, time.time()))

    async def test_a_regular_mute_isnt_lifted_as_a_mini_mute(self):
        ctx = self.context(author=self.admin)
        await self.voice.mini_mute_lift.callback(self.voice, ctx, self.member)
        self.assertEqual(ctx.oks, [])
        self.assertIn("regular mute", ctx.fails[-1])
        self.assertEqual(self.bot.logs.posted, [])

    async def test_the_page_hears_whether_the_role_came_off(self):
        await self.db.run("INSERT INTO mini_mutes (guild_id, user_id, expires_at) VALUES (?, ?, ?)", (self.guild.id, self.member.id, time.time() + 60))
        client = await self.client()
        reply = await client.post(self.path(f"/voice/mini-mutes/{self.member.id}/lift"))
        self.assertEqual(reply.status, 200)
        self.assertIs((await reply.json())["released"], False)
        self.assertIn(self.role, self.member.roles)


class WithoutCasesTest(BotTest):
    modules = [Voice]

    async def test_cases_turned_on_with_no_moderation_feature_still_mutes(self):
        self.bot.logs = FakeLogSink()
        voice = self.cog(Voice)
        role = self.guild.role("MiniMuted", 1)
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": role.id, "mini_mute_cases": True})
        ok, why, _ = await voice.apply_mini_mute(self.member, 10, None, "a", self.admin)
        self.assertTrue(ok, why)
