"""What a punishment leaves behind: the length that stands, the case that says so, and what staff hear when something couldn't finish."""
from __future__ import annotations

import datetime
import json
import time
from typing import Any
from unittest import mock

import aiohttp
import discord

from captainguild.core.validate import DELETE, TEMPMUTE
from captainguild.modules.moderation import Moderation, ModerationError, Someone
from tests.fakes import FakeChannel, FakeMember, _FakeResponse, new_id
from tests.test_automod import AutomodBase
from tests.test_moderation import ModBase


class AutomaticMuteLengthTest(AutomodBase):
    async def mute_for(self, seconds: int) -> None:
        await self.rule("words", entries=["boat"], punishment={"actions": DELETE | TEMPMUTE, "seconds": seconds, "purge_days": 0})
        await self.say("boat")

    async def test_a_rule_cant_cut_a_permanent_mute_down(self):
        await self.mods.mute.callback(self.mods, self.context(self.admin), self.member, None, reason="for good")
        await self.mute_for(300)
        row = await self.db.one("SELECT expires_at, moderator_id FROM active_mutes")
        self.assertIsNone(row["expires_at"])
        self.assertEqual(row["moderator_id"], self.admin.id)
        self.assertIsNone(await self.bot.timers.get_key("moderation.unmute", f"{self.guild.id}:{self.member.id}"))
        last = (await self.cases())[-1]
        self.assertEqual(last["action"], "mute")
        self.assertIsNone(last["seconds"])
        self.assertIn("longer mute", last["reason"])
        self.assertIn("longer mute stands", self.logged()[-1]["description"])

    async def test_a_rule_cant_cut_a_longer_mute_down_and_sends_no_false_length(self):
        await self.mods.mute.callback(self.mods, self.context(self.admin), self.member, "7d", reason="a week")
        self.member.dms.clear()
        await self.mute_for(300)
        row = await self.db.one("SELECT expires_at FROM active_mutes")
        self.assertGreater(row["expires_at"], time.time() + 6 * 86400)
        self.assertEqual(self.member.dms, [])

    async def test_a_rule_can_lengthen_a_shorter_mute(self):
        await self.mods.mute.callback(self.mods, self.context(self.admin), self.member, "1m", reason="short")
        await self.mute_for(3600)
        row = await self.db.one("SELECT expires_at FROM active_mutes")
        self.assertGreater(row["expires_at"], time.time() + 3000)

    async def test_the_warn_threshold_cant_end_a_hardmute_early(self):
        await self.mods.hardmute.callback(self.mods, self.context(self.admin), self.member, None, reason="for good")
        await self.bot.settings.save(self.guild.id, "automod", {"warns": {"enabled": True, "threshold": 1, "punishment": {"actions": TEMPMUTE, "seconds": 60, "purge_days": 0}}})
        await self.mods.record_case(self.guild, "warn", self.member, self.admin)
        await self.mods.record_case(self.guild, "warn", self.member, self.admin)
        row = await self.db.one("SELECT kind, expires_at FROM active_mutes")
        self.assertEqual((row["kind"], row["expires_at"]), ("hardmute", None))

    async def test_a_moderator_can_still_shorten_one(self):
        await self.mods.mute.callback(self.mods, self.context(self.admin), self.member, None, reason="for good")
        await self.mods.mute.callback(self.mods, self.context(self.admin), self.member, "1h", reason="less")
        row = await self.db.one("SELECT expires_at FROM active_mutes")
        self.assertIsNotNone(row["expires_at"])


class MuteRoleRankTest(ModBase):
    async def test_a_moderator_cant_make_a_role_above_their_own_the_mute_role(self):
        vip = self.guild.role("VIP", 7)
        ctx = self.context(self.mod)
        await self.cog_.muterole_set.callback(self.cog_, ctx, role=vip)
        self.assertIn("at or above your highest role", ctx.fails[0])
        self.assertEqual(self.bot.settings.get(self.guild.id, "moderation")["mute_role"], self.role_muted.id)

    async def test_the_group_form_refuses_too(self):
        vip = self.guild.role("VIP", 5)
        ctx = self.context(self.mod)
        await self.cog_.muterole.callback(self.cog_, ctx, role=vip)
        self.assertTrue(ctx.fails)

    async def test_a_role_below_them_is_fine(self):
        low = self.guild.role("Quiet", 3)
        ctx = self.context(self.mod)
        await self.cog_.muterole_set.callback(self.cog_, ctx, role=low)
        self.assertEqual(ctx.fails, [])
        self.assertEqual(self.bot.settings.get(self.guild.id, "moderation")["mute_role"], low.id)

    async def test_an_administrator_passes_as_on_the_dashboard(self):
        high = self.guild.role("High", 8)
        ctx = self.context(self.owner)
        await self.cog_.muterole_set.callback(self.cog_, ctx, role=high)
        self.assertEqual(ctx.fails, [])

    async def test_the_dashboard_counts_the_mute_role_as_handed_out(self):
        self.assertEqual(self.cog_.roles_given({"mute_role": 5}), {"mute_role": {5}})
        self.assertEqual(self.cog_.roles_given({"mute_role": None}), {"mute_role": set()})


