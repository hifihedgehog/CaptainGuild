"""Someone can hand out through the bot only what they already hold. The rule covers the dashboard, commands and undo alike."""
from __future__ import annotations

import logging

from aiohttp.test_utils import TestClient, TestServer
from discord.ext import commands

from captainguild.core.module import Module
from captainguild.core.ops import DiscordOps
from captainguild.core.perms import Who, evaluate
from captainguild.core.validate import Field, Schema, ValidationError, optional, snowflake
from captainguild.modules.config import Config
from captainguild.modules.moderation import Moderation
from captainguild.modules.notifications import Notifications
from captainguild.modules.proposals import Proposals
from captainguild.modules.reports import Reports
from captainguild.modules.voice import Voice
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import BotTest, FakeChannel, FakeMember, FakePermissions


class Posting(Module):
    """A feature whose settings post a person's text."""
    key = "postingprobe"
    schema = Schema({"welcome_channel": Field(None, optional(snowflake)), "ping_channel": Field(None, optional(snowflake))})

    def posted_channels(self, settings):
        return {"welcome_channel": {settings["welcome_channel"]} if settings["welcome_channel"] else set()}

    def pinged_channels(self, settings):
        return {"ping_channel": {settings["ping_channel"]} if settings["ping_channel"] else set()}


class ReadOnly(FakeChannel):
    """A channel the manager can read and nothing more, unless told otherwise."""

    def __init__(self, guild, name, person, *names):
        super().__init__(guild, name)
        self.person = person
        self.names = names or ("view_channel", "read_message_history")

    def permissions_for(self, member):
        return FakePermissions(*self.names) if member is self.person else member.guild_permissions


class DelegationCase(BotTest):
    modules = [Config, Moderation, Reports, Voice, Proposals, Notifications, Posting]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.manager_role = self.guild.role("Manager", 4, "manage_guild", "view_channel")
        self.manager = FakeMember(self.guild, "manager", self.manager_role)
        self.low = self.guild.role("Low", 3)
        self.high = self.guild.role("High", 6)

    async def site(self, member):
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def put(self, member, module, form):
        client = await self.site(member)
        response = await client.put(self.path(f"/settings/{module}"), json=form)
        return response.status, await response.json()


class StaffRolesTest(DelegationCase):
    async def test_only_the_owner_or_an_administrator_names_the_manager_role(self):
        status, body = await self.put(self.manager, "commands", {"mod_role": str(self.low.id)})
        self.assertEqual(status, 400)
        self.assertIn("owner or an Administrator", body["fields"]["mod_role"])
        self.assertIsNone(self.settings("commands")["mod_role"])
        status, _ = await self.put(self.admin, "commands", {"mod_role": str(self.low.id)})
        self.assertEqual(status, 200)
        status, _ = await self.put(self.manager, "commands", {"mod_role": None})
        self.assertEqual(status, 400, "taking it away is a change too")
        self.assertEqual(self.settings("commands")["mod_role"], self.low.id)

    async def test_an_unchanged_manager_role_doesnt_stop_the_rest_of_the_form(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.low.id})
        status, _ = await self.put(self.manager, "commands", {"mod_role": str(self.low.id), "mod_only": True})
        self.assertEqual(status, 200)

    async def test_dashboard_staff_roles_take_an_administrator(self):
        status, body = await self.put(self.manager, "core", {"dashboard_staff_roles": [str(self.low.id)]})
        self.assertEqual(status, 400)
        self.assertIn("owner or an Administrator", body["fields"]["dashboard_staff_roles"])
        self.assertEqual((await self.put(self.admin, "core", {"dashboard_staff_roles": [str(self.low.id)]}))[0], 200)

    async def test_every_setting_that_makes_a_role_staff_takes_an_administrator(self):
        for module, field in (("reports", "moderator_role"), ("voice", "moderator_role"), ("voice", "veteran_role"), ("voice", "server_veteran_role"), ("proposals", "moderator_role")):
            status, body = await self.put(self.manager, module, {field: str(self.low.id)})
            self.assertEqual(status, 400, (module, field))
            self.assertIn("owner or an Administrator", body["fields"][field])
            self.assertEqual((await self.put(self.admin, module, {field: str(self.low.id)}))[0], 200, (module, field))

    async def test_undo_cant_bring_a_staff_role_back_for_a_manager(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.low.id})
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": None})
        entry = (await self.bot.activity_log.recent(self.guild.id))[0]
        client = await self.site(self.manager)
        response = await client.post(self.path(f"/activity/{entry['id']}/undo"))
        self.assertEqual(response.status, 400)
        self.assertIsNone(self.settings("commands")["mod_role"])

    async def test_elevated_roles_still_take_an_administrator(self):
        self.assertEqual((await self.put(self.manager, "commands", {"elevated_roles": True}))[0], 400)


