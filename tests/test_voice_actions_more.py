"""Voice actions at the edges: long-lived rows, rejoining the server, restarts, who counts as staff, and report noise."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core.perms import MODULE as COMMANDS_MODULE
from captainguild.modules.voice import BLOCK, DEAFEN, MUTE, REPORT_DISMISS_ID, REPORT_MUTE_ID, Voice
from tests.fakes import BotTest, FakeLogSink
from tests.test_voice import VoiceTest


class OldRowsTest(VoiceTest):
    async def test_a_mute_older_than_the_prune_stays_until_their_next_connect(self):
        self.watch_member_edits()
        old = time.time() - 400 * 86400
        await self.db.many("INSERT INTO voice_actions (guild_id, user_id, action, expires_at) VALUES (?, ?, ?, ?)", [(self.guild_id, self.member_id, MUTE, old), (self.guild_id, self.vet_id, BLOCK, old)])
        await self.voice_module._load_active()
        self.voice_module._started = False
        await self.voice_module.on_ready()
        self.assertEqual(await self.actions(self.member_id), {MUTE})
        self.assertEqual(await self.actions(self.vet_id), set())
        await self.join(self.member_id, mute=True)
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], False)


class RejoinTest(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.watch_member_edits()
        await self.voice_module._record_action(self.guild_id, self.member_id, MUTE, 10, self.staff_id, "loud")

    async def rejoin(self) -> None:
        await self.voice_module.on_member_join(self.member(self.member_id))

    async def test_a_running_mute_goes_back_on_at_the_first_connect_after_rejoining(self):
        await self.rejoin()
        await self.join(self.member_id)
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], True)
        self.assertEqual(await self.actions(self.member_id), {MUTE})

    async def test_a_mute_discord_kept_needs_nothing(self):
        await self.rejoin()
        await self.join(self.member_id, mute=True)
        self.assertEqual(self.edits_of(self.member_id), [])

    async def test_a_moderators_own_unmute_of_someone_who_never_left_stands(self):
        await self.join(self.member_id)
        self.assertEqual(self.edits_of(self.member_id), [])

    async def test_it_goes_back_on_once(self):
        await self.rejoin()
        await self.join(self.member_id)
        count = len(self.edits_of(self.member_id))
        await self.leave(self.member_id)
        await self.join(self.member_id)
        self.assertEqual(len(self.edits_of(self.member_id)), count)

    async def test_a_deafen_too(self):
        await self.voice_module._record_action(self.guild_id, self.member_id, DEAFEN, 10, self.staff_id, "loud")
        await self.rejoin()
        await self.join(self.member_id, mute=True)
        self.assertEqual(self.edits_of(self.member_id)[-1]["deaf"], True)


class RestartBlockTest(VoiceTest):
    async def test_a_block_is_enforced_on_someone_already_in_voice_at_startup(self):
        self.watch_member_edits()
        await self.join(self.member_id)
        await self.db.run("INSERT INTO voice_actions (guild_id, user_id, action, expires_at) VALUES (?, ?, ?, ?)", (self.guild_id, self.member_id, BLOCK, time.time() + 600))
        self.voice_module._started = False
        await self.voice_module.on_ready()
        self.assertIsNone(self.edits_of(self.member_id)[-1]["channel_id"])

    async def test_someone_out_of_voice_is_left_alone(self):
        self.watch_member_edits()
        await self.db.run("INSERT INTO voice_actions (guild_id, user_id, action, expires_at) VALUES (?, ?, ?, ?)", (self.guild_id, self.member_id, BLOCK, time.time() + 600))
        self.voice_module._started = False
        await self.voice_module.on_ready()
        self.assertEqual(self.edits_of(self.member_id), [])


class EveryoneRoleTest(VoiceTest):
    async def test_everyone_stored_as_a_veteran_role_grants_nothing(self):
        await self.configure(veteran_role=self.guild_id, server_veteran_role=self.guild_id, moderator_role=self.guild_id)
        member = self.member(self.member_id)
        self.assertFalse(self.voice_module.is_vc_veteran(member))
        self.assertFalse(self.voice_module.is_server_veteran(member))
        self.assertFalse(self.voice_module.is_moderator(member))

    async def test_everyone_stored_as_the_bot_manager_role_grants_nothing(self):
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"mod_role": self.guild_id})
        self.assertFalse(self.voice_module.is_moderator(self.member(self.member_id)))


class StaffTargetTest(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.ban_role = self.add_role("Ban Helper", 1, discord.Permissions(ban_members=True).value)
        self.helper_id = self.add_member("helper", self.ban_role["id"])

    async def test_a_vc_veteran_cant_act_on_a_role_with_moderation_permissions(self):
        await self.join(self.helper_id)
        await self.slash("vc_mute", self.vet_id, user=self.member(self.helper_id))
        self.assertIn("can't act on moderators", self.last_answer())
        self.assertEqual(await self.actions(self.helper_id), set())

    async def test_a_server_veteran_cant_mini_mute_one(self):
        await self.slash("mini_mute", self.server_vet_id, user=self.member(self.helper_id))
        self.assertIn("can't mini mute moderators", self.last_answer())
        self.assertIsNone(await self.db.one("SELECT * FROM mini_mutes"))

    async def test_that_permission_doesnt_make_them_a_moderator(self):
        self.assertFalse(self.voice_module.is_moderator(self.member(self.helper_id)))


class VeteranLogTest(VoiceTest):
    async def test_a_veteran_acts_with_no_log_set_and_nothing_is_posted(self):
        await self.configure(report_channel=None)
        await self.join(self.member_id)
        await self.slash("vc_mute", self.vet_id, user=self.member(self.member_id))
        self.assertEqual(await self.actions(self.member_id), {MUTE})
        self.assertEqual(self.bot.logs.posted, [])
        self.assertIn("Server-muted", self.last_answer())

    async def test_a_veteran_action_goes_to_the_log_when_one_is_set(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.vet_id, user=self.member(self.member_id))
        logged = self.logged(self.reports_channel)
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["title"], "VC Veteran Action")
        self.assertIn(str(self.vet_id), logged[0]["fields"][0]["value"])

    async def test_a_moderator_doesnt_need_it(self):
        await self.configure(report_channel=None)
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        self.assertEqual(await self.actions(self.member_id), {MUTE})


class LegacyReportTest(VoiceTest):
    """Posts from before 1.2.3, whose own buttons still answer."""

    async def test_an_action_marks_the_report_handled_and_keeps_the_buttons(self):
        await self.join(self.member_id)
        post = await self.legacy_report_post()
        await self.press(REPORT_MUTE_ID, post, self.staff_id)
        row = await self.db.one("SELECT status, handled_by_name FROM voice_reports")
        self.assertEqual(row["status"], "acted")
        self.assertEqual(row["handled_by_name"], "staffer")
        await self.press(REPORT_DISMISS_ID, post, self.staff_id)
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_reports"), "handled")
        self.assert_no_errors()

    async def test_a_gap_in_the_cache_doesnt_read_as_gone(self):
        post = await self.legacy_report_post()
        cached = self.guild._members.pop(self.member_id)
        await self.press(REPORT_MUTE_ID, post, self.staff_id)
        self.guild._members[self.member_id] = cached
        self.assertNotIn("no longer in the server", self.last_answer())
        self.assertIn("isn't connected", self.last_answer())


class ReasonColumnTest(VoiceTest):
    async def test_the_table_shows_the_reason_given(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id), reason="shouting")
        self.assertEqual(await self.db.scalar("SELECT reason FROM voice_actions"), "shouting")

    async def test_and_no_prefix_when_none_was_given(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        self.assertIsNone(await self.db.scalar("SELECT reason FROM voice_actions"))

    async def test_a_report_action_carries_the_reports_reason(self):
        await self.join(self.member_id)
        post = await self.legacy_report_post(reason="shouting")
        await self.press(REPORT_MUTE_ID, post, self.staff_id)
        self.assertEqual(await self.db.scalar("SELECT reason FROM voice_actions"), "shouting")


class PostingTest(BotTest):
    modules = [Voice]

    async def test_the_button_command_refuses_a_channel_the_person_cant_post_in(self):
        voice = self.cog(Voice)
        ctx = self.context(author=self.mod)
        self.mod.roles.append(self.guild.role("Manager", 4, "manage_guild"))
        closed = self.channel
        with mock.patch.object(closed, "permissions_for", lambda member: discord.Permissions(view_channel=True)):
            await voice.vc_button.callback(voice, ctx, closed)
        self.assertEqual(ctx.fails, ["You can't post in that channel."])
        self.assertEqual(closed.sent, [])

    async def test_the_settings_form_names_the_channels_people_write_into(self):
        voice = self.cog(Voice)
        named = voice.posted_channels({"report_channel": 1, "approval_channel": 2, "mini_mute_log_channel": None})
        self.assertEqual(named, {"report_channel": {1}, "approval_channel": {2}, "mini_mute_log_channel": set()})


class ShutdownTest(BotTest):
    modules = [Voice]

    async def test_a_batch_being_written_at_shutdown_isnt_lost(self):
        self.bot.logs = FakeLogSink()
        voice = self.cog(Voice)
        started = asyncio.Event()
        real = voice.db.many
        calls = []

        async def many(sql, rows):
            calls.append(rows)
            if len(calls) == 1:
                started.set()
                await asyncio.sleep(3600)
            return await real(sql, rows)
        voice._pending_counts[(self.guild.id, self.member.id)] = 3
        with mock.patch.object(voice.db, "many", many):
            voice._flush_loop.start()
            await started.wait()
            voice._pending_counts[(self.guild.id, self.member.id)] = 2
            await voice.cog_unload()
        self.assertEqual(await self.db.scalar("SELECT count FROM voice_message_counts WHERE user_id = ?", (self.member.id,)), 5)
        self.assertEqual(voice._pending_counts, {})


class SimpleOnReady(SimpleNamespace):
    pass