class SoftbanHalfDoneTest(ModBase):
    def refuse_unban(self, *errors: Exception) -> None:
        self.real_unban = self.guild.unban
        left = list(errors)

        async def unban(user: Any, *, reason: str | None = None) -> None:
            if left:
                raise left.pop(0)
            await self.real_unban(user, reason=reason)
        self.guild.unban = unban

    async def test_a_failed_unban_is_recorded_and_retried(self):
        self.refuse_unban(discord.HTTPException(_FakeResponse(500), "boom"))
        ctx = self.context(self.admin)
        await self.cog_.softban.callback(self.cog_, ctx, self.member, None, reason="spam")
        self.assertIn(self.member.id, self.guild.banned)
        row = (await self.cases())[-1]
        self.assertEqual(row["action"], "ban")
        self.assertTrue(json.loads(row["extra"])["softban_unban_failed"])
        self.assertEqual(ctx.fails, [])
        self.assertIn("banned for now", ctx.oks[0])
        timer = await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:{self.member.id}")
        self.assertIsNotNone(timer)
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(self.guild.banned, [])

    async def test_a_network_error_on_the_unban_is_the_same(self):
        self.refuse_unban(aiohttp.ClientOSError())
        ctx = self.context(self.admin)
        await self.cog_.softban.callback(self.cog_, ctx, self.member, None, reason="spam")
        self.assertIn("banned for now", ctx.oks[0])
        self.assertEqual((await self.cases())[-1]["action"], "ban")

    async def test_the_timer_unban_it_retries_is_not_blocked_by_its_own_case(self):
        self.refuse_unban(discord.HTTPException(_FakeResponse(500), "boom"))
        await self.cog_.punish(self.guild, self.member, "softban", moderator=self.admin, reason="spam")
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(self.guild.banned, [])


class TempbanTimerTest(ModBase):
    async def test_a_restored_timer_doesnt_undo_a_permanent_ban(self):
        person = Someone(new_id(), "person")
        await self.cog_.punish(self.guild, person, "tempban", moderator=self.admin, seconds=3600)
        timer = await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:{person.id}")
        await self.cog_.punish(self.guild, person, "ban", moderator=self.admin)
        await self.bot.timers.schedule("moderation.unban", time.time() + 60, timer["payload"], guild_id=self.guild.id, key=f"{self.guild.id}:{person.id}")
        await self.bot.timers.run_due(time.time() + 120)
        self.assertIn(person.id, self.guild.banned)
        self.assertEqual([row["action"] for row in await self.cases()], ["tempban", "ban"])

    async def test_a_voided_ban_doesnt_count(self):
        person = Someone(new_id(), "person")
        await self.cog_.punish(self.guild, person, "tempban", moderator=self.admin, seconds=3600)
        timer = await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:{person.id}")
        banned = await self.cog_.record_case(self.guild, "ban", person, self.admin)
        await self.cog_.void_case(self.guild.id, banned["number"], self.admin)
        await self.bot.timers.schedule("moderation.unban", time.time() + 60, timer["payload"], guild_id=self.guild.id, key=f"{self.guild.id}:{person.id}")
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(self.guild.banned, [])

    async def test_a_tempban_still_ends(self):
        person = Someone(new_id(), "person")
        await self.cog_.punish(self.guild, person, "tempban", moderator=self.admin, seconds=60)
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(self.guild.banned, [])

    async def test_past_due_timers_stay_out_of_a_restore(self):
        self.assertIn("moderation.unmute", Moderation.role_timers)
        self.assertIn("moderation.unban", Moderation.role_timers)