class BypassRolesTest(DelegationCase):
    def rows(self, role):
        return {"overwrites": {"ban": {"bypass_roles": [str(role.id)]}}}

    async def test_a_bypass_needs_the_permission_it_skips(self):
        status, body = await self.put(self.manager, "commands", self.rows(self.low))
        self.assertEqual(status, 400)
        self.assertIn("Ban Members", body["fields"]["overwrites"])
        self.assertEqual(self.settings("commands")["overwrites"], {})

    async def test_someone_who_holds_it_can_share_it_below_their_own_top_role(self):
        holder = FakeMember(self.guild, "holder", self.guild.role("Banner", 5, "manage_guild", "ban_members", "view_channel"))
        self.assertEqual((await self.put(holder, "commands", self.rows(self.low)))[0], 200)
        self.assertEqual(self.settings("commands")["overwrites"]["ban"]["bypass_roles"], [self.low.id])

    async def test_the_role_must_sit_below_their_top_role(self):
        holder = FakeMember(self.guild, "holder", self.guild.role("Banner", 5, "manage_guild", "ban_members", "view_channel"))
        status, body = await self.put(holder, "commands", self.rows(self.high))
        self.assertEqual(status, 400)
        self.assertIn("High", body["fields"]["overwrites"])

    async def test_a_groups_permissions_include_its_subcommands(self):
        self.assertIn("manage_guild", self.bot.command_permissions("prefix"))
        self.assertEqual(self.bot.command_permissions("nosuchcommand"), set())

    async def test_an_administrator_passes(self):
        self.assertEqual((await self.put(self.admin, "commands", self.rows(self.high)))[0], 200)

    async def test_an_old_bypass_on_a_row_doesnt_stop_an_edit(self):
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"ban": {"bypass_roles": [self.high.id]}}})
        status, _ = await self.put(self.manager, "commands", {"overwrites": {"ban": {"bypass_roles": [str(self.high.id)], "mod_only": True}}})
        self.assertEqual(status, 200)


class EveryoneTest(DelegationCase):
    async def test_everyone_never_holds_a_power_even_from_an_administrator(self):
        everyone = str(self.guild.default_role.id)
        for module, form, field in (
            ("commands", {"mod_role": everyone}, "mod_role"),
            ("commands", {"overwrites": {"ban": {"bypass_roles": [everyone]}}}, "overwrites"),
            ("core", {"dashboard_staff_roles": [everyone]}, "dashboard_staff_roles"),
            ("reports", {"moderator_role": everyone}, "moderator_role"),
            ("reports", {"alert_role": everyone}, "alert_role"),
            ("voice", {"moderator_role": everyone}, "moderator_role"),
            ("voice", {"veteran_role": everyone}, "veteran_role"),
            ("voice", {"server_veteran_role": everyone}, "server_veteran_role"),
            ("proposals", {"moderator_role": everyone}, "moderator_role"),
            ("proposals", {"participation_role": everyone}, "participation_role"),
            ("notifications", {"alert_role": everyone}, "alert_role"),
        ):
            status, body = await self.put(self.admin, module, form)
            self.assertEqual(status, 400, (module, field))
            self.assertIn("@everyone", body["fields"][field], (module, field))

    async def test_a_command_refuses_everyone_too(self):
        cog = self.cog(Config)
        ctx = self.context(author=self.admin)
        await cog.config_modrole.callback(cog, ctx, self.guild.default_role)
        self.assertIn("@everyone", ctx.fails[0])
        self.assertIsNone(self.settings("commands")["mod_role"])


class CommandSideTest(DelegationCase):
    async def test_modrole_takes_an_administrator(self):
        cog = self.cog(Config)
        ctx = self.context(author=self.manager)
        await cog.config_modrole.callback(cog, ctx, self.low)
        self.assertIn("owner or an Administrator", ctx.fails[0])
        self.assertIsNone(self.settings("commands")["mod_role"])
        ctx = self.context(author=self.admin)
        await cog.config_modrole.callback(cog, ctx, self.low)
        self.assertEqual(self.settings("commands")["mod_role"], self.low.id)

    async def test_clearing_it_takes_an_administrator(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.low.id})
        cog = self.cog(Config)
        ctx = self.context(author=self.manager)
        await cog.config_clearmodrole.callback(cog, ctx)
        self.assertEqual(self.settings("commands")["mod_role"], self.low.id)
        self.assertIn("owner or an Administrator", ctx.fails[0])


