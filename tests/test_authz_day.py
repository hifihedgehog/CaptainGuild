"""A normal day for the people who run a server, on commands and on the dashboard. Tighter checks must not stop ordinary work, and where something is refused the sentence names what's missing."""
from __future__ import annotations

import time

from aiohttp import ContentTypeError
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.modules import load_all
from captainguild.modules.moderation import Moderation
from captainguild.modules.reports import Reports
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import BotTest, FakeChannel, FakeMember, FakePermissions

EVERYONE = ("view_channel", "send_messages", "read_message_history", "add_reactions", "embed_links", "attach_files")
MODERATOR_DAY = ["ban", "kick", "softban", "tempban", "massban", "unban", "tempbans", "report", "vc_report", "dashboard", "my_data", "help", "ping", "highlight add", "rank", "level", "leaderboard", "members info", "role info", "reminder", "tag", "tag list"]
BOT_MANAGER_DAY = [*MODERATOR_DAY, "mute", "unmute", "warn", "warns", "removewarning", "clearwarnings", "timeout", "untimeout", "purge all", "notes setnote", "notes view", "reason", "modlog reason", "role add", "temprole add", "lockdown channel", "unlockdown channel", "raid on", "raid off", "setnick"]
NEEDS_MORE = {"mute": "Manage Roles", "warn": "Manage Roles", "purge all": "Manage Server", "notes setnote": "Manage Server", "role add": "Manage Roles", "config disable": "Manage Server", "lockdown channel": "Manage Channels"}


class DayCase(BotTest):
    modules = load_all()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.guild.default_role.permissions = FakePermissions(*EVERYONE)
        self.moderator_role = self.guild.role("Mods", 6, "ban_members", "kick_members", "manage_messages")
        self.moderator = FakeMember(self.guild, "moderator", self.moderator_role)
        self.manager_role = self.guild.role("Manager", 7, "manage_guild")
        self.manager = FakeMember(self.guild, "manager", self.manager_role)
        self.target = FakeMember(self.guild, "target", self.role_member)

    async def site(self, member):
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def go(self, member, method, tail, form=None):
        client = await self.site(member)
        response = await client.request(method, self.path(tail), json=form if form is not None else {})
        try:
            return response.status, await response.json()
        except (ValueError, ContentTypeError):
            return response.status, {}

    def runs(self, member, name):
        command = self.bot.get_command(name)
        self.assertIsNotNone(command, name)
        return self.bot.command_decision(command, member, self.channel)

    def make_a_moderator_role_without_manage_server(self):
        """The moderator role lacks Manage Server. Reach comes from the bot manager role."""
        return self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.moderator_role.id})


class ModeratorWithNoBotSetupTest(DayCase):
    async def test_their_own_permissions_carry_their_own_work(self):
        for name in MODERATOR_DAY:
            self.assertTrue(self.runs(self.moderator, name).allowed, name)

    async def test_what_they_lack_is_refused_by_name(self):
        for name, label in NEEDS_MORE.items():
            decision = self.runs(self.moderator, name)
            self.assertFalse(decision.allowed, name)
            self.assertIn(label, decision.reason, name)

    async def test_they_ban_and_kick_a_member(self):
        moderation = self.cog(Moderation)
        ctx = self.context(self.moderator)
        await moderation.ban.callback(moderation, ctx, self.target, reason="spam")
        self.assertEqual(ctx.fails, [])
        self.assertIn(self.target.id, self.guild.banned)
        other = FakeMember(self.guild, "other", self.role_member)
        ctx = self.context(self.moderator)
        await moderation.kick.callback(moderation, ctx, other, reason="spam")
        self.assertEqual(ctx.fails, [])
        self.assertNotIn(other, self.guild.members)

    async def test_they_cant_open_the_dashboard_until_a_role_lets_them(self):
        self.assertEqual((await self.go(self.moderator, "GET", "/info"))[0], 403)


