import asyncio
import datetime
import json
import time
from unittest import mock

import discord

from captainguild.core.module import Module
from captainguild.modules.autoroles import RETRY_SECONDS, SCAN_RETRY_SECONDS, STICKY_RECHECK_SECONDS, Autoroles
from captainguild.modules import moderation as moderation_module
from captainguild.modules.greetings import Greetings
from captainguild.modules.moderation import Moderation
from captainguild.modules.roles import Roles
from captainguild.modules.voicelink import VoiceLink
from tests.fakes import BotTest, FakeChannel, FakeMember, _FakeResponse
from tests.gateway import GatewayTest, iso, new_id, user_json


class AutorolesTest(BotTest):
    modules = [Autoroles]

    async def test_add_command_saves_and_records_activity(self):
        cog = self.cog(Autoroles)
        ctx = self.context()
        await cog.autorole_add.callback(cog, ctx, self.role_member)
        self.assertEqual(self.settings("autoroles")["roles"], [self.role_member.id])
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "autorole.add")
        self.assertEqual(rows[0]["user_name"], "admin")

    async def test_joining_member_gets_autoroles(self):
        cog = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [self.role_member.id]})
        newcomer = FakeMember(self.guild, "newcomer")
        await cog.on_member_join(newcomer)
        self.assertIn(self.role_member, newcomer.roles)

    async def test_pending_member_waits_for_the_rules_screen(self):
        cog = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [self.role_member.id]})
        newcomer = FakeMember(self.guild, "newcomer", pending=True)
        await cog.on_member_join(newcomer)
        self.assertNotIn(self.role_member, newcomer.roles)
        accepted = newcomer
        before = FakeMember(self.guild, "before", pending=True)
        accepted.pending = False
        await cog.on_member_update(before, accepted)
        self.assertIn(self.role_member, newcomer.roles)

    async def test_role_above_the_bot_or_with_power_is_refused(self):
        cog = self.cog(Autoroles)
        high = self.guild.role("High", 20)
        powerful = self.guild.role("Powerful", 3, "ban_members")
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [high.id, powerful.id, self.role_member.id]})
        newcomer = FakeMember(self.guild, "newcomer")
        await cog.on_member_join(newcomer)
        self.assertEqual(newcomer.roles, [self.guild.default_role, self.role_member])

    async def test_elevated_setting_allows_the_powerful_role(self):
        cog = self.cog(Autoroles)
        powerful = self.guild.role("Powerful", 3, "ban_members")
        await self.bot.settings.save(self.guild.id, "commands", {"elevated_roles": True})
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [powerful.id]})
        newcomer = FakeMember(self.guild, "newcomer")
        await cog.on_member_join(newcomer)
        self.assertIn(powerful, newcomer.roles)

    async def test_sticky_roles_come_back_except_the_blacklist(self):
        cog = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True, "blacklist": [self.role_muted.id]})
        leaver = FakeMember(self.guild, "leaver", self.role_member, self.role_muted)
        await cog.on_member_remove(leaver)
        returning = FakeMember(self.guild, "returning")
        returning.id = leaver.id
        await cog.on_member_join(returning)
        self.assertIn(self.role_member, returning.roles)
        self.assertNotIn(self.role_muted, returning.roles)

    async def test_sticky_roles_expire(self):
        cog = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True, "reassign_days": 1})
        leaver = FakeMember(self.guild, "leaver", self.role_member)
        await cog.on_member_remove(leaver)
        await self.db.run("UPDATE sticky_roles SET left_at = ?", (time.time() - 90000,))
        returning = FakeMember(self.guild, "returning")
        returning.id = leaver.id
        await cog.on_member_join(returning)
        self.assertNotIn(self.role_member, returning.roles)

    async def test_timed_role_arrives_when_due_and_only_if_still_here(self):
        cog = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"timed": [{"role_id": self.role_member.id, "seconds": 60}]})
        stays = FakeMember(self.guild, "stays")
        leaves = FakeMember(self.guild, "leaves")
        await cog.on_member_join(stays)
        await cog.on_member_join(leaves)
        self.guild.members.remove(leaves)
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 61), 2)
        self.assertIn(self.role_member, stays.roles)
        self.assertNotIn(self.role_member, leaves.roles)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 61), 0)

    async def test_dashboard_reads_and_saves_settings_with_ids_as_text(self):
        client = await self.client()
        response = await client.put(self.path("/settings/autoroles"), json={"roles": [str(self.role_member.id)], "reassign": True})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["roles"], [str(self.role_member.id)])
        self.assertEqual(self.settings("autoroles")["roles"], [self.role_member.id])
        response = await client.put(self.path("/settings/autoroles"), json={"roles": ["not a role"]})
        self.assertEqual(response.status, 400)
        self.assertIn("roles", (await response.json())["fields"])
        response = await client.put(self.path("/settings/autoroles"), json={"nonsense": True})
        self.assertEqual(response.status, 400)

    async def test_staff_access_can_read_but_not_write_settings(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/settings/autoroles"))).status, 200)
        self.assertEqual((await client.put(self.path("/settings/autoroles"), json={"reassign": True})).status, 403)

    async def test_undo_puts_the_settings_back(self):
        client = await self.client()
        await client.put(self.path("/settings/autoroles"), json={"reassign": True})
        rows = await (await client.get(self.path("/activity"))).json()
        self.assertEqual(self.settings("autoroles")["reassign"], True)
        response = await client.post(self.path(f"/activity/{rows[0]['id']}/undo"))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.settings("autoroles")["reassign"], False)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class StickyFailureTest(BotTest):
    modules = [Autoroles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True})

    async def returning(self):
        leaver = FakeMember(self.guild, "leaver", self.role_member)
        await self.autoroles.on_member_remove(leaver)
        self.guild.members.remove(leaver)
        back = FakeMember(self.guild, "returning")
        back.id = leaver.id
        return back

    async def test_a_failed_add_keeps_what_to_put_back_and_tries_again(self):
        back = await self.returning()
        real = back.add_roles
        back.add_roles = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(503), "down"))
        await self.autoroles.on_member_join(back)
        self.assertNotIn(self.role_member, back.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_roles"), 1)
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        back.add_roles = real
        self.assertEqual(await self.bot.timers.run_due(time.time() + RETRY_SECONDS + 1), 1)
        self.assertIn(self.role_member, back.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_roles"), 0)

    async def test_a_retry_that_fails_again_keeps_trying(self):
        back = await self.returning()
        back.add_roles = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(503), "down"))
        await self.autoroles.on_member_join(back)
        await self.bot.timers.run_due(time.time() + RETRY_SECONDS + 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'autorole_retry'"), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_roles"), 1)

    async def test_a_retry_for_someone_who_left_ends(self):
        back = await self.returning()
        back.add_roles = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(503), "down"))
        await self.autoroles.on_member_join(back)
        self.guild.members.remove(back)
        await self.bot.timers.run_due(time.time() + RETRY_SECONDS + 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'autorole_retry'"), 0)

    async def test_a_put_back_that_works_clears_the_record(self):
        back = await self.returning()
        await self.autoroles.on_member_join(back)
        self.assertIn(self.role_member, back.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_roles"), 0)

    async def test_an_expired_record_goes_when_they_return(self):
        back = await self.returning()
        await self.db.run("UPDATE sticky_roles SET left_at = 1")
        await self.autoroles.on_member_join(back)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_roles"), 0)
        self.assertNotIn(self.role_member, back.roles)

    async def test_records_past_their_time_go_when_anyone_leaves(self):
        await self.db.run("INSERT INTO sticky_roles (guild_id, user_id, role_ids, left_at) VALUES (?, 1, '[]', 1)", (self.guild.id,))
        await self.db.run("INSERT INTO sticky_roles (guild_id, user_id, role_ids, left_at) VALUES (?, 2, '[]', ?)", (self.guild.id, time.time()))
        await self.autoroles.on_member_remove(FakeMember(self.guild, "leaver"))
        rows = await self.db.all("SELECT user_id FROM sticky_roles")
        self.assertEqual([row["user_id"] for row in rows], [2])

    async def test_records_still_go_with_reassign_off(self):
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": False})
        await self.db.run("INSERT INTO sticky_roles (guild_id, user_id, role_ids, left_at) VALUES (?, 1, '[]', 1)", (self.guild.id,))
        await self.autoroles.on_member_remove(FakeMember(self.guild, "leaver"))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_roles"), 0)


class PassingRolesTest(BotTest):
    modules = [Autoroles, Roles, VoiceLink]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True})

    async def leave_and_return(self, leaver: FakeMember) -> FakeMember:
        await self.autoroles.on_member_remove(leaver)
        self.guild.members.remove(leaver)
        back = FakeMember(self.guild, "back")
        back.id = leaver.id
        await self.autoroles.on_member_join(back)
        return back

    async def test_a_temporary_role_doesnt_become_permanent_by_leaving(self):
        temp = self.guild.role("Weekend", 3)
        leaver = FakeMember(self.guild, "visitor", temp, self.role_member)
        await self.bot.timers.schedule("temprole", time.time() + 3600, {"guild_id": self.guild.id, "user_id": leaver.id, "role_id": temp.id, "undo": "remove"}, guild_id=self.guild.id, key=f"{self.guild.id}:{leaver.id}:{temp.id}")
        back = await self.leave_and_return(leaver)
        self.assertNotIn(temp, back.roles)
        self.assertIn(self.role_member, back.roles)

    async def test_a_reaction_role_timer_counts_too(self):
        temp = self.guild.role("Picked", 3)
        leaver = FakeMember(self.guild, "visitor", temp)
        await self.bot.timers.schedule("rr_temp", time.time() + 3600, {"guild_id": self.guild.id, "user_id": leaver.id, "role_id": temp.id}, guild_id=self.guild.id, key=f"{self.guild.id}:{leaver.id}:{temp.id}")
        back = await self.leave_and_return(leaver)
        self.assertNotIn(temp, back.roles)

    async def test_a_voice_linked_role_doesnt_come_back(self):
        linked = self.guild.role("In voice", 3)
        lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        await self.bot.settings.save(self.guild.id, "voicelink", {"links": [{"channel_id": lounge.id, "roles": [linked.id]}]})
        await self.cog(VoiceLink).settle()
        leaver = FakeMember(self.guild, "visitor", linked, self.role_member)
        back = await self.leave_and_return(leaver)
        self.assertNotIn(linked, back.roles)
        self.assertIn(self.role_member, back.roles)