class ManagerCanBanTest(DelegationCase):
    def run_rule(self, **who):
        return evaluate({"mod_role": 5}, root="ban", required=["ban_members"], who=Who(**who), channel_id=1)

    def test_the_manager_role_can_ban(self):
        self.assertTrue(self.run_rule(role_ids={5}).allowed)

    def test_a_tag_override_still_cant(self):
        self.assertFalse(self.run_rule(role_ids=set(), moderator=True).allowed)

    def test_a_plain_member_cant(self):
        self.assertFalse(self.run_rule(role_ids={6}).allowed)


class AlertRoleTest(DelegationCase):
    async def test_the_reports_alert_role_isnt_staff(self):
        await self.bot.settings.save(self.guild.id, "reports", {"alert_role": self.low.id})
        member = FakeMember(self.guild, "plain", self.low)
        self.assertFalse(self.cog(Reports).is_staff(member))
        await self.bot.settings.save(self.guild.id, "reports", {"moderator_role": self.low.id})
        self.assertTrue(self.cog(Reports).is_staff(member))


class PostedChannelsTest(DelegationCase):
    async def test_a_setting_that_posts_a_persons_text_needs_send_there(self):
        news = ReadOnly(self.guild, "news", self.manager)
        status, body = await self.put(self.manager, "postingprobe", {"welcome_channel": str(news.id)})
        self.assertEqual(status, 400)
        self.assertEqual(body["fields"]["welcome_channel"], "You can't post in that channel.")
        self.assertEqual((await self.put(self.admin, "postingprobe", {"welcome_channel": str(news.id)}))[0], 200)

    async def test_a_channel_set_up_by_someone_else_stays_when_they_fix_a_typo(self):
        news = ReadOnly(self.guild, "news", self.manager)
        await self.bot.settings.save(self.guild.id, "postingprobe", {"welcome_channel": news.id})
        self.assertEqual((await self.put(self.manager, "postingprobe", {"welcome_channel": str(news.id)}))[0], 200)

    async def test_a_channel_they_can_post_in_passes(self):
        open_channel = ReadOnly(self.guild, "open", self.manager, "view_channel", "send_messages")
        self.assertEqual((await self.put(self.manager, "postingprobe", {"welcome_channel": str(open_channel.id)}))[0], 200)

    async def test_a_saved_ping_needs_mention_everyone_there(self):
        open_channel = ReadOnly(self.guild, "open", self.manager, "view_channel", "send_messages")
        status, body = await self.put(self.manager, "postingprobe", {"ping_channel": str(open_channel.id)})
        self.assertEqual(status, 400)
        self.assertIn("Mention Everyone", body["fields"]["ping_channel"])
        open_channel.names = ("view_channel", "send_messages", "mention_everyone")
        self.assertEqual((await self.put(self.manager, "postingprobe", {"ping_channel": str(open_channel.id)}))[0], 200)


class RankTest(DelegationCase):
    async def test_a_role_above_the_bot_cant_be_set_up_to_be_handed_out(self):
        above = self.guild.role("Above", 9)
        status, body = await self.put(self.admin, "voice", {"access_role": str(above.id)})
        self.assertEqual(status, 400)
        self.assertIn("the bot", body["fields"]["access_role"])
        self.assertEqual((await self.put(self.admin, "voice", {"access_role": str(self.low.id)}))[0], 200)

    async def test_a_role_above_the_person_cant_either(self):
        status, body = await self.put(self.manager, "voice", {"access_role": str(self.high.id)})
        self.assertEqual(status, 400)
        self.assertIn("High", body["fields"]["access_role"])


class SentencesTest(DelegationCase):
    async def test_a_refused_setting_answers_with_the_sentence_and_logs_nothing(self):
        ctx = self.context(author=self.admin)
        with self.assertLogs("captainguild.bot", level="ERROR") as logged:
            logging.getLogger("captainguild.bot").error("positive control")
            await self.bot.on_command_error(ctx, commands.CommandInvokeError(ValidationError({"prefixes": "Must be at most 10 characters. This is 11."})))
        self.assertEqual(ctx.fails, ["Must be at most 10 characters. This is 11."])
        self.assertEqual(len(logged.records), 1)

    async def test_a_long_prefix_raises_the_error_that_carries_the_sentence(self):
        cog = self.cog(Config)
        with self.assertRaises(ValidationError):
            await cog.prefix_add.callback(cog, self.context(author=self.admin), "x" * 11)