class ReconcileEveryReadyTest(ModBase):
    async def row_for(self, member: Any, *, started: float) -> None:
        await self.db.run(
            "INSERT INTO active_mutes (guild_id, user_id, kind, role_id, removed_roles, started_at, expires_at) VALUES (?, ?, 'mute', ?, '[]', ?, NULL)",
            (self.guild.id, member.id, self.role_muted.id, started),
        )

    async def test_a_second_ready_checks_again(self):
        await self.cog_.on_ready()
        rejoined = FakeMember(self.guild, "back", self.role_member, joined=datetime.datetime.now(datetime.timezone.utc))
        await self.row_for(rejoined, started=time.time() - 3600)
        await self.cog_.on_ready()
        self.assertIn(self.role_muted, rejoined.roles)

    async def test_two_at_once_run_one_check(self):
        calls: list[int] = []
        real = self.cog_.reconcile

        async def counted(guild: Any) -> None:
            calls.append(guild.id)
            await self.cog_.on_ready()  # a second READY landing while this one runs
            await real(guild)
        self.cog_.reconcile = counted
        await self.cog_.on_ready()
        self.assertEqual(len(calls), 1)


class MuteTimerMemberListTest(ModBase):
    async def test_a_half_filled_list_asks_discord(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin, seconds=60)
        member = self.member
        self.guild.members.remove(member)
        self.guild.chunked = False
        self.guild.query_members = mock.AsyncMock(return_value=[member])
        await self.bot.timers.run_due(time.time() + 120)
        self.assertNotIn(self.role_muted, member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_no_answer_leaves_the_timer_to_retry(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin, seconds=60)
        member = self.member
        self.guild.members.remove(member)
        self.guild.chunked = False
        self.guild.query_members = mock.AsyncMock(side_effect=TimeoutError())
        await self.bot.timers.run_due(time.time() + 120)
        self.assertIsNotNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertEqual([row["action"] for row in await self.cases()], ["mute"])
        self.assertEqual((await self.db.one("SELECT tries FROM timers"))["tries"], 1)


class DmStepTest(ModBase):
    async def test_a_network_error_in_the_dm_leaves_the_case(self):
        async def broken(*args: Any, **kwargs: Any) -> None:
            raise aiohttp.ClientOSError()
        self.member.send = broken
        row = await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin, seconds=60)
        self.assertIs(row["dm"], False)
        self.assertEqual((await self.cases())[0]["action"], "mute")

    async def test_a_timeout_in_the_dm_counts_too(self):
        async def broken(*args: Any, **kwargs: Any) -> None:
            raise TimeoutError()
        self.member.send = broken
        row = await self.cog_.punish(self.guild, self.member, "warn", moderator=self.admin)
        self.assertIs(row["dm"], False)

    async def refused_after_dm(self, action: str, **kwargs: Any) -> list[dict]:
        await self.bot.settings.save(self.guild.id, "moderation", {action: {"mode": 2}})
        deleted: list[int] = []

        class Sent:
            async def delete(self) -> None:
                deleted.append(1)

        async def send(*args: Any, **more: Any) -> Any:
            return Sent()
        self.member.send = send
        with self.assertRaises(ModerationError):
            await self.cog_.punish(self.guild, self.member, action, moderator=self.admin, **kwargs)
        return deleted

    async def test_a_refused_ban_takes_its_dm_back(self):
        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.guild.ban = refuse
        self.assertEqual(await self.refused_after_dm("ban"), [1])

    async def test_a_refused_kick_takes_its_dm_back(self):
        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.member.kick = refuse
        self.assertEqual(await self.refused_after_dm("kick"), [1])