class BotManagerRoleTest(DayCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.make_a_moderator_role_without_manage_server()

    async def test_the_bot_manager_role_reaches_the_commands_of_the_day(self):
        for name in BOT_MANAGER_DAY:
            self.assertTrue(self.runs(self.moderator, name).allowed, name)

    async def test_a_bypass_role_carries_a_command_without_the_manager_role(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": None, "overwrites": {"mute": {"bypass_roles": [self.moderator_role.id]}, "purge": {"bypass_roles": [self.moderator_role.id]}}})
        self.assertTrue(self.runs(self.moderator, "mute").allowed)
        self.assertTrue(self.runs(self.moderator, "purge all").allowed)
        self.assertFalse(self.runs(self.moderator, "warn").allowed)

    async def case(self):
        moderation = self.cog(Moderation)
        ctx = self.context(self.moderator)
        await moderation.warn.callback(moderation, ctx, self.target, reason="first")
        self.assertEqual(ctx.fails, [])
        return await self.db.scalar("SELECT number FROM cases WHERE guild_id = ? ORDER BY number DESC LIMIT 1", (self.guild.id,))

    async def test_the_cases_page_works_for_them(self):
        number = await self.case()
        self.assertEqual((await self.go(self.moderator, "GET", "/cases"))[0], 200)
        status, row = await self.go(self.moderator, "GET", f"/cases/{number}")
        self.assertEqual(status, 200)
        self.assertEqual((await self.go(self.moderator, "PATCH", f"/cases/{number}", {"reason": "second thoughts"}))[0], 200)
        self.assertEqual((await self.go(self.moderator, "POST", f"/cases/{number}/void", {"reason": "mistake"}))[0], 200)
        self.assertEqual((await self.go(self.moderator, "POST", f"/cases/{number}/restore"))[0], 200)
        status, note = await self.go(self.moderator, "POST", f"/cases/member/{self.target.id}/notes", {"text": "keep an eye"})
        self.assertEqual(status, 200)
        self.assertEqual((await self.go(self.moderator, "DELETE", f"/cases/notes/{note['number']}"))[0], 200)

    async def test_the_running_tab_lifts_what_they_can_lift(self):
        moderation = self.cog(Moderation)
        role = self.guild.role("Muted role", 1)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": role.id})
        ctx = self.context(self.moderator)
        await moderation.mute.callback(moderation, ctx, self.target, reason="loud")
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.go(self.moderator, "GET", "/moderation/running"))[0], 200)
        status, body = await self.go(self.moderator, "POST", f"/moderation/running/mutes/{self.target.id}/lift", {"reason": "done"})
        self.assertEqual(status, 200, body)

    async def report(self, channel):
        return await self.db.insert(
            "INSERT INTO reports (guild_id, number, kind, state, reporter_id, reporter_name, target_id, target_name, reason, channel_id, message_id, content, created_at) VALUES (?, 1, 'message', 'open', ?, 'r', ?, 't', 'why', ?, 55, 'said this', ?)",
            (self.guild.id, self.member.id, self.target.id, channel.id, time.time()),
        )

    async def test_the_reports_page_works_for_them(self):
        report = await self.report(self.channel)
        status, row = await self.go(self.moderator, "GET", f"/reports/{report}")
        self.assertEqual(status, 200)
        self.assertEqual(row["content"], "said this")
        self.assertEqual((await self.go(self.moderator, "GET", "/reports"))[0], 200)
        self.assertEqual((await self.go(self.moderator, "GET", "/reports/threads"))[0], 200)
        status, body = await self.go(self.moderator, "POST", f"/reports/{report}/state", {"state": "actioned"})
        self.assertEqual(status, 200, body)

    async def test_the_pages_they_read_answer(self):
        for tail in ("/info", "/settings/reports", "/members/search?q=tar", "/logs/search", "/logs/export", "/logging/catalog", "/cases/leaderboard", "/levels/leaderboard", "/proposals/list"):
            self.assertEqual((await self.go(self.moderator, "GET", tail))[0], 200, tail)

    async def test_settings_stay_closed_with_a_plain_answer(self):
        status, body = await self.go(self.moderator, "PUT", "/settings/reports", {"cooldown_seconds": 5})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "That takes Manage Server. Ask someone who has it.")