class DowntimeTest(BotTest):
    modules = [Autoroles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [self.role_muted.id]})
        self.old = now_utc() - datetime.timedelta(days=10)
        for member in self.guild.members:
            member.joined_at = self.old

    async def back_from_downtime(self, hours_ago: float = 1) -> None:
        await self.db.run("UPDATE kv SET value = ? WHERE key = ?", (str(time.time() - hours_ago * 3600), f"autoroles_handled:{self.guild.id}"))
        await self.autoroles.on_guild_available(self.guild)

    def joined(self, name: str, *, pending: bool = False) -> FakeMember:
        return FakeMember(self.guild, name, pending=pending, joined=now_utc() - datetime.timedelta(minutes=5))

    async def test_someone_who_joined_while_the_bot_was_away_gets_the_roles(self):
        newcomer = self.joined("newcomer")
        await self.back_from_downtime()
        self.assertIn(self.role_muted, newcomer.roles)

    async def test_someone_who_joined_before_gets_nothing_new(self):
        await self.back_from_downtime()
        self.assertNotIn(self.role_muted, self.member.roles)

    async def test_timed_roles_start_for_them_too(self):
        await self.bot.settings.save(self.guild.id, "autoroles", {"timed": [{"role_id": self.role_member.id, "seconds": 60}]})
        self.joined("newcomer")
        await self.back_from_downtime()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'timedrole'"), 1)

    async def test_someone_still_on_the_rules_screen_waits(self):
        newcomer = self.joined("newcomer", pending=True)
        await self.back_from_downtime()
        self.assertNotIn(self.role_muted, newcomer.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autorole_waiting"), 1)

    async def test_someone_who_passed_the_rules_screen_while_the_bot_was_away_gets_the_roles(self):
        waiting = FakeMember(self.guild, "waiting", pending=True, joined=self.old)
        await self.autoroles.on_member_join(waiting)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autorole_waiting"), 1)
        waiting.pending = False
        await self.back_from_downtime()
        self.assertIn(self.role_muted, waiting.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autorole_waiting"), 0)

    async def test_passing_the_rules_screen_with_the_bot_up_clears_the_record(self):
        waiting = FakeMember(self.guild, "waiting", pending=True)
        await self.autoroles.on_member_join(waiting)
        before = FakeMember(self.guild, "before", pending=True)
        waiting.pending = False
        await self.autoroles.on_member_update(before, waiting)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autorole_waiting"), 0)

    async def test_someone_who_left_from_the_rules_screen_is_forgotten(self):
        waiting = FakeMember(self.guild, "waiting", pending=True, joined=self.old)
        await self.autoroles.on_member_join(waiting)
        self.guild.members.remove(waiting)
        await self.back_from_downtime()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autorole_waiting"), 0)
        waiting = FakeMember(self.guild, "again", pending=True, joined=self.old)
        await self.autoroles.on_member_join(waiting)
        await self.autoroles.on_member_remove(waiting)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autorole_waiting"), 0)

    async def test_a_new_setting_isnt_retroactive(self):
        newcomer = self.joined("before the setting")
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [self.role_member.id]})
        await self.autoroles.on_guild_available(self.guild)
        self.assertNotIn(self.role_member, newcomer.roles)

    async def test_the_first_start_with_nothing_on_record_gives_nothing(self):
        await self.db.run("DELETE FROM kv WHERE key = ?", (f"autoroles_handled:{self.guild.id}",))
        newcomer = self.joined("newcomer")
        await self.autoroles.on_guild_available(self.guild)
        self.assertNotIn(self.role_muted, newcomer.roles)

    async def test_a_second_look_gives_nothing_twice(self):
        newcomer = self.joined("newcomer")
        await self.back_from_downtime()
        newcomer.calls.clear()
        await self.autoroles.on_guild_available(self.guild)
        self.assertEqual(newcomer.calls, [])

    async def test_an_outage_waits_for_the_server_to_come_back(self):
        newcomer = self.joined("newcomer")
        self.guild.unavailable = True
        await self.back_from_downtime()
        self.assertNotIn(self.role_muted, newcomer.roles)
        self.guild.unavailable = False
        await self.autoroles.on_guild_available(self.guild)
        self.assertIn(self.role_muted, newcomer.roles)

    async def test_a_feature_not_in_use_writes_nothing(self):
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": []})
        await self.db.run("DELETE FROM kv WHERE key = ?", (f"autoroles_handled:{self.guild.id}",))
        await self.autoroles.on_member_join(FakeMember(self.guild, "someone", pending=True))
        await self.autoroles.on_member_join(FakeMember(self.guild, "someone else"))
        await self.autoroles.on_guild_available(self.guild)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autorole_waiting"), 0)
        self.assertIsNone(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"autoroles_handled:{self.guild.id}",)))