class FailedTimersTellStaffTest(ModBase):
    async def stuck_mute(self) -> None:
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin, seconds=60)
        self.modlog.sent.clear()

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.member.remove_roles = refuse

    def told(self) -> list[str]:
        return [message.embeds[0].description for message in self.modlog.sent if message.embeds and "stuck" in (message.embeds[0].title or "").lower()]

    async def test_a_stuck_unmute_says_so_once(self):
        await self.stuck_mute()
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(len(self.told()), 1)
        self.assertIn(str(self.member.id), self.told()[0])
        self.assertIn("Manage Roles", self.told()[0])
        await self.bot.timers.run_due(time.time() + 100000)
        self.assertEqual(len(self.told()), 1)

    async def test_a_stuck_unban_says_so(self):
        person = Someone(new_id(), "person")
        await self.cog_.punish(self.guild, person, "tempban", moderator=self.admin, seconds=60)
        self.modlog.sent.clear()

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.guild.unban = refuse
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(len(self.told()), 1)
        self.assertIn("Ban Members", self.told()[0])

    async def test_a_stuck_unlock_says_so(self):
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin), self.channel, "1m", reason=None)

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.set_permissions = refuse
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(len(self.told()), 1)
        self.assertIn(self.channel.mention, self.told()[0])

    async def test_it_still_fails_so_the_timer_retries(self):
        await self.stuck_mute()
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual((await self.db.one("SELECT tries FROM timers"))["tries"], 1)


