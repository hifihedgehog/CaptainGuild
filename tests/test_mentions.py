"""Mass mention: who gets pinged, how the pings are packed, and what stops one."""
from __future__ import annotations

import asyncio
import re
from unittest import mock

import discord

from captainguild.core.module import required_permissions
from captainguild.modules import mentions as module
from captainguild.modules.mentions import CONFIRM_OVER, MESSAGE_LIMIT, MOST_MEMBERS, USERS_PER_MESSAGE, Mentions, header_line, pack
from captainguild.modules.roles import Confirm
from captainguild.core.perms import Decision
from tests.fakes import BotTest, FakeMember, FakePermissions
from tests.gateway import GatewayTest, iso, new_id as gateway_id, user_json

MENTION = re.compile(r"<@(\d+)>")


class Base(BotTest):
    modules = [Mentions]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.mentions = self.cog(Mentions)
        self.mod.roles.append(self.guild.role("Pinger", 3, "view_channel", "send_messages", "mention_everyone"))  # what running it in a channel takes
        self.red = self.guild.role("Red", 3)
        self.blue = self.guild.role("Blue", 3)
        self.both = FakeMember(self.guild, "both", self.red, self.blue)
        self.red_only = FakeMember(self.guild, "red only", self.red)
        self.blue_only = FakeMember(self.guild, "blue only", self.blue)

    async def run_command(self, ctx, *roles) -> None:
        await self.mentions.mass_mention.callback(self.mentions, ctx, *roles)

    def pinged(self, ctx) -> list[int]:
        return [int(user_id) for sent in ctx.sent if not sent.get("view") for user_id in MENTION.findall(sent["content"] or "")]

    def crowd(self, count: int, *roles) -> list[FakeMember]:
        return [FakeMember(self.guild, f"m{index}", *roles) for index in range(count)]

    async def answered(self, ctx, roles, answer) -> None:
        task = asyncio.create_task(self.run_command(ctx, *roles))
        while not any(sent.get("view") for sent in ctx.sent) and not task.done():
            await asyncio.sleep(0)
        view = next(sent["view"] for sent in ctx.sent if sent.get("view"))
        view.answer = answer
        view.stop()
        await task


