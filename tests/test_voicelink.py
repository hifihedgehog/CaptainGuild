import asyncio
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.modules import voicelink as voicelink_module
from captainguild.modules.backup import Backup
from captainguild.modules.voicelink import VoiceLink
from tests.fakes import BotTest, FakeChannel, FakeMember, _FakeResponse
from tests.gateway import GatewayTest
from tests.test_backup import BackupCase


class Base(BotTest):
    modules = [VoiceLink]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.link = self.cog(VoiceLink)
        self.lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        self.gaming = FakeChannel(self.guild, "Gaming", discord.ChannelType.voice)
        self.in_voice = self.guild.role("In voice", 3)
        self.gamer = self.guild.role("Gamer", 3)

    async def asyncTearDown(self) -> None:
        await self.link.cog_unload()
        await super().asyncTearDown()

    async def links(self, *rows) -> None:
        await self.bot.settings.save(self.guild.id, "voicelink", {"links": [{"channel_id": channel.id, "roles": [r.id for r in roles]} for channel, roles in rows]})
        await self.link.settle()

    async def move(self, member: FakeMember, channel: FakeChannel | None) -> None:
        before = SimpleNamespace(channel=member.voice.channel if member.voice else None)
        if before.channel is not None:
            before.channel.members.remove(member)
        member.voice = SimpleNamespace(channel=channel) if channel else None
        if channel is not None:
            channel.members.append(member)
        await self.link.on_voice_state_update(member, before, SimpleNamespace(channel=channel))
        await self.link.settle()