class ScanMarkTest(BotTest):
    modules = [Autoroles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [self.role_muted.id]})
        for member in self.guild.members:
            member.joined_at = now_utc() - datetime.timedelta(days=10)
        self.down_at = time.time() - 3600
        await self.autoroles._handled_until(self.guild.id, self.down_at)

    async def mark(self) -> float:
        return float(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"autoroles_handled:{self.guild.id}",)))

    def joined_while_down(self, name: str = "away") -> FakeMember:
        return FakeMember(self.guild, name, joined=now_utc() - datetime.timedelta(minutes=30))

    async def test_a_live_join_before_the_scan_doesnt_cost_anyone_their_roles(self):
        away = self.joined_while_down()
        await self.autoroles.on_member_join(FakeMember(self.guild, "live", joined=now_utc()))
        await self.autoroles.on_member_join(FakeMember(self.guild, "live on the rules screen", pending=True, joined=now_utc()))
        self.assertEqual(await self.mark(), self.down_at)
        await self.autoroles.on_guild_available(self.guild)
        self.assertIn(self.role_muted, away.roles)

    async def test_a_settings_save_before_the_scan_leaves_the_mark(self):
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True})
        self.assertEqual(await self.mark(), self.down_at)

    async def test_a_crash_partway_through_looks_at_everyone_again(self):
        first, second = self.joined_while_down("one"), self.joined_while_down("two")
        second.add_roles = mock.AsyncMock(side_effect=RuntimeError("crash"))
        with self.assertRaises(RuntimeError):
            await self.autoroles.on_guild_available(self.guild)
        self.assertIn(self.role_muted, first.roles)
        self.assertEqual(await self.mark(), self.down_at)

    async def test_after_the_scan_live_events_move_the_mark(self):
        await self.autoroles.on_guild_available(self.guild)
        scanned = await self.mark()
        self.assertGreater(scanned, self.down_at)
        await self.db.run("UPDATE kv SET value = ? WHERE key = ?", ("1", f"autoroles_handled:{self.guild.id}"))
        await self.autoroles.on_member_join(FakeMember(self.guild, "live", joined=now_utc()))
        self.assertGreater(await self.mark(), 1)

    async def test_an_outage_sends_it_back_to_waiting_for_the_scan(self):
        await self.autoroles.on_guild_available(self.guild)
        await self.autoroles.on_guild_unavailable(self.guild)
        marked = await self.mark()
        await self.autoroles.on_member_join(FakeMember(self.guild, "live", joined=now_utc()))
        self.assertEqual(await self.mark(), marked)

    async def test_a_list_that_wont_load_tries_again_by_timer(self):
        away = self.joined_while_down()
        with mock.patch("captainguild.modules.autoroles.whole_member_list", mock.AsyncMock(return_value=False)):
            await self.autoroles.on_guild_available(self.guild)
        self.assertEqual(await self.mark(), self.down_at)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'autorole_scan'"), 1)
        self.assertNotIn(self.role_muted, away.roles)
        await self.bot.timers.run_due(time.time() + SCAN_RETRY_SECONDS + 1)
        self.assertIn(self.role_muted, away.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'autorole_scan'"), 0)
        self.assertGreater(await self.mark(), self.down_at)


class TransientStickyTest(BotTest):
    class Holder(Module):
        key = "holder"

        def transient_roles(self, guild_id):
            return set(self.held)

        held: tuple = ()

    modules = [Autoroles, Holder]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)
        self.holder = self.cog(self.Holder)
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True})

    async def back(self, leaver: FakeMember) -> FakeMember:
        await self.autoroles.on_member_remove(leaver)
        self.guild.members.remove(leaver)
        back = FakeMember(self.guild, "back")
        back.id = leaver.id
        await self.autoroles.on_member_join(back)
        return back

    async def test_a_role_a_feature_names_doesnt_come_back(self):
        self.holder.held = (self.role_muted.id,)
        back = await self.back(FakeMember(self.guild, "leaver", self.role_muted, self.role_member))
        self.assertNotIn(self.role_muted, back.roles)
        self.assertIn(self.role_member, back.roles)

    async def test_the_record_leaves_it_out_when_they_go(self):
        self.holder.held = (self.role_muted.id,)
        leaver = FakeMember(self.guild, "leaver", self.role_muted, self.role_member)
        await self.autoroles.on_member_remove(leaver)
        self.assertEqual(json.loads(await self.db.scalar("SELECT role_ids FROM sticky_roles WHERE user_id = ?", (leaver.id,))), [self.role_member.id])

    async def test_a_record_from_before_it_was_named_doesnt_bring_it_back(self):
        leaver = FakeMember(self.guild, "leaver", self.role_muted)
        await self.autoroles.on_member_remove(leaver)
        self.holder.held = (self.role_muted.id,)
        self.guild.members.remove(leaver)
        back = FakeMember(self.guild, "back")
        back.id = leaver.id
        await self.autoroles.on_member_join(back)
        self.assertNotIn(self.role_muted, back.roles)

    async def test_a_timed_role_comes_from_its_timer_not_the_record(self):
        await self.bot.settings.save(self.guild.id, "autoroles", {"timed": [{"role_id": self.role_member.id, "seconds": 60}]})
        self.assertEqual(self.bot.transient_roles(self.guild.id), {self.role_member.id})
        back = await self.back(FakeMember(self.guild, "leaver", self.role_member))
        self.assertNotIn(self.role_member, back.roles)
        await self.bot.timers.run_due(time.time() + 61)
        self.assertIn(self.role_member, back.roles)

    async def test_one_members_temporary_role_is_another_members_lasting_one(self):
        temp = self.guild.role("Weekend", 3)
        visitor = FakeMember(self.guild, "visitor", temp)
        await self.bot.timers.schedule("temprole", time.time() + 3600, {"guild_id": self.guild.id, "user_id": visitor.id, "role_id": temp.id, "undo": "remove"}, guild_id=self.guild.id, key=f"{self.guild.id}:{visitor.id}:{temp.id}")
        back = await self.back(FakeMember(self.guild, "regular", temp))
        self.assertIn(temp, back.roles)


