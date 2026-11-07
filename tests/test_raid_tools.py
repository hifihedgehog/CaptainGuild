"""The tools for a raid on a big server: a mass ban by filter with a preview, raid mode, and lists of what's running."""
from __future__ import annotations

import asyncio
import datetime
import time
from types import SimpleNamespace
from typing import Any
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.ops import DiscordOps
from captainguild.modules import moderation as moderation_module
from captainguild.modules.moderation import MassBanPreview, ModerationError, Someone, name_fits, parse_ban_filters, send_private
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import FakeChannel, FakeMember, FakeMessage, FakePermissions, new_id
from tests.test_moderation import ModBase

NOW = datetime.datetime.now(datetime.timezone.utc)


def ago(**kwargs: Any) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**kwargs)


class ParseTest(ModBase):
    def test_filters_then_the_reason(self):
        filters, reason = parse_ban_filters('joined:10m created:1d name:free*nitro noroles spam raid accounts')
        self.assertEqual(filters, {"joined": "10m", "created": "1d", "name": "free*nitro", "noroles": ""})
        self.assertEqual(reason, "spam raid accounts")

    def test_quotes_keep_text_together(self):
        filters, _ = parse_ban_filters('contains:"free nitro" search:500')
        self.assertEqual(filters, {"contains": "free nitro", "search": "500"})

    def test_a_named_reason(self):
        filters, reason = parse_ban_filters("noavatar reason:raid accounts")
        self.assertEqual((filters, reason), ({"noavatar": ""}, "raid accounts"))

    def test_a_word_like_a_filter_that_isnt_one_is_refused(self):
        with self.assertRaises(ModerationError) as caught:
            parse_ban_filters("created:1d joind:10m")
        self.assertIn("joind", str(caught.exception))

    def test_a_reason_alone_is_no_filter(self):
        self.assertEqual(parse_ban_filters("spam accounts")[0], {})

    def test_name_patterns(self):
        self.assertTrue(name_fits("nitro", "FREE NITRO"))
        self.assertTrue(name_fits("free*nitro", "free_some_nitro"))
        self.assertFalse(name_fits("nitro*free", "free nitro"))
        self.assertTrue(name_fits("x", None, "x"))
        self.assertFalse(name_fits("x", None, ""))


