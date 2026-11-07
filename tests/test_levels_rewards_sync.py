"""Reward roles following a change to everyone's XP or to the list, and who may hand them out."""
from __future__ import annotations

import asyncio
import json
import time
from unittest import mock

from captainguild.core.ops import DiscordOps
from captainguild.modules.levels import MEE6_URL, Levels, level_for_xp, xp_for_level
from tests.fake_outside import FakeOutside, reply
from tests.fakes import BotTest, FakeMember, new_id


class Base(BotTest):
    modules = [Levels]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.levels = self.cog(Levels)
        self.levels._sleep = mock.AsyncMock()
        self.ten = self.guild.role("Level ten", 3)
        self.five = self.guild.role("Level five", 3)
        await self.bot.settings.save(self.guild.id, "levels", {"enabled": True})
        await self.levels.set_xp(self.guild.id, self.member.id, xp_for_level(25), name="member")
        await self.levels.set_xp(self.guild.id, self.mod.id, xp_for_level(7), name="mod")
        self.levels._confirm = mock.AsyncMock(return_value=True)

    async def asyncTearDown(self) -> None:
        await self.levels.cog_unload()
        await super().asyncTearDown()

    async def rewards(self, *pairs) -> None:
        await self.bot.settings.save(self.guild.id, "levels", {"rewards": [{"level": level, "role_id": role.id} for level, role in pairs]})

    async def finished(self) -> dict:
        while self.levels.role_sync_running(self.guild.id):
            await asyncio.gather(*self.levels._tasks, return_exceptions=True)
        return self.levels._syncs[self.guild.id]


class ListChangeTest(Base):
    async def test_adding_a_reward_reaches_everyone_already_past_it(self):
        await self.rewards((10, self.ten))
        job = await self.finished()
        self.assertIn(self.ten, self.member.roles)
        self.assertNotIn(self.ten, self.mod.roles)
        self.assertEqual((job["state"], job["changed"]), ("done", 1))

    async def test_raising_a_threshold_takes_it_off_those_now_below(self):
        await self.rewards((5, self.five))
        await self.finished()
        self.assertIn(self.five, self.mod.roles)
        await self.rewards((10, self.five))
        await self.finished()
        self.assertNotIn(self.five, self.mod.roles)
        self.assertIn(self.five, self.member.roles)

    async def test_a_role_taken_off_the_list_comes_off_the_people_who_have_it(self):
        await self.rewards((5, self.five))
        await self.finished()
        await self.rewards()
        await self.finished()
        self.assertNotIn(self.five, self.member.roles)

    async def test_a_save_that_leaves_the_rewards_alone_starts_nothing(self):
        await self.rewards((5, self.five))
        await self.finished()
        self.levels._syncs.clear()
        await self.bot.settings.save(self.guild.id, "levels", {"rate": 2})
        self.assertEqual(self.levels._syncs, {})

    async def test_switching_replace_on_brings_roles_in_line(self):
        await self.rewards((5, self.five), (10, self.ten))
        await self.finished()
        self.assertIn(self.five, self.member.roles)
        await self.bot.settings.save(self.guild.id, "levels", {"replace_rewards": True})
        await self.finished()
        self.assertNotIn(self.five, self.member.roles)
        self.assertIn(self.ten, self.member.roles)

    async def test_it_pauses_after_each_change_and_not_otherwise(self):
        await self.rewards((5, self.five))
        job = await self.finished()
        self.assertEqual(self.levels._sleep.await_count, job["changed"])
        self.levels._sleep.reset_mock()
        self.levels.start_role_sync(self.guild.id)
        await self.finished()
        self.levels._sleep.assert_not_awaited()

    async def test_a_second_ask_during_a_run_goes_round_again(self):
        gate = asyncio.Event()

        async def wait(_seconds):
            await gate.wait()
        self.levels._sleep = wait
        await self.rewards((5, self.five))
        await asyncio.sleep(0)
        while not self.levels._syncs[self.guild.id]["changed"]:
            await asyncio.sleep(0)
        await self.rewards((5, self.five), (10, self.ten))
        self.assertTrue(self.levels._syncs[self.guild.id]["again"])
        gate.set()
        await self.finished()
        self.assertIn(self.ten, self.member.roles)
        self.assertEqual(self.levels._syncs[self.guild.id]["state"], "done")

    async def test_someone_who_left_isnt_touched(self):
        await self.db.run("UPDATE level_members SET left_at = 1 WHERE user_id = ?", (self.member.id,))
        await self.rewards((5, self.five))
        job = await self.finished()
        self.assertNotIn(self.five, self.member.roles)
        self.assertEqual(job["total"], 1)

    async def test_levels_off_starts_nothing(self):
        await self.bot.settings.save(self.guild.id, "levels", {"enabled": False})
        await self.rewards((5, self.five))
        self.assertEqual(self.levels._syncs, {})