class WhoTest(Base):
    async def test_only_members_with_every_role(self):
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, self.red, self.blue)
        self.assertEqual(self.pinged(ctx), [self.both.id])

    async def test_one_role_pings_everyone_with_it(self):
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, self.red)
        self.assertEqual(sorted(self.pinged(ctx)), sorted([self.both.id, self.red_only.id]))

    async def test_bots_are_left_out(self):
        robot = FakeMember(self.guild, "robot", self.red, self.blue, bot=True)
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, self.red, self.blue)
        self.assertNotIn(robot.id, self.pinged(ctx))
        self.assertIn(self.both.id, self.pinged(ctx))

    async def test_nobody_matching_says_so(self):
        lonely = self.guild.role("Lonely", 3)
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, lonely, self.red)
        self.assertEqual(ctx.sent, [])
        self.assertIn("Nobody holds all of those roles", ctx.fails[0])

    async def test_the_same_role_twice_counts_once(self):
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, self.red, self.red)
        self.assertEqual(ctx.sent[0]["content"].splitlines()[0], "Pinging every member with the role Red.")

    async def test_the_first_line_names_the_roles_and_pings_no_role(self):
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, self.red, self.blue)
        first = ctx.sent[0]
        self.assertEqual(first["content"].splitlines()[0], "Pinging every member with all of these roles: Red, Blue.")
        allowed = first["allowed_mentions"]
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.assertEqual([user.id for user in allowed.users], [self.both.id])

    async def test_a_role_name_cant_ping(self):
        sneaky = self.guild.role("@everyone <@&1> **bold**", 3)
        FakeMember(self.guild, "s", sneaky)
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, sneaky)
        line = ctx.sent[0]["content"].splitlines()[0]
        self.assertNotIn("@everyone", line.replace("@" + chr(0x200B) + "everyone", ""))  # a zero-width space after the @ keeps it from pinging
        self.assertIn("\\*\\*bold\\*\\*", line)

    async def test_it_is_recorded_in_the_activity_log(self):
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, self.red, self.blue)
        row = await self.db.one("SELECT * FROM activity WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual(row["action"], "mass_mention")
        self.assertEqual(row["detail"], "Red, Blue: 1 members")
        self.assertEqual(row["user_id"], self.mod.id)


class PackTest(Base):
    async def test_every_message_fits_and_no_mention_splits(self):
        ids = [10 ** 18 + index for index in range(950)]
        messages = pack("Header line.", ids)
        self.assertTrue(messages[0][0].startswith("Header line.\n"))
        seen = []
        for text, carried in messages:
            self.assertLess(len(text), MESSAGE_LIMIT)
            self.assertLessEqual(len(carried), USERS_PER_MESSAGE)
            found = [int(user_id) for user_id in MENTION.findall(text)]
            self.assertEqual(found, carried)
            self.assertEqual(MENTION.sub("", text.removeprefix("Header line.")).strip(), "")
            seen += found
        self.assertEqual(seen, ids)

    async def test_a_message_stops_just_under_the_limit(self):
        # A header of 1,978 characters leaves room for exactly one 20-character mention after the line break: 1,999 in all.
        header = "x" * 1978
        messages = pack(header, [10 ** 16, 10 ** 16 + 1])
        self.assertEqual(len(messages[0][0]), 1999)
        self.assertEqual(messages[0][1], [10 ** 16])
        self.assertEqual(messages[1], (f"<@{10 ** 16 + 1}>", [10 ** 16 + 1]))
        # One more character would make exactly 2,000, so the mention moves to the next message.
        messages = pack("x" * 1979, [10 ** 16])
        self.assertEqual(messages, [("x" * 1979, []), (f"<@{10 ** 16}>", [10 ** 16])])

    async def test_short_ids_stop_at_100_users(self):
        messages = pack("h", list(range(10 ** 14, 10 ** 14 + 150)))
        self.assertEqual([len(ids) for _, ids in messages], [100, 50])

    async def test_each_message_allows_only_its_own_users(self):
        crowd = self.crowd(120, self.red, self.blue)
        with mock.patch.object(module, "CONFIRM_OVER", 1000):
            ctx = self.context(author=self.mod)
            await self.run_command(ctx, self.red, self.blue)
        self.assertGreater(len(ctx.sent), 1)
        for sent in ctx.sent:
            self.assertEqual([user.id for user in sent["allowed_mentions"].users], [int(user_id) for user_id in MENTION.findall(sent["content"])])
        self.assertEqual(sorted(self.pinged(ctx)), sorted([self.both.id, *(m.id for m in crowd)]))

    async def test_a_long_list_of_long_names_fits_the_first_line(self):
        roles = [self.guild.role(f"{'n' * 95}{index}", 3) for index in range(25)]
        line = header_line(roles)
        self.assertLess(len(line), 1600)
        self.assertTrue(line.endswith("more."))


class LimitsTest(Base):
    async def test_over_fifty_asks_first_and_pings_on_yes(self):
        crowd = self.crowd(CONFIRM_OVER, self.red)
        ctx = self.context(author=self.mod)
        await self.answered(ctx, [self.red], True)
        prompt = next(sent for sent in ctx.sent if sent.get("view"))
        self.assertIn(f"{CONFIRM_OVER + 2} members", prompt["content"])
        self.assertTrue(prompt["ephemeral"])
        self.assertEqual(len(self.pinged(ctx)), len(crowd) + 2)

    async def test_no_pings_without_a_yes(self):
        self.crowd(CONFIRM_OVER, self.red)
        for answer in (False, None):
            ctx = self.context(author=self.mod)
            await self.answered(ctx, [self.red], answer)
            self.assertEqual(self.pinged(ctx), [])
            self.assertIn("Nobody was pinged.", ctx.fails)

    async def test_fifty_goes_without_asking(self):
        self.crowd(CONFIRM_OVER - 2, self.red)
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, self.red)
        self.assertFalse(any(sent.get("view") for sent in ctx.sent))
        self.assertEqual(len(self.pinged(ctx)), CONFIRM_OVER)

    async def test_only_the_person_who_asked_can_press(self):
        view = Confirm(self.mod.id)
        other = mock.Mock(user=self.member, response=mock.Mock(send_message=mock.AsyncMock()))
        self.assertFalse(await view.interaction_check(other))
        self.assertIn("Only the person", other.response.send_message.await_args.args[0])
        own = mock.Mock(user=self.mod)
        self.assertTrue(await view.interaction_check(own))

    async def test_over_a_thousand_is_refused(self):
        self.crowd(MOST_MEMBERS - 1, self.red)
        ctx = self.context(author=self.mod)
        with mock.patch.object(module, "CONFIRM_OVER", 10_000):  # so the count alone decides, with no question asked first
            await self.run_command(ctx, self.red)
        self.assertEqual(ctx.sent, [])
        self.assertIn("ping a role instead", ctx.fails[0])
        self.assertIn(f"{MOST_MEMBERS + 1:,}", ctx.fails[0])

    async def test_one_at_a_time_per_server(self):
        self.crowd(CONFIRM_OVER, self.red)
        first = self.context(author=self.mod)
        task = asyncio.create_task(self.run_command(first, self.red))
        while not first.sent:
            await asyncio.sleep(0)
        second = self.context(author=self.admin)
        await self.run_command(second, self.blue)
        self.assertIn("already running", second.fails[0])
        first.sent[-1]["view"].stop()
        await task
        third = self.context(author=self.admin)
        await self.run_command(third, self.blue)
        self.assertEqual(third.fails, [])

    async def test_a_loading_member_list_stops_it(self):
        self.bot.members_settled.clear()
        ctx = self.context(author=self.mod)
        await self.run_command(ctx, self.red)
        self.assertEqual(ctx.sent, [])
        self.assertIn("still loading", ctx.fails[0])
        self.bot.members_settled.set()
        with mock.patch.object(type(self.guild), "member_count", new_callable=mock.PropertyMock, return_value=500):
            ctx = self.context(author=self.mod)
            await self.run_command(ctx, self.red)
        self.assertIn("still loading", ctx.fails[0])


