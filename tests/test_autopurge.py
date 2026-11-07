import asyncio
import time
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core.ops import DiscordOps
from captainguild.modules import autopurge as autopurge_module
from captainguild.modules.autopurge import AutoPurge, wanted_check
from captainguild.modules.sticky import Sticky
from tests.fakes import BotTest, FakeChannel, FakeMember, FakeMessage, FakePermissions, TestBot, _FakeResponse
from tests.test_access_limits import HiddenChannel, SiteCase


class PurgeChannel(FakeChannel):
    async def purge(self, *, limit=100, check=lambda m: True, reason=None, **kwargs):
        self.calls.append(("purge", {"limit": limit, "reason": reason, **kwargs}))
        deleted = [m for m in list(reversed(self.sent))[:limit] if check(m)]
        for message in deleted:
            self.sent.remove(message)
        return deleted


class AutoPurgeBase(BotTest):
    modules = [AutoPurge]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.auto = self.cog(AutoPurge)
        self.room = PurgeChannel(self.guild, "room")
        self.other_bot = FakeMember(self.guild, "SomeBot", bot=True)
        # The dashboard asks ops whether a channel belongs to this server.
        self.bot.ops.channel = lambda guild_id, channel_id: {"id": channel_id} if guild_id == self.guild.id and self.guild.get_channel(channel_id) else None

    def post(self, author, content="", **kwargs):
        message = FakeMessage(self.room, author, content, **kwargs)
        self.room.sent.append(message)
        return message

    async def finish(self):
        running = list(self.auto._running.values())
        if running:
            await asyncio.gather(*running)


class TypeTest(AutoPurgeBase):
    def test_each_type(self):
        human = SimpleNamespace(author=SimpleNamespace(bot=False), content="hi", attachments=[], pinned=False)
        robot = SimpleNamespace(author=SimpleNamespace(bot=True), content="beep", attachments=[], pinned=False)
        link = SimpleNamespace(author=SimpleNamespace(bot=False), content="see https://example.com", attachments=[], pinned=False)
        invite = SimpleNamespace(author=SimpleNamespace(bot=False), content="discord.gg/abc", attachments=[], pinned=False)
        file = SimpleNamespace(author=SimpleNamespace(bot=False), content="", attachments=[1], pinned=False)
        pinned = SimpleNamespace(author=SimpleNamespace(bot=False), content="rules", attachments=[], pinned=True)
        table = {
            "all": {"human", "robot", "link", "invite", "file"}, "humans": {"human", "link", "invite", "file"}, "bots": {"robot"},
            "links": {"link"}, "invites": {"invite"}, "attachments": {"file"},
        }
        everything = {"human": human, "robot": robot, "link": link, "invite": invite, "file": file, "pinned": pinned}
        for kind, expected in table.items():
            check = wanted_check(kind)
            self.assertEqual({name for name, m in everything.items() if check(m)}, expected, kind)