class MuteAcrossALeaveTest(BotTest):
    modules = [Autoroles, Moderation]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)
        self.moderation = self.cog(Moderation)
        self.guild.default_role.permissions = type(self.guild.default_role.permissions)("view_channel")
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": self.role_muted.id})
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True})
        self.audit = mock.patch.object(moderation_module, "AUDIT_WAIT", 0)
        self.audit.start()
        self.addCleanup(self.audit.stop)
        self.target = FakeMember(self.guild, "target", self.role_member)
        await self.moderation.mute.callback(self.moderation, self.context(self.admin), self.target, "1h", reason="x")
        self.assertIn(self.role_muted, self.target.roles)
        self.guild.members.remove(self.target)
        await self.autoroles.on_member_remove(self.target)

    async def come_back(self) -> None:
        self.target.roles = [self.guild.default_role]
        self.guild.members.append(self.target)
        await self.moderation.on_member_join(self.target)
        await self.autoroles.on_member_join(self.target)

    async def test_a_mute_that_ended_while_they_were_away_stays_ended(self):
        await self.bot.timers.run_due(time.time() + 7200)
        await self.come_back()
        self.assertNotIn(self.role_muted, self.target.roles)
        self.assertIn(self.role_member, self.target.roles)

    async def test_a_mute_still_running_goes_back_on(self):
        await self.come_back()
        for _ in range(20):
            if self.role_muted in self.target.roles:
                break
            await asyncio.sleep(0.01)
        self.assertIn(self.role_muted, self.target.roles)