class OwnRecordTest(ModBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.boss = FakeMember(self.guild, "boss", self.role_admin)
        self.staffer = FakeMember(self.guild, "staffer", self.guild.role("Staff", 6, "manage_roles", "manage_guild"))

    async def test_nobody_removes_their_own_warning(self):
        row = await self.cog_.record_case(self.guild, "warn", self.staffer, self.boss, reason="x")
        ctx = self.context(self.staffer)
        await self.cog_.removewarning.callback(self.cog_, ctx, row["number"])
        self.assertIn("record about yourself", ctx.fails[0])
        self.assertEqual(await self.cog_.warning_count(self.guild.id, self.staffer.id), 1)

    async def test_nobody_clears_their_own_warnings(self):
        await self.cog_.record_case(self.guild, "warn", self.staffer, self.boss, reason="x")
        ctx = self.context(self.staffer)
        await self.cog_.clearwarns.callback(self.cog_, ctx, member=self.staffer)
        self.assertTrue(ctx.fails)
        self.assertEqual(await self.cog_.warning_count(self.guild.id, self.staffer.id), 1)

    async def test_nobody_rewrites_the_reason_on_their_own_case(self):
        row = await self.cog_.record_case(self.guild, "warn", self.staffer, self.boss, reason="x")
        ctx = self.context(self.staffer)
        await self.cog_.reason_text.callback(self.cog_, ctx, row["number"], reason="nothing")
        self.assertTrue(ctx.fails)
        self.assertEqual((await self.cog_.get_case(self.guild.id, row["number"]))["reason"], "x")

    async def test_nobody_clears_or_removes_their_own_notes(self):
        note = await self.cog_.add_note(self.guild.id, self.staffer.id, self.boss, "watch this one")
        ctx = self.context(self.staffer)
        await self.cog_.clearnotes_text.callback(self.cog_, ctx, member=self.staffer)
        self.assertTrue(ctx.fails)
        ctx = self.context(self.staffer)
        await self.cog_.removenote_text.callback(self.cog_, ctx, note["number"])
        self.assertTrue(ctx.fails)
        self.assertEqual(len(await self.cog_._notes_of(self.guild.id, self.staffer.id)), 1)

    async def test_someone_elses_record_is_fine(self):
        row = await self.cog_.record_case(self.guild, "warn", self.member, self.boss, reason="x")
        ctx = self.context(self.staffer)
        await self.cog_.removewarning.callback(self.cog_, ctx, row["number"])
        self.assertEqual(ctx.fails, [])

    async def test_the_owner_and_administrators_can(self):
        row = await self.cog_.record_case(self.guild, "warn", self.boss, self.owner, reason="x")
        ctx = self.context(self.boss)
        await self.cog_.removewarning.callback(self.cog_, ctx, row["number"])
        self.assertEqual(ctx.fails, [])


class AfterTheTimerTest(ModBase):
    async def test_someone_who_left_gets_no_automatic_unmute_case(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin, seconds=60)
        await self.cog_.punish(self.guild, self.member, "ban", moderator=self.admin, reason="bye")
        self.guild.members.remove(self.member)
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual([row["action"] for row in await self.cases()], ["mute", "ban"])
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_a_past_end_on_a_kept_row_reads_as_not_muted(self):
        client = await self.client()
        held = self.guild.role("Held", 3)
        self.member.roles.append(held)
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.admin, seconds=60)
        self.guild.members.remove(self.member)
        await self.bot.timers.run_due(time.time() + 120)
        row = await self.db.one("SELECT * FROM active_mutes")
        self.assertIsNotNone(row)
        response = await client.get(self.path(f"/cases/member/{self.member.id}"))
        self.assertIsNone((await response.json())["muted_until"])

    async def test_a_mute_role_taken_off_by_hand_says_when_roles_couldnt_return(self):
        held = self.guild.role("Held", 3)
        self.member.roles.append(held)
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.admin)
        before = list(self.member.roles)
        self.member.roles = [r for r in self.member.roles if r is not self.role_muted]

        async def refuse(**kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.member.edit = refuse
        snapshot = type("Before", (), {"pending": False, "timed_out_until": None, "get_role": lambda s, role_id: next((r for r in before if r.id == role_id), None)})()
        await self.cog_.on_member_update(snapshot, self.member)
        last = (await self.cases())[-1]
        self.assertEqual(last["action"], "unmute")
        self.assertIn("couldn't be given back", last["reason"])
        self.assertIn("Held", last["reason"])


class UnmuteSomeoneGoneTest(ModBase):
    async def test_a_mute_on_someone_who_left_comes_off(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin)
        gone = self.member
        self.guild.members.remove(gone)
        ctx = self.context(self.admin)
        await self.cog_.unmute.callback(self.cog_, ctx, Someone(gone.id, "gone"), reason="wrong person")
        self.assertEqual(ctx.fails, [])
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertEqual((await self.cases())[-1]["action"], "unmute")

    async def test_nothing_to_lift_says_so(self):
        ctx = self.context(self.admin)
        await self.cog_.unmute.callback(self.cog_, ctx, Someone(new_id(), "gone"), reason=None)
        self.assertIn("isn't muted", ctx.fails[0])
        self.assertEqual(await self.cases(), [])

    async def test_they_arent_muted_again_when_they_return(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin)
        gone = self.member
        self.guild.members.remove(gone)
        await self.cog_.unmute.callback(self.cog_, self.context(self.admin), Someone(gone.id, "gone"), reason=None)
        self.guild.members.append(gone)
        gone.roles = [self.guild.default_role]
        await self.cog_.on_member_join(gone)
        self.assertNotIn(self.role_muted, gone.roles)


class ModlogHealthTest(ModBase):
    async def test_modlog_set_refuses_a_channel_the_bot_cant_post_in(self):
        class Shut(FakeChannel):
            def permissions_for(self, member: Any) -> Any:
                return type(self.guild.default_role.permissions)("view_channel")
        shut = Shut(self.guild, "shut")
        ctx = self.context(self.admin)
        await self.cog_.modlog_set.callback(self.cog_, ctx, channel=shut)
        self.assertIn("Embed Links", ctx.fails[0])
        self.assertEqual(self.bot.settings.get(self.guild.id, "moderation")["modlog_channel"], self.modlog.id)

    async def test_the_reply_says_when_the_modlog_refused(self):
        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.modlog.send = refuse
        ctx = self.context(self.admin)
        await self.cog_.warn.callback(self.cog_, ctx, self.member, reason="x")
        self.assertIn("couldn't post to the modlog", ctx.oks[0])

    async def test_a_mass_ban_keeps_posting_after_one_batch_fails(self):
        real = self.modlog.send
        calls: list[int] = []

        async def first_fails(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise discord.HTTPException(_FakeResponse(500), "boom")
            return await real(*args, **kwargs)
        self.modlog.send = first_fails
        rows = [await self.cog_.record_case(self.guild, "ban", Someone(new_id(), f"p{index}"), self.admin, post=False) for index in range(15)]
        await self.cog_._post_cases(self.guild, rows)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.modlog.sent), 1)

    async def test_repost_sends_cases_that_never_posted(self):
        real = self.modlog.send

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.modlog.send = refuse
        await self.cog_.record_case(self.guild, "warn", self.member, self.admin, reason="x")
        self.modlog.send = real
        ctx = self.context(self.admin)
        await self.cog_.modlog_repost.callback(self.cog_, ctx)
        self.assertIn("1 case", ctx.oks[0])
        self.assertIsNotNone((await self.cases())[0]["message_id"])
        ctx = self.context(self.admin)
        await self.cog_.modlog_repost.callback(self.cog_, ctx)
        self.assertIn("Every case", ctx.oks[0])