class VoiceLinkTest(Base):
    async def test_joining_gives_and_leaving_takes(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        self.assertIn(self.in_voice, self.member.roles)
        await self.move(self.member, None)
        self.assertNotIn(self.in_voice, self.member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 0)

    async def test_several_roles(self):
        await self.links((self.lounge, [self.in_voice, self.gamer]))
        await self.move(self.member, self.lounge)
        self.assertIn(self.in_voice, self.member.roles)
        self.assertIn(self.gamer, self.member.roles)

    async def test_moving_swaps_only_what_differs(self):
        await self.links((self.lounge, [self.in_voice]), (self.gaming, [self.in_voice, self.gamer]))
        await self.move(self.member, self.lounge)
        self.member.calls.clear()
        await self.move(self.member, self.gaming)
        self.assertIn(self.in_voice, self.member.roles)
        self.assertIn(self.gamer, self.member.roles)
        self.assertEqual([c for c in self.member.calls if c[0] == "remove_roles"], [])
        await self.move(self.member, self.lounge)
        self.assertIn(self.in_voice, self.member.roles)
        self.assertNotIn(self.gamer, self.member.roles)

    async def test_someone_who_held_the_role_already_keeps_it(self):
        await self.links((self.lounge, [self.in_voice]))
        self.member.roles.append(self.in_voice)
        await self.move(self.member, self.lounge)
        await self.move(self.member, None)
        self.assertIn(self.in_voice, self.member.roles)

    async def test_an_unlinked_channel_does_nothing(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.gaming)
        self.assertEqual(self.member.calls, [])

    async def test_mute_changes_cost_nothing(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        self.member.calls.clear()
        same = SimpleNamespace(channel=self.lounge)
        await self.link.on_voice_state_update(self.member, same, same)
        await self.link.settle()
        self.assertEqual(self.member.calls, [])

    async def test_bots_get_nothing(self):
        other = FakeMember(self.guild, "music", bot=True)
        await self.links((self.lounge, [self.in_voice]))
        await self.move(other, self.lounge)
        self.assertNotIn(self.in_voice, other.roles)

    async def test_a_role_above_the_bot_or_with_power_is_refused(self):
        high = self.guild.role("High", 20)
        strong = self.guild.role("Strong", 3, "ban_members")
        await self.links((self.lounge, [high, strong, self.in_voice]))
        await self.move(self.member, self.lounge)
        self.assertEqual(self.member.roles, [self.guild.default_role, self.role_member, self.in_voice])
        await self.move(self.member, None)
        await self.bot.settings.save(self.guild.id, "commands", {"elevated_roles": True})
        await self.move(self.member, self.lounge)
        self.assertIn(strong, self.member.roles)

    async def test_fast_hopping_ends_up_right(self):
        await self.links((self.lounge, [self.in_voice]), (self.gaming, [self.gamer]))
        real = self.member.add_roles

        async def slow(*roles, **kwargs):
            await asyncio.sleep(0.005)
            await real(*roles, **kwargs)

        self.member.add_roles = slow
        for channel in (self.lounge, self.gaming, None, self.lounge, self.gaming):
            before = SimpleNamespace(channel=self.member.voice.channel if self.member.voice else None)
            self.member.voice = SimpleNamespace(channel=channel) if channel else None
            await self.link.on_voice_state_update(self.member, before, SimpleNamespace(channel=channel))
        await self.link.settle()
        self.assertIn(self.gamer, self.member.roles)
        self.assertNotIn(self.in_voice, self.member.roles)
        rows = await self.db.all("SELECT role_id FROM voicelink_held")
        self.assertEqual([row["role_id"] for row in rows], [self.gamer.id])

    async def test_leaving_and_rejoining_before_the_gateway_catches_up_keeps_the_role(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        calls = []

        async def remove_without_updating_cache(*roles, **kwargs):
            calls.append("remove")

        async def add_without_updating_cache(*roles, **kwargs):
            calls.append("add")

        self.member.remove_roles = remove_without_updating_cache
        self.member.add_roles = add_without_updating_cache
        await self.move(self.member, None)
        self.assertIn(self.in_voice, self.member.roles)  # the stale copy, as the gateway would still show it
        await self.move(self.member, self.lounge)
        self.assertEqual(calls, ["remove", "add"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)

    async def test_a_new_link_covers_people_already_in_the_channel(self):
        self.member.voice = SimpleNamespace(channel=self.lounge)
        self.lounge.members.append(self.member)
        await self.links((self.lounge, [self.in_voice]))
        self.assertIn(self.in_voice, self.member.roles)

    async def test_a_link_to_a_text_channel_looks_at_nobody(self):
        self.channel.members.extend(self.guild.members)
        await self.links((self.channel, [self.in_voice]))
        self.assertEqual(self.link._running, {})
        self.assertNotIn(self.in_voice, self.member.roles)

    async def test_a_removed_link_takes_the_roles_back(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        await self.links()
        self.assertNotIn(self.in_voice, self.member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 0)

    async def test_startup_catches_what_happened_during_downtime(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        # They left while the bot was down, and someone else joined.
        self.lounge.members.remove(self.member)
        self.member.voice = None
        self.mod.voice = SimpleNamespace(channel=self.lounge)
        self.lounge.members.append(self.mod)
        fresh = VoiceLink(self.bot)
        await fresh.on_ready()
        await fresh._startup
        await fresh.settle()
        self.assertNotIn(self.in_voice, self.member.roles)
        self.assertIn(self.in_voice, self.mod.roles)

    async def test_someone_who_left_the_server_while_down_is_forgotten(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        self.guild.members.remove(self.member)
        self.lounge.members.remove(self.member)
        await self.link._startup_checks()
        await self.link.settle()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 0)

    async def test_leaving_the_server_forgets_them(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        await self.link.on_member_remove(self.member)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 0)

    async def test_a_restored_backup_gets_checked(self):
        await self.links((self.lounge, [self.in_voice]))
        self.member.voice = SimpleNamespace(channel=self.lounge)
        self.lounge.members.append(self.member)
        with mock.patch.object(voicelink_module, "RESTORE_SETTLE", 0):
            await self.link.on_guild_restored(self.guild.id)
            await asyncio.gather(*self.link._after_restores)
        await self.link.settle()
        self.assertIn(self.in_voice, self.member.roles)

    async def test_a_failure_tries_again_at_the_next_check(self):
        await self.links((self.lounge, [self.in_voice]))
        real = self.member.add_roles

        async def refused(*roles, **kwargs):
            raise discord.HTTPException(_FakeResponse(503), "down")

        self.member.add_roles = refused
        await self.move(self.member, self.lounge)
        self.assertNotIn(self.in_voice, self.member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 0)
        self.member.add_roles = real
        await self.link._check_guild(self.guild.id)
        await self.link.settle()
        self.assertIn(self.in_voice, self.member.roles)

    async def test_a_failed_removal_stays_on_record(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)

        async def refused(*roles, **kwargs):
            raise discord.HTTPException(_FakeResponse(503), "down")

        self.member.remove_roles = refused
        await self.move(self.member, None)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)

    async def test_a_deleted_channel_or_role_leaves_the_links(self):
        await self.links((self.lounge, [self.in_voice]), (self.gaming, [self.gamer, self.in_voice]))
        await self.link.on_guild_channel_delete(SimpleNamespace(guild=self.guild, id=self.lounge.id))
        self.assertEqual([row["channel_id"] for row in self.settings("voicelink")["links"]], [self.gaming.id])
        await self.link.on_guild_role_delete(SimpleNamespace(guild=self.guild, id=self.in_voice.id))
        self.assertEqual(self.settings("voicelink")["links"][0]["roles"], [self.gamer.id])
        await self.link.on_guild_role_delete(SimpleNamespace(guild=self.guild, id=self.gamer.id))
        self.assertEqual(self.settings("voicelink")["links"], [])
        await self.link.settle()

    async def test_an_unrelated_delete_saves_nothing(self):
        await self.links((self.lounge, [self.in_voice]))
        with mock.patch.object(self.bot.settings, "save") as save:
            await self.link.on_guild_channel_delete(SimpleNamespace(guild=self.guild, id=self.gaming.id))
            await self.link.on_guild_role_delete(SimpleNamespace(guild=self.guild, id=self.gamer.id))
        save.assert_not_called()


class DashboardTest(Base):
    async def test_saving_from_the_dashboard(self):
        client = await self.client()
        body = {"links": [{"channel_id": str(self.lounge.id), "roles": [str(self.in_voice.id)]}]}
        response = await client.put(self.path("/settings/voicelink"), json=body)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["links"], body["links"])
        self.assertEqual(self.settings("voicelink")["links"], [{"channel_id": self.lounge.id, "roles": [self.in_voice.id]}])
        rules = await (await client.get(self.path("/voicelink/rules"))).json()
        self.assertIn("ban_members", rules["dangerous"])
        self.assertFalse(rules["elevated_roles"])

    async def test_bad_links_are_refused(self):
        client = await self.client()
        lounge = str(self.lounge.id)
        role = str(self.in_voice.id)
        for links in ("nope", [{"channel_id": "nope", "roles": [role]}], [{"channel_id": lounge, "roles": []}], [{"channel_id": lounge, "roles": ["nope"]}], ["nope"],
                      [{"channel_id": lounge, "roles": [role]}, {"channel_id": lounge, "roles": [role]}]):
            response = await client.put(self.path("/settings/voicelink"), json={"links": links})
            self.assertEqual(response.status, 400, str(links))
            self.assertIn("links", (await response.json())["fields"])

    async def test_an_id_from_another_server_finds_nothing(self):
        other = 123456789012345678
        await self.bot.settings.save(self.guild.id, "voicelink", {"links": [{"channel_id": other, "roles": [other]}]})
        self.member.voice = SimpleNamespace(channel=self.lounge)
        await self.link._check_guild(self.guild.id)
        await self.link.settle()
        self.assertEqual(self.member.calls, [])

    async def test_staff_can_read_but_not_write(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/settings/voicelink"))).status, 200)
        self.assertEqual((await client.put(self.path("/settings/voicelink"), json={"links": []})).status, 403)
        self.assertEqual((await client.get(self.path("/voicelink/rules"))).status, 403)

    async def test_undo_takes_the_roles_back(self):
        client = await self.client()
        await client.put(self.path("/settings/voicelink"), json={"links": [{"channel_id": str(self.lounge.id), "roles": [str(self.in_voice.id)]}]})
        await self.link.settle()
        await self.move(self.member, self.lounge)
        rows = await (await client.get(self.path("/activity"))).json()
        response = await client.post(self.path(f"/activity/{rows[0]['id']}/undo"))
        self.assertEqual(response.status, 200)
        await self.link.settle()
        self.assertNotIn(self.in_voice, self.member.roles)


class RealLibraryTest(GatewayTest):
    modules = [VoiceLink]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.link = next(m for m in self.bot.modules if isinstance(m, VoiceLink))

    def voice_update(self, user_id: int, channel_id: str | None) -> None:
        self.bot._connection.parse_voice_state_update({
            "guild_id": str(self.guild_id), "channel_id": channel_id, "user_id": str(user_id), "member": self.member_json[user_id], "session_id": "s", "deaf": False, "mute": False,
            "self_deaf": False, "self_mute": False, "self_video": False, "suppress": False, "request_to_speak_timestamp": None,
        })

    async def test_a_voice_state_reaches_discord_as_a_role_change(self):
        await self.bot.settings.save(self.guild_id, "voicelink", {"links": [{"channel_id": int(self.voice["id"]), "roles": [int(self.role_muted["id"])]}]})
        await self.link.settle()
        self.voice_update(self.member_id, self.voice["id"])
        await self.settle()
        await self.link.settle()
        self.assert_no_errors()
        self.assertTrue(any(m == "PUT" and p == "/guilds/{guild_id}/members/{user_id}/roles/{role_id}" for m, p, _ in self.net.calls))
        self.net.calls.clear()
        self.voice_update(self.member_id, None)
        await self.settle()
        await self.link.settle()
        self.assert_no_errors()
        self.assertTrue(any(m == "DELETE" and p == "/guilds/{guild_id}/members/{user_id}/roles/{role_id}" for m, p, _ in self.net.calls))


class MissingFromTheCacheTest(Base):
    async def test_someone_the_cache_doesnt_have_stays_on_record(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        self.guild.members.remove(self.member)
        self.guild.fetch_member = mock.AsyncMock(return_value=self.member)
        await self.link._startup_checks()
        await self.link.settle()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)
        self.assertIn(self.in_voice, self.member.roles)

    async def test_a_lookup_that_fails_says_nothing_about_them_leaving(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        self.guild.members.remove(self.member)
        self.guild.fetch_member = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(503), "down"))
        await self.link._startup_checks()
        await self.link.settle()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)

    async def test_someone_in_the_channel_the_cache_doesnt_have_still_gets_the_role(self):
        await self.links((self.lounge, [self.in_voice]))
        other = FakeMember(self.guild, "not cached yet")
        self.guild.members.remove(other)
        other.voice = SimpleNamespace(channel=self.lounge)
        self.lounge.voice_states = {other.id: other.voice}
        self.guild.fetch_member = mock.AsyncMock(return_value=other)
        await self.link._startup_checks()
        await self.link.settle()
        self.assertIn(self.in_voice, other.roles)

    async def test_an_outage_touches_nothing_and_the_check_runs_when_it_ends(self):
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)
        self.lounge.members.remove(self.member)
        self.member.voice = None
        self.guild.unavailable = True
        await self.link._startup_checks()
        await self.link.settle()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)
        self.assertIn(self.in_voice, self.member.roles)
        self.guild.unavailable = False
        await self.link.on_guild_available(self.guild)
        await self.link.settle()
        self.assertNotIn(self.in_voice, self.member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 0)

    async def test_a_server_that_was_never_away_isnt_checked_again(self):
        await self.links((self.lounge, [self.in_voice]))
        with mock.patch.object(self.link, "_check_guild", side_effect=AssertionError("checked")):
            await self.link.on_guild_available(self.guild)
        await self.link.on_guild_unavailable(self.guild)
        with mock.patch.object(self.link, "_check_guild", new_callable=mock.AsyncMock) as check:
            await self.link.on_guild_available(self.guild)
        check.assert_awaited_once_with(self.guild.id)


class OutageThroughTheLibraryTest(GatewayTest):
    """READY listing the server as unavailable, the way the gateway does in an outage."""

    modules = [VoiceLink]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.link = next(m for m in self.bot.modules if isinstance(m, VoiceLink))
        self.state = self.bot._connection
        self.voice_id = int(self.voice["id"])
        self.role_id = int(self.role_muted["id"])
        await self.bot.settings.save(self.guild_id, "voicelink", {"links": [{"channel_id": self.voice_id, "roles": [self.role_id]}]})
        await self.link.settle()

    def voice_update(self, channel_id: int | None) -> None:
        self.state.parse_voice_state_update({
            "guild_id": str(self.guild_id), "channel_id": str(channel_id) if channel_id else None, "user_id": str(self.member_id), "session_id": "s", "deaf": False, "mute": False,
            "self_deaf": False, "self_mute": False, "self_video": False, "suppress": False, "request_to_speak_timestamp": None, "member": self.member_json[self.member_id],
        })

    async def checked(self) -> None:
        await asyncio.sleep(0.05)
        if self.link._startup is not None:
            await self.link._startup
        await self.link.settle()

    async def join_voice(self) -> None:
        self.voice_update(self.voice_id)
        await self.checked()
        self.member_json[self.member_id]["roles"].append(str(self.role_id))
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **self.member_json[self.member_id]})
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)

    async def test_an_outage_at_ready_keeps_the_record(self):
        await self.join_voice()
        self.state.guild_ready_timeout = 0.01
        self.state.parse_ready({"v": 10, "user": self.bot_user, "guilds": [{"id": str(self.guild_id), "unavailable": True}], "session_id": "x", "resume_gateway_url": "wss://x", "application": {"id": str(self.bot_id), "flags": 0}})
        await self.checked()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)
        self.assert_no_errors()

    async def test_a_chunk_still_on_its_way_at_ready_keeps_the_record_and_leaving_takes_the_role(self):
        await self.join_voice()
        guild = self.bot.get_guild(self.guild_id)
        member = guild._members.pop(self.member_id)
        await self.link._startup_checks()
        await self.link.settle()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)
        guild._members[self.member_id] = member  # the chunk lands
        calls = len(self.net.calls)
        self.voice_update(None)
        await self.checked()
        self.assertTrue(any(method == "DELETE" and "/roles/" in path for method, path, _ in self.net.calls[calls:]))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 0)