class CommandAndRouteTest(Base):
    async def test_the_command_runs_and_refuses_a_second_run(self):
        await self.rewards((5, self.five))
        await self.finished()
        self.member.roles.remove(self.five)
        ctx = self.context()
        gate = asyncio.Event()

        async def wait(_seconds):
            await gate.wait()
        self.levels._sleep = wait
        await self.levels.level_sync.callback(self.levels, ctx)
        self.assertEqual(len(ctx.oks), 1)
        while not self.levels._syncs[self.guild.id]["changed"]:
            await asyncio.sleep(0)
        await self.levels.level_sync.callback(self.levels, ctx)
        self.assertIn("already running", ctx.fails[-1])
        gate.set()
        await self.finished()
        self.assertIn(self.five, self.member.roles)

    async def test_the_route_shows_progress(self):
        client = await self.client()
        self.assertEqual((await (await client.get(self.path("/levels/sync"))).json())["state"], "idle")
        self.assertEqual((await client.post(self.path("/levels/sync"))).status, 200)
        await self.finished()
        data = await (await client.get(self.path("/levels/sync"))).json()
        self.assertEqual((data["state"], data["checked"], data["total"]), ("done", 2, 2))
        self.assertIsNotNone(data["finished_at"])

    async def test_the_route_refuses_a_second_run(self):
        client = await self.client()
        self.levels._syncs[self.guild.id] = {"state": "running"}
        self.assertEqual((await client.post(self.path("/levels/sync"))).status, 409)


class XpChangeTest(Base):
    async def test_restoring_a_backup_brings_roles_in_line(self):
        await self.rewards((10, self.ten))
        await self.finished()
        await self.levels.set_xp(self.guild.id, self.mod.id, xp_for_level(20))
        backup = await self.levels.snapshot(self.guild.id, "daily")
        await self.levels.set_xp(self.guild.id, self.mod.id, 0)
        restored = await self.levels.restore_backup(self.guild.id, backup)
        await self.finished()
        self.assertIn(self.ten, self.mod.roles)
        self.assertEqual(restored["role_updates"], 2)

    async def test_an_import_brings_roles_in_line_and_counts_them(self):
        self.levels.outside = FakeOutside()
        self.bot.ops = DiscordOps(self.bot)
        await self.rewards((10, self.ten))
        await self.finished()
        self.levels.outside.answer(MEE6_URL.format(guild_id=self.guild.id), reply({"players": [{"id": str(self.mod.id), "xp": xp_for_level(30), "username": "mod"}], "role_rewards": []}))
        job = await self.levels.import_mee6(self.guild.id, self.bot.actor(self.admin), self.levels._start_import(self.guild.id))
        await self.finished()
        self.assertIn(self.ten, self.mod.roles)
        self.assertEqual(job["role_updates"], 1)
        self.assertIsNotNone(job["finished_at"])

    async def test_someone_on_the_mee6_board_who_left_comes_in_marked_gone(self):
        self.levels.outside = FakeOutside()
        self.bot.ops = DiscordOps(self.bot)
        stranger = new_id()
        self.levels.outside.answer(MEE6_URL.format(guild_id=self.guild.id), reply({"players": [{"id": str(stranger), "xp": 500, "username": "gone"}, {"id": str(self.mod.id), "xp": 500, "username": "mod"}], "role_rewards": []}))
        await self.levels.import_mee6(self.guild.id, self.bot.actor(self.admin), self.levels._start_import(self.guild.id))
        self.assertIsNotNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = ?", (stranger,)))
        self.assertIsNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = ?", (self.mod.id,)))
        self.assertEqual((await self.levels.board(self.guild.id))["total"], 1)