class BirthdayAcrossALeaveTest(BotTest):
    modules = [Autoroles, Greetings]

    async def test_the_birthday_role_doesnt_come_back(self):
        autoroles = self.cog(Autoroles)
        birthday = self.guild.role("Birthday", 3)
        await self.bot.settings.save(self.guild.id, "greetings", {"birthday_role": birthday.id})
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True})
        leaver = FakeMember(self.guild, "leaver", birthday, self.role_member)
        await autoroles.on_member_remove(leaver)
        self.guild.members.remove(leaver)
        back = FakeMember(self.guild, "back")
        back.id = leaver.id
        await autoroles.on_member_join(back)
        self.assertNotIn(birthday, back.roles)
        self.assertIn(self.role_member, back.roles)


class TimedRowsTest(BotTest):
    modules = [Autoroles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)

    async def test_removing_a_timed_role_on_the_page_stops_it_going_out(self):
        other = self.guild.role("Other", 2)
        await self.bot.settings.save(self.guild.id, "autoroles", {"timed": [{"role_id": self.role_member.id, "seconds": 60}, {"role_id": other.id, "seconds": 60}]})
        member = FakeMember(self.guild, "n")
        await self.autoroles.on_member_join(member)
        client = await self.client()
        response = await client.put(self.path("/settings/autoroles"), json={"timed": [{"role_id": str(other.id), "seconds": 60}]})
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'timedrole'"), 1)
        await self.bot.timers.run_due(time.time() + 120)
        self.assertNotIn(self.role_member, member.roles)
        self.assertIn(other, member.roles)

    async def test_a_timer_with_no_row_gives_nothing(self):
        member = FakeMember(self.guild, "n")
        await self.bot.timers.schedule("timedrole", time.time(), {"guild_id": self.guild.id, "user_id": member.id, "role_id": self.role_member.id}, guild_id=self.guild.id, key=f"{self.guild.id}:{member.id}:{self.role_member.id}")
        await self.bot.timers.run_due(time.time() + 1)
        self.assertNotIn(self.role_member, member.roles)

    async def test_the_message_names_the_row(self):
        client = await self.client()
        for rows, expected in (
            ([{"role_id": None, "seconds": 3600}], "Timed role 1 needs a role."),
            ([{"role_id": str(self.role_member.id), "seconds": 3600}, {"role_id": str(self.role_muted.id), "seconds": 0}], "Timed role 2 needs a delay"),
            ([{"role_id": str(self.role_member.id), "seconds": 3600}, {"role_id": str(self.role_member.id), "seconds": 60}], "Timed role 2 is already on this list"),
        ):
            response = await client.put(self.path("/settings/autoroles"), json={"timed": rows})
            self.assertEqual(response.status, 400)
            self.assertIn(expected, (await response.json())["fields"]["timed"])

    async def test_a_leaver_loses_their_timer(self):
        await self.bot.settings.save(self.guild.id, "autoroles", {"timed": [{"role_id": self.role_member.id, "seconds": 60}]})
        member = FakeMember(self.guild, "n")
        await self.autoroles.on_member_join(member)
        await self.autoroles.on_member_remove(member)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'timedrole'"), 0)

    async def test_someone_on_the_rules_screen_waits(self):
        await self.bot.settings.save(self.guild.id, "autoroles", {"timed": [{"role_id": self.role_member.id, "seconds": 60}]})
        member = FakeMember(self.guild, "n")
        await self.autoroles.on_member_join(member)
        member.pending = True
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual([c for c in member.calls if c[0] == "add_roles"], [])


