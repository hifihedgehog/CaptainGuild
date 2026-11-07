"""Reaction role messages: who may change them, what comes off when, and what a message says about its own roles."""
import asyncio
import time
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core.ops import OpsError
from captainguild.core.validate import Invalid
from captainguild.modules.embeds import Embeds
from captainguild.modules.logs import Logs
from captainguild.modules.reactionroles import RR, Option, ReactionRoles, Work, clean_emoji, emoji_key, parse_pairs
from tests.fakes import FakeChannel, FakeMember, FakePermissions, _FakeResponse
from tests.test_reactionroles import BLUE, GREEN, RED, Base

YELLOW = "\N{LARGE YELLOW CIRCLE}"


class Noticed(Base):
    def notices(self) -> list[str]:
        return [row[1] for row in self.told]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.told: list[tuple] = []
        real = self.rr._tell_staff

        def tell(guild_id, topic, text):
            self.told.append((topic, text))
            real(guild_id, topic, text)

        self.rr._tell_staff = tell


class StaffMember(Base):
    def staffer(self, position: int = 2, *permissions: str) -> FakeMember:
        role = self.guild.role("Staff", position, "manage_guild", "view_channel", "send_messages", "read_message_history", *permissions)
        return FakeMember(self.guild, "staffer", role)


class OpeningAGateTest(StaffMember):
    """A role a person can't hand out by adding it, they can't reach by taking a gate off the message that hands it out."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.high = self.guild.role("High", 8)
        self.trusted = self.guild.role("Trusted", 1)
        self.actor = self.staffer()

    async def test_clearing_a_whitelist_is_refused(self):
        rr = await self.make((RED, self.high), whitelist=[self.trusted.id])
        ctx = self.context(self.actor)
        await self.rr._edit_list(ctx, "whitelist", "clear", str(rr.message_id), None)
        self.assertIn("highest role", ctx.fails[0])
        self.assertEqual((await self.rr.get(rr.message_id)).whitelist, [self.trusted.id])

    async def test_adding_to_a_whitelist_is_refused(self):
        rr = await self.make((RED, self.high), whitelist=[self.trusted.id])
        ctx = self.context(self.actor)
        await self.rr._edit_list(ctx, "whitelist", "add", str(rr.message_id), "Staff")
        self.assertIn("highest role", ctx.fails[0])

    async def test_shrinking_a_whitelist_lets_fewer_in_and_is_allowed(self):
        other = self.guild.role("Other", 1)
        rr = await self.make((RED, self.high), whitelist=[self.trusted.id, other.id])
        ctx = self.context(self.actor)
        await self.rr._edit_list(ctx, "whitelist", "remove", str(rr.message_id), "Other")
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.rr.get(rr.message_id)).whitelist, [self.trusted.id])

    async def test_taking_a_role_off_a_blacklist_is_refused(self):
        rr = await self.make((RED, self.high), blacklist=[self.actor.top_role.id])
        ctx = self.context(self.actor)
        await self.rr._edit_list(ctx, "blacklist", "clear", str(rr.message_id), None)
        self.assertIn("highest role", ctx.fails[0])

    async def test_unlocking_is_refused(self):
        rr = await self.make((RED, self.high), kind="lock")
        ctx = self.context(self.actor)
        await self.rr._set_kind(ctx, str(rr.message_id), "normal")
        self.assertIn("highest role", ctx.fails[0])
        self.assertEqual((await self.rr.get(rr.message_id)).kind, "lock")

    async def test_someone_above_every_role_can_open_it(self):
        rr = await self.make((RED, self.red), kind="lock", whitelist=[self.trusted.id])
        low = self.guild.role("Low", 1)
        rr2 = await self.make((BLUE, low), kind="lock")
        actor = self.staffer(2)
        ctx = self.context(actor)
        await self.rr._set_kind(ctx, str(rr2.message_id), "normal")
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.rr.get(rr2.message_id)).kind, "normal")
        self.assertEqual((await self.rr.get(rr.message_id)).kind, "lock")

    async def test_an_administrator_can_open_it(self):
        rr = await self.make((RED, self.high), kind="lock")
        ctx = self.context(self.admin)
        await self.rr._set_kind(ctx, str(rr.message_id), "normal")
        self.assertEqual(ctx.fails, [])

    async def test_the_form_refuses_the_same_way(self):
        rr = await self.make((RED, self.high), whitelist=[self.trusted.id])
        self.ops.member = lambda guild_id, user_id: {"role_ids": [self.actor.top_role.id]} if user_id == self.admin.id else None
        client = await self.client()
        form = {"type": "normal", "mode": "reactions", "whitelist": [], "blacklist": [], "rows": [{"emoji": RED, "roles": [str(self.high.id)]}]}
        response = await client.patch(self.path(f"/reactionroles/{rr.message_id}"), json=form)
        self.assertEqual(response.status, 400)
        self.assertIn("whitelist", (await response.json())["fields"])


class ChannelsTest(StaffMember):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.secret = FakeChannel(self.guild, "secret")
        self.ops.channels.append(self._row(self.secret))
        self.secret.permissions_for = lambda who: FakePermissions("administrator") if who.guild_permissions.administrator else FakePermissions()
        self.small = self.guild.role("Small", 1)
        self.actor = self.staffer(2)

    async def on_secret(self) -> RR:
        rr = RR(0, self.guild.id, self.secret.id, created_at=time.time(), options=[Option(0, RED, emoji_key(RED), [self.small.id])])
        sent = await self.ops.send_message(self.secret.id, {"content": "x", "embeds": [], "buttons": []})
        rr.message_id = sent["id"]
        await self.rr._save(rr, None, fresh=True)
        return rr

    async def test_all_in_one_wont_post_where_the_person_cant_see(self):
        before = set(self.ops.messages)
        ctx = self.context(self.actor)
        await self.rr.rr_aio.callback(self.rr, ctx, "default", "none", text=f"Hidden | text {RED} Small", channel=self.secret)
        self.assertEqual(set(self.ops.messages), before)
        self.assertEqual(ctx.fails, ["You can't see that channel."])

    async def test_all_in_one_wont_post_where_the_person_cant_post(self):
        readonly = FakeChannel(self.guild, "readonly")
        self.ops.channels.append(self._row(readonly))
        readonly.permissions_for = lambda who: FakePermissions("view_channel")
        before = set(self.ops.messages)
        ctx = self.context(self.actor)
        await self.rr.rr_aio.callback(self.rr, ctx, "default", "none", text=f"Hidden | text {RED} Small", channel=readonly)
        self.assertEqual(set(self.ops.messages), before)
        self.assertEqual(ctx.fails, ["You can't post in that channel."])

    async def test_a_tracked_message_in_a_hidden_channel_cant_be_changed(self):
        rr = await self.on_secret()
        for action in (
            lambda ctx: self.rr.rr_edit.callback(self.rr, ctx, str(rr.message_id), message_string="new | text"),
            lambda ctx: self.rr.rr_selfdestruct.callback(self.rr, ctx, str(rr.message_id), when="1h"),
            lambda ctx: self.rr.rr_move.callback(self.rr, ctx, str(rr.message_id), str(rr.message_id)),
            lambda ctx: self.rr._set_kind(ctx, str(rr.message_id), "unique"),
            lambda ctx: self.rr.rr_color.callback(self.rr, ctx, str(rr.message_id), "ff0000"),
        ):
            ctx = self.context(self.actor)
            await action(ctx)
            self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertEqual((await self.rr.get(rr.message_id)).kind, "normal")

    async def test_the_owner_passes(self):
        rr = await self.on_secret()
        ctx = self.context(self.owner)
        await self.rr._set_kind(ctx, str(rr.message_id), "unique")
        self.assertEqual(ctx.fails, [])

    async def test_a_listing_leaves_out_hidden_channels(self):
        await self.on_secret()
        visible = await self.make((BLUE, self.small))
        ctx = self.context(self.actor)
        await self.rr._show(ctx, None)
        text = ctx.sent[0]["embed"].description
        self.assertIn(str(visible.message_id), text)
        self.assertNotIn(str(self.secret.id), text)
        admin_ctx = self.context(self.admin)
        await self.rr._show(admin_ctx, None)
        self.assertIn(str(self.secret.id), admin_ctx.sent[0]["embed"].description)

    async def test_the_form_wont_post_where_the_person_cant_post(self):
        readonly = FakeChannel(self.guild, "readonly")
        self.ops.channels.append(self._row(readonly))
        client = await self.client()
        self.admin.roles = [self.guild.default_role, self.guild.role("Mgr", 2, "manage_guild", "view_channel")]
        before = set(self.ops.messages)
        form = {"create_mode": "post", "channel_id": str(readonly.id), "payload": {"content": "hello"}, "type": "normal", "mode": "reactions", "rows": [{"emoji": RED, "roles": [str(self.small.id)]}]}
        response = await client.post(self.path("/reactionroles"), json=form)
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"]["channel_id"], "You can't post in that channel.")
        self.assertEqual(set(self.ops.messages), before)

    async def test_setup_wont_take_a_channel_the_person_cant_post_in(self):
        answers = iter([f"<#{self.secret.id}>", "cancel"])
        said = []

        async def wait_for(event, *, check, timeout):
            return SimpleNamespace(content=next(answers), author=self.actor, channel=self.channel)

        ctx = self.context(self.actor)
        real_send = ctx.send

        async def send(content=None, **kwargs):
            said.append(content)
            return await real_send(content, **kwargs)

        ctx.send = send
        with mock.patch.object(self.bot, "wait_for", wait_for):
            self.assertIsNone(await self.rr._ask_channel(ctx))
        self.assertIn("You can't see that channel.", said[1])


class ColorTest(Base):
    async def test_another_features_buttons_stay(self):
        sent = await self.ops.send_message(self.channel.id, {"content": "", "embeds": [{"title": "Vote", "color": 1}], "buttons": []}, components=[{"type": "button", "custom_id": "other:vote", "label": "Vote", "style": "primary"}])
        self.ops.messages[sent["id"]]["controls"] = 1
        ctx = self.context()
        await self.rr.rr_color.callback(self.rr, ctx, str(sent["id"]), "ff0000")
        self.assertIn("another feature", ctx.fails[0])
        self.assertEqual(self.ops.messages[sent["id"]]["payload"]["embeds"][0]["color"], 1)

    async def test_a_role_message_keeps_its_own_controls(self):
        rr = await self.make((RED, self.red), mode="buttons")
        stored = self.ops.messages[rr.message_id]
        stored["payload"] = {"content": "", "embeds": [{"title": "Pick", "color": 1}], "buttons": []}
        stored["controls"] = 1
        ctx = self.context()
        await self.rr.rr_color.callback(self.rr, ctx, str(rr.message_id), "ff0000")
        self.assertEqual(ctx.fails, [])
        self.assertEqual(stored["payload"]["embeds"][0]["color"], 0xff0000)


class SetupWithoutMessageTextTest(Base):
    async def test_it_says_so_and_points_to_the_dashboard(self):
        self.bot.reads_message_text = False
        ctx = self.context()
        with mock.patch.object(self.bot, "wait_for", side_effect=AssertionError("it waited for a reply it can't read")):
            await self.rr.rr_setup.callback(self.rr, ctx)
        self.assertIn("Message Content", ctx.fails[0])
        self.assertIn(f"/dashboard/{self.guild.id}/reactionroles", ctx.fails[0])


class BlockedRemovalTest(Noticed):
    async def test_a_reaction_taken_off_while_the_role_is_out_of_reach_stays_on_record_and_the_fix_works(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.red.position = self.me.top_role.position + 5
        await self.remove(rr, RED, member)
        self.assertIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE user_id = ?", (member.id,)), 1)
        self.red.position = 3
        await self.rr.rr_fix.callback(self.rr, self.context(), str(rr.message_id))
        await self.rr.settle()
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE user_id = ?", (member.id,)), 0)


class TemporaryEndsTest(Noticed):
    async def picked(self) -> tuple[RR, FakeMember]:
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=60)
        member = self.newcomer()
        await self.add(rr, RED, member)
        return rr, member

    async def test_above_the_bot_the_timer_stays_the_reaction_stays_and_staff_hear(self):
        rr, member = await self.picked()
        self.red.position = self.me.top_role.position + 5
        await self.bot.timers.run_due(time.time() + 120)
        self.assertIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_temp'"), 1)
        self.assertEqual(self.reactors(rr, RED), [member.id])
        self.assertIn("highest role", self.notices()[0])

    async def test_a_refusal_keeps_the_timer_and_a_retry_finishes_it(self):
        rr, member = await self.picked()
        real = member.remove_roles

        async def refuse(*roles, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Missing Permissions")

        member.remove_roles = refuse
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_temp'"), 1)
        self.assertEqual(self.reactors(rr, RED), [member.id])
        member.remove_roles = real
        await self.bot.timers.run_due(time.time() + 7200)
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_temp'"), 0)
        self.assertEqual(self.reactors(rr, RED), [])

    async def test_someone_who_no_longer_holds_it_ends_quietly(self):
        rr, member = await self.picked()
        await member.remove_roles(self.red)
        calls = len(member.calls)
        self.red.position = self.me.top_role.position + 5
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(len(member.calls), calls)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_temp'"), 0)


class LeftoverTokenTest(Base):
    async def test_a_removal_after_a_new_reaction_counts(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue), kind="unique")
        member = self.newcomer(self.red)  # a moderator gave Red, so there's no reaction to take off
        await self.add(rr, BLUE, member)
        self.assertEqual(len(self.rr._own_removals), 1)
        await self.add(rr, RED, member)
        self.assertIn(self.red, member.roles)
        await self.remove(rr, RED, member)
        self.assertNotIn(self.red, member.roles)


class DropButtonTest(Base):
    async def test_someone_holding_one_of_its_roles_loses_it(self):
        rr = await self.make((RED, [self.red, self.blue]), kind="drop", mode="buttons")
        member = self.newcomer(self.red)
        text = await self.click(rr, member, 0)
        self.assertIn("no longer have", text)
        self.assertNotIn(self.red, member.roles)

    async def test_someone_holding_neither_is_told(self):
        rr = await self.make((RED, [self.red, self.blue]), kind="drop", mode="buttons")
        self.assertIn("nobody can pick it up", await self.click(rr, self.newcomer(), 0))


class TextFormsTest(Base):
    async def test_latest_skips_the_command_itself(self):
        above = await self.ops.send_message(self.channel.id, {"content": "the post", "embeds": [], "buttons": []})
        ctx = self.context()
        command = await self.ops.send_message(self.channel.id, {"content": "!rr add", "embeds": [], "buttons": []})
        ctx.message.id = command["id"]
        await self.rr.rr_add.callback(self.rr, ctx, RED, "Red", role="")
        self.assertEqual(ctx.fails, [])
        self.assertIsNotNone(await self.rr.get(above["id"]))
        self.assertIsNone(await self.rr.get(command["id"]))

    async def test_a_slash_command_has_no_message_to_skip(self):
        above = await self.ops.send_message(self.channel.id, {"content": "the post", "embeds": [], "buttons": []})
        ctx = self.context()
        ctx.interaction = object()
        ctx.message.id = 1
        place = await self.rr._locate(ctx, "latest")
        self.assertEqual(place[1], above["id"])

    async def test_a_name_starting_with_a_keeps_its_letter(self):
        self.assertEqual(clean_emoji("apple:123456789012345678"), "<:apple:123456789012345678>")
        self.assertEqual(clean_emoji("a:apple:123456789012345678"), "<a:apple:123456789012345678>")
        self.assertEqual(clean_emoji(":apple:123456789012345678"), "<:apple:123456789012345678>")
        self.assertEqual(clean_emoji("a:123456789012345678"), "<:a:123456789012345678>")

    async def test_slow_commands_answer_first(self):
        rr = await self.make((RED, self.red))
        calls = [
            (self.rr.rr_add, (str(rr.message_id), BLUE), {"role": "Blue"}),
            (self.rr.rr_remove, (), {"role": "Blue", "message_id": str(rr.message_id)}),
            (self.rr.rr_purge, (str(rr.message_id),), {}),
            (self.rr.rr_edit, (str(rr.message_id),), {"message_string": "a | b"}),
            (self.rr.rr_color, (str(rr.message_id), "ff0000"), {}),
            (self.rr.rr_modify, ("normal", str(rr.message_id)), {}),
            (self.rr.rr_normal, (str(rr.message_id),), {}),
            (self.rr.rr_temp, (str(rr.message_id),), {"duration": "1h"}),
            (self.rr.rr_limit, (str(rr.message_id), 2), {}),
        ]
        for command, args, kwargs in calls:
            ctx = self.context()
            order = []
            ctx.defer = mock.AsyncMock(side_effect=lambda **_: order.append("defer"))
            with mock.patch.object(self.rr, "get", side_effect=lambda *a, **k: order.append("work") or asyncio.sleep(0)):
                try:
                    await command.callback(self.rr, ctx, *args, **kwargs)
                except Exception:
                    pass
            self.assertEqual(order[:1], ["defer"], command.name)

    async def test_long_lists_are_cut_to_fit_a_message(self):
        ctx = self.context()
        rr = await self.make((RED, self.red))
        with mock.patch.object(self.rr, "_react", mock.AsyncMock(return_value="x" * 400)):
            await self.rr._add_pairs(ctx, str(rr.message_id), [(emoji, self.blue) for emoji in (BLUE, GREEN, YELLOW, "\N{LARGE ORANGE CIRCLE}", "\N{LARGE PURPLE CIRCLE}", "\N{LARGE BROWN CIRCLE}")])
        self.assertLessEqual(len(ctx.oks[0]), 2000)
        ctx = self.context()
        await self.rr._edit_list(ctx, "whitelist", "add", str(rr.message_id), " ".join(["x" * 200] * 30))
        self.assertLessEqual(len(ctx.fails[0]), 2000)

    async def test_the_all_in_one_warns_about_a_role_the_bot_wont_hand_out(self):
        strong = self.guild.role("Strong", 3, "ban_members")
        ctx = self.context()
        await self.rr.rr_aio.callback(self.rr, ctx, "default", "none", text=f"Title | text {RED} Strong")
        self.assertIn("elevated roles", ctx.oks[0])
        self.assertTrue(any(strong.id in rr.role_ids for rr in await self.rr.for_guild(self.guild.id)))

    def test_the_addmany_example_reads(self):
        description = self.rr.rr_addmany.app_command.parameters[1].description
        example = description.split(": ", 1)[1]
        self.guild.role("Guest", 1)
        pairs, problem = parse_pairs(self.guild, example)
        self.assertIsNone(problem)
        self.assertEqual(len(pairs), 2)

    async def test_a_long_title_with_emoji_reads_fast(self):
        reads = []
        real = list(self.guild.roles)

        class Counting(type(self.guild)):
            @property
            def roles(self):
                reads.append(1)
                return real

            @roles.setter
            def roles(self, value):
                pass

        self.guild.__class__ = Counting
        from captainguild.modules.reactionroles import split_text_and_pairs
        split_text_and_pairs(self.guild, " ".join([RED] * 40) + f" Title {RED} Red")
        self.assertEqual(len(reads), 1)


class SelfDestructTest(StaffMember):
    async def test_it_takes_manage_messages(self):
        rr = await self.make((RED, self.red))
        actor = self.staffer(5)
        ctx = self.context(actor)
        await self.rr.rr_selfdestruct.callback(self.rr, ctx, str(rr.message_id), when="1h")
        self.assertIn("Manage Messages", ctx.fails[0])
        self.assertIsNone((await self.rr.get(rr.message_id)).destruct_at)

    async def test_turning_it_off_doesnt(self):
        rr = await self.make((RED, self.red), destruct_at=time.time() + 3600)
        actor = self.staffer(5)
        ctx = self.context(actor)
        await self.rr.rr_selfdestruct.callback(self.rr, ctx, str(rr.message_id), when="off")
        self.assertEqual(ctx.fails, [])
        self.assertIsNone((await self.rr.get(rr.message_id)).destruct_at)

    async def test_with_manage_messages_it_runs(self):
        rr = await self.make((RED, self.red))
        actor = self.staffer(5, "manage_messages")
        ctx = self.context(actor)
        await self.rr.rr_selfdestruct.callback(self.rr, ctx, str(rr.message_id), when="1h")
        self.assertEqual(ctx.fails, [])

    async def test_the_form_refuses_too(self):
        rr = await self.make((RED, self.red))
        client = await self.client()
        self.admin.roles = [self.guild.default_role, self.guild.role("Mgr", 5, "manage_guild", "view_channel")]
        form = {"type": "normal", "mode": "reactions", "rows": [{"emoji": RED, "roles": [str(self.red.id)]}], "destruct_seconds": 3600}
        response = await client.patch(self.path(f"/reactionroles/{rr.message_id}"), json=form)
        self.assertEqual(response.status, 400)
        self.assertIn("destruct_seconds", (await response.json())["fields"])


class LinkFormTest(Base):
    async def test_linking_to_one_of_a_group_keeps_the_rest_of_it(self):
        b = await self.make((BLUE, self.blue))
        c = await self.make((GREEN, self.green))
        await self.rr._link_group(self.guild.id, [b, c])
        d = await self.make((YELLOW, self.verified))
        req = SimpleNamespace(guild_id=self.guild.id)
        rr = await self.rr.get(d.message_id)
        await self.rr._apply_links(req, rr, await self.rr._read_links(req, rr, {"linked": [str(b.message_id)]}))
        groups = {row["message_id"]: row["group_id"] for row in await self.db.all("SELECT message_id, group_id FROM rr_messages")}
        self.assertIsNotNone(groups[d.message_id])
        self.assertEqual(groups[c.message_id], groups[d.message_id])
        self.assertEqual(groups[b.message_id], groups[d.message_id])

    async def test_a_menu_dropped_from_a_group_takes_many_picks_again(self):
        a = await self.make((RED, self.red), (BLUE, self.blue), mode="select")
        b = await self.make((GREEN, self.green), mode="select")
        c = await self.make((YELLOW, self.verified), ("\N{LARGE PURPLE CIRCLE}", self.guild.role("P", 2)), mode="select")
        req = SimpleNamespace(guild_id=self.guild.id)
        first = await self.rr.get(a.message_id)
        await self.rr._apply_links(req, first, [b, c])
        self.assertEqual(self.ops.messages[c.message_id]["components"][0]["max"], 1)
        first = await self.rr.get(a.message_id)
        await self.rr._apply_links(req, first, [await self.rr.get(b.message_id)])
        self.assertIsNone((await self.rr.get(c.message_id)).group_id)
        self.assertEqual(self.ops.messages[c.message_id]["components"][0]["max"], 2)

    async def test_a_message_leaving_a_group_of_two_redraws_both_menus(self):
        a = await self.make((RED, self.red), (BLUE, self.blue), mode="select")
        b = await self.make((GREEN, self.green), (YELLOW, self.verified), mode="select")
        req = SimpleNamespace(guild_id=self.guild.id)
        await self.rr._apply_links(req, await self.rr.get(a.message_id), [b])
        await self.rr._apply_links(req, await self.rr.get(a.message_id), [])
        self.assertEqual(self.ops.messages[a.message_id]["components"][0]["max"], 2)
        self.assertEqual(self.ops.messages[b.message_id]["components"][0]["max"], 2)

    async def test_a_bad_list_posts_nothing(self):
        client = await self.client()
        before = set(self.ops.messages)
        form = {"create_mode": "post", "channel_id": str(self.channel.id), "payload": {"content": "hello"}, "type": "normal", "mode": "reactions", "linked": ["123456789012345678"], "rows": [{"emoji": RED, "roles": [str(self.red.id)]}]}
        response = await client.post(self.path("/reactionroles"), json=form)
        self.assertEqual(response.status, 400)
        self.assertIn("linked", (await response.json())["fields"])
        self.assertEqual(set(self.ops.messages), before)
        self.assertEqual(await self.rr.for_guild(self.guild.id), [])


class MoveTest(Base):
    async def buttons(self, count: int = 25) -> RR:
        roles = [self.guild.role(f"R{i}", 1) for i in range(count)]
        rr = RR(0, self.guild.id, self.channel.id, mode="buttons", created_at=time.time(), options=[Option(i, "", f"row{i}", [role.id]) for i, role in enumerate(roles)])
        sent = await self.ops.send_message(self.channel.id, {"content": "x", "embeds": [], "buttons": []}, components=self.rr.render(rr))
        rr.message_id = sent["id"]
        await self.rr._save(rr, None, fresh=True)
        return rr

    async def test_a_full_message_wont_move_onto_a_post_with_a_link_button(self):
        rr = await self.buttons()
        target = await self.ops.send_message(self.channel.id, {"content": "t", "embeds": [], "buttons": [{"label": "Site", "url": "https://example.com"}]})
        with self.assertRaisesRegex(Invalid, "25 buttons"):
            await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, target["id"])
        self.assertIsNotNone(await self.rr.get(rr.message_id))
        self.assertIsNone(await self.rr.get(target["id"]))

    async def test_an_edit_discord_refuses_leaves_the_record_where_it_was(self):
        rr = await self.buttons(2)
        target = await self.ops.send_message(self.channel.id, {"content": "t", "embeds": [], "buttons": []})
        with mock.patch.object(self.ops, "edit_message", side_effect=OpsError("no")):
            with self.assertRaises(OpsError):
                await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, target["id"])
        self.assertIsNotNone(await self.rr.get(rr.message_id))
        self.assertIsNone(await self.rr.get(target["id"]))

    async def test_a_reader_never_sees_a_moved_message_with_no_rows(self):
        rr = await self.make((RED, self.red))
        target = await self.ops.send_message(self.channel.id, {"content": "t", "embeds": [], "buttons": []})
        seen = []
        stop = asyncio.Event()

        async def read():
            while not stop.is_set():
                found = await self.rr.get(target["id"])
                if found is not None:
                    seen.append(len(found.options))
                await asyncio.sleep(0)

        reader = asyncio.ensure_future(read())
        await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, target["id"])
        stop.set()
        await reader
        self.assertNotIn(0, seen)
        self.assertEqual(len((await self.rr.get(target["id"])).options), 1)


class CatchUpRaceTest(Base):
    async def racing(self, rr: RR, during) -> None:
        real = self.ops.reaction_users
        fired = []

        async def listing(*args, **kwargs):
            page = await real(*args, **kwargs)
            if not fired and page:
                fired.append(1)
                await during()
            return page

        self.ops.reaction_users = listing
        await self.rr.reconcile_message(rr.message_id, thorough=True)
        await self.rr.settle()
        self.ops.reaction_users = real

    async def test_someone_who_took_it_off_during_the_read_doesnt_get_it_back(self):
        rr = await self.make((RED, self.red))
        await self.add(rr, RED, self.newcomer())
        member = self.newcomer()
        self.ops.react_as(rr.message_id, RED, member.id, bot=False)  # while the bot was down

        async def during():
            await self.rr.on_raw_reaction_remove(self.unreacted(rr, RED, member))

        await self.racing(rr, during)
        self.assertNotIn(self.red, member.roles)

    async def test_someone_who_reacted_again_during_the_read_keeps_it(self):
        rr = await self.make((RED, self.red))
        keeper = self.newcomer()
        await self.add(rr, RED, keeper)
        other = self.newcomer()
        await self.add(rr, RED, other)
        self.unreacted(rr, RED, keeper)  # while the bot was down

        async def during():
            await self.rr.on_raw_reaction_add(self.reacted(rr, RED, keeper))

        await self.racing(rr, during)
        self.assertIn(self.red, keeper.roles)

    async def test_a_baseline_keeps_a_reaction_recorded_during_the_read(self):
        rr = await self.make((RED, self.red), fresh=False)
        await self.rr.settle()
        await self.db.run("DELETE FROM rr_synced WHERE message_id = ?", (rr.message_id,))
        late = self.newcomer()  # a lower ID than anyone on the page already read, so the list never shows them
        early = self.newcomer()
        self.ops.react_as(rr.message_id, RED, early.id, bot=False)

        async def during():
            await self.rr.on_raw_reaction_add(self.reacted(rr, RED, late))
            await self.rr.settle()

        await self.racing(rr, during)
        rows = {row["user_id"] for row in await self.db.all("SELECT user_id FROM rr_seen WHERE message_id = ?", (rr.message_id,))}
        self.assertIn(late.id, rows)
        self.assertIn(early.id, rows)

    async def test_a_ready_during_a_pass_runs_one_more(self):
        passes = []
        gate = asyncio.Event()

        async def run(*, thorough=True):
            passes.append(thorough)
            if len(passes) == 1:
                await gate.wait()

        with mock.patch.object(self.rr, "reconcile_all", run):
            await self.rr.on_ready()
            await asyncio.sleep(0)
            await self.rr.on_ready()
            gate.set()
            await self.rr._reconciling
        self.assertEqual(passes, [True, False])


class DeletedThingsTest(Noticed):
    async def test_a_tracked_emoji_deleted_tells_staff_and_names_the_message(self):
        custom = "<:party:123456789012345678>"
        rr = await self.make((custom, self.red))
        emoji = SimpleNamespace(id=123456789012345678)
        await self.rr.on_guild_emojis_update(self.guild, [emoji], [])
        self.assertEqual(len(self.notices()), 1)
        self.assertIn(rr.url(), self.notices()[0])

    async def test_an_emoji_nothing_uses_says_nothing(self):
        await self.make((RED, self.red))
        await self.rr.on_guild_emojis_update(self.guild, [SimpleNamespace(id=5)], [])
        self.assertEqual(self.notices(), [])

    async def test_a_role_deleted_during_downtime_comes_off_at_catch_up(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue))
        self.guild.roles.remove(self.red)
        await self.rr.reconcile_message(rr.message_id)
        await self.rr.settle()
        final = await self.rr.get(rr.message_id)
        self.assertEqual([o.key for o in final.options], [emoji_key(BLUE)])
        self.assertIn("no longer exist", self.notices()[0])

    async def test_a_message_left_with_no_roles_stops(self):
        rr = await self.make((RED, self.red))
        self.guild.roles.remove(self.red)
        await self.rr.reconcile_message(rr.message_id)
        self.assertIsNone(await self.rr.get(rr.message_id))


class OffTempTest(Base):
    async def test_moving_a_message_off_temp_ends_its_timers(self):
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=60)
        member = self.newcomer()
        await self.add(rr, RED, member)
        ctx = self.context()
        await self.rr._set_kind(ctx, str(rr.message_id), "normal")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_temp'"), 0)
        await self.bot.timers.run_due(time.time() + 120)
        self.assertIn(self.red, member.roles)

    async def test_a_role_held_for_good_doesnt_start_counting_down(self):
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=60)
        member = self.newcomer(self.red)
        await self.add(rr, RED, member)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_temp'"), 0)
        await self.bot.timers.run_due(time.time() + 120)
        self.assertIn(self.red, member.roles)

    async def test_picking_it_again_while_it_counts_down_moves_the_end(self):
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=60)
        member = self.newcomer()
        await self.add(rr, RED, member)
        first = (await self.bot.timers.get_key("rr_temp", f"{self.guild.id}:{member.id}:{self.red.id}"))["due_at"]
        await self.rr.on_raw_reaction_add(self.reacted(rr, RED, member))
        await self.rr.settle()
        self.assertGreaterEqual((await self.bot.timers.get_key("rr_temp", f"{self.guild.id}:{member.id}:{self.red.id}"))["due_at"], first)

    async def test_another_temp_message_with_the_role_keeps_them(self):
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=60)
        await self.make((BLUE, self.red), kind="temp", temp_seconds=60)
        await self.add(rr, RED, self.newcomer())
        await self.rr._set_kind(self.context(), str(rr.message_id), "normal")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_temp'"), 1)


class ImportTest(Base):
    async def test_a_reaction_the_bot_couldnt_add_is_reported(self):
        sent = await self.ops.send_message(self.channel.id, {"content": "x", "embeds": [], "buttons": []})
        with mock.patch.object(self.rr, "_react", mock.AsyncMock(return_value="The bot couldn't react.")):
            result = await self.rr.import_rows(self.guild.id, {"rr": [{"message_id": str(sent["id"]), "messages": [{"channel_id": str(self.channel.id), "reaction_emoji": RED, "role_id": str(self.red.id)}]}]})
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["warnings"], [{"message_id": str(sent["id"]), "reason": "The bot couldn't react."}])

    async def test_one_row_discord_refuses_doesnt_stop_the_rest(self):
        one = await self.ops.send_message(self.channel.id, {"content": "x", "embeds": [], "buttons": []})
        two = await self.ops.send_message(self.channel.id, {"content": "y", "embeds": [], "buttons": []})
        real = self.rr._save

        async def save(rr, previous=None, **kwargs):
            if rr.message_id == one["id"]:
                raise OpsError("Discord refused.")
            return await real(rr, previous, **kwargs)

        row = lambda sent: {"message_id": str(sent["id"]), "messages": [{"channel_id": str(self.channel.id), "reaction_emoji": RED, "role_id": str(self.red.id)}]}
        with mock.patch.object(self.rr, "_save", save):
            result = await self.rr.import_rows(self.guild.id, {"rr": [row(one), row(two)]})
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"][0]["reason"], "Discord refused.")


class HistoryTest(Base):
    modules = [ReactionRoles, Embeds]

    async def versions(self, message_id: int) -> list[str]:
        embeds = self.cog(Embeds)
        row = await embeds._find(self.guild.id, message_id)
        rows = await self.db.all("SELECT note FROM posted_versions WHERE guild_id = ? AND posted_id = ? ORDER BY number", (self.guild.id, row["id"]))
        return [r["note"] for r in rows]

    async def test_an_edit_from_the_command_adds_a_version(self):
        ctx = self.context()
        await self.rr.rr_aio.callback(self.rr, ctx, "default", "none", text=f"Title | text {RED} Red")
        rr = (await self.rr.for_guild(self.guild.id))[0]
        await self.rr.rr_edit.callback(self.rr, self.context(), str(rr.message_id), message_string="New | words")
        self.assertEqual(await self.versions(rr.message_id), ["Posted", "Edited"])

    async def test_an_edit_from_the_form_adds_a_version(self):
        client = await self.client()
        form = {"create_mode": "post", "channel_id": str(self.channel.id), "payload": {"content": "first"}, "type": "normal", "mode": "reactions", "rows": [{"emoji": RED, "roles": [str(self.red.id)]}]}
        created = await (await client.post(self.path("/reactionroles"), json=form)).json()
        message_id = int(created["message_id"])
        edit = {"payload": {"content": "second"}, "type": "normal", "mode": "reactions", "rows": [{"emoji": RED, "roles": [str(self.red.id)]}]}
        self.assertEqual((await client.patch(self.path(f"/reactionroles/{message_id}"), json=edit)).status, 200)
        self.assertEqual(await self.versions(message_id), ["Posted", "Edited"])


class RolesBlockTest(Base):
    def stored(self, rr: RR) -> dict:
        return self.ops.messages[rr.message_id]["payload"]

    async def all_in_one(self, text: str) -> RR:
        ctx = self.context()
        await self.rr.rr_aio.callback(self.rr, ctx, "default", "none", text=text)
        self.assertEqual(ctx.fails, [])
        return (await self.rr.for_guild(self.guild.id))[-1]

    async def test_it_lists_each_emoji_and_role(self):
        rr = await self.all_in_one(f"Pick | choose: {{roles}} {RED} Red {BLUE} Blue")
        text = self.stored(rr)["embeds"][0]["description"]
        self.assertNotIn("{roles}", text)
        self.assertIn(f"{RED} <@&{self.red.id}>", text)
        self.assertIn(f"{BLUE} <@&{self.blue.id}>", text)

    async def test_a_title_gets_names_since_it_cant_show_mentions(self):
        rr = await self.all_in_one(f"Pick {{roles}} {RED} Red")
        title = self.stored(rr)["embeds"][0]["title"]
        self.assertIn(f"{RED} Red", title)
        self.assertNotIn("<@&", title)

    async def test_a_new_row_updates_the_list(self):
        rr = await self.all_in_one(f"Pick | choose: {{roles}} {RED} Red")
        await self.rr._add_pairs(self.context(), str(rr.message_id), [(BLUE, self.blue)])
        text = self.stored(rr)["embeds"][0]["description"]
        self.assertIn(f"{BLUE} <@&{self.blue.id}>", text)
        self.assertIn(f"{RED} <@&{self.red.id}>", text)

    async def test_a_deleted_role_comes_off_the_list(self):
        rr = await self.all_in_one(f"Pick | choose: {{roles}} {RED} Red {BLUE} Blue")
        await self.rr.on_guild_role_delete(self.red)
        await self.rr.settle()
        self.assertNotIn(str(self.red.id), self.stored(rr)["embeds"][0]["description"])

    async def test_the_form_shows_what_was_written(self):
        rr = await self.all_in_one(f"Pick | choose: {{roles}} {RED} Red")
        client = await self.client()
        body = await (await client.get(self.path(f"/reactionroles/{rr.message_id}/message"))).json()
        self.assertIn("{roles}", body["payload"]["embeds"][0]["description"])

    async def test_a_form_edit_with_the_block_fills_it(self):
        rr = await self.all_in_one(f"Pick | choose {RED} Red")
        self.assertIsNone(rr.template)
        client = await self.client()
        form = {"payload": {"content": "Roles: {roles}"}, "type": "normal", "mode": "reactions", "rows": [{"emoji": RED, "roles": [str(self.red.id)]}]}
        self.assertEqual((await client.patch(self.path(f"/reactionroles/{rr.message_id}"), json=form)).status, 200)
        self.assertEqual(self.stored(rr)["content"], f"Roles: {RED} <@&{self.red.id}>")
        self.assertIsNotNone((await self.rr.get(rr.message_id)).template)

    async def test_a_message_with_no_block_is_left_alone(self):
        rr = await self.all_in_one(f"Pick | choose {RED} Red")
        before = dict(self.stored(rr))
        await self.rr._add_pairs(self.context(), str(rr.message_id), [(BLUE, self.blue)])
        self.assertEqual(self.stored(rr), before)

    async def test_it_survives_a_reload_and_a_move(self):
        rr = await self.all_in_one(f"Pick | choose: {{roles}} {RED} Red")
        target = await self.ops.send_message(self.channel.id, {"content": "t", "embeds": [], "buttons": []})
        await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, target["id"])
        self.assertIsNotNone((await self.rr.get(target["id"])).template)
