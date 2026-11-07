"""Restoring a backup against what's running now: a past-due mute stays out, and a running one isn't stranded."""
from __future__ import annotations

import time

from captainguild.modules.autoroles import Autoroles
from captainguild.modules.backup import Backup
from captainguild.modules.moderation import Moderation
from captainguild.modules.voice import MUTE, Voice
from tests.fakes import FakeLogSink
from tests.test_backup import BackupCase, Sample


class RestoreBase(BackupCase):
    modules = [Autoroles, Voice, Moderation, Sample, Backup]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.logs = FakeLogSink()
        self.voice = self.cog(Voice)
        self.role = self.guild.role("MiniMuted", 1)
        await self.bot.settings.save(self.guild.id, "voice", {"mini_mute_role": self.role.id, "mini_mute_cases": True})

    async def restore_file(self, body: bytes) -> None:
        client = await self.client()
        job = await self.preview_of(client, body)
        await self.restore(client, job["result"])

    async def finish_due_timers(self) -> None:
        await self.db.run("UPDATE timers SET due_at = ? WHERE kind IN ('mini_mute', 'voice_action')", (time.time() - 1,))
        await self.bot.timers.run_due(time.time())


class PastDueTest(RestoreBase):
    async def test_a_finished_mini_mute_in_a_backup_doesnt_run_again(self):
        ended = time.time() + 0.1
        await self.db.run("INSERT INTO mini_mutes (guild_id, user_id, user_name, expires_at) VALUES (?, ?, 'member', ?)", (self.guild.id, self.member.id, ended))
        await self.voice._schedule_mini_mute(self.guild.id, self.member.id, ended)
        body = await self.export_bytes()
        await self.db.run("DELETE FROM mini_mutes")
        await self.db.run("DELETE FROM timers WHERE kind = 'mini_mute'")
        self.member.roles.append(self.role)  # staff put the same role on by hand since
        await self.wait_past(ended)
        await self.restore_file(body)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM mini_mutes"), 0)
        self.assertIsNone(await self.bot.timers.get_key("mini_mute", f"{self.guild.id}:{self.member.id}"))
        await self.voice._rearm_timers()
        await self.finish_due_timers()
        self.assertIn(self.role, self.member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM cases"), 0)

    async def test_a_finished_voice_action_in_a_backup_stays_out(self):
        ended = time.time() + 0.1
        await self.db.run("INSERT INTO voice_actions (guild_id, user_id, action, expires_at) VALUES (?, ?, ?, ?)", (self.guild.id, self.member.id, MUTE, ended))
        body = await self.export_bytes()
        await self.db.run("DELETE FROM voice_actions")
        await self.wait_past(ended)
        await self.restore_file(body)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voice_actions"), 0)
        self.assertEqual(self.voice._active, set())

    async def wait_past(self, moment: float) -> None:
        while time.time() <= moment:
            await self.db.scalar("SELECT 1")


class RunningTest(RestoreBase):
    async def test_a_mini_mute_made_after_the_backup_keeps_its_row_and_comes_off(self):
        body = await self.export_bytes()
        ok, why, _ = await self.voice.apply_mini_mute(self.member, 10, "r", "a", self.admin)
        self.assertTrue(ok, why)
        await self.restore_file(body)
        row = await self.db.one("SELECT * FROM mini_mutes WHERE user_id = ?", (self.member.id,))
        self.assertIsNotNone(row)
        self.assertEqual(row["role_id"], self.role.id)
        await self.db.run("UPDATE mini_mutes SET expires_at = ?", (time.time() - 1,))
        await self.finish_due_timers()
        self.assertNotIn(self.role, self.member.roles)

    async def test_a_voice_action_made_after_the_backup_stays(self):
        body = await self.export_bytes()
        await self.voice._record_action(self.guild.id, self.member.id, MUTE, 10, self.admin.id, "loud")
        await self.restore_file(body)
        self.assertEqual(await self.db.scalar("SELECT reason FROM voice_actions WHERE user_id = ?", (self.member.id,)), "loud")
        self.assertIn((self.guild.id, self.member.id), self.voice._active)
        self.assertIsNotNone(await self.bot.timers.get_key("voice_action", f"{self.guild.id}:{self.member.id}:mute"))

    async def test_someone_waiting_to_be_unmuted_at_their_next_connect_stays_waiting(self):
        body = await self.export_bytes()
        await self.db.run("INSERT INTO voice_actions (guild_id, user_id, action, expires_at) VALUES (?, ?, ?, ?)", (self.guild.id, self.member.id, MUTE, time.time() - 500))
        await self.restore_file(body)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voice_actions WHERE user_id = ?", (self.member.id,)), 1)

    async def test_a_row_in_the_backup_with_a_later_end_keeps_a_timer(self):
        await self.db.run("INSERT INTO mini_mutes (guild_id, user_id, user_name, expires_at) VALUES (?, ?, 'member', ?)", (self.guild.id, self.member.id, time.time() + 3000))
        body = await self.export_bytes()
        await self.db.run("DELETE FROM mini_mutes")
        await self.restore_file(body)
        self.assertIsNotNone(await self.bot.timers.get_key("mini_mute", f"{self.guild.id}:{self.member.id}"))

    async def test_a_hook_call_with_no_restore_behind_it_changes_nothing(self):
        # My data calls this to refresh what memory holds, and a person's deletion must not end anyone's mute.
        await self.db.run("INSERT INTO voice_actions (guild_id, user_id, action, expires_at) VALUES (?, ?, ?, ?)", (self.guild.id, self.member.id, MUTE, time.time() - 500))
        await self.voice.on_guild_restored(self.guild.id)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voice_actions"), 1)