class OutOfReachTest(BotTest):
    modules = [Autoroles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign": True})

    async def test_a_role_moved_above_the_bot_stays_on_record_and_comes_back_later(self):
        role = self.guild.role("Kept", 2)
        leaver = FakeMember(self.guild, "n", role, self.role_member)
        await self.autoroles.on_member_remove(leaver)
        role.position = self.me.top_role.position + 3
        self.guild.members.remove(leaver)
        back = FakeMember(self.guild, "back")
        back.id = leaver.id
        with mock.patch("captainguild.modules.autoroles.tell_staff") as told:
            await self.autoroles.on_member_join(back)
        told.assert_called_once()
        self.assertIn("highest role", told.call_args.args[4])
        self.assertIn(self.role_member, back.roles)
        self.assertNotIn(role, back.roles)
        self.assertEqual(await self.db.scalar("SELECT role_ids FROM sticky_roles WHERE user_id = ?", (back.id,)), f"[{role.id}]")
        role.position = 2
        await self.bot.timers.run_due(time.time() + STICKY_RECHECK_SECONDS + 1)
        self.assertIn(role, back.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_roles"), 0)

    async def test_a_look_later_gives_no_autoroles_staff_took_off(self):
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [self.role_muted.id]})
        role = self.guild.role("Kept", 2)
        leaver = FakeMember(self.guild, "n", role)
        await self.autoroles.on_member_remove(leaver)
        role.position = self.me.top_role.position + 3
        self.guild.members.remove(leaver)
        back = FakeMember(self.guild, "back")
        back.id = leaver.id
        await self.autoroles.on_member_join(back)
        await back.remove_roles(self.role_muted)
        await self.bot.timers.run_due(time.time() + STICKY_RECHECK_SECONDS + 1)
        self.assertNotIn(self.role_muted, back.roles)

    async def test_a_deleted_role_leaves_no_record(self):
        role = self.guild.role("Kept", 2)
        leaver = FakeMember(self.guild, "n", role)
        await self.autoroles.on_member_remove(leaver)
        self.guild.roles.remove(role)
        await self.autoroles.on_member_join(leaver)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_roles"), 0)

    async def test_leaving_again_keeps_what_was_still_waiting(self):
        role = self.guild.role("Kept", 2)
        leaver = FakeMember(self.guild, "n", role, self.role_member)
        await self.autoroles.on_member_remove(leaver)
        role.position = self.me.top_role.position + 3
        leaver.roles = [self.guild.default_role]
        await self.autoroles.on_member_join(leaver)
        await self.autoroles.on_member_remove(leaver)
        kept = json.loads(await self.db.scalar("SELECT role_ids FROM sticky_roles WHERE user_id = ?", (leaver.id,)))
        self.assertEqual(sorted(kept), sorted([role.id, self.role_member.id]))

    async def test_a_form_refuses_a_role_with_power_as_the_command_does(self):
        strong = self.guild.role("Strong", 3, "ban_members")
        client = await self.client()
        response = await client.put(self.path("/settings/autoroles"), json={"roles": [str(strong.id)]})
        self.assertEqual(response.status, 400)
        self.assertIn("elevated roles", (await response.json())["fields"]["roles"])
        response = await client.put(self.path("/settings/autoroles"), json={"timed": [{"role_id": str(strong.id), "seconds": 60}]})
        self.assertEqual(response.status, 400)

    async def test_the_page_can_read_the_rules_for_its_warnings(self):
        self.bot.ops.describe_guild = lambda guild_id: {"channels": []}
        client = await self.client()
        body = await (await client.get(self.path("/info"))).json()
        self.assertFalse(body["elevated_roles"])
        self.assertIn("ban_members", body["dangerous"])