class MassBanBase(ModBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.fresh = [FakeMember(self.guild, f"raider{index}", joined=ago(minutes=2), created=ago(hours=3)) for index in range(3)]
        self.member.joined_at = ago(days=300)
        self.member.created_at = ago(days=900)
        for person in (self.owner, self.admin, self.mod, self.me):
            person.joined_at = ago(days=500)
            person.created_at = ago(days=900)

    async def preview(self, words: str, author: Any = None) -> Any:
        ctx = self.context(author or self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, words, reason=None)
        return ctx


class PreviewTest(MassBanBase):
    async def test_nothing_happens_until_someone_presses(self):
        ctx = await self.preview("joined:10m raid wave")
        self.assertEqual(self.guild.banned, [])
        self.assertEqual(await self.cases(), [])
        sent = ctx.sent[0]
        self.assertIn("bans 3 people", sent["content"])
        self.assertIn("raid wave", sent["content"])
        self.assertIsInstance(sent["view"], MassBanPreview)

    async def test_the_press_bans_who_the_preview_showed_and_counts(self):
        ctx = await self.preview("joined:10m raid wave")
        view = ctx.sent[0]["view"]
        latecomer = FakeMember(self.guild, "latecomer", joined=ago(seconds=1))
        report = await self.cog_.run_massban(self.guild, self.admin, view.matched, view.reason)
        self.assertEqual(sorted(self.guild.banned), sorted(person.id for person in self.fresh))
        self.assertNotIn(latecomer.id, self.guild.banned)
        self.assertIn("Banned 3 of 3", report)
        rows = await self.cases()
        self.assertEqual([row["action"] for row in rows], ["ban"] * 3)
        self.assertEqual(rows[0]["reason"], "raid wave")

    async def test_someone_already_banned_shows_in_the_count(self):
        ctx = await self.preview("joined:10m")
        view = ctx.sent[0]["view"]
        self.guild.banned.append(self.fresh[0].id)
        report = await self.cog_.run_massban(self.guild, self.admin, view.matched, view.reason)
        self.assertIn("Banned 2 of 3", report)
        self.assertIn("1 didn't go through", report)

    async def test_a_rank_change_after_the_preview_counts(self):
        ctx = await self.preview("joined:10m")
        view = ctx.sent[0]["view"]
        self.fresh[0].roles.append(self.role_admin)
        report = await self.cog_.run_massban(self.guild, self.admin, view.matched, view.reason)
        self.assertNotIn(self.fresh[0].id, self.guild.banned)
        self.assertIn("Banned 2 of 3", report)

    async def test_a_long_list_comes_as_a_file(self):
        for index in range(25):
            FakeMember(self.guild, f"more{index}", joined=ago(minutes=1))
        ctx = await self.preview("joined:10m")
        self.assertIn("file", ctx.sent[0])
        self.assertLessEqual(len(ctx.sent[0]["content"]), 2000)

    async def test_too_many_refuses(self):
        with mock.patch.object(moderation_module, "MASSBAN_MOST", 2):
            ctx = await self.preview("joined:10m")
        self.assertIn("over the limit", ctx.fails[0])
        self.assertEqual(ctx.sent, [])

    async def test_only_the_person_who_ran_it_can_press(self):
        ctx = await self.preview("joined:10m")
        view = ctx.sent[0]["view"]
        told: list[str] = []

        async def send_message(text: str, **kwargs: Any) -> None:
            told.append(text)
        stranger = SimpleNamespace(user=self.mod, response=SimpleNamespace(send_message=send_message))
        self.assertFalse(await view.interaction_check(stranger))
        self.assertTrue(told)
        self.assertTrue(await view.interaction_check(SimpleNamespace(user=self.admin, response=None)))

    async def test_no_filter_and_no_ids_refuses(self):
        ctx = await self.preview("spam accounts")
        self.assertIn("at least one ID", ctx.fails[0])

    async def test_a_big_run_pauses_between_batches(self):
        people = [Someone(new_id(), f"p{index}") for index in range(5)]
        with mock.patch.object(moderation_module, "BULK_BAN_CHUNK", 2), mock.patch.object(moderation_module.asyncio, "sleep", new=mock.AsyncMock()) as pause:
            banned = await self.cog_._bulk_ban(self.guild, people, "x", 0)
        self.assertEqual(len(banned), 5)
        self.assertEqual(pause.await_count, 2)
        self.assertEqual(pause.await_args.args[0], moderation_module.BULK_BAN_PAUSE)


class FilterTest(MassBanBase):
    def matched(self, ctx: Any) -> set[int]:
        return {person.id for person in ctx.sent[0]["view"].matched} if ctx.sent else set()

    async def test_joined(self):
        ctx = await self.preview("joined:10m")
        self.assertEqual(self.matched(ctx), {person.id for person in self.fresh})

    async def test_created(self):
        young = FakeMember(self.guild, "young", joined=ago(days=30), created=ago(hours=1))
        ctx = await self.preview("created:30m")
        self.assertEqual(self.matched(ctx), set())
        ctx = await self.preview("created:2h")
        self.assertEqual(self.matched(ctx), {young.id})

    async def test_two_filters_both_apply(self):
        self.fresh[0].created_at = ago(days=400)
        ctx = await self.preview("joined:10m created:1d")
        self.assertEqual(self.matched(ctx), {self.fresh[1].id, self.fresh[2].id})

    async def test_name(self):
        ctx = await self.preview("name:raider1")
        self.assertEqual(self.matched(ctx), {self.fresh[1].id})

    async def test_a_nickname_counts_for_name(self):
        self.member.nick = "free nitro"
        ctx = await self.preview("name:nitro")
        self.assertEqual(self.matched(ctx), {self.member.id})

    async def test_no_roles(self):
        ctx = await self.preview("noroles")
        self.assertIn(self.fresh[0].id, self.matched(ctx))
        self.assertNotIn(self.member.id, self.matched(ctx))

    async def test_no_avatar(self):
        self.fresh[0].avatar = None
        ctx = await self.preview("noavatar")
        self.assertEqual(self.matched(ctx), {self.fresh[0].id})

    async def test_after_someone(self):
        first = FakeMember(self.guild, "first", joined=ago(minutes=30))
        later = FakeMember(self.guild, "later", joined=ago(minutes=20))
        ctx = await self.preview(f"after:<@{first.id}>")
        self.assertIn(later.id, self.matched(ctx))
        self.assertNotIn(first.id, self.matched(ctx))
        self.assertNotIn(self.member.id, self.matched(ctx))

    async def test_a_length_it_cant_read_refuses(self):
        ctx = await self.preview("joined:soon")
        self.assertIn("needs a length", ctx.fails[0])

    async def test_staff_bots_and_anyone_above_are_left_out_and_counted(self):
        FakeMember(self.guild, "bot", joined=ago(minutes=1), bot=True)
        helper = FakeMember(self.guild, "helper", self.guild.role("Helper", 1, "manage_messages"), joined=ago(minutes=1))
        ctx = await self.preview("joined:10m")
        self.assertNotIn(helper.id, self.matched(ctx))
        self.assertIn("left out", ctx.sent[0]["content"])

    async def test_nobody_matches_says_so(self):
        ctx = await self.preview("name:zzzzzz")
        self.assertIn("Nobody matches", ctx.oks[0])

    async def test_it_needs_the_whole_member_list(self):
        with mock.patch.object(moderation_module, "whole_member_list", new=mock.AsyncMock(return_value=False)):
            ctx = await self.preview("joined:10m")
        self.assertIn("whole member list", ctx.fails[0])


class MessageFilterTest(MassBanBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.channel.sent.append(FakeMessage(self.channel, self.fresh[0], "free NITRO here"))
        self.channel.sent.append(FakeMessage(self.channel, self.member, "hello there"))

    async def test_contains_reads_the_channel(self):
        ctx = await self.preview("contains:nitro")
        self.assertEqual({person.id for person in ctx.sent[0]["view"].matched}, {self.fresh[0].id})

    async def test_someone_who_posted_and_left_still_counts(self):
        gone = Someone(new_id(), "gone")
        gone.created_at = ago(hours=1)
        self.channel.sent.append(FakeMessage(self.channel, gone, "free nitro again"))
        ctx = await self.preview("contains:nitro joined:10m")
        self.assertIn(gone.id, {person.id for person in ctx.sent[0]["view"].matched})

    async def test_it_refuses_a_channel_they_cant_read(self):
        outer = self
        watcher = FakeMember(self.guild, "watcher", self.guild.role("Bans", 8, "ban_members"))

        class Hidden(FakeChannel):
            def permissions_for(self, member: Any) -> Any:
                return FakePermissions() if member is watcher else member.guild_permissions
        hidden = Hidden(outer.guild, "hidden")
        ctx = await self.preview(f"contains:nitro channel:<#{hidden.id}>", watcher)
        self.assertEqual(ctx.fails, ["You can't see that channel."])

    async def test_it_stands_down_without_message_content(self):
        self.bot.reads_message_text = False
        ctx = await self.preview("contains:nitro")
        self.assertIn("Message Content", ctx.fails[0])


class RaidModeTest(ModBase):
    async def turn_on(self, *args: Any) -> Any:
        ctx = self.context(self.admin)
        await self.cog_.raid_on_command.callback(self.cog_, ctx, *(args or (None,)), reason=None)
        return ctx

    async def test_someone_joining_is_held(self):
        await self.turn_on()
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        self.assertIn(self.role_muted, joiner.roles)
        self.assertEqual(await self.cases(), [])
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_off_means_nobody_is_held(self):
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        self.assertNotIn(self.role_muted, joiner.roles)

    async def test_a_bot_isnt_held(self):
        await self.turn_on()
        joiner = FakeMember(self.guild, "joiner", bot=True)
        await self.cog_.on_member_join(joiner)
        self.assertNotIn(self.role_muted, joiner.roles)

    async def test_someone_on_the_rules_screen_waits(self):
        await self.turn_on()
        joiner = FakeMember(self.guild, "joiner", pending=True)
        await self.cog_.on_member_join(joiner)
        self.assertNotIn(self.role_muted, joiner.roles)
        before = SimpleNamespace(pending=True, timed_out_until=None, get_role=joiner.get_role)
        joiner.pending = False
        await self.cog_.on_member_update(before, joiner)
        self.assertIn(self.role_muted, joiner.roles)

    async def test_leaving_doesnt_shed_a_hold(self):
        await self.turn_on()
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        await self.cog_.raid_off(self.guild, self.admin)
        joiner.roles = [self.guild.default_role]
        await self.cog_.on_member_join(joiner)
        self.assertIn(self.role_muted, joiner.roles)

    async def test_release_lets_them_go_and_counts(self):
        await self.turn_on()
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        ctx = self.context(self.admin)
        await self.cog_.raid_release.callback(self.cog_, ctx)
        self.assertNotIn(self.role_muted, joiner.roles)
        self.assertIn("Let 1 person go", ctx.oks[0])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM raid_holds"), 0)

    async def test_release_leaves_a_real_mute_on(self):
        await self.turn_on()
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        await self.cog_.punish(self.guild, joiner, "mute", moderator=self.admin)
        await self.cog_.raid_release.callback(self.cog_, self.context(self.admin))
        self.assertIn(self.role_muted, joiner.roles)

    async def test_it_ends_on_time_and_tells_staff(self):
        await self.turn_on("1h")
        self.modlog.sent.clear()
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        await self.bot.timers.run_due(time.time() + 3700)
        self.assertIsNone(await self.db.one("SELECT * FROM raid_modes"))
        self.assertIn("1 person is still held", self.modlog.sent[-1].embeds[0].description)
        second = FakeMember(self.guild, "second")
        await self.cog_.on_member_join(second)
        self.assertNotIn(self.role_muted, second.roles)

    async def test_it_needs_a_mute_role(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": None})
        ctx = await self.turn_on()
        self.assertIn("no mute role", ctx.fails[0])
        self.assertIsNone(await self.db.one("SELECT * FROM raid_modes"))

    async def test_taking_the_role_off_by_hand_lets_them_go_with_no_case(self):
        await self.turn_on()
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        before = SimpleNamespace(pending=False, timed_out_until=None, get_role=lambda role_id: self.role_muted if role_id == self.role_muted.id else None)
        joiner.roles.remove(self.role_muted)
        await self.cog_.on_member_update(before, joiner)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM raid_holds"), 0)
        self.assertEqual(await self.cases(), [])

    async def test_massban_raid_shows_who_is_held(self):
        await self.turn_on()
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        ctx = self.context(self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, "raid", reason=None)
        view = ctx.sent[0]["view"]
        self.assertEqual([person.id for person in view.matched], [joiner.id])
        self.assertEqual(view.reason, "Raid")

    async def test_the_mute_role_never_comes_back_through_sticky_roles(self):
        self.assertIn(self.role_muted.id, self.cog_.transient_roles(self.guild.id))
        old = self.guild.role("Old", 1)
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": old.id})
        self.assertIn(self.role_muted.id, self.bot.transient_roles(self.guild.id))


class JoinRateTest(ModBase):
    async def test_a_fast_wave_tells_staff_once(self):
        self.modlog.sent.clear()
        for index in range(moderation_module.JOIN_ALERT_COUNT + 3):
            await self.cog_.on_member_join(FakeMember(self.guild, f"j{index}"))
        told = [message for message in self.modlog.sent if message.embeds and "joining fast" in message.embeds[0].title]
        self.assertEqual(len(told), 1)
        self.assertIn("/raid on", told[0].embeds[0].description)

    async def test_a_slow_trickle_doesnt(self):
        self.modlog.sent.clear()
        with mock.patch.object(moderation_module, "JOIN_ALERT_SECONDS", 0):
            for index in range(moderation_module.JOIN_ALERT_COUNT + 3):
                await self.cog_.on_member_join(FakeMember(self.guild, f"j{index}"))
        self.assertEqual(self.modlog.sent, [])

    async def test_raid_mode_already_on_needs_no_notice(self):
        await self.cog_.raid_on(self.guild, self.admin, None, None)
        self.modlog.sent.clear()
        for index in range(moderation_module.JOIN_ALERT_COUNT + 1):
            await self.cog_.on_member_join(FakeMember(self.guild, f"j{index}"))
        self.assertEqual(self.modlog.sent, [])


class ListsTest(ModBase):
    async def test_mutes_lists_who_and_when(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin, seconds=3600)
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.mutes.callback(self.cog_, ctx)
        description = ctx.sent[0]["embed"].description
        self.assertIn("member", description)
        self.assertIn("<t:", description)

    async def test_a_past_end_kept_for_someone_gone_isnt_listed(self):
        await self.db.run("INSERT INTO active_mutes (guild_id, user_id, kind, role_id, removed_roles, started_at, expires_at) VALUES (?, ?, 'mute', 1, '[]', 1, 2)", (self.guild.id, new_id()))
        self.assertEqual((await self.cog_.running(self.guild.id))["mutes"], [])

    async def test_someone_gone_shows_by_their_last_name(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin)
        self.guild.members.remove(self.member)
        row = (await self.cog_.running(self.guild.id))["mutes"][0]
        self.assertEqual((row["name"], row["here"]), ("member", False))
        self.assertNotIn("known_name", row)

    async def test_tempbans_lists_them(self):
        person = Someone(new_id(), "person")
        await self.cog_.punish(self.guild, person, "tempban", moderator=self.admin, seconds=3600)
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.tempbans.callback(self.cog_, ctx)
        self.assertIn(str(person.id), ctx.sent[0]["embed"].description)

    async def test_lockdown_list(self):
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin), self.channel, "2h", reason="raid")
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.lockdown_list.callback(self.cog_, ctx)
        self.assertIn(self.channel.mention, ctx.sent[0]["embed"].description)
        self.assertIn("raid", ctx.sent[0]["embed"].description)

    async def test_a_long_list_stays_within_an_embed(self):
        for _ in range(100):
            await self.db.run("INSERT INTO active_mutes (guild_id, user_id, kind, role_id, removed_roles, started_at, moderator_name) VALUES (?, ?, 'mute', 1, '[]', 1, ?)", (self.guild.id, new_id(), "x" * 50))
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.mutes.callback(self.cog_, ctx)
        self.assertLessEqual(len(ctx.sent[0]["embed"].description), 4096)
        self.assertIn("more", ctx.sent[0]["embed"].description)


class SendPrivateTest(ModBase):
    async def test_a_slash_reply_stays_ephemeral(self):
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await send_private(ctx, "record")
        self.assertTrue(ctx.sent[0]["ephemeral"])

    async def test_a_staff_channel_answers_there(self):
        shut = FakeChannel(self.guild, "staff")
        self.guild.default_role.permissions = FakePermissions()
        ctx = self.context(self.admin, shut)
        await send_private(ctx, "record")
        self.assertEqual(ctx.sent[0]["content"], "record")

    async def test_a_public_channel_goes_to_dms(self):
        ctx = self.context(self.admin)
        await send_private(ctx, "record")
        self.assertEqual(ctx.sent, [])
        self.assertEqual(self.admin.dms[0]["content"], "record")
        self.assertIn("DMs", ctx.oks[0])

    async def test_dms_closed_leaves_it_private(self):
        async def closed(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(mock.Mock(status=403, reason="no"), "closed")
        self.admin.send = closed
        ctx = self.context(self.admin)
        await send_private(ctx, "record")
        self.assertEqual(ctx.sent, [])
        self.assertIn("stayed private", ctx.fails[0])

    async def test_notes_typed_in_public_dont_post_there(self):
        await self.cog_.add_note(self.guild.id, self.member.id, self.admin, "watch this one")
        ctx = self.context(self.admin)
        await self.cog_.notes.callback(self.cog_, ctx, member=self.member)
        self.assertEqual(ctx.sent, [])
        self.assertIn("watch this one", self.admin.dms[0]["embed"].description)


class DashboardTest(ModBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.helper_role = self.guild.role("Helper", 4, "view_channel")
        self.helper = FakeMember(self.guild, "helper", self.helper_role)
        await self.bot.settings.save(self.guild.id, "core", {"dashboard_staff_roles": [self.helper_role.id]})

    async def site(self, member: Any) -> TestClient:
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def test_the_running_tab_lists_all_of_it(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin, seconds=3600)
        person = Someone(new_id(), "person")
        await self.cog_.punish(self.guild, person, "tempban", moderator=self.admin, seconds=3600)
        await self.cog_._lock_channel(self.guild, self.channel, self.admin, "raid", 3600)
        client = await self.site(self.helper)
        response = await client.get(self.path("/moderation/running"))
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(str(body["mutes"][0]["user_id"]), str(self.member.id))
        self.assertEqual(str(body["bans"][0]["user_id"]), str(person.id))
        self.assertEqual(str(body["lockdowns"][0]["channel_id"]), str(self.channel.id))

    async def test_a_channel_staff_cant_see_stays_off_the_list(self):
        helper = self.helper

        class Hidden(FakeChannel):
            def permissions_for(self, member: Any) -> Any:
                return FakePermissions() if member is helper else member.guild_permissions
        hidden = Hidden(self.guild, "hidden")
        await self.cog_._lock_channel(self.guild, hidden, self.admin, None, None)
        client = await self.site(self.helper)
        body = await (await client.get(self.path("/moderation/running"))).json()
        self.assertEqual(body["lockdowns"], [])

    async def test_lifting_a_mute_takes_what_the_command_takes(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin)
        client = await self.site(self.helper)
        response = await client.post(self.path(f"/moderation/running/mutes/{self.member.id}/lift"), json={})
        self.assertEqual(response.status, 403)
        self.assertIn(self.role_muted, self.member.roles)
        self.helper_role.permissions = FakePermissions("view_channel", "manage_roles")
        response = await client.post(self.path(f"/moderation/running/mutes/{self.member.id}/lift"), json={"reason": "fine"})
        self.assertEqual(response.status, 200)
        self.assertNotIn(self.role_muted, self.member.roles)
        self.assertEqual((await self.cases())[-1]["reason"], "fine")

    async def test_lifting_a_ban_needs_ban_members(self):
        person = Someone(new_id(), "person")
        await self.cog_.punish(self.guild, person, "tempban", moderator=self.admin, seconds=3600)
        client = await self.site(self.helper)
        response = await client.post(self.path(f"/moderation/running/bans/{person.id}/lift"), json={})
        self.assertEqual(response.status, 403)
        self.helper_role.permissions = FakePermissions("view_channel", "ban_members")
        response = await client.post(self.path(f"/moderation/running/bans/{person.id}/lift"), json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.guild.banned, [])

    async def test_lifting_a_lockdown_needs_manage_channels(self):
        await self.cog_._lock_channel(self.guild, self.channel, self.admin, None, None)
        client = await self.site(self.helper)
        response = await client.post(self.path(f"/moderation/running/lockdowns/{self.channel.id}/lift"), json={})
        self.assertEqual(response.status, 403)
        self.helper_role.permissions = FakePermissions("view_channel", "manage_channels")
        response = await client.post(self.path(f"/moderation/running/lockdowns/{self.channel.id}/lift"), json={})
        self.assertEqual(response.status, 200)
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    async def test_raid_mode_from_the_page(self):
        client = await self.site(self.admin)
        response = await client.post(self.path("/moderation/raid"), json={"action": "on", "seconds": 600, "reason": "wave"})
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(await self.db.one("SELECT * FROM raid_modes"))
        joiner = FakeMember(self.guild, "joiner")
        await self.cog_.on_member_join(joiner)
        response = await client.post(self.path("/moderation/raid"), json={"action": "release"})
        self.assertEqual((await response.json())["released"], 1)
        response = await client.post(self.path("/moderation/raid"), json={"action": "off"})
        self.assertIsNone(await self.db.one("SELECT * FROM raid_modes"))

    async def test_a_big_release_answers_the_page_at_once_and_says_when_its_done(self):
        client = await self.site(self.admin)
        role = self.guild.role("Muted", 1)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": role.id})
        gate = asyncio.Event()
        people = [FakeMember(self.guild, f"held{number}", role) for number in range(moderation_module.RELEASE_INLINE + 1)]

        async def waits(*roles, **kwargs):
            await gate.wait()

        for person in people:
            person.remove_roles = waits
            await self.db.run("INSERT INTO raid_holds (guild_id, user_id, user_name, role_id, held_at) VALUES (?, ?, ?, ?, ?)", (self.guild.id, person.id, person.name, role.id, 1.0))
        told = []

        async def tell(guild, title, body):
            told.append(body)

        with mock.patch.object(self.cog_, "_tell_staff", tell):
            response = await asyncio.wait_for(client.post(self.path("/moderation/raid"), json={"action": "release"}), 5)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["releasing"], len(people))
            self.assertEqual(told, [])
            gate.set()
            await asyncio.gather(*self.cog_._background)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM raid_holds"), 0)
        self.assertIn(str(len(people)), told[0])

    async def test_raid_mode_needs_what_the_command_needs(self):
        client = await self.site(self.helper)
        response = await client.post(self.path("/moderation/raid"), json={"action": "on"})
        self.assertEqual(response.status, 403)

    async def test_applying_the_mute_role_needs_the_permissions(self):
        manager = FakeMember(self.guild, "manager", self.guild.role("Manager", 4, "manage_guild", "view_channel"))
        client = await self.site(manager)
        response = await client.post(self.path("/moderation/mute-role/apply"), json={})
        self.assertEqual(response.status, 403)
        client = await self.site(self.admin)
        response = await client.post(self.path("/moderation/mute-role/apply"), json={})
        self.assertEqual(response.status, 200)
        for task in list(self.cog_._background):
            await task
        self.assertIs(self.channel.overwrites_for(self.role_muted).send_messages, False)

    async def test_a_case_export_streams_past_one_chunk(self):
        for index in range(12):
            await self.cog_.record_case(self.guild, "warn", Someone(new_id(), f"p{index}"), self.admin, post=False)
        client = await self.site(self.admin)
        with mock.patch.object(moderation_module, "EXPORT_CHUNK", 5):
            response = await client.get(self.path("/cases/export?format=json"))
            rows = await response.json(content_type=None)
            self.assertEqual(len(rows), 12)
            self.assertEqual(rows[0]["number"], 12)
            response = await client.get(self.path("/cases/export?format=csv"))
            self.assertEqual(len((await response.text()).strip().splitlines()), 13)

    async def test_odd_digits_in_a_filter_box_read_as_text(self):
        client = await self.site(self.admin)
        for query in ("number=%C2%B2", "q=%C2%B2", "user=" + "%C2%B2" * 16, "moderator=" + "1" * 30):
            response = await client.get(self.path(f"/cases?{query}"))
            self.assertEqual(response.status, 200, query)


class PreviewEdgesTest(MassBanBase):
    async def test_the_bot_needs_ban_members(self):
        self.role_bot.permissions = FakePermissions("manage_roles")
        ctx = await self.preview("joined:10m")
        self.assertIn("Ban Members", ctx.fails[0])

    async def test_a_preview_that_runs_out_says_so(self):
        ctx = await self.preview("joined:10m")
        view = ctx.sent[0]["view"]
        await view.on_timeout()
        self.assertIn("ran out", view.message.content)
        self.assertEqual(self.guild.banned, [])

    async def test_for_isnt_near_a_channel_called_forum(self):
        FakeChannel(self.guild, "forum")
        ctx = self.context(self.admin)
        await self.cog_.lockdown.callback(self.cog_, ctx, None, "for", reason="2h")
        self.assertEqual(ctx.fails, [])
        self.assertIsNotNone(await self.db.one("SELECT * FROM lockdowns"))
