"""What a member, a staff person or someone with Manage Server can't reach, however they ask: tags, dashboard settings, records, hidden channels and sign-in."""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urljoin

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.ops import DiscordOps
from captainguild.core.shortcuts import SHORTCUTS
from captainguild.core.tagscript import RegexGuard, Scope, budget, run_tag
from captainguild.core.tagscript import runner as runner_module
from captainguild.modules import moderation as moderation_module
from captainguild.modules.automod import Automod
from captainguild.modules.autopurge import AutoPurge
from captainguild.modules.autoroles import Autoroles
from captainguild.modules.config import Config
from captainguild.modules.embeds import Embeds
from captainguild.modules.feeds import Feeds
from captainguild.modules.greetings import Greetings
from captainguild.modules.levels import Levels
from captainguild.modules.logs import Logs, search_parent
from captainguild.modules.moderation import Moderation
from captainguild.modules.notifications import Notifications
from captainguild.modules.personalize import Personalize
from captainguild.modules.reactionroles import ReactionRoles
from captainguild.modules.roles import Roles
from captainguild.modules.sticky import Sticky
from captainguild.modules.tags import Tags
from captainguild.modules.triggers import Triggers
from captainguild.modules.voice import Voice
from captainguild.modules.voicelink import VoiceLink
from captainguild.web.api import User
from captainguild.web.app import SITE, build_app, local_path
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakePermissions
from tests.gateway import GatewayTest, iso, new_id, user_json


class HiddenChannel(FakeChannel):
    """A channel only the people listed can see."""

    def __init__(self, guild: FakeGuild, name: str, allowed: set[int]) -> None:
        super().__init__(guild, name)
        self.allowed = allowed

    def permissions_for(self, member):
        permissions = super().permissions_for(member)
        if member.id in self.allowed:
            return permissions
        return FakePermissions(*(set(permissions.names) - {"view_channel", "read_messages", "administrator"}))


