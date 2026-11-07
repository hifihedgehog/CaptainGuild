"""Limits that cross features: what a viewer may read, which server a setting may point at, who may take all the data out, and what comes back after a restore."""
from __future__ import annotations

import time

from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.ops import DiscordOps
from captainguild.core.perms import PERMISSION_LABELS
from captainguild.modules import logs as logs_module
from captainguild.modules.backup import Backup
from captainguild.modules.logs import Logs
from captainguild.modules.reactionroles import ReactionRoles
from captainguild.modules.embeds import Embeds
from captainguild.modules.sticky import Sticky, _Live
from captainguild.modules.triggers import Triggers
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakePermissions


class Case(BotTest):
    async def site(self, member: FakeMember, access: str | None = None) -> TestClient:
        """A signed-in browser for this person. Without `access`, the real rules decide what they can open."""
        if access is not None:
            self.bot.ops.access_for = lambda guild_id, user_id: access if guild_id == self.guild.id else None
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client


class HiddenChannel(FakeChannel):
    """A channel only people with the secret role can see."""

    def __init__(self, guild: FakeGuild, name: str, allowed: set[int]) -> None:
        super().__init__(guild, name)
        self.allowed = allowed

    def permissions_for(self, member):
        permissions = super().permissions_for(member)
        if member.id in self.allowed:
            return permissions
        return FakePermissions(*(set(permissions.names) - {"view_channel", "read_messages", "administrator"}))


class LogSearchTest(Case):
    modules = [Logs]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.logs = self.cog(Logs)
        self.mod.roles.append(self.guild.role("Reader", 4, "view_channel", "read_message_history"))
        self.secret = HiddenChannel(self.guild, "staff-only", {self.admin.id, self.owner.id})
        now = time.time()
        entries = [
            ("delete", "In general", self.channel.id, None),
            ("delete", "In the secret channel", self.secret.id, None),
            ("threadcreate", "In a thread of general", 111222333, self.channel.id),
            ("threadcreate", "In a thread of the secret channel", 444555666, self.secret.id),
            ("join", "No channel at all", None, None),
        ]
        for event, title, channel_id, parent_id in entries:
            self.logs._store_entry(self.guild.id, event, logs_module.make_embed(title, description="text", at=now), at=now, channel_id=channel_id, parent_id=parent_id)
        await self.logs.flush()

    async def titles(self, client: TestClient) -> set[str]:
        response = await client.get(f"/api/g/{self.guild.id}/logs/search", params={"size": "25"})
        self.assertEqual(response.status, 200, await response.text())
        return {row["embed"]["title"] for row in (await response.json())["rows"]}

    async def test_staff_see_only_channels_discord_shows_them(self):
        client = await self.site(self.mod, access="staff")
        self.assertEqual(await self.titles(client), {"In general", "In a thread of general", "No channel at all"})

    async def test_full_access_sees_everything(self):
        client = await self.site(self.admin, access="full")
        self.assertEqual(len(await self.titles(client)), 5)

    async def test_someone_gone_from_the_server_sees_no_channel(self):
        stranger = FakeMember(FakeGuild(), "stranger")
        client = await self.site(stranger, access="staff")
        self.assertEqual(await self.titles(client), {"No channel at all"})

    async def test_opening_one_entry_follows_the_same_rule(self):
        client = await self.site(self.admin, access="full")
        rows = (await (await client.get(f"/api/g/{self.guild.id}/logs/search", params={"size": "25"})).json())["rows"]
        ids = {row["embed"]["title"]: row["id"] for row in rows}
        staff = await self.site(self.mod, access="staff")
        self.assertEqual((await staff.get(f"/api/g/{self.guild.id}/logs/entries/{ids['In general']}")).status, 200)
        self.assertEqual((await staff.get(f"/api/g/{self.guild.id}/logs/entries/{ids['In the secret channel']}")).status, 404)
        self.assertEqual((await staff.get(f"/api/g/{self.guild.id}/logs/entries/{ids['In a thread of the secret channel']}")).status, 404)

    async def test_an_export_follows_the_same_rule(self):
        client = await self.site(self.mod, access="staff")
        for kind in ("csv", "json"):
            response = await client.get(f"/api/g/{self.guild.id}/logs/export", params={"format": kind})
            self.assertEqual(response.status, 200)
            text = await response.text()
            self.assertIn("In general", text)
            self.assertNotIn("secret", text)