class PermissionTest(Base):
    async def test_it_needs_mention_everyone(self):
        command = self.bot.get_command("mass_mention")
        self.assertEqual(required_permissions(command), ("mention_everyone",))
        self.assertFalse(self.bot.command_decision(command, self.member, self.channel).allowed)
        pinger = self.guild.role("Pinger", 3, "mention_everyone")
        self.member.roles.append(pinger)
        self.assertTrue(self.bot.command_decision(command, self.member, self.channel).allowed)

    async def test_a_bypass_role_and_the_bot_manager_role_let_someone_in(self):
        command = self.bot.get_command("mass_mention")
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"mass_mention": {"bypass_roles": [self.role_member.id]}}})
        self.assertTrue(self.bot.command_decision(command, self.member, self.channel).allowed)
        helper = FakeMember(self.guild, "helper", self.red)
        self.assertFalse(self.bot.command_decision(command, helper, self.channel).allowed)
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.red.id})
        self.assertTrue(self.bot.command_decision(command, helper, self.channel).allowed)

    async def test_the_typed_names_reach_it(self):
        for name in ("mass_mention", "massmention", "mm"):
            self.assertIs(self.bot.get_command(name), self.bot.get_command("mass_mention"))


class ChannelRightsTest(Base):
    async def test_someone_who_cant_post_or_ping_here_is_refused(self):
        for missing in ("send_messages", "mention_everyone"):
            allowed = [name for name in ("view_channel", "send_messages", "mention_everyone") if name != missing]
            with mock.patch.object(type(self.channel), "permissions_for", lambda channel, member: FakePermissions(*allowed)):
                ctx = self.context(author=self.mod)
                await self.run_command(ctx, self.red)
            self.assertEqual(ctx.sent, [], missing)
            self.assertTrue(ctx.fails, missing)

    async def test_a_message_moved_to_the_bot_channel_leaves_room_for_the_authors_mention(self):
        self.crowd(150, self.red)
        ctx = self.context(author=self.mod)
        ctx.decision = Decision(restricted_channel=self.channel.id)
        with mock.patch.object(module, "CONFIRM_OVER", 10_000):
            await self.run_command(ctx, self.red)
        room = MESSAGE_LIMIT - len(self.mod.mention) - 1
        self.assertGreater(len(ctx.sent), 1)
        self.assertTrue(all(len(sent["content"]) < room for sent in ctx.sent))
        self.assertGreater(max(len(sent["content"]) for sent in ctx.sent), room - 30, "the messages still fill the room they have")