class RestoreTest(BackupCase):
    """The record says what's true right now, so it stays out of backups and voice state keeps it right."""

    modules = [VoiceLink, Backup]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.link = self.cog(VoiceLink)
        self.lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        self.in_voice = self.guild.role("In voice", 3)
        self.settle_patch = mock.patch.object(voicelink_module, "RESTORE_SETTLE", 0)
        self.settle_patch.start()
        await self.bot.settings.save(self.guild.id, "voicelink", {"links": [{"channel_id": self.lounge.id, "roles": [self.in_voice.id]}]})
        await self.link.settle()

    async def asyncTearDown(self) -> None:
        self.settle_patch.stop()
        await self.link.cog_unload()
        await super().asyncTearDown()

    async def move(self, channel: FakeChannel | None) -> None:
        before = SimpleNamespace(channel=self.member.voice.channel if self.member.voice else None)
        if before.channel is not None:
            before.channel.members.remove(self.member)
        self.member.voice = SimpleNamespace(channel=channel) if channel else None
        if channel is not None:
            channel.members.append(self.member)
        await self.link.on_voice_state_update(self.member, before, SimpleNamespace(channel=channel))
        await self.link.settle()

    async def restore_file(self, body: bytes) -> None:
        client = await self.client()
        job = await self.preview_of(client, body)
        await self.restore(client, job["result"])
        if self.link._startup is not None:
            await self.link._startup
        await self.link.settle()

    async def test_the_record_isnt_in_a_backup(self):
        await self.move(self.lounge)
        doc = await self.export_doc()
        self.assertNotIn("main.voicelink_held", doc["tables"])

    async def test_someone_who_joined_after_the_backup_loses_the_role_on_leaving(self):
        body = await self.export_bytes()
        await self.move(self.lounge)
        self.assertIn(self.in_voice, self.member.roles)
        await self.restore_file(body)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 1)
        await self.move(None)
        self.assertNotIn(self.in_voice, self.member.roles)

    async def test_a_role_a_moderator_gave_since_the_backup_stays(self):
        await self.move(self.lounge)
        body = await self.export_bytes()
        await self.move(None)
        self.member.roles.append(self.in_voice)  # a moderator gave it by hand
        await self.restore_file(body)
        self.assertIn(self.in_voice, self.member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held"), 0)


class OutOfReachTest(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.logs = mock.Mock()
        real = self.bot.module
        self.bot.module = lambda key: self.logs if key == "logging" else real(key)
        await self.links((self.lounge, [self.in_voice]))
        await self.move(self.member, self.lounge)

    async def held(self) -> int:
        return await self.db.scalar("SELECT COUNT(*) FROM voicelink_held WHERE user_id = ?", (self.member.id,))

    async def test_a_role_moved_above_the_bot_keeps_its_record_and_staff_hear_once(self):
        self.in_voice.position = 20
        await self.move(self.member, None)
        self.assertIn(self.in_voice, self.member.roles)
        self.assertEqual(await self.held(), 1)
        await self.move(self.mod, self.lounge)
        await self.move(self.mod, None)
        self.assertEqual(self.logs.staff_notice.call_count, 1)

    async def test_it_comes_off_once_the_role_is_back_in_reach(self):
        self.in_voice.position = 20
        await self.move(self.member, None)
        self.in_voice.position = 3
        await self.link.on_guild_role_update(self.in_voice, self.in_voice)
        await self.link.settle()
        self.assertNotIn(self.in_voice, self.member.roles)
        self.assertEqual(await self.held(), 0)

    async def test_a_deleted_role_still_ends_its_record(self):
        self.guild.roles.remove(self.in_voice)
        await self.move(self.member, None)
        self.assertEqual(await self.held(), 0)


class StopAtTheWrongMomentTest(Base):
    async def test_a_stop_right_after_the_role_lands_still_leaves_a_record(self):
        await self.links((self.lounge, [self.in_voice]))
        real = self.member.add_roles

        async def add(*args, **kwargs):
            await real(*args, **kwargs)
            raise asyncio.CancelledError()
        self.member.add_roles = add
        self.member.voice = SimpleNamespace(channel=self.lounge)
        self.lounge.members.append(self.member)
        await self.link.on_voice_state_update(self.member, SimpleNamespace(channel=None), SimpleNamespace(channel=self.lounge))
        await asyncio.gather(*self.link._running.values(), return_exceptions=True)
        self.member.add_roles = real
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held WHERE user_id = ?", (self.member.id,)), 1)
        await self.move(self.member, None)
        self.assertNotIn(self.in_voice, self.member.roles)

    async def test_a_role_discord_refuses_leaves_no_record(self):
        await self.links((self.lounge, [self.in_voice]))

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.member.add_roles = refuse
        await self.move(self.member, self.lounge)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voicelink_held WHERE user_id = ?", (self.member.id,)), 0)


class RestoreTaskTest(Base):
    async def test_a_second_restore_doesnt_orphan_the_first_or_block_a_start(self):
        with mock.patch.object(voicelink_module, "RESTORE_SETTLE", 3600):
            await self.link.on_guild_restored(self.guild.id)
            await self.link.on_guild_restored(self.guild.id)
            await asyncio.sleep(0)
            self.assertEqual(len(self.link._after_restores), 2)
            self.assertIsNone(self.link._startup)
            pending = list(self.link._after_restores)
            await self.link.on_ready()
            self.assertIsNotNone(self.link._startup)
            await self.link.cog_unload()
            self.assertTrue(all(task.cancelled() for task in pending))


class NoLinksTest(Base):
    async def test_a_server_with_no_links_costs_no_check(self):
        with mock.patch.object(self.link, "_touch") as touch:
            await self.link.on_voice_state_update(self.member, SimpleNamespace(channel=None), SimpleNamespace(channel=self.lounge))
        touch.assert_not_called()