class TimerTest(AutoPurgeBase):
    async def test_a_listed_channel_is_purged_and_goes_again(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        self.post(self.member, "one")
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        await self.bot.timers.run_due(time.time() + 3601)
        await self.finish()
        self.assertEqual(self.room.sent, [])
        row = await self.db.one("SELECT * FROM autopurge_channels")
        self.assertEqual(row["last_deleted"], 1)
        self.assertGreater(row["next_run"], time.time() + 3000)
        timer = await self.bot.timers.get_key("autopurge.run", f"{self.guild.id}:{self.room.id}")
        self.assertIsNotNone(timer)
        self.assertEqual(self.room.calls[-1][1]["limit"], 1000)

    async def test_only_the_chosen_type_goes_and_pins_stay(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "bots")
        keep = self.post(self.member, "stay")
        pinned_bot = self.post(self.other_bot, "pinned")
        pinned_bot.pinned = True
        gone = self.post(self.other_bot, "beep")
        await self.bot.timers.run_due(time.time() + 3601)
        await self.finish()
        self.assertEqual(self.room.sent, [keep, pinned_bot])
        self.assertNotIn(gone, self.room.sent)

    async def test_a_deleted_channel_comes_off_the_list(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        self.guild.channels.remove(self.room)
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertIsNone(await self.db.one("SELECT * FROM autopurge_channels"))
        self.assertIsNone(await self.bot.timers.get_key("autopurge.run", f"{self.guild.id}:{self.room.id}"))

    async def test_a_removed_channel_is_left_alone(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        self.post(self.member, "one")
        await self.auto.remove_channel(self.guild.id, self.room.id)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 0)
        self.assertEqual(len(self.room.sent), 1)

    async def test_it_survives_a_restart(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        self.post(self.member, "one")
        restarted = TestBot(self.db, self.message_db)
        await restarted.prepare([AutoPurge])
        restarted.fake_guilds.append(self.guild)
        await restarted.timers.run_due(time.time() + 3601)
        await asyncio.gather(*restarted.get_cog("AutoPurge")._running.values())
        self.assertEqual(self.room.sent, [])

    async def test_before_the_gateway_connects_it_waits_and_keeps_the_channel(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        self.bot.fake_guilds.clear()
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertIsNotNone(await self.db.one("SELECT * FROM autopurge_channels"))
        self.assertIsNotNone(await self.bot.timers.get_key("autopurge.run", f"{self.guild.id}:{self.room.id}"))

    async def test_a_refusal_does_not_stop_the_schedule(self):
        async def refuse(**kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.room.purge = refuse
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        await self.bot.timers.run_due(time.time() + 3601)
        await self.finish()
        self.assertIsNotNone(await self.bot.timers.get_key("autopurge.run", f"{self.guild.id}:{self.room.id}"))

    async def test_a_slow_purge_does_not_hold_up_other_timers(self):
        release = asyncio.Event()

        async def slow(**kwargs):
            await release.wait()
            return []
        self.room.purge = slow
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        await asyncio.wait_for(self.bot.timers.run_due(time.time() + 3601), 5)
        self.assertFalse(self.auto._running[self.room.id].done())
        # A second due run while one is going doesn't pile on.
        first = self.auto._running[self.room.id]
        await self.db.run("UPDATE timers SET due_at = 0")
        await self.bot.timers.run_due()
        self.assertIs(self.auto._running[self.room.id], first)
        release.set()
        await self.finish()


class CommandTest(AutoPurgeBase):
    async def test_set_config_and_remove(self):
        ctx = self.context(self.admin)
        await self.auto.autopurge_set.callback(self.auto, ctx, self.room, 7200, "links")
        self.assertEqual(ctx.fails, [])
        row = await self.db.one("SELECT * FROM autopurge_channels")
        self.assertEqual((row["guild_id"], row["channel_id"], row["interval_seconds"], row["message_type"]), (self.guild.id, self.room.id, 7200, "links"))
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "autopurge.set")
        ctx = self.context(self.admin)
        await self.auto.autopurge.callback(self.auto, ctx)
        self.assertIn(self.room.mention, ctx.sent[0]["content"])
        self.assertIn("2 hours", ctx.sent[0]["content"])
        ctx = self.context(self.admin)
        await self.auto.autopurge_remove.callback(self.auto, ctx, channel=self.room)
        self.assertEqual(ctx.fails, [])
        self.assertIsNone(await self.db.one("SELECT * FROM autopurge_channels"))
        ctx = self.context(self.admin)
        await self.auto.autopurge_remove.callback(self.auto, ctx, channel=self.room)
        self.assertIn("isn't on the list", ctx.fails[0])

    async def test_interval_limits(self):
        for seconds in (3599, 14 * 86400 + 1):
            ctx = self.context(self.admin)
            await self.auto.autopurge_set.callback(self.auto, ctx, self.room, seconds, "all")
            self.assertTrue(ctx.fails, seconds)
        self.assertIsNone(await self.db.one("SELECT * FROM autopurge_channels"))
        ctx = self.context(self.admin)
        await self.auto.autopurge_set.callback(self.auto, ctx, self.room, 14 * 86400, "all")
        self.assertEqual(ctx.fails, [])

    async def test_remove_with_no_channel_stops_them_all(self):
        another = PurgeChannel(self.guild, "another")
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        await self.auto.set_channel(self.guild.id, another.id, 3600, "all")
        ctx = self.context(self.admin)
        await self.auto.autopurge_remove.callback(self.auto, ctx, channel=None)
        self.assertIn("Stopped purging 2", ctx.oks[0])
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 0)

    async def test_setting_again_replaces_rather_than_doubles(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        await self.auto.set_channel(self.guild.id, self.room.id, 7200, "bots")
        rows = await self.db.all("SELECT * FROM autopurge_channels")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_type"], "bots")
        self.assertEqual(len(await self.bot.timers.list_kind("autopurge.run")), 1)

    async def test_it_warns_when_the_bot_cannot_delete_there(self):
        self.role_bot.permissions = type(self.role_bot.permissions)("send_messages")
        ctx = self.context(self.admin)
        await self.auto.autopurge_set.callback(self.auto, ctx, self.room, 3600, "all")
        self.assertIn("Manage Messages", ctx.oks[0])


class ApiTest(AutoPurgeBase):
    async def test_set_list_remove(self):
        client = await self.client()
        response = await client.put(self.path(f"/autopurge/{self.room.id}"), json={"interval_seconds": 7200, "message_type": "invites"})
        self.assertEqual(response.status, 200)
        rows = await (await client.get(self.path("/autopurge"))).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel_id"], str(self.room.id))
        self.assertEqual(rows[0]["message_type"], "invites")
        activity = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(activity[0]["action"], "autopurge.set")
        self.assertEqual((await client.delete(self.path(f"/autopurge/{self.room.id}"))).status, 200)
        self.assertEqual(await (await client.get(self.path("/autopurge"))).json(), [])
        self.assertEqual((await client.delete(self.path(f"/autopurge/{self.room.id}"))).status, 404)

    async def test_blank_type_means_all(self):
        client = await self.client()
        await client.put(self.path(f"/autopurge/{self.room.id}"), json={"interval_seconds": 3600, "message_type": None})
        self.assertEqual((await self.db.one("SELECT * FROM autopurge_channels"))["message_type"], "all")

    async def test_bad_input(self):
        client = await self.client()
        for form, field in (
            ({"interval_seconds": 60}, "interval_seconds"), ({"interval_seconds": 15 * 86400}, "interval_seconds"), ({"interval_seconds": "soon"}, "interval_seconds"),
            ({"interval_seconds": 3600, "message_type": "nope"}, "message_type"),
        ):
            response = await client.put(self.path(f"/autopurge/{self.room.id}"), json=form)
            self.assertEqual(response.status, 400, form)
            self.assertIn(field, (await response.json())["fields"], form)
        self.assertEqual((await client.put(self.path(f"/autopurge/{self.room.id}"), json=["no"])).status, 400)

    async def test_a_channel_from_another_server_is_refused(self):
        client = await self.client()
        response = await client.put(self.path("/autopurge/111222333444555666"), json={"interval_seconds": 3600})
        self.assertEqual(response.status, 400)
        self.assertIsNone(await self.db.one("SELECT * FROM autopurge_channels"))

    async def test_huge_ids_do_not_crash(self):
        client = await self.client()
        huge = "9" * 40
        self.assertEqual((await client.put(self.path(f"/autopurge/{huge}"), json={"interval_seconds": 3600})).status, 400)
        self.assertEqual((await client.delete(self.path(f"/autopurge/{huge}"))).status, 404)

    async def test_another_servers_row_stays_out_of_reach(self):
        await self.auto.set_channel(self.guild.id + 1, self.room.id, 3600, "all")
        client = await self.client()
        self.assertEqual(await (await client.get(self.path("/autopurge"))).json(), [])
        self.assertEqual((await client.delete(self.path(f"/autopurge/{self.room.id}"))).status, 404)
        self.assertIsNotNone(await self.db.one("SELECT * FROM autopurge_channels WHERE guild_id = ?", (self.guild.id + 1,)))

    async def test_staff_cannot_reach_it(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/autopurge"))).status, 403)
        self.assertEqual((await client.put(self.path(f"/autopurge/{self.room.id}"), json={"interval_seconds": 3600})).status, 403)


class FailureTest(AutoPurgeBase):
    """A purge that can't finish says so on the channel's row, and a good run clears it."""

    async def run_once(self):
        await self.bot.timers.run_due(time.time() + 3601)
        await self.finish()

    async def last_error(self):
        return (await self.db.one("SELECT last_error FROM autopurge_channels"))["last_error"]

    async def test_without_permission_nothing_goes_and_the_row_says_why(self):
        self.role_bot.permissions = FakePermissions("send_messages")
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        self.post(self.member, "one")
        await self.run_once()
        self.assertEqual(len(self.room.sent), 1)
        self.assertIn("Manage Messages", await self.last_error())

    async def test_a_refusal_shows_until_a_run_works(self):
        real = self.room.purge

        async def refuse(**kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.room.purge = refuse
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        await self.run_once()
        self.assertIn("refused", await self.last_error())
        self.room.purge = real
        await self.db.run("UPDATE timers SET due_at = 0")
        await self.run_once()
        self.assertIsNone(await self.last_error())
        self.assertEqual((await self.db.one("SELECT last_deleted FROM autopurge_channels"))["last_deleted"], 0)

    async def test_a_server_error_or_a_message_gone_midway_goes_again_once(self):
        for status, kind in ((500, discord.DiscordServerError), (404, discord.NotFound)):
            calls = []
            real = PurgeChannel.purge

            async def flaky(*, failure=(status, kind), **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise failure[1](_FakeResponse(failure[0]), "hiccup")
                return await real(self.room, **kwargs)
            self.room.purge = flaky
            await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
            self.post(self.member, "one")
            with mock.patch.object(autopurge_module, "RETRY_SECONDS", 0):
                await self.run_once()
            self.assertEqual(len(calls), 2, status)
            self.assertEqual(self.room.sent, [], status)
            self.assertIsNone(await self.last_error(), status)
            await self.db.run("UPDATE timers SET due_at = 0")

    async def test_a_second_failure_says_what_is_left_waits(self):
        calls = []

        async def broken(**kwargs):
            calls.append(kwargs)
            raise discord.DiscordServerError(_FakeResponse(500), "down")
        self.room.purge = broken
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        with mock.patch.object(autopurge_module, "RETRY_SECONDS", 0):
            await self.run_once()
        self.assertEqual(len(calls), 2)
        self.assertIn("next run", await self.last_error())

    async def test_a_kind_that_reads_text_stands_down_without_message_content(self):
        self.bot.reads_message_text = False
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "links")
        await self.run_once()
        self.assertEqual(self.room.calls, [])
        self.assertIn("read message text", await self.last_error())


class EditTest(AutoPurgeBase):
    async def row(self):
        return await self.db.one("SELECT * FROM autopurge_channels")

    async def test_changing_only_the_kind_keeps_the_next_run(self):
        first = await self.auto.set_channel(self.guild.id, self.room.id, 7200, "all")
        await self.db.run("UPDATE autopurge_channels SET next_run = next_run - 3000")
        moved = (await self.row())["next_run"]
        self.assertLess(moved, first["next_run"])
        again = await self.auto.set_channel(self.guild.id, self.room.id, 7200, "bots")
        self.assertEqual(again["next_run"], moved)
        self.assertEqual(again["message_type"], "bots")

    async def test_a_new_interval_starts_the_count_again(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 7200, "all")
        await self.db.run("UPDATE autopurge_channels SET next_run = next_run - 3000")
        again = await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        self.assertGreater(again["next_run"], time.time() + 3000)


class RowStatusTest(AutoPurgeBase):
    async def test_a_channel_level_deny_shows_on_its_row(self):
        class Denied(PurgeChannel):
            def permissions_for(self, member):
                return FakePermissions("view_channel")
        locked = Denied(self.guild, "locked")
        await self.auto.set_channel(self.guild.id, locked.id, 3600, "all")
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        rows = await (await (await self.client()).get(self.path("/autopurge"))).json()
        self.assertEqual({row["channel_id"]: row["can_purge"] for row in rows}, {str(locked.id): False, str(self.room.id): True})

    async def test_a_deleted_channel_leaves_the_list_at_once(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        await self.auto.on_guild_channel_delete(self.room)
        self.assertIsNone(await self.db.one("SELECT * FROM autopurge_channels"))
        self.assertIsNone(await self.bot.timers.get_key("autopurge.run", f"{self.guild.id}:{self.room.id}"))

    async def test_a_channel_deleted_while_the_bot_was_away_is_marked(self):
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        self.guild.channels.remove(self.room)
        rows = await (await (await self.client()).get(self.path("/autopurge"))).json()
        self.assertTrue(rows[0]["gone"])


class HiddenChannelTest(SiteCase):
    modules = [AutoPurge]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.auto = self.cog(AutoPurge)
        self.secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.me.id})
        self.deleter = FakeMember(self.guild, "deleter", self.guild.role("Deleter", 4, "manage_guild", "view_channel", "manage_messages"))

    async def test_the_command_refuses_a_channel_they_cant_see(self):
        ctx = self.context(self.deleter)
        await self.auto.autopurge_set.callback(self.auto, ctx, self.secret, 3600, "all")
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertEqual(await self.auto._rows(self.guild.id), [])

    async def test_setting_a_purge_takes_manage_messages_on_both_sides(self):
        ctx = self.context(self.manager)
        await self.auto.autopurge_set.callback(self.auto, ctx, self.channel, 3600, "all")
        self.assertIn("Manage Messages", ctx.fails[0])
        client = await self.site(self.manager)
        response = await client.put(self.path(f"/autopurge/{self.channel.id}"), json={"interval_seconds": 3600, "message_type": "all"})
        self.assertEqual(response.status, 400)
        self.assertIn("Manage Messages", (await response.json())["error"])
        self.assertEqual(await self.auto._rows(self.guild.id), [])

    async def test_a_manager_can_still_change_the_interval_of_a_row_someone_set_up(self):
        await self.auto.set_channel(self.guild.id, self.channel.id, 3600, "all")
        client = await self.site(self.manager)
        response = await client.put(self.path(f"/autopurge/{self.channel.id}"), json={"interval_seconds": 7200, "message_type": "all"})
        self.assertEqual(response.status, 200, await response.text())

    async def test_someone_who_holds_it_passes(self):
        ctx = self.context(self.deleter)
        await self.auto.autopurge_set.callback(self.auto, ctx, self.channel, 3600, "all")
        self.assertEqual(ctx.fails, [])

    async def test_the_rule_for_a_hidden_channel_stays_out_of_sight_and_reach(self):
        await self.auto.set_channel(self.guild.id, self.secret.id, 3600, "all")
        client = await self.site(self.manager)
        self.assertEqual(await (await client.get(self.path("/autopurge"))).json(), [])
        self.assertEqual((await client.delete(self.path(f"/autopurge/{self.secret.id}"))).status, 404)
        self.assertEqual(len(await self.auto._rows(self.guild.id)), 1)
        ctx = self.context(self.manager)
        await self.auto.autopurge.callback(self.auto, ctx)
        self.assertNotIn(str(self.secret.id), ctx.sent[0]["content"])
        ctx = self.context(self.manager)
        await self.auto.autopurge_remove.callback(self.auto, ctx, channel=self.secret)
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        ctx = self.context(self.manager)
        await self.auto.autopurge_remove.callback(self.auto, ctx, channel=None)
        self.assertIn("Stopped purging 0", ctx.oks[0])
        self.assertEqual(len(await self.auto._rows(self.guild.id)), 1)

    async def test_an_administrator_sees_and_removes_it(self):
        await self.auto.set_channel(self.guild.id, self.secret.id, 3600, "all")
        client = await self.site(self.admin)
        self.assertEqual(len(await (await client.get(self.path("/autopurge"))).json()), 1)
        self.assertEqual((await client.delete(self.path(f"/autopurge/{self.secret.id}"))).status, 200)

    async def test_a_rule_for_a_channel_thats_gone_stays_removable(self):
        await self.auto.set_channel(self.guild.id, 111222333444555666, 3600, "all")
        client = await self.site(self.manager)
        self.assertEqual(len(await (await client.get(self.path("/autopurge"))).json()), 1)
        self.assertEqual((await client.delete(self.path("/autopurge/111222333444555666"))).status, 200)


class WithStickyTest(AutoPurgeBase):
    modules = [AutoPurge, Sticky]

    async def test_the_channels_sticky_stays_through_a_purge(self):
        sticky = self.cog(Sticky)
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        await sticky.create(self.guild.id, self.room.id, {"content": "Read the rules", "embeds": []}, seconds=None, actor=self.bot.actor(self.admin))
        self.post(self.member, "chatter")
        await self.auto.set_channel(self.guild.id, self.room.id, 3600, "all")
        await self.bot.timers.run_due(time.time() + 3601)
        await self.finish()
        self.assertEqual([m.content for m in self.room.sent], ["Read the rules"])
        sticky.cog_unload()