class TypedNameTest(Base):
    async def test_a_name_several_roles_share_is_refused(self):
        twin = self.guild.role("Twin", 3)
        self.guild.role("twin", 3)
        FakeMember(self.guild, "t", twin)
        ctx = self.context(author=self.mod)
        ctx.message.content = "!mass_mention Twin"
        await self.run_command(ctx, twin)
        self.assertEqual(ctx.sent, [])
        self.assertIn("Several roles are called Twin", ctx.fails[0])

    async def test_the_same_role_by_id_goes_ahead(self):
        twin = self.guild.role("Twin", 3)
        self.guild.role("twin", 3)
        holder = FakeMember(self.guild, "t", twin)
        ctx = self.context(author=self.mod)
        ctx.message.content = f"!mass_mention {twin.id}"
        await self.run_command(ctx, twin)
        self.assertEqual(self.pinged(ctx), [holder.id])


class RealLibraryTest(GatewayTest):
    modules = [Mentions]

    def slash(self, options: list[dict], resolved: dict, *, user_id: int) -> None:
        data = {"id": str(gateway_id()), "name": "mass_mention", "type": 1, "options": options, "resolved": resolved}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id))

    def pings(self) -> list[dict]:
        return [body for _, _, body in self.net.calls if "<@" in str((body.get("data") or body).get("content") or "") and "users" in str((body.get("data") or body).get("allowed_mentions"))]

    async def test_slash_pings_the_holders(self):
        options = [{"name": "role1", "type": 8, "value": self.role_admin["id"]}]
        self.slash(options, {"roles": {self.role_admin["id"]: self.role_admin}}, user_id=self.owner_id)
        await self.settle()
        self.assert_no_errors()
        sent = self.pings()
        self.assertEqual(len(sent), 1)
        data = sent[0].get("data") or sent[0]
        self.assertEqual(sorted(int(u) for u in data["allowed_mentions"]["users"]), sorted([self.owner_id, self.admin_id]))
        self.assertEqual(data["allowed_mentions"]["parse"], [])

    async def test_typed_form_takes_mentions_ids_and_quoted_names(self):
        self.dispatch_message(user_id=self.owner_id, content=f"!mass_mention <@&{self.role_admin['id']}>")
        await self.settle()
        self.dispatch_message(user_id=self.owner_id, content=f"!mm {self.role_member['id']}")
        await self.settle()
        self.dispatch_message(user_id=self.owner_id, content='!massmention "Member"')
        await self.settle()
        self.assert_no_errors()
        users = [sorted(int(u) for u in body["allowed_mentions"]["users"]) for body in self.pings()]
        self.assertEqual(users, [sorted([self.owner_id, self.admin_id]), [self.member_id], [self.member_id]])

    async def test_someone_without_mention_everyone_is_refused(self):
        options = [{"name": "role1", "type": 8, "value": self.role_admin["id"]}]
        self.slash(options, {"roles": {self.role_admin["id"]: self.role_admin}}, user_id=self.member_id)
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.pings(), [])
        answer = next(body for _, path, body in self.net.calls if path.endswith("/callback"))
        self.assertIn("Mention Everyone", answer["data"]["content"])

    async def test_the_slash_command_keeps_25_role_options(self):
        data = self.bot.tree.get_command("mass_mention").to_dict(self.bot.tree)
        self.assertEqual([option["name"] for option in data["options"]], [f"role{n}" for n in range(1, 26)])
        self.assertTrue(all(option["type"] == 8 for option in data["options"]))
        self.assertEqual([option.get("required", False) for option in data["options"]], [True] + [False] * 24)

    async def test_the_confirm_button_answers_only_its_owner(self):
        with mock.patch.object(module, "CONFIRM_OVER", 1):
            options = [{"name": "role1", "type": 8, "value": self.role_admin["id"]}]
            self.slash(options, {"roles": {self.role_admin["id"]: self.role_admin}}, user_id=self.owner_id)
            await self.settle(0.5)
            prompt = next(body for _, path, body in self.net.calls if path.endswith("/callback"))
            button = prompt["data"]["components"][0]["components"][0]
            message = self.net.message_json(int(self.text["id"]), prompt["data"])
            press = {"custom_id": button["custom_id"], "component_type": 2}
            self.bot._connection.parse_interaction_create(self.interaction_payload(3, press, user_id=self.admin_id, message=message))
            await asyncio.sleep(0.2)
            self.assertEqual(self.pings(), [])
            self.bot._connection.parse_interaction_create(self.interaction_payload(3, press, user_id=self.owner_id, message=message))
            for _ in range(200):
                if self.pings():
                    break
                await asyncio.sleep(0.02)
        self.assert_no_errors()
        refusals = [body for _, path, body in self.net.calls if path.endswith("/callback") and "Only the person" in str(body.get("data"))]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(len(self.pings()), 1)