class TypedFormsTest(BotTest):
    modules = [Autoroles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)

    async def test_the_time_can_come_first_or_last(self):
        for role, time_text in (("2h", "Member"), ("Member", "2h")):
            await self.autoroles.timedrole_add.callback(self.autoroles, self.context(self.owner), None, time=f"{role} {time_text}")
            self.assertEqual(self.settings("autoroles")["timed"], [{"role_id": self.role_member.id, "seconds": 7200}])

    async def test_a_role_name_with_spaces_and_a_time_all_in_one(self):
        two_words = self.guild.role("Two Words", 2)
        await self.autoroles.timedrole_add.callback(self.autoroles, self.context(self.owner), None, time="Two Words 3h")
        self.assertEqual(self.settings("autoroles")["timed"], [{"role_id": two_words.id, "seconds": 10800}])

    async def test_no_time_is_refused(self):
        ctx = self.context(self.owner)
        await self.autoroles.timedrole_add.callback(self.autoroles, ctx, self.role_member)
        self.assertIn("length of time", ctx.fails[0])

    async def test_blacklist_takes_several_roles(self):
        ctx = self.context(self.owner)
        await self.autoroles.autorole_blacklist.callback(self.autoroles, ctx, None, more="Muted Member")
        self.assertEqual(self.settings("autoroles")["blacklist"], [self.role_muted.id, self.role_member.id])
        await self.autoroles.autorole_unblacklist.callback(self.autoroles, self.context(self.owner), None, more="Muted")
        self.assertEqual(self.settings("autoroles")["blacklist"], [self.role_member.id])

    async def test_a_name_that_isnt_a_role_changes_nothing(self):
        ctx = self.context(self.owner)
        await self.autoroles.autorole_blacklist.callback(self.autoroles, ctx, None, more="Muted Nothing")
        self.assertIn("Nothing", ctx.fails[0])
        self.assertEqual(self.settings("autoroles")["blacklist"], [])

    def test_the_documented_short_names_exist(self):
        self.assertIn("autoroles", self.autoroles.autorole.aliases)
        self.assertIn("readd", self.autoroles.autorole_reassign.aliases)
        self.assertIn("bl", self.autoroles.autorole_blacklist.aliases)
        self.assertIn("tr", self.autoroles.timedrole.aliases)
        self.assertIn("+", self.autoroles.timedrole_add.aliases)
        self.assertIn("-", self.autoroles.timedrole_remove.aliases)


class BotsTest(BotTest):
    modules = [Autoroles]

    async def test_a_bot_gets_autoroles_unless_the_server_said_not_to(self):
        autoroles = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [self.role_member.id], "timed": [{"role_id": self.role_muted.id, "seconds": 60}]})
        first = FakeMember(self.guild, "first", bot=True)
        await autoroles.on_member_join(first)
        self.assertIn(self.role_member, first.roles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"ignore_bots": True})
        second = FakeMember(self.guild, "second", bot=True)
        await autoroles.on_member_join(second)
        self.assertNotIn(self.role_member, second.roles)
        self.assertIsNone(await self.bot.timers.get_key("timedrole", f"{self.guild.id}:{second.id}:{self.role_muted.id}"))
        person = FakeMember(self.guild, "person")
        await autoroles.on_member_join(person)
        self.assertIn(self.role_member, person.roles)