class OutOfReachTest(Base):
    async def test_a_role_above_the_bot_is_skipped_kept_and_staff_hear_once(self):
        logs = mock.Mock()
        real = self.bot.module
        self.bot.module = lambda key: logs if key == "logging" else real(key)
        await self.rewards((5, self.five))
        await self.finished()
        self.five.position = 50
        self.member.roles.remove(self.five)
        self.levels.start_role_sync(self.guild.id)
        await self.finished()
        self.assertNotIn(self.five, self.member.roles)
        self.assertEqual(self.cog(Levels).cfg(self.guild.id)["rewards"], [{"level": 5, "role_id": self.five.id}])
        self.assertEqual(logs.staff_notice.call_count, 1)


class ImportRankTest(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.levels.outside = FakeOutside()
        self.bot.ops = DiscordOps(self.bot)
        self.high = self.guild.role("High", 7)

    async def take(self, actor) -> tuple[int, int]:
        return await self.levels._take_mee6_rewards(self.guild.id, [{"rank": 1, "role": {"id": str(self.high.id)}}, {"rank": 2, "role": {"id": str(self.five.id)}}], self.bot.actor(actor))

    async def test_a_role_above_the_person_importing_stays_out(self):
        self.assertEqual(await self.take(self.mod), (1, 1))
        self.assertEqual(self.levels.cfg(self.guild.id)["rewards"], [{"level": 2, "role_id": self.five.id}])

    async def test_an_administrator_isnt_held_to_it(self):
        self.assertEqual(await self.take(self.admin), (2, 0))

    async def test_a_role_above_the_bot_stays_out_for_everyone(self):
        self.high.position = 50
        self.assertEqual(await self.take(self.owner), (1, 1))

    async def test_someone_no_longer_in_the_server_adds_nothing(self):
        self.guild.members.remove(self.mod)
        self.assertEqual(await self.take(self.mod), (0, 2))


class ManualLevelRankTest(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.high = self.guild.role("High", 7)
        await self.rewards((100, self.high), (5, self.five))
        await self.finished()

    async def test_a_level_that_earns_a_role_above_the_person_is_refused(self):
        ctx = self.context(author=self.mod)
        await self.levels.level_set_level.callback(self.levels, ctx, 200, self.mod)
        self.assertIn("at or above your top role", ctx.fails[-1])
        self.assertEqual(level_for_xp((await self.levels._entry(self.guild.id, self.mod.id)).xp), 7)
        self.assertNotIn(self.high, self.mod.roles)

    async def test_adding_xp_past_it_is_refused(self):
        await self.levels.set_xp(self.guild.id, self.mod.id, xp_for_level(100) - 5)
        ctx = self.context(author=self.mod)
        await self.levels.level_add_xp.callback(self.levels, ctx, 500, self.mod)
        self.assertIn("at or above your top role", ctx.fails[-1])

    async def test_lower_levels_and_the_people_above_it_are_fine(self):
        ctx = self.context(author=self.mod)
        await self.levels.level_set_level.callback(self.levels, ctx, 50, self.mod)
        self.assertEqual(ctx.fails, [])
        ctx = self.context(author=self.admin)
        await self.levels.level_set_level.callback(self.levels, ctx, 200, self.mod)
        self.assertEqual(ctx.fails, [])

    async def test_someone_already_past_it_can_be_lowered(self):
        await self.levels.set_xp(self.guild.id, self.member.id, xp_for_level(300))
        ctx = self.context(author=self.mod)
        await self.levels.level_set_level.callback(self.levels, ctx, 200, self.member)
        self.assertEqual(ctx.fails, [])

    async def test_the_route_says_the_same(self):
        client = await self.client()
        self.admin.roles = [self.guild.default_role, self.role_member]
        reply_ = await client.put(self.path(f"/levels/members/{self.member.id}"), json={"level": 200})
        self.assertEqual(reply_.status, 403)
        self.assertIn("at or above your top role", (await reply_.json())["error"])


class StrangerTest(Base):
    async def test_someone_with_no_member_and_no_row_gets_no_row(self):
        stranger = mock.Mock(id=new_id(), bot=False, mention="<@1>", display_name="gone")
        ctx = self.context()
        await self.levels.level_add_xp.callback(self.levels, ctx, 500, stranger)
        await self.levels.level_set_level.callback(self.levels, ctx, 3, stranger)
        self.assertEqual(ctx.fails, ["That person isn't in this server."] * 2)
        self.assertIsNone(await self.db.one("SELECT * FROM level_members WHERE user_id = ?", (stranger.id,)))


class LeftAtTest(Base):
    async def test_a_staff_edit_of_someone_who_left_keeps_them_off_the_board(self):
        await self.levels.on_member_remove(self.member)
        client = await self.client()
        reply_ = await client.put(self.path(f"/levels/members/{self.member.id}"), json={"xp": 900})
        self.assertEqual(reply_.status, 200)
        self.assertIsNotNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = ?", (self.member.id,)))
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members WHERE user_id = ?", (self.member.id,)), 900)

    async def test_earning_xp_marks_someone_present(self):
        await self.levels.on_member_remove(self.member)
        await self.levels._award(self.member, self.channel, self.levels.cfg(self.guild.id), message_count=1)
        await self.levels.flush()
        self.assertIsNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = ?", (self.member.id,)))


class ReconcileTest(BotTest):
    modules = [Levels]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.levels = self.cog(Levels)
        await self.bot.settings.save(self.guild.id, "levels", {"enabled": True})
        await self.db.run("INSERT INTO level_members (guild_id, user_id, xp, updated_at) VALUES (?, 999, 10, ?)", (self.guild.id, time.time()))
        await self.db.run("INSERT INTO level_members (guild_id, user_id, xp, left_at, updated_at) VALUES (?, ?, 10, 5, ?)", (self.guild.id, self.member.id, time.time()))

    async def test_a_count_off_by_one_still_runs(self):
        self.guild.chunked = False  # what the library says of any server with a count that doesn't match exactly
        for index in range(150):
            FakeMember(self.guild, f"person{index}")
        with mock.patch.object(type(self.guild), "member_count", property(lambda guild: len(guild.members) + 1)):
            await self.levels.on_ready()
        self.assertIsNotNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = 999"))

    async def test_someone_who_came_back_while_the_bot_was_down_shows_again(self):
        await self.levels.on_ready()
        self.assertIsNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = ?", (self.member.id,)))

    async def test_a_list_far_short_of_the_count_changes_nothing(self):
        with mock.patch.object(type(self.guild), "member_count", property(lambda guild: len(guild.members) * 10)):
            self.guild.chunk = mock.AsyncMock()
            await self.levels.on_ready()
        self.assertIsNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = 999"))


class DeletedDataTest(Base):
    async def test_a_persons_entry_comes_out_of_every_backup(self):
        from captainguild.modules.mydata import MyData
        mydata = MyData(self.bot)
        await self.levels.snapshot(self.guild.id, "daily")
        await self.levels.snapshot(self.guild.id, "daily")
        await mydata.delete(self.member.id, "member", self.guild.id, "levels")
        for row in await self.db.all("SELECT data, members FROM level_backups WHERE guild_id = ?", (self.guild.id,)):
            people = json.loads(row["data"])
            self.assertEqual([person[0] for person in people], [self.mod.id])
            self.assertEqual(row["members"], 1)
        await self.levels.restore_backup(self.guild.id)
        self.assertIsNone(await self.db.one("SELECT * FROM level_members WHERE user_id = ?", (self.member.id,)))

    async def test_another_servers_copies_are_left_alone(self):
        await self.db.run("INSERT INTO level_backups (guild_id, reason, made_at, members, data) VALUES (5, 'daily', 1, 1, ?)", (json.dumps([[self.member.id, 1, 0, 0, "x", None]]),))
        await self.levels.forget_person(self.guild.id, self.member.id)
        self.assertEqual(await self.db.scalar("SELECT members FROM level_backups WHERE guild_id = 5"), 1)