class SiteCase(BotTest):
    """A signed-in browser, with the real rules deciding what each person can open."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.manager_role = self.guild.role("Manager", 4, "manage_guild", "view_channel")
        self.manager = FakeMember(self.guild, "manager", self.manager_role)
        self.senior = self.guild.role("Senior Admin", 8, "administrator")

    async def site(self, member):
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client


class SettingsReachTest(SiteCase):
    modules = [Levels, Autoroles, VoiceLink, Roles, Logs, Voice, Greetings]

    async def test_a_spaced_id_is_read_as_the_schema_keeps_it(self):
        other = FakeGuild("Another Server")
        other.me = FakeMember(other, "Bot", other.role("Bot", 9, "administrator", managed=True), bot=True)
        self.bot.fake_guilds.append(other)
        theirs = FakeChannel(other, "their-general")
        client = await self.site(self.admin)
        for spelling in (str(theirs.id), f" {theirs.id}", f"{theirs.id}\t"):
            response = await client.put(self.path("/settings/logging"), json={"default_channel": spelling})
            self.assertEqual(response.status, 400, spelling)
            self.assertIn("default_channel", (await response.json())["fields"])
        self.assertIsNone(self.settings("logging")["default_channel"])

    async def test_only_the_owner_or_an_administrator_changes_elevated_roles(self):
        client = await self.site(self.manager)
        response = await client.put(self.path("/settings/commands"), json={"elevated_roles": True})
        self.assertEqual(response.status, 400)
        self.assertIn("elevated_roles", (await response.json())["fields"])
        self.assertFalse(self.settings("commands")["elevated_roles"])
        admin = await self.site(self.admin)
        self.assertEqual((await admin.put(self.path("/settings/commands"), json={"elevated_roles": True})).status, 200)
        # Everything else on the page still saves for them, with the switch where someone else left it.
        response = await client.put(self.path("/settings/commands"), json={"mod_only": True, "elevated_roles": True})
        self.assertEqual(response.status, 200, await response.text())

    async def test_undo_can_not_turn_elevated_roles_back_on(self):
        admin = await self.site(self.admin)
        await admin.put(self.path("/settings/commands"), json={"elevated_roles": True})
        await admin.put(self.path("/settings/commands"), json={"elevated_roles": False})
        rows = await self.bot.activity_log.recent(self.guild.id)
        client = await self.site(self.manager)
        response = await client.post(self.path(f"/activity/{rows[0]['id']}/undo"))
        self.assertEqual(response.status, 400, await response.text())
        self.assertFalse(self.settings("commands")["elevated_roles"])

    async def test_a_role_above_the_asker_can_not_be_handed_out(self):
        client = await self.site(self.manager)
        below = self.guild.role("Helper", 3)
        for module, form, field in (
            ("levels", {"rewards": [{"level": 1, "role_id": str(self.senior.id)}]}, "rewards"),
            ("autoroles", {"roles": [str(self.senior.id)]}, "roles"),
            ("autoroles", {"timed": [{"role_id": str(self.senior.id), "seconds": 3600}]}, "timed"),
            ("voicelink", {"links": [{"channel_id": str(self.channel.id), "roles": [str(self.senior.id)]}]}, "links"),
            ("roles", {"ranks": [str(self.senior.id)]}, "ranks"),
            ("voice", {"access_role": str(self.senior.id)}, "access_role"),
            ("greetings", {"birthday_role": str(self.senior.id)}, "birthday_role"),
            ("levels", {"rewards": [{"level": 1, "role_id": str(self.manager_role.id)}]}, "rewards"),
        ):
            response = await client.put(self.path(f"/settings/{module}"), json=form)
            self.assertEqual(response.status, 400, (module, form))
            self.assertIn(field, (await response.json())["fields"])
        response = await client.put(self.path("/settings/levels"), json={"rewards": [{"level": 1, "role_id": str(below.id)}]})
        self.assertEqual(response.status, 200, await response.text())

    async def test_a_role_already_there_can_stay_and_the_owner_can_add_any(self):
        admin = await self.site(self.admin)
        response = await admin.put(self.path("/settings/levels"), json={"rewards": [{"level": 1, "role_id": str(self.senior.id)}]})
        self.assertEqual(response.status, 200, await response.text())
        client = await self.site(self.manager)
        response = await client.put(self.path("/settings/levels"), json={"rate": 2})
        self.assertEqual(response.status, 200, await response.text())

    async def test_a_setting_can_not_newly_name_a_channel_the_person_can_not_see(self):
        secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.me.id})
        self.manager_role.permissions = FakePermissions("manage_guild", "view_channel", "send_messages")  # a log channel takes posting there too
        client = await self.site(self.manager)
        response = await client.put(self.path("/settings/logging"), json={"join_channel": str(secret.id)})
        self.assertEqual(response.status, 400, await response.text())
        self.assertIn("join_channel", (await response.json())["fields"])
        self.assertIsNone(self.settings("logging")["join_channel"])
        self.assertEqual((await client.put(self.path("/settings/logging"), json={"join_channel": str(self.channel.id)})).status, 200)
        admin = await self.site(self.admin)
        self.assertEqual((await admin.put(self.path("/settings/logging"), json={"join_channel": str(secret.id)})).status, 200)
        # A channel someone else chose can stay when this person saves the page again.
        response = await client.put(self.path("/settings/logging"), json={"join_channel": str(secret.id)})
        self.assertEqual(response.status, 200, await response.text())

    async def test_undo_can_not_name_a_channel_the_person_can_not_see(self):
        secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.me.id})
        admin = await self.site(self.admin)
        await admin.put(self.path("/settings/logging"), json={"default_channel": str(secret.id)})
        await admin.put(self.path("/settings/logging"), json={"default_channel": str(self.channel.id)})
        rows = await self.bot.activity_log.recent(self.guild.id)
        client = await self.site(self.manager)
        response = await client.post(self.path(f"/activity/{rows[0]['id']}/undo"))
        self.assertEqual(response.status, 400, await response.text())
        self.assertEqual(self.settings("logging")["default_channel"], self.channel.id)

    async def test_manage_server_can_not_reach_administrator_through_a_reward(self):
        client = await self.site(self.manager)
        await client.put(self.path("/settings/commands"), json={"elevated_roles": True})
        await client.put(self.path("/settings/levels"), json={"rewards": [{"level": 1, "role_id": str(self.senior.id)}]})
        response = await client.put(self.path(f"/levels/members/{self.manager.id}"), json={"level": 5})
        self.assertEqual(response.status, 200)
        await self.cog(Levels)._sync_due({"guild_id": self.guild.id, "user_id": self.manager.id}, {})
        self.assertNotIn(self.senior, self.manager.roles)
        self.assertFalse(self.manager.guild_permissions.administrator)


class RecordsTest(SiteCase):
    modules = [Moderation]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.moderation = self.cog(Moderation)
        await self.bot.settings.save(self.guild.id, "core", {"dashboard_staff_roles": [self.role_member.id]})

    async def records(self, about):
        case = await self.moderation.record_case(self.guild, "warn", about, self.admin, reason="Spamming")
        ban = await self.moderation.record_case(self.guild, "ban", about, self.admin, reason="Worse")
        note = await self.moderation.add_note(self.guild.id, about.id, self.admin, "Watch this one")
        return case["number"], ban["number"], note["number"]

    async def test_a_staff_role_with_no_permissions_can_open_records_and_change_nothing(self):
        warning, _, note = await self.records(self.mod)
        client = await self.site(self.member)
        self.assertEqual((await client.get(self.path("/cases"))).status, 200)
        attempts = [
            client.post(self.path(f"/cases/{warning}/void"), json={}),
            client.post(self.path(f"/cases/{warning}/restore"), json={}),
            client.patch(self.path(f"/cases/{warning}"), json={"reason": "changed"}),
            client.delete(self.path(f"/cases/notes/{note}")),
            client.post(self.path(f"/cases/member/{self.mod.id}/notes"), json={"text": "planted"}),
        ]
        for attempt in attempts:
            response = await attempt
            self.assertEqual(response.status, 403, await response.text())
        self.assertEqual(await self.moderation.warning_count(self.guild.id, self.mod.id), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM notes"), 1)

    async def test_a_bot_manager_can_change_someone_else_s_record(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.role_mod.id})
        await self.bot.settings.save(self.guild.id, "core", {"dashboard_staff_roles": []})
        warning, _, note = await self.records(self.member)
        client = await self.site(self.mod)
        self.assertEqual((await client.post(self.path(f"/cases/{warning}/void"), json={})).status, 200)
        self.assertEqual((await client.patch(self.path(f"/cases/{warning}"), json={"reason": "changed"})).status, 200)
        self.assertEqual((await client.delete(self.path(f"/cases/notes/{note}"))).status, 200)
        self.assertEqual((await client.post(self.path(f"/cases/member/{self.member.id}/notes"), json={"text": "new"})).status, 200)

    async def test_nobody_below_an_administrator_changes_a_record_about_themselves(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.role_mod.id})
        warning, ban, note = await self.records(self.mod)
        client = await self.site(self.mod)
        for attempt in (
            client.post(self.path(f"/cases/{warning}/void"), json={}), client.post(self.path(f"/cases/{ban}/void"), json={}),
            client.patch(self.path(f"/cases/{warning}"), json={"reason": "changed"}), client.delete(self.path(f"/cases/notes/{note}")),
            client.post(self.path(f"/cases/member/{self.mod.id}/notes"), json={"text": "fine by me"}),
        ):
            response = await attempt
            self.assertEqual(response.status, 403)
            self.assertIn("yourself", (await response.json())["error"])
        warning, _, _ = await self.records(self.manager)
        manager = await self.site(self.manager)
        self.assertEqual((await manager.post(self.path(f"/cases/{warning}/void"), json={})).status, 403)

    async def test_an_administrator_still_can(self):
        warning, _, note = await self.records(self.admin)
        client = await self.site(self.admin)
        self.assertEqual((await client.post(self.path(f"/cases/{warning}/void"), json={})).status, 200)
        self.assertEqual((await client.delete(self.path(f"/cases/notes/{note}"))).status, 200)

    async def test_a_turned_off_command_stops_the_matching_change(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.role_mod.id, "overwrites": {"modlog": {"enabled": False}}})
        warning, _, _ = await self.records(self.member)
        client = await self.site(self.mod)
        self.assertEqual((await client.patch(self.path(f"/cases/{warning}"), json={"reason": "changed"})).status, 403)

    async def test_a_channel_rule_on_the_command_does_not_count_against_the_page(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.role_mod.id, "overwrites": {"modlog": {"channel_wl": [self.channel.id]}}})
        warning, _, _ = await self.records(self.member)
        client = await self.site(self.mod)
        self.assertEqual((await client.patch(self.path(f"/cases/{warning}"), json={"reason": "changed"})).status, 200)


class HiddenChannelTest(SiteCase):
    modules = [Embeds, ReactionRoles, Logs]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.secret = HiddenChannel(self.guild, "owners-only", {self.owner.id, self.admin.id, self.me.id})
        self.message = await self.secret.send("private text")
        self.link = f"https://discord.com/channels/{self.guild.id}/{self.secret.id}/{self.message.id}"

    async def test_manage_server_can_not_read_or_post_where_discord_hides_the_channel(self):
        client = await self.site(self.manager)
        for attempt in (
            client.get(self.path("/embeds/channel-messages"), params={"channel_id": str(self.secret.id)}),
            client.get(self.path("/embeds/lookup"), params={"link": self.link}),
            client.post(self.path("/embeds/post"), json={"channel_id": str(self.secret.id), "payload": {"content": "hello"}}),
            client.patch(self.path("/embeds/message"), json={"channel_id": str(self.secret.id), "message_id": str(self.message.id), "payload": {"content": "changed"}}),
            client.get(self.path("/reactionroles/lookup"), params={"channel_id": str(self.secret.id), "latest": "1"}),
            client.post(self.path("/embeds/adoptions"), json={"messages": [{"channel_id": str(self.secret.id), "message_id": str(self.message.id)}]}),
        ):
            response = await attempt
            self.assertEqual(response.status, 400, await response.text())
            self.assertNotIn("private text", await response.text())
        self.assertEqual(len(self.secret.sent), 1)

    async def test_the_owner_and_administrators_can(self):
        client = await self.site(self.admin)
        response = await client.get(self.path("/embeds/channel-messages"), params={"channel_id": str(self.secret.id)})
        self.assertIn("private text", await response.text())

    async def test_a_saved_post_in_a_hidden_channel_stays_out_of_reach(self):
        embeds = self.cog(Embeds)
        posted = await embeds.record_post(self.guild.id, self.secret.id, self.message.id, {"content": "private text", "embeds": [], "buttons": []}, source="dashboard")
        client = await self.site(self.manager)
        self.assertEqual((await (await client.get(self.path("/embeds/messages"))).json())["rows"], [])
        self.assertEqual((await client.get(self.path(f"/embeds/messages/{posted}"))).status, 404)
        self.assertEqual((await client.delete(self.path(f"/embeds/messages/{posted}"))).status, 404)
        self.assertFalse(self.message.deleted)

    async def test_reaction_roles_on_a_hidden_message_stay_out_of_reach(self):
        await self.db.run("INSERT INTO rr_messages (message_id, guild_id, channel_id, created_at) VALUES (?, ?, ?, 1)", (self.message.id, self.guild.id, self.secret.id))
        client = await self.site(self.manager)
        response = await client.get(self.path(f"/reactionroles/{self.message.id}/message"))
        self.assertEqual(response.status, 403)
        self.assertNotIn("private text", await response.text())
        self.assertEqual((await client.delete(self.path(f"/reactionroles/{self.message.id}"))).status, 403)
        admin = await self.site(self.admin)
        self.assertIn("private text", await (await admin.get(self.path(f"/reactionroles/{self.message.id}/message"))).text())

    async def test_a_private_thread_takes_more_than_seeing_its_parent(self):
        thread = FakeChannel(self.guild, "private-talk", discord.ChannelType.private_thread)
        kept = await thread.send("thread text")
        plain = await self.channel.send("plain text")
        link = f"https://discord.com/channels/{self.guild.id}/{{}}/{{}}"
        reader = FakeMember(self.guild, "reader", self.guild.role("Reader", 5, "manage_guild", "view_channel", "read_message_history"))
        client = await self.site(reader)
        response = await client.get(self.path("/embeds/lookup"), params={"link": link.format(thread.id, kept.id)})
        self.assertEqual(response.status, 400, await response.text())
        self.assertNotIn("thread text", await response.text())
        response = await client.get(self.path("/embeds/lookup"), params={"link": link.format(self.channel.id, plain.id)})
        self.assertIn("plain text", await response.text(), "the same person and the same request, in an ordinary channel")
        threads = FakeMember(self.guild, "threads", self.guild.role("Threads", 5, "manage_guild", "view_channel", "read_message_history", "manage_threads"))
        response = await (await self.site(threads)).get(self.path("/embeds/lookup"), params={"link": link.format(thread.id, kept.id)})
        self.assertIn("thread text", await response.text())

    async def test_log_search_follows_the_same_rule_for_manage_server(self):
        logs = self.cog(Logs)
        logs._store_entry(self.guild.id, "delete", {"title": "In the hidden channel", "description": "x"}, at=time.time(), channel_id=self.secret.id)
        logs._store_entry(self.guild.id, "delete", {"title": "In general", "description": "x"}, at=time.time(), channel_id=self.channel.id)
        await logs.flush()
        self.manager_role.permissions = FakePermissions("manage_guild", "view_channel", "read_message_history")
        client = await self.site(self.manager)
        rows = (await (await client.get(self.path("/logs/search"), params={"size": "25"})).json())["rows"]
        self.assertEqual([row["embed"]["title"] for row in rows], ["In general"])

    async def test_staff_only_get_the_names_of_channels_they_can_see(self):
        await self.bot.settings.save(self.guild.id, "core", {"dashboard_staff_roles": [self.role_member.id]})
        self.member.roles.append(self.guild.role("Reader", 1, "view_channel"))
        client = await self.site(self.member)
        info = await (await client.get(self.path("/info"))).json()
        self.assertEqual(info["access"], "staff")
        names = [row["name"] for row in info["channels"]]
        self.assertIn("general", names)
        self.assertNotIn("owners-only", names)
        admin = await self.site(self.admin)
        self.assertIn("owners-only", [row["name"] for row in (await (await admin.get(self.path("/info"))).json())["channels"]])


class HiddenChannelFormsTest(SiteCase):
    modules = [AutoPurge, Sticky, Greetings, Voice, Feeds, Notifications, Triggers]

    async def test_every_form_that_names_a_channel_asks_whether_the_person_can_see_it(self):
        secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.me.id})
        channel = str(secret.id)
        message = {"content": "x"}
        client = await self.site(self.manager)
        for attempt in (
            client.put(self.path(f"/autopurge/{channel}"), json={"interval_seconds": 3600, "message_type": "all"}),
            client.post(self.path("/sticky"), json={"channel_id": channel, "payload": message, "forever": True}),
            client.post(self.path("/greetings/test"), json={"which": "welcome", "channel_id": channel, "payload": message}),
            client.post(self.path("/voice/button"), json={"channel_id": channel}),
            client.post(self.path("/feeds"), json={"name": "news", "role_id": str(self.role_member.id), "channel_id": channel}),
            client.post(self.path("/twitch"), json={"channel_id": channel, "name": "someone"}),
            client.post(self.path("/youtube"), json={"channel_id": channel, "link": "@someone"}),
            client.post(self.path("/triggers"), json={"match_type": 8, "trigger": "", "message": message, "channel_id": channel}),
        ):
            response = await attempt
            self.assertEqual(response.status, 400, await response.text())
            self.assertIn("can't see", (await response.json())["error"])
        self.assertEqual(secret.sent, [])

    async def test_a_saved_sticky_in_a_hidden_channel_stays_out_of_reach(self):
        secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.me.id})
        admin = await self.site(self.admin)
        response = await admin.post(self.path("/sticky"), json={"channel_id": str(secret.id), "payload": {"content": "private text"}, "forever": True})
        self.assertEqual(response.status, 200, await response.text())
        posted = len(secret.sent)
        client = await self.site(self.manager)
        self.assertEqual((await (await client.get(self.path("/sticky"))).json())["stickies"], [])
        self.assertEqual((await client.patch(self.path(f"/sticky/{secret.id}"), json={"payload": {"content": "changed"}})).status, 404)
        self.assertEqual((await client.delete(self.path(f"/sticky/{secret.id}"))).status, 404)
        self.assertEqual(len(secret.sent), posted)
        self.assertIn("private text", await (await admin.get(self.path("/sticky"))).text())

    async def test_a_trigger_can_not_move_to_or_arrive_in_a_hidden_channel(self):
        self.addAsyncCleanup(self.cog(Triggers).guard.close)
        self.manager_role.permissions = FakePermissions("manage_guild", "view_channel", "send_messages")
        secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.me.id})
        message = {"content": "x"}
        client = await self.site(self.manager)
        made = await client.post(self.path("/triggers"), json={"match_type": 8, "trigger": "", "message": message, "channel_id": str(self.channel.id)})
        self.assertEqual(made.status, 200, await made.text())
        trigger_id = (await made.json())["id"]
        self.assertEqual((await client.put(self.path(f"/triggers/{trigger_id}"), json={"channel_id": str(secret.id)})).status, 400)
        admin = await self.site(self.admin)
        self.assertEqual((await admin.put(self.path(f"/triggers/{trigger_id}"), json={"channel_id": str(secret.id)})).status, 200)
        self.assertEqual((await client.put(self.path(f"/triggers/{trigger_id}"), json={"cooldown": 5})).status, 200, "a channel someone else chose can stay")
        data = {"triggers": [{"match_type": 3, "trigger": "imported", "content": "x", "channel_id": str(secret.id)}]}
        answer = await (await client.post(self.path("/triggers/import"), json={"data": json.dumps(data)})).json()
        self.assertEqual((answer["imported"], answer["rows"][0]["action"]), (0, "skip"))


class PrivateThreadTest(BotTest):
    modules = [Logs]

    def test_a_private_thread_does_not_hang_off_its_parent(self):
        self.assertIsNone(search_parent(discord.ChannelType.private_thread, 5))
        self.assertEqual(search_parent(discord.ChannelType.public_thread, 5), 5)
        self.assertEqual(search_parent(None, 5), 5)

    async def test_staff_who_see_the_parent_do_not_see_a_private_thread(self):
        logs = self.cog(Logs)
        thread = SimpleNamespace(id=444555666, type=discord.ChannelType.private_thread, parent_id=self.channel.id)
        self.guild.get_channel_or_thread = lambda channel_id: thread if channel_id == thread.id else FakeGuild.get_channel_or_thread(self.guild, channel_id)
        logs._emit(self.guild, "delete", {"title": "In a private thread", "description": "x"}, channel_id=thread.id)
        await logs.flush()
        row = await self.message_db.one("SELECT channel_id, parent_id FROM log_entries")
        self.assertEqual((row["channel_id"], row["parent_id"]), (thread.id, None))


class ImageTypeTest(BotTest):
    modules = [Personalize]

    async def test_a_stored_page_or_script_is_never_served(self):
        for avatar_type in ("text/html", "text/javascript", "image/svg+xml", "application/json"):
            await self.db.run(
                "INSERT INTO bot_profiles (guild_id, avatar, avatar_type, bio, updated_at) VALUES (?, ?, ?, '', 1) ON CONFLICT (guild_id) DO UPDATE SET avatar = excluded.avatar, avatar_type = excluded.avatar_type",
                (self.guild.id, base64.b64encode(b"<script>alert(1)</script>").decode(), avatar_type),
            )
            client = await self.client()
            response = await client.get(self.path("/personalize/image/avatar"))
            self.assertEqual(response.status, 404, avatar_type)

    async def test_a_real_image_type_is(self):
        await self.db.run("INSERT INTO bot_profiles (guild_id, avatar, avatar_type, bio, updated_at) VALUES (?, ?, 'image/png', '', 1)", (self.guild.id, base64.b64encode(b"\x89PNG").decode()))
        client = await self.client()
        response = await client.get(self.path("/personalize/image/avatar"))
        self.assertEqual((response.status, response.headers["Content-Type"]), (200, "image/png"))


class SignInPathTest(BotTest):
    def test_no_control_character_gets_through(self):
        for bad in ("/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "/\x7f/evil.example", "/\x01/evil.example", "/ok\tpath"):
            self.assertEqual(local_path(bad), "/servers", repr(bad))
        self.assertEqual(local_path("/dashboard/1/tags?a=b c"), "/dashboard/1/tags?a=b c")

    async def browser(self):
        client = TestClient(TestServer(build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False)))
        await client.start_server()
        self._clients.append(client)
        return client

    async def test_a_tab_in_next_can_not_leave_the_site(self):
        client = await self.browser()
        response = await client.get("/login?next=/%09/evil.example/fake", allow_redirects=False)
        self.assertEqual(response.status, 200, "someone signed out gets the page, not an error")
        token = await client.app[SITE].sessions.create(User(self.member.id, "member", None))
        client.session.cookie_jar.update_cookies({"cg_session": token})
        response = await client.get("/login?next=/%09/evil.example/fake", allow_redirects=False)
        self.assertEqual((response.status, urljoin("https://site.example/login", response.headers["Location"])), (302, "https://site.example/servers"))


class RegexWorkerTest(BotTest):
    async def test_a_slow_pattern_being_saved_does_not_hold_up_matching(self):
        guard = RegexGuard(time_limit=30.0)
        self.addAsyncCleanup(guard.close)
        # Both workers are up, so neither one's start is what gets timed.
        self.assertTrue(await guard.search("a", "a"))
        self.assertIsNone(await guard.problem("a"))
        saving = asyncio.ensure_future(guard.problem("(?i)" + "[\x80-\U0010FFFF]" * 190))  # each set walks all of Unicode with case ignored
        await asyncio.sleep(0.02)
        self.assertTrue(await guard.search("a", "a"))
        self.assertFalse(saving.done(), "matching had to wait for the pattern being saved")
        self.assertIsNone(await saving)


class BudgetTest(BotTest):
    modules = [Tags]

    def heavy(self):
        expression = "+".join(["1"] * 2400)
        return {"content": "{=(e):" + expression + "}" + "{m:{e}}" * 3000, "embeds": [], "buttons": []}

    async def test_a_heavy_tag_leaves_the_event_loop_free(self):
        asyncio.get_running_loop().set_debug(False)
        scope = Scope(guild=self.guild, member=self.member, channel=self.channel, args="")
        gaps = []

        async def ticker():
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        tick = asyncio.ensure_future(ticker())
        result = await run_tag(self.bot, self.heavy(), scope, trusted=True)
        tick.cancel()
        self.assertTrue(result.too_big)
        self.assertGreater(len(gaps), 3)
        self.assertLess(max(gaps), 0.2, "one run holds the thread for a quarter second, and the loop must keep turning through it")
        self.assertGreater(budget.spent(self.guild.id, pool=budget.STAFF), 0.05, "a run that hit the limit is the one that most needs counting")

    async def test_waiting_for_a_turn_is_not_charged_to_the_server(self):
        loop = asyncio.get_running_loop()
        real = runner_module.render_strict
        holding = threading.Event()

        def hold_the_loop():
            holding.set()
            time.sleep(0.3)

        def render_while_the_loop_is_busy(payload, scope):
            loop.call_soon_threadsafe(hold_the_loop)
            holding.wait(5)
            return real(payload, scope)

        scope = Scope(guild=self.guild, member=self.member, channel=self.channel, args="")
        with mock.patch.object(runner_module, "render_strict", render_while_the_loop_is_busy):
            self.assertTrue((await run_tag(self.bot, {"content": "Hello", "embeds": [], "buttons": []}, scope, trusted=True)).ran)
        self.assertTrue(holding.is_set())
        self.assertLess(budget.spent(self.guild.id, pool=budget.STAFF), 0.1)

    async def test_a_server_that_spends_its_minute_waits_and_others_do_not(self):
        scope = Scope(guild=self.guild, member=self.member, channel=self.channel, args="")
        budget.charge(self.guild.id, budget.STAFF, None, budget.SERVER_SECONDS)
        result = await run_tag(self.bot, {"content": "Hello", "embeds": [], "buttons": []}, scope, trusted=True)
        self.assertEqual((result.ran, result.too_big, self.channel.sent), (False, True, []))
        other = FakeGuild("Another Server")
        elsewhere = FakeChannel(other, "general")
        other.me = self.me
        result = await run_tag(self.bot, {"content": "Hello", "embeds": [], "buttons": []}, Scope(guild=other, member=self.member, channel=elsewhere, args=""), trusted=True)
        self.assertEqual((result.ran, [message.content for message in elsewhere.sent]), (True, ["Hello"]))
        budget._spent[(self.guild.id, budget.STAFF, None)] = (time.monotonic() - budget.WINDOW - 1, budget.SERVER_SECONDS)
        self.assertTrue((await run_tag(self.bot, {"content": "Hello", "embeds": [], "buttons": []}, scope, trusted=True)).ran)


class StoredTrustTest(BotTest):
    modules = [Tags]

    async def test_trust_stored_before_only_an_administrator_could_give_it_does_not_carry_over(self):
        tags = self.cog(Tags)
        namespace = f"module:{tags.key}"
        await tags._insert(self.guild.id, "old", {"content": "x", "embeds": [], "buttons": []}, owner_id=self.member.id, trusted=True)
        await self.db.run("DELETE FROM schema_steps WHERE namespace = ? AND step = ?", (namespace, len(Tags.migrations) - 1))
        await self.db.migrate(namespace, Tags.migrations)
        self.assertFalse((await tags.find(self.guild.id, "old"))["trusted"])
        await tags._insert(self.guild.id, "new", {"content": "x", "embeds": [], "buttons": []}, owner_id=self.member.id, trusted=True)
        await self.db.migrate(namespace, Tags.migrations)
        self.assertTrue((await tags.find(self.guild.id, "new"))["trusted"], "the reset runs once")


class PreviewTest(SiteCase):
    modules = [Tags, Triggers]

    async def test_trying_a_heavy_template_on_the_page_leaves_the_event_loop_free(self):
        asyncio.get_running_loop().set_debug(False)
        self.addAsyncCleanup(self.cog(Triggers).guard.close)
        heavy = {"content": "{=(e):" + "+".join(["1"] * 2400) + "}" + "{m:{e}}" * 2800, "embeds": [], "buttons": []}
        client = await self.site(self.manager)
        for route in ("/tags/preview", "/triggers/preview"):
            gaps = []

            async def ticker():
                last = time.perf_counter()
                while True:
                    await asyncio.sleep(0.005)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            tick = asyncio.ensure_future(ticker())
            started = time.perf_counter()
            response = await client.post(self.path(route), json={"message": heavy})
            took = time.perf_counter() - started
            tick.cancel()
            self.assertEqual(response.status, 200, await response.text())
            self.assertGreater(took, 0.1, "the template has to be heavy for this to mean anything")
            self.assertGreater(len(gaps), 3, route)
            self.assertLess(max(gaps), took * 0.75, route)


class TagTrustSiteTest(SiteCase):
    modules = [Tags]

    async def tag(self, name="x", content="Hello", *, trusted=False, owner=None):
        tags = self.cog(Tags)
        await tags._insert(self.guild.id, name, {"content": content, "embeds": [], "buttons": []}, owner_id=(owner or self.member).id, trusted=trusted)
        return await tags.find(self.guild.id, name)

    async def test_a_flag_saved_by_staff_lends_no_trust(self):
        tag = await self.tag(content="{=(a):cmdAdmin}{{a}:ban 1 x}Hi")
        for member in (self.manager, self.admin):
            client = await self.site(member)
            response = await client.put(self.path(f"/tags/{tag['id']}"), json={"nsfw": False, "name": "x"})
            self.assertEqual(response.status, 200, await response.text())
            self.assertFalse((await self.cog(Tags).find(self.guild.id, "x"))["trusted"])

    async def test_only_the_owner_or_an_administrator_gives_trust(self):
        tag = await self.tag()
        client = await self.site(self.manager)
        response = await client.put(self.path(f"/tags/{tag['id']}"), json={"trusted": True})
        self.assertEqual(response.status, 400)
        self.assertIn("trusted", (await response.json())["fields"])
        response = await client.post(self.path("/tags"), json={"name": "new", "message": {"content": "x"}, "trusted": True})
        self.assertEqual(response.status, 400)
        self.assertIsNone(await self.cog(Tags).find(self.guild.id, "new"))
        admin = await self.site(self.admin)
        self.assertEqual((await admin.put(self.path(f"/tags/{tag['id']}"), json={"trusted": True})).status, 200)
        self.assertTrue((await self.cog(Tags).find(self.guild.id, "x"))["trusted"])
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "tag.trust")
        self.assertTrue((await (await admin.get(self.path("/tags"))).json())["can_trust"])
        self.assertFalse((await (await client.get(self.path("/tags"))).json())["can_trust"])

    async def test_new_tags_start_untrusted_unless_asked(self):
        client = await self.site(self.admin)
        await client.post(self.path("/tags"), json={"name": "plain", "message": {"content": "x"}})
        await client.post(self.path("/tags"), json={"name": "asked", "message": {"content": "x"}, "trusted": True})
        self.assertFalse((await self.cog(Tags).find(self.guild.id, "plain"))["trusted"])
        self.assertTrue((await self.cog(Tags).find(self.guild.id, "asked"))["trusted"])

    async def test_changing_a_trusted_tag_s_text_takes_the_trust_away_unless_you_could_give_it(self):
        tag = await self.tag(trusted=True)
        client = await self.site(self.manager)
        response = await client.put(self.path(f"/tags/{tag['id']}"), json={"nsfw": True, "trusted": True})
        self.assertEqual(response.status, 200)
        self.assertTrue((await self.cog(Tags).find(self.guild.id, "x"))["trusted"], "a flag isn't the words")
        response = await client.put(self.path(f"/tags/{tag['id']}"), json={"message": {"content": "{cmdAdmin:ban 1}"}, "trusted": True})
        self.assertEqual(response.status, 200)
        self.assertFalse((await self.cog(Tags).find(self.guild.id, "x"))["trusted"])
        admin_tag = await self.tag("y", trusted=True)
        admin = await self.site(self.admin)
        await admin.put(self.path(f"/tags/{admin_tag['id']}"), json={"message": {"content": "New words"}})
        self.assertTrue((await self.cog(Tags).find(self.guild.id, "y"))["trusted"])

    async def test_lifting_a_trusted_tag_s_limits_takes_the_trust_away_unless_you_could_give_it(self):
        tag = await self.tag(trusted=True)
        role = str(self.senior.id)
        admin = await self.site(self.admin)
        self.assertEqual((await admin.put(self.path(f"/tags/{tag['id']}"), json={"role_wl": [role]})).status, 200)
        self.assertTrue((await self.cog(Tags).find(self.guild.id, "x"))["trusted"])
        client = await self.site(self.manager)
        same = await client.put(self.path(f"/tags/{tag['id']}"), json={"role_wl": [role], "channel_wl": [], "trusted": True})
        self.assertEqual(same.status, 200, await same.text())
        self.assertTrue((await self.cog(Tags).find(self.guild.id, "x"))["trusted"], "the page sends the same list back on every save")
        self.assertEqual((await client.put(self.path(f"/tags/{tag['id']}"), json={"role_wl": [], "trusted": True})).status, 200)
        self.assertFalse((await self.cog(Tags).find(self.guild.id, "x"))["trusted"])

    async def test_an_import_comes_in_untrusted_with_its_owner_only_from_someone_who_can_vouch(self):
        await self.tag("old", trusted=True)
        data = {"tags": [{"name": "old", "content": "{cmdAdmin:ban 1}"}, {"name": "fresh", "content": "{c:ban 1}", "owner_id": str(self.admin.id)}]}
        client = await self.site(self.manager)
        response = await client.post(self.path("/tags/import"), json={"data": json.dumps(data), "replace": True})
        self.assertEqual(response.status, 200, await response.text())
        tags = self.cog(Tags)
        self.assertFalse((await tags.find(self.guild.id, "old"))["trusted"])
        fresh = await tags.find(self.guild.id, "fresh")
        self.assertEqual((fresh["trusted"], fresh["owner_id"]), (False, self.manager.id))
        admin = await self.site(self.admin)
        await admin.post(self.path("/tags/import"), json={"data": json.dumps({"tags": [{"name": "kept", "content": "x", "owner_id": str(self.member.id)}]})})
        kept = await tags.find(self.guild.id, "kept")
        self.assertEqual((kept["trusted"], kept["owner_id"]), (False, self.member.id))

    async def test_an_import_reports_a_name_it_skipped(self):
        data = {"tags": [{"name": "deny", "content": "x"}, {"name": "kept", "content": "x", "aliases": ["approve", "other"]}]}
        client = await self.site(self.admin)
        body = await (await client.post(self.path("/tags/import"), json={"data": json.dumps(data), "preview": True})).json()
        rows = {row["name"]: row for row in body["rows"]}
        self.assertEqual(rows["deny"]["action"], "skip")
        self.assertIn("deny", rows["deny"]["reason"])
        self.assertIn("approve", rows["kept"]["reason"])
        body = await (await client.post(self.path("/tags/import"), json={"data": json.dumps(data)})).json()
        self.assertEqual(body["imported"], 1)
        tags = self.cog(Tags)
        self.assertIsNone(await tags.find(self.guild.id, "deny"))
        self.assertIsNone(await tags.find(self.guild.id, "approve"))
        self.assertIsNotNone(await tags.find(self.guild.id, "other"))

    async def test_a_shortcut_name_is_refused_on_the_page(self):
        client = await self.site(self.admin)
        response = await client.post(self.path("/tags"), json={"name": "deny", "message": {"content": "x"}})
        self.assertEqual(response.status, 400)
        self.assertIn("name", (await response.json())["fields"])

    def test_a_tag_named_invite_is_free(self):
        self.assertNotIn("invite", SHORTCUTS)


class TagGateway(GatewayTest):
    modules = [Config, Moderation, Roles, Automod, Tags]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = mock.patch.object(moderation_module, "AUDIT_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tags = next(m for m in self.bot.modules if isinstance(m, Tags))
        self.attacker = new_id()
        data = {"user": user_json(self.attacker, "attacker"), "roles": [self.role_member["id"]], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "premium_since": None, "avatar": None, "communication_disabled_until": None}
        self.net.members[self.attacker] = data
        self.guild._add_member(discord.Member(data=data, guild=self.guild, state=self.bot._connection))
        self.seen = []
        real = self.bot.http.request

        async def spy(route, **kwargs):
            self.seen.append((route.method, route.path))
            return await real(route, **kwargs)
        self.bot.http.request = spy

    async def typed(self, content, user_id):
        self.tags._recent.clear()
        self.dispatch_message(user_id=user_id, content=content)
        await self.settle()

    def said(self):
        return [message.get("content") for message in self.net.posted]

    def acts(self):
        return [call for call in self.seen if call[0] in ("PUT", "PATCH") or (call[0] == "DELETE" and "members" in call[1])]


class CommandBlockTest(TagGateway):
    async def test_a_member_s_tag_does_nothing_with_a_moderator_s_permissions(self):
        await self.typed(f"!tag create rules {{silent}}{{c:kick {self.member_id} spam}}{{c:ban {self.member_id} spam}}Be nice.", self.attacker)
        self.assertFalse((await self.tags.find(self.guild_id, "rules"))["trusted"])
        self.seen.clear()
        await self.typed("!rules", self.admin_id)
        self.assertEqual(self.acts(), [])
        self.assertIn("Be nice.", self.said())
        self.assertTrue(any("only run for its owner" in (line or "") for line in self.said()), "the person running it hears why")

    async def test_the_owner_still_runs_their_own_tag_s_commands_at_their_own_level(self):
        await self.typed("!tag create mine {c:prefix list}", self.attacker)
        await self.typed("!mine", self.attacker)
        self.assertEqual(self.said()[-1], "Prefixes: `!`, `?`")
        await self.typed("!tag create rough {c:prefix add ?}", self.attacker)
        await self.typed("!rough", self.attacker)
        self.assertEqual(self.bot.settings.get(self.guild_id, "core")["prefixes"], ["!", "?"])

    async def test_a_trusted_tag_runs_its_commands_for_anyone(self):
        await self.tags._insert(self.guild_id, "open", {"content": "{c:prefix list}", "embeds": [], "buttons": []}, owner_id=self.owner_id, trusted=True)
        await self.typed("!open", self.attacker)
        self.assertEqual(self.said()[-1], "Prefixes: `!`, `?`")

    async def test_a_tag_can_not_take_a_shortcut_s_name(self):
        await self.typed("!tag create deny {c:ban 1 x}", self.attacker)
        self.assertIsNone(await self.tags.find(self.guild_id, "deny"))
        await self.tags._insert(self.guild_id, "real", {"content": "x", "embeds": [], "buttons": []}, owner_id=self.attacker, trusted=False)
        await self.typed("!tag alias approve real", self.attacker)
        self.assertIsNone(await self.tags.find(self.guild_id, "approve"))

    async def test_a_tag_already_holding_a_shortcut_s_name_still_can_not_act_for_a_moderator(self):
        await self.tags._insert(self.guild_id, "deny", {"content": f"{{silent}}{{c:ban {self.member_id} x}}Denied.", "embeds": [], "buttons": []}, owner_id=self.attacker, trusted=False)
        self.seen.clear()
        await self.typed("!deny 5 not now", self.admin_id)
        self.assertEqual(self.acts(), [])


class PowerBlockTest(TagGateway):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_mod["id"])})

    async def test_a_bot_manager_can_not_reach_a_higher_role_through_cmdadmin(self):
        await self.typed(f"!tag create up {{cmdAdmin:role add {self.mod_id} {self.role_admin['id']}}}", self.mod_id)
        self.assertFalse((await self.tags.find(self.guild_id, "up"))["trusted"])
        self.seen.clear()
        await self.typed("!up", self.mod_id)
        self.assertEqual([call for call in self.seen if "roles" in call[1]], [])

    async def test_a_bot_manager_s_tag_bans_only_for_the_bot_manager(self):
        await self.typed(f"!tag create b {{cmdAdmin:ban {self.member_id} x}}", self.mod_id)
        self.assertFalse((await self.tags.find(self.guild_id, "b"))["trusted"])
        self.seen.clear()
        await self.typed("!b", self.member_id)
        self.assertEqual([call for call in self.seen if "bans" in call[1]], [], "nobody borrows a moderator's power by running their tag")
        await self.typed("!b", self.mod_id)
        self.assertEqual(len([call for call in self.seen if "bans" in call[1]]), 1, "they could ban by hand, and the block runs at their own permissions")

    async def test_a_trusted_tag_keeps_its_power_until_someone_else_edits_it(self):
        tag_id = await self.tags._insert(self.guild_id, "up", {"content": f"{{cmdAdmin:ban {self.member_id} x}}", "embeds": [], "buttons": []}, owner_id=self.mod_id, trusted=True)
        await self.typed("!tag append up more", self.mod_id)
        self.assertFalse((await self.tags._by_id(self.guild_id, tag_id))["trusted"], "a bot manager can edit it and can't vouch for it")
        await self.db.run("UPDATE tags SET trusted = 1 WHERE id = ?", (tag_id,))
        await self.typed("!tag append up again", self.admin_id)
        self.assertTrue((await self.tags._by_id(self.guild_id, tag_id))["trusted"], "an administrator can")


class TriggerTrustTest(TagGateway):
    """A trigger runs for whoever sets it off, so it's the same door as a tag, and only someone who could trust a tag can open it."""
    modules = [Config, Moderation, Roles, Automod, Tags, Triggers]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.guild.get_role(int(self.role_mod["id"]))._permissions = discord.Permissions(manage_guild=True).value
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True).value
        self.triggers = next(m for m in self.bot.modules if isinstance(m, Triggers))
        self.addAsyncCleanup(self.triggers.guard.close)

    async def site(self, user_id):
        self.bot.ops.access_for = lambda guild_id, person: "full"
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(user_id, "person", None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    def path(self, tail):
        return f"/api/g/{self.guild_id}{tail}"

    def form(self, phrase, content, **more):
        return {"match_type": 1, "trigger": phrase, "message": {"content": content, "embeds": [], "buttons": []}, **more}

    async def test_manage_server_can_not_reach_the_bot_s_power_through_a_trigger(self):
        client = await self.site(self.mod_id)
        response = await client.post(self.path("/triggers"), json=self.form("letmein", f"{{cmdAdmin:role add {self.mod_id} {self.role_admin['id']}}}"))
        self.assertEqual(response.status, 200, await response.text())
        self.seen.clear()
        await self.typed("letmein", self.mod_id)
        self.assertEqual([call for call in self.seen if "roles" in call[1]], [])

    async def test_a_trigger_can_not_borrow_the_permissions_of_whoever_sets_it_off(self):
        client = await self.site(self.mod_id)
        await client.post(self.path("/triggers"), json=self.form("hello", f"{{silent}}{{c:ban {self.member_id} x}}Hi."))
        self.seen.clear()
        await self.typed("hello there", self.admin_id)
        self.assertEqual(self.acts(), [])
        self.assertIn("Hi.", self.said())

    async def test_only_the_owner_or_an_administrator_trusts_a_trigger(self):
        client = await self.site(self.mod_id)
        response = await client.post(self.path("/triggers"), json=self.form("one", "x", trusted=True))
        self.assertEqual(response.status, 400)
        self.assertIn("trusted", (await response.json())["fields"])
        trigger_id = (await (await client.post(self.path("/triggers"), json=self.form("two", f"{{cmdAdmin:ban {self.member_id} x}}"))).json())["id"]
        self.assertEqual((await client.put(self.path(f"/triggers/{trigger_id}"), json={"trusted": True})).status, 400)
        self.assertFalse((await (await client.get(self.path("/triggers"))).json())["can_trust"])
        admin = await self.site(self.admin_id)
        self.assertTrue((await (await admin.get(self.path("/triggers"))).json())["can_trust"])
        self.assertEqual((await admin.put(self.path(f"/triggers/{trigger_id}"), json={"trusted": True})).status, 200)
        self.seen.clear()
        await self.typed("two", self.member_id)
        self.assertTrue([call for call in self.seen if "bans" in call[1]], "a trusted trigger keeps its power")
        # Someone who couldn't vouch for it takes that away by changing its words, and a change to anything else leaves it.
        self.assertEqual((await client.put(self.path(f"/triggers/{trigger_id}"), json={"cooldown": 5})).status, 200)
        self.assertTrue((await (await client.get(self.path("/triggers"))).json())["triggers"][0]["trusted"])
        self.assertEqual((await client.put(self.path(f"/triggers/{trigger_id}"), json={"message": {"content": "Changed"}})).status, 200)
        self.assertFalse((await (await client.get(self.path("/triggers"))).json())["triggers"][0]["trusted"])

    async def trusted_trigger(self, phrase="two"):
        admin = await self.site(self.admin_id)
        form = self.form(phrase, f"{{cmdAdmin:ban {self.member_id} x}}", trusted=True, role_wl=[self.role_admin["id"]])
        response = await admin.post(self.path("/triggers"), json=form)
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["id"]

    async def trusted_now(self, client):
        return (await (await client.get(self.path("/triggers"))).json())["triggers"][0]["trusted"]

    async def test_changing_what_sets_off_a_trusted_trigger_takes_the_trust_away(self):
        client = await self.site(self.mod_id)
        for change in ({"role_wl": []}, {"trigger": "anyone"}, {"match_type": 2}):
            trigger_id = await self.trusted_trigger()
            same = await client.put(self.path(f"/triggers/{trigger_id}"), json={"role_wl": [self.role_admin["id"]], "trigger": "two", "match_type": 1, "cooldown": 3})
            self.assertEqual(same.status, 200, await same.text())
            self.assertTrue(await self.trusted_now(client), "the page sends everything back on every save")
            self.assertEqual((await client.put(self.path(f"/triggers/{trigger_id}"), json=change)).status, 200)
            self.assertFalse(await self.trusted_now(client), change)
            self.assertEqual((await client.delete(self.path(f"/triggers/{trigger_id}"))).status, 200)

    async def test_the_role_commands_take_the_trust_away_too(self):
        client = await self.site(self.mod_id)
        trigger_id = await self.trusted_trigger()
        await self.typed(f"!triggers role_whitelist {self.role_admin['id']} {trigger_id}", self.mod_id)
        rows = (await (await client.get(self.path("/triggers"))).json())["triggers"]
        self.assertEqual((rows[0]["role_wl"], rows[0]["trusted"]), ([], False))
        trigger_id = await self.trusted_trigger("three")
        await self.typed(f"!triggers role_blacklist {self.role_member['id']} {trigger_id}", self.admin_id)
        row = next(row for row in (await (await client.get(self.path("/triggers"))).json())["triggers"] if row["id"] == trigger_id)
        self.assertEqual(row["trusted"], True, "an Administrator could have trusted it anyway")
        self.assertEqual(row["role_bl"], [self.role_member["id"]])

    async def test_an_import_comes_in_untrusted(self):
        client = await self.site(self.admin_id)
        data = {"triggers": [{"match_type": 1, "trigger": "imported", "content": f"{{cmdAdmin:ban {self.member_id} x}}", "trusted": True}]}
        response = await client.post(self.path("/triggers/import"), json={"data": json.dumps(data)})
        self.assertEqual(response.status, 200, await response.text())
        self.assertFalse((await (await client.get(self.path("/triggers"))).json())["triggers"][0]["trusted"])

    async def test_an_untrusted_trigger_can_still_ping_and_post_elsewhere(self):
        client = await self.site(self.mod_id)
        await client.post(self.path("/triggers"), json=self.form("hello", f"<@{self.owner_id}> look{{redirect:logs}}"))
        self.net.calls.clear()
        await self.typed("hello", self.member_id)
        body = [call[2] for call in self.net.calls if call[0] == "POST" and call[1].endswith("/messages")][-1]
        self.assertIn(self.owner_id, [int(user) for user in body["allowed_mentions"]["users"]])
        self.assertEqual(self.net.posted[-1]["channel_id"], self.log_channel["id"])


class ClaimTest(TagGateway):
    async def test_claiming_lends_no_trust(self):
        await self.tags._insert(self.guild_id, "left", {"content": "x", "embeds": [], "buttons": []}, owner_id=new_id(), trusted=False)
        await self.typed("!tag claim left", self.admin_id)
        tag = await self.tags.find(self.guild_id, "left")
        self.assertEqual((tag["owner_id"], tag["trusted"]), (self.admin_id, False))


class TagOutputTest(TagGateway):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "automod", {
            "words": {"enabled": True, "entries": ["scamword"], "punishment": {"actions": 1}},
        })
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True).value

    def slash_tag(self, sub, values, *, channel=None, user_id=None):
        options = [{"name": name, "type": 3, "value": value} for name, value in values.items()]
        data = {"id": str(new_id()), "name": "tag", "type": 1, "options": [{"name": sub, "type": 1, "options": options}], "resolved": {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id or self.attacker, channel=channel))

    async def test_an_untrusted_tag_mentions_nobody_and_a_trusted_one_can(self):
        await self.tags._insert(self.guild_id, "mine", {"content": f"<@{self.owner_id}> look", "embeds": [], "buttons": []}, owner_id=self.attacker, trusted=False)
        self.net.calls.clear()
        await self.typed("!mine", self.attacker)
        bodies = [call[2] for call in self.net.calls if call[0] == "POST" and call[1].endswith("/messages")]
        self.assertEqual(bodies[-1]["allowed_mentions"]["parse"], [])
        await self.tags._insert(self.guild_id, "staff", {"content": f"<@{self.owner_id}> look", "embeds": [], "buttons": []}, owner_id=self.admin_id, trusted=True)
        self.net.calls.clear()
        await self.typed("!staff", self.attacker)
        bodies = [call[2] for call in self.net.calls if call[0] == "POST" and call[1].endswith("/messages")]
        self.assertIn(self.owner_id, [int(user) for user in bodies[-1]["allowed_mentions"]["users"]])

    async def test_a_member_s_words_meet_the_word_rule_when_saved(self):
        self.slash_tag("create", {"name": "free", "content": "buy scamword now"})
        await self.settle()
        self.assertIsNone(await self.tags.find(self.guild_id, "free"))
        self.slash_tag("create", {"name": "scamword", "content": "fine"})
        await self.settle()
        self.assertIsNone(await self.tags.find(self.guild_id, "scamword"))
        await self.tags._insert(self.guild_id, "real", {"content": "fine", "embeds": [], "buttons": []}, owner_id=self.attacker, trusted=False)
        await self.typed("!tag edit real has scamword in it", self.attacker)
        self.assertEqual((await self.tags.find(self.guild_id, "real"))["message"]["content"], "fine")
        await self.typed("!tag append real scamword", self.attacker)
        self.assertEqual((await self.tags.find(self.guild_id, "real"))["message"]["content"], "fine")
        self.slash_tag("alias", {"point": "scamword2", "name": "real"})
        await self.settle()
        self.assertIsNone(await self.tags.find(self.guild_id, "scamword2"))

    async def test_a_word_past_what_a_pattern_reads_at_once_is_still_caught(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": [{"text": "sc[a]mword", "kind": "regex"}], "punishment": {"actions": 1}}})
        automod = next(m for m in self.bot.modules if isinstance(m, Automod))
        self.addAsyncCleanup(automod.regex.close)
        member = self.guild.get_member(self.attacker)
        self.assertIsNone(await automod.word_problem(member, "clean text " * 2000))
        self.assertIsNotNone(await automod.word_problem(member, "clean text " * 2000 + "scamword"))

    async def test_manage_server_and_an_exempt_role_skip_the_word_rule_when_saving(self):
        self.slash_tag("create", {"name": "free", "content": "buy scamword now"}, user_id=self.admin_id)
        await self.settle()
        self.assertIsNotNone(await self.tags.find(self.guild_id, "free"))
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["scamword"], "exempt_roles": [int(self.role_member["id"])], "punishment": {"actions": 1}}})
        self.slash_tag("create", {"name": "also", "content": "buy scamword now"})
        await self.settle()
        self.assertIsNotNone(await self.tags.find(self.guild_id, "also"))

    def channel_where_members_can_not_post(self):
        deny = str(discord.Permissions(send_messages=True, send_messages_in_threads=True).value)
        data = {**self.text, "id": str(new_id()), "name": "announcements", "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": deny}]}
        self.bot._connection.parse_channel_create(data)
        self.net.channels[int(data["id"])] = data
        return data

    async def test_tag_get_refuses_someone_who_can_not_post_there(self):
        await self.tags._insert(self.guild_id, "free", {"content": "posted anyway", "embeds": [], "buttons": []}, owner_id=self.attacker, trusted=False)
        self.slash_tag("get", {"name": "free"}, channel=self.channel_where_members_can_not_post())
        await self.settle()
        self.assertNotIn("posted anyway", self.said())
        self.assertTrue(any("can't post in this channel" in (line or "") for line in self.said()))

    async def test_tag_get_works_for_someone_who_can(self):
        await self.tags._insert(self.guild_id, "free", {"content": "posted", "embeds": [], "buttons": []}, owner_id=self.attacker, trusted=False)
        self.slash_tag("get", {"name": "free"})
        await self.settle()
        self.assertIn("posted", self.said())


class AliasLimitTest(BotTest):
    modules = [Tags]

    async def test_a_member_can_only_make_so_many_aliases(self):
        tags = self.cog(Tags)
        await tags._insert(self.guild.id, "real", {"content": "x", "embeds": [], "buttons": []}, owner_id=self.member.id, trusted=False)
        with mock.patch("captainguild.modules.tags.MEMBER_TAG_LIMIT", 2):
            for name in ("one", "two"):
                ctx = self.context(author=self.member)
                await tags.tag_alias.callback(tags, ctx, name, "real")
                self.assertEqual(ctx.fails, [])
            ctx = self.context(author=self.member)
            await tags.tag_alias.callback(tags, ctx, "three", "real")
            self.assertIn("limit", ctx.fails[0])
            ctx = self.context(author=self.admin)
            await tags.tag_alias.callback(tags, ctx, "three", "real")
            self.assertEqual(ctx.fails, [])