class OtherServerTest(Case):
    modules = [Logs]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other = FakeGuild("Another Server")
        self.bot.fake_guilds.append(self.other)
        self.other_channel = FakeChannel(self.other, "their-logs")
        self.other_role = self.other.role("Their role", 3)

    async def test_a_channel_from_another_server_is_refused(self):
        client = await self.site(self.admin, access="full")
        response = await client.put(f"/api/g/{self.guild.id}/settings/logging", json={"default_channel": str(self.other_channel.id)})
        self.assertEqual(response.status, 400, await response.text())
        self.assertIn("default_channel", (await response.json())["fields"])

    async def test_a_role_from_another_server_is_refused(self):
        client = await self.site(self.admin, access="full")
        response = await client.put(f"/api/g/{self.guild.id}/settings/core", json={"dashboard_staff_roles": [str(self.other_role.id)]})
        self.assertEqual(response.status, 400, await response.text())

    async def test_this_servers_own_channel_is_fine(self):
        client = await self.site(self.admin, access="full")
        response = await client.put(f"/api/g/{self.guild.id}/settings/logging", json={"default_channel": str(self.channel.id)})
        self.assertEqual(response.status, 200, await response.text())


class ExportTest(Case):
    modules = [Backup]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager_role = self.guild.role("Manager", 6, "manage_guild")
        self.manager = FakeMember(self.guild, "manager", self.manager_role)

    async def test_manage_server_alone_cant_take_the_data_out(self):
        client = await self.site(self.manager, access="full")
        overview = await (await client.get(f"/api/g/{self.guild.id}/backup")).json()
        self.assertFalse(overview["admin"])
        tail = f"/api/g/{self.guild.id}/backup"
        self.assertEqual((await client.get(f"{tail}/export")).status, 403)
        self.assertEqual((await client.post(f"{tail}/upload", data=b"x")).status, 403)
        self.assertEqual((await client.post(f"{tail}/restore", json={})).status, 403)
        self.assertEqual((await client.post(f"{tail}/snapshots", json={})).status, 403)
        self.assertEqual((await client.get(f"{tail}/snapshots/1/download")).status, 403)
        self.assertEqual((await client.delete(f"{tail}/snapshots/1")).status, 403)

    async def test_an_administrator_can(self):
        client = await self.site(self.admin, access="full")
        overview = await (await client.get(f"/api/g/{self.guild.id}/backup")).json()
        self.assertTrue(overview["admin"])
        response = await client.get(f"/api/g/{self.guild.id}/backup/export")
        self.assertEqual(response.status, 200, await response.text())
        await response.read()


class RestoredTest(Case):
    modules = [Sticky, Triggers]

    async def test_sticky_messages_reload_from_their_table(self):
        sticky = self.cog(Sticky)
        sticky._live[123] = _Live(self.guild.id, 1)
        await sticky.on_guild_restored(self.guild.id)
        self.assertNotIn(123, sticky._live, "A channel the restored table doesn't have is forgotten")

    async def test_triggers_read_their_table_again(self):
        triggers = self.cog(Triggers)
        triggers._rows_by_guild[self.guild.id] = [{"stale": True}]
        await triggers.on_guild_restored(self.guild.id)
        self.assertNotIn(self.guild.id, triggers._rows_by_guild)


class LibraryTest(Case):
    modules = [Embeds, ReactionRoles]

    async def test_a_role_post_from_the_site_joins_the_message_library(self):
        self.bot.ops = DiscordOps(self.bot)
        rr = self.cog(ReactionRoles)
        from captainguild.modules.reactionroles import RR
        record = RR(555, self.guild.id, self.channel.id, created_at=time.time())
        await rr._add_to_library(record, {"content": "Pick a role", "embeds": [], "buttons": []}, self.bot.actor(self.admin))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_messages WHERE guild_id = ? AND message_id = 555", (self.guild.id,)), 1)


class WordingTest(Case):
    modules = []

    async def test_permissions_use_discords_names(self):
        self.assertEqual(PERMISSION_LABELS["manage_guild"], "Manage Server")