class MissingFromTheCacheTest(BotTest):
    modules = [Autoroles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)
        await self.bot.settings.save(self.guild.id, "autoroles", {"timed": [{"role_id": self.role_muted.id, "seconds": 60}]})
        await self.autoroles.on_member_join(self.member)
        self.guild.members.remove(self.member)

    async def test_a_timed_role_arrives_for_someone_the_cache_doesnt_have(self):
        self.guild.fetch_member = mock.AsyncMock(return_value=self.member)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 61), 1)
        self.assertIn(self.role_muted, self.member.roles)

    async def test_a_lookup_that_fails_keeps_the_timer(self):
        self.guild.fetch_member = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(503), "down"))
        await self.bot.timers.run_due(time.time() + 61)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'timedrole'"), 1)


class WhoMaySetItTest(BotTest):
    modules = [Autoroles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.autoroles = self.cog(Autoroles)

    async def refused(self, role, author=None) -> str:
        ctx = self.context(author=author or self.admin)
        await self.autoroles.autorole_add.callback(self.autoroles, ctx, role)
        timed = self.context(author=author or self.admin)
        await self.autoroles.timedrole_add.callback(self.autoroles, timed, role, time="1h")
        self.assertEqual(self.settings("autoroles")["roles"], [])
        self.assertEqual(self.settings("autoroles")["timed"], [])
        self.assertEqual(ctx.oks, [])
        self.assertEqual(ctx.fails, timed.fails)
        return ctx.fails[0]

    async def test_a_role_above_the_bot(self):
        self.assertIn("bot's highest role", await self.refused(self.guild.role("High", 20)))

    async def test_a_role_with_power(self):
        self.assertIn("elevated roles", await self.refused(self.guild.role("Strong", 3, "ban_members")))

    async def test_a_role_that_belongs_to_an_integration(self):
        self.assertIn("integration", await self.refused(self.guild.role("Booster", 3, managed=True)))

    async def test_a_role_above_the_person_asking(self):
        high = self.guild.role("Senior", self.mod.top_role.position + 1)
        self.assertIn("your highest role", await self.refused(high, self.mod))

    async def test_the_owner_can_set_any_role_the_bot_can_give(self):
        high = self.guild.role("Senior", self.me.top_role.position - 1)
        ctx = self.context(author=self.owner)
        await self.autoroles.autorole_add.callback(self.autoroles, ctx, high)
        self.assertEqual(self.settings("autoroles")["roles"], [high.id])

    async def test_the_elevated_setting_allows_a_role_with_power(self):
        strong = self.guild.role("Strong", 3, "ban_members")
        await self.bot.settings.save(self.guild.id, "commands", {"elevated_roles": True})
        ctx = self.context()
        await self.autoroles.autorole_add.callback(self.autoroles, ctx, strong)
        self.assertEqual(self.settings("autoroles")["roles"], [strong.id])

    async def test_the_dashboard_knows_which_roles_these_settings_give(self):
        given = self.autoroles.roles_given({"roles": [1, 2], "timed": [{"role_id": 3, "seconds": 60}], "reassign": False, "reassign_days": 30, "blacklist": []})
        self.assertEqual(given, {"roles": {1, 2}, "timed": {3}})


class RealLibraryTest(GatewayTest):
    modules = [Autoroles]

    async def test_someone_who_joined_while_the_bot_was_away_gets_the_roles(self):
        await self.bot.settings.save(self.guild_id, "autoroles", {"roles": [int(self.role_muted["id"])]})
        await self.db.run("UPDATE kv SET value = ? WHERE key = ?", (str(time.time() - 3600), f"autoroles_handled:{self.guild_id}"))
        guild = self.bot.get_guild(self.guild_id)
        newcomer = new_id()
        data = {"user": user_json(newcomer, "newcomer"), "roles": [], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "premium_since": None, "avatar": None, "communication_disabled_until": None}
        guild._add_member(discord.Member(data=data, guild=guild, state=self.bot._connection))
        guild._member_count += 1
        self.bot._connection.dispatch("guild_available", guild)
        await self.settle()
        self.assert_no_errors()
        changed = [path for method, path, _ in self.net.calls if method == "PUT" and path == "/guilds/{guild_id}/members/{user_id}/roles/{role_id}"]
        self.assertEqual(len(changed), 1)  # the newcomer, and nobody who was already here