class ManagerDayTest(DayCase):
    async def test_a_manager_sets_up_the_server_on_the_dashboard(self):
        room = FakeChannel(self.guild, "welcome")
        low = self.guild.role("Regular", 2)
        for module, form in (
            ("greetings", {"welcome_channel": str(room.id), "welcome": {"content": "Hi {user}"}}),
            ("levels", {"rewards": [{"level": 5, "role_id": str(low.id)}], "destination": str(room.id)}),
            ("autoroles", {"roles": [str(low.id)]}),
            ("roles", {"ranks": [str(low.id)]}),
            ("moderation", {"modlog_channel": str(room.id), "mute_role": str(low.id)}),
            ("reports", {"report_channel": str(room.id), "cooldown_seconds": 30}),
            ("suggestions", {"channel_id": str(room.id)}),
            ("starboard", {"channel_id": str(room.id)}),
            ("logging", {"join_channel": str(room.id)}),
            ("logging", {"message_channel": str(room.id)}),
            ("automod", {"log_channel": str(room.id)}),
            ("notifications", {"alert_channel": str(room.id)}),
            ("proposals", {"channel_id": str(room.id)}),
            ("commands", {"bot_channels": [str(room.id)]}),
            ("core", {"prefixes": ["!"]}),
        ):
            status, body = await self.go(self.manager, "PUT", f"/settings/{module}", form)
            self.assertEqual(status, 200, (module, body))

    async def test_a_manager_posts_and_edits_a_message_with_no_ping(self):
        status, body = await self.go(self.manager, "POST", "/embeds/post", {"channel_id": str(self.channel.id), "payload": {"content": "Hello"}})
        self.assertEqual(status, 200, body)
        status, refused = await self.go(self.manager, "POST", "/embeds/post", {"channel_id": str(self.channel.id), "payload": {"content": "Hello"}, "ping": True})
        self.assertEqual(status, 400)
        self.assertIn("Mention Everyone", refused["error"])

    async def test_a_manager_manages_tags_triggers_and_reaction_roles(self):
        low = self.guild.role("Regular", 2)
        self.assertEqual((await self.go(self.manager, "POST", "/tags", {"name": "rules", "message": {"content": "Read them"}}))[0], 200)
        self.assertEqual((await self.go(self.manager, "POST", "/triggers", {"trigger": "hello there", "message": {"content": "hi"}}))[0], 200)
        status, body = await self.go(self.manager, "POST", "/reactionroles", {"channel_id": str(self.channel.id), "payload": {"content": "Pick"}, "rows": [{"emoji": "\N{THUMBS UP SIGN}", "roles": [str(low.id)]}]})
        self.assertEqual(status, 200, body)

    async def test_what_takes_more_than_manage_server_says_so(self):
        for method, tail, form, words in (
            ("POST", "/setup/role-template", {"template": "colors"}, "Manage Roles"),
            ("POST", "/setup/channel", {"name": "x"}, "Manage Channels"),
            ("POST", "/starboard/create", {"name": "x"}, "Manage Channels"),
            ("POST", "/suggestions/create-channel", {"name": "x"}, "Manage Channels"),
            ("POST", "/logging/aio", {}, "Manage Channels"),
            ("POST", "/backup/snapshots", {}, "dministrator"),
            ("POST", "/tags/trust", {"ids": []}, "Administrator"),
        ):
            status, body = await self.go(self.manager, method, tail, form)
            self.assertEqual(status, 403, tail)
            self.assertIn(words, body["error"], tail)

    async def test_commands_a_manager_runs(self):
        for name in ("config disable", "config modonly", "prefix add", "tag modonly", "greetings welcome", "level reward", "starboard setup", "reactionrole add", "automod log", "log channel", "embed create", "sticky add", "autopurge set", "triggers create", "feeds create", "twitch add", "suggestion channel", "echo", "poll", "giveaway create", "purge all", "cleanup", "notes setnote", "modlog reason", "reason"):
            self.assertTrue(self.runs(self.manager, name).allowed, name)
        for name in ("ban", "kick", "mute", "warn", "role add", "lockdown channel", "timeout", "autorole add", "rank addrank"):
            decision = self.runs(self.manager, name)
            self.assertFalse(decision.allowed, name)
            self.assertIn("You need", decision.reason, name)


class AdministratorDayTest(DayCase):
    async def test_an_administrator_reaches_every_command_with_a_permission(self):
        for command in self.bot.walk_commands():
            if required_permissions(command):
                self.assertTrue(self.bot.command_decision(command, self.admin, self.channel).allowed, command.qualified_name)

    async def test_an_administrator_sets_what_only_they_can(self):
        low = self.guild.role("Regular", 2)
        for module, form in (
            ("commands", {"mod_role": str(self.moderator_role.id), "elevated_roles": True}),
            ("core", {"dashboard_staff_roles": [str(self.moderator_role.id)]}),
            ("reports", {"moderator_role": str(self.moderator_role.id), "alert_role": str(low.id)}),
            ("voice", {"moderator_role": str(self.moderator_role.id), "veteran_role": str(low.id)}),
        ):
            status, body = await self.go(self.admin, "PUT", f"/settings/{module}", form)
            self.assertEqual(status, 200, (module, body))
        self.assertEqual((await self.go(self.admin, "POST", "/backup/snapshots"))[0] in (200, 409), True)