class ChannelRightsLibraryTest(GatewayTest):
    """A slash command runs where its user can't post. The pings would land there."""
    modules = [Mentions]

    async def test_no_pings_where_the_moderator_cant_post_or_ping(self):
        self.role_mod["permissions"] = str(discord.Permissions(manage_messages=True, mention_everyone=True, send_messages=True, view_channel=True).value)
        self.bot._connection.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.role_mod})
        closed = discord.Permissions(send_messages=True, mention_everyone=True).value
        self.text["permission_overwrites"] = [{"id": self.role_mod["id"], "type": 0, "allow": "0", "deny": str(closed)}]
        self.bot._connection.parse_channel_update(dict(self.text))
        data = {"id": str(gateway_id()), "name": "mass_mention", "type": 1, "options": [{"name": "role1", "type": 8, "value": self.role_member["id"]}],
                "resolved": {"roles": {self.role_member["id"]: self.role_member}}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.mod_id, channel=self.text))
        await self.settle()
        self.assert_no_errors()
        callbacks = [body for _, path, body in self.net.calls if path.endswith("/callback")]
        self.assertFalse([body for body in callbacks if "users" in str((body.get("data") or {}).get("allowed_mentions"))])
        self.assertIn("You can't post in that channel.", str(callbacks))


class RestrictedLibraryTest(GatewayTest):
    """A command the server sends to its bot channel puts the author's mention in front, and every mention must still fit."""
    modules = [Mentions]

    async def test_every_allowed_member_is_mentioned_in_full(self):
        self.role_mod["permissions"] = str(discord.Permissions(manage_messages=True, mention_everyone=True, send_messages=True, view_channel=True).value)
        self.bot._connection.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.role_mod})
        targets = []
        for index in range(120):
            user_id = gateway_id()
            member = {"user": user_json(user_id, f"m{index}"), "roles": [self.role_member["id"]], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0,
                      "pending": False, "nick": None, "avatar": None, "premium_since": None, "communication_disabled_until": None}
            self.member_json[user_id] = member
            self.bot._connection.parse_guild_member_add({**member, "guild_id": str(self.guild_id)})
            targets.append(user_id)
        await self.bot.settings.save(self.guild_id, "commands", {"bot_channels": [self.log_channel["id"]], "overwrites": {"mass_mention": {"restricted": True}}})
        with mock.patch.object(module, "CONFIRM_OVER", 10_000):
            self.dispatch_message(user_id=self.mod_id, content=f"!mass_mention <@&{self.role_member['id']}>")
            await self.settle()
        self.assert_no_errors()
        sent = [body for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages" and "users" in str(body.get("allowed_mentions"))]
        allowed = sorted(int(user) for body in sent for user in body["allowed_mentions"]["users"])
        written = sorted(int(user) for body in sent for user in MENTION.findall(body["content"]) if int(user) != self.mod_id)
        self.assertEqual(allowed, sorted(targets + [self.member_id]))
        self.assertEqual(written, allowed)