class ReasonHistoryTest(ModBase):
    async def test_a_changed_reason_keeps_the_old_one(self):
        row = await self.cog_.record_case(self.guild, "warn", self.member, self.admin, reason="first")
        ctx = self.context(self.mod)
        ctx.author = self.mod
        await self.cog_._change_reason(ctx, row["number"], "second")
        stored = await self.cog_.get_case(self.guild.id, row["number"])
        self.assertEqual(stored["reason"], "second")
        self.assertEqual(stored["extra"]["earlier_reasons"][0]["reason"], "first")
        self.assertEqual(stored["extra"]["earlier_reasons"][0]["by"], self.mod.id)

    async def test_a_case_with_no_moderator_takes_the_name_of_who_explains_it(self):
        row = await self.cog_.record_case(self.guild, "ban", Someone(new_id(), "x"), None, source="external")
        await self.cog_._change_reason(self.context(self.mod), row["number"], "raid")
        stored = await self.cog_.get_case(self.guild.id, row["number"])
        self.assertEqual(stored["moderator_id"], self.mod.id)

    async def test_an_automod_case_keeps_its_source(self):
        row = await self.cog_.record_case(self.guild, "warn", self.member, None, source="automod", reason="x")
        await self.cog_._change_reason(self.context(self.mod), row["number"], "y")
        self.assertIsNone((await self.cog_.get_case(self.guild.id, row["number"]))["moderator_id"])

    async def test_a_late_audit_entry_fills_in_the_moderator(self):
        person = Someone(new_id(), "x")
        row = await self.cog_.record_case(self.guild, "ban", person, None, source="external")
        entry = type("Entry", (), {
            "guild": self.guild, "action": discord.AuditLogAction.ban, "target": type("T", (), {"id": person.id})(),
            "user_id": self.mod.id, "user": self.mod, "reason": "found them", "changes": None,
        })()
        await self.cog_.on_audit_log_entry_create(entry)
        stored = await self.cog_.get_case(self.guild.id, row["number"])
        self.assertEqual(stored["moderator_id"], self.mod.id)
        self.assertEqual(stored["reason"], "found them")

    async def test_a_late_entry_from_the_bot_itself_changes_nothing(self):
        person = Someone(new_id(), "x")
        row = await self.cog_.record_case(self.guild, "ban", person, None, source="external")
        self.bot._connection.user = self.me
        entry = type("Entry", (), {
            "guild": self.guild, "action": discord.AuditLogAction.ban, "target": type("T", (), {"id": person.id})(),
            "user_id": self.me.id, "user": self.me, "reason": "x", "changes": None,
        })()
        await self.cog_.on_audit_log_entry_create(entry)
        self.assertIsNone((await self.cog_.get_case(self.guild.id, row["number"]))["moderator_id"])


class PurgeTest(ModBase):
    async def test_odd_digits_are_no_count(self):
        ctx = self.context(self.admin)
        await self.cog_.purge.callback(self.cog_, ctx, target="²")
        self.assertIn("Give a number", ctx.fails[0])

    async def test_a_purge_that_outlasts_its_token_reports_in_the_channel(self):
        ctx = self.context(self.admin)
        ctx.interaction = type("Late", (), {"created_at": discord.utils.utcnow() - datetime.timedelta(minutes=15)})()
        await self.cog_.purge_all.callback(self.cog_, ctx, 5)
        self.assertEqual(ctx.oks, [])
        self.assertIn("Removed 0 messages", self.channel.sent[-1].content)

    async def test_a_quick_one_answers_as_usual(self):
        ctx = self.context(self.admin)
        ctx.interaction = type("Soon", (), {"created_at": discord.utils.utcnow()})()
        await self.cog_.purge_all.callback(self.cog_, ctx, 5)
        self.assertIn("Removed 0 messages", ctx.oks[0])


class MuteRoleMovedTest(ModBase):
    async def test_staff_hear_once_and_the_record_stays(self):
        for person in (self.member, FakeMember(self.guild, "second", self.role_member)):
            await self.cog_.punish(self.guild, person, "mute", moderator=self.admin)
            person.roles = [self.guild.default_role]
        self.role_muted.position = 50
        self.modlog.sent.clear()
        for person in list(self.guild.members):
            if await self.db.one("SELECT 1 FROM active_mutes WHERE user_id = ?", (person.id,)):
                await self.cog_.on_member_join(person)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM active_mutes"), 2)
        told = [message for message in self.modlog.sent if message.embeds and "moved above" in message.embeds[0].title]
        self.assertEqual(len(told), 1)
