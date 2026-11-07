"""Mini mutes and regular mutes on one role, through the real discord.py library. Discord answers role changes with member updates here, as it does for real.

The role comes off only when no mute of either kind still holds it, and one action makes one case. With two separate roles, the two features don't touch each other.
"""
from __future__ import annotations

import time
from typing import Any
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.ops import DiscordOps
from captainguild.modules import moderation as moderation_module
from captainguild.modules.moderation import Moderation
from captainguild.modules.voice import Voice
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.gateway import GatewayTest


class RoleCase(GatewayTest):
    modules = [Moderation, Voice]
    separate_roles = False

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        patcher = mock.patch.object(moderation_module, "AUDIT_WAIT", 0)  # no audit entry arrives in time, the hard case
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot.ops = DiscordOps(self.bot)
        self.muted = int(self.role_muted["id"])
        self.mini_role = int(self.role_member["id"]) if self.separate_roles else self.muted
        self.target_id = self.mod_id if self.separate_roles else self.member_id
        await self.bot.settings.save(self.guild_id, "moderation", {"mute_role": self.muted})
        await self.bot.settings.save(self.guild_id, "voice", {"mini_mute_role": self.mini_role, "mini_mute_minutes": 10, "mini_mute_cases": True})
        self.mods = next(m for m in self.bot.modules if isinstance(m, Moderation))
        self.voice = next(m for m in self.bot.modules if isinstance(m, Voice))
        self.clients: list[TestClient] = []
        real = self.net.http

        async def discord_answers(route: Any, **kwargs: Any) -> Any:
            answer = await real(route, **kwargs)
            self._echo_role_change(route, kwargs)
            return answer

        self.bot.http.request = discord_answers

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        await super().asyncTearDown()

    def _echo_role_change(self, route: Any, kwargs: dict) -> None:
        key = f"{route.method} {route.path}"
        if key == "PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}":
            self.set_roles(int(route.user_id), add=int(route.role_id))
        elif key == "DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id}":
            self.set_roles(int(route.user_id), remove=int(route.role_id))
        elif key == "PATCH /guilds/{guild_id}/members/{user_id}" and "roles" in (kwargs.get("json") or {}):
            self.set_roles(int(route.user_id), replace=[int(role) for role in kwargs["json"]["roles"]])

    def set_roles(self, user_id: int, *, add: int | None = None, remove: int | None = None, replace: list[int] | None = None) -> None:
        data = self.member_json[user_id]
        roles = [int(role) for role in data["roles"]] if replace is None else list(replace)
        if add is not None and add not in roles:
            roles.append(add)
        if remove in roles:
            roles.remove(remove)
        data["roles"] = [str(role) for role in roles]
        self.bot._connection.parse_guild_member_update({**data, "guild_id": str(self.guild_id)})

    @property
    def target(self) -> Any:
        return self.guild.get_member(self.target_id)

    @property
    def moderator(self) -> Any:
        return self.guild.get_member(self.admin_id)

    def has_role(self, role_id: int | None = None) -> bool:
        return self.target.get_role(role_id or self.mini_role) is not None

    async def mini_mute(self) -> None:
        ok, why, _ = await self.voice.apply_mini_mute(self.target, 10, "Rude", "Mini mute", self.moderator)
        self.assertTrue(ok, why)
        await self.settle(0.05)

    async def mute(self, seconds: int | None = None) -> None:
        await self.mods.punish(self.guild, self.target, "mute", moderator=self.moderator, reason="Worse", seconds=seconds, dm=False)
        await self.settle(0.05)

    async def time_passes(self, table: str, column: str = "expires_at") -> None:
        """A mute's time runs out, and its timer comes due."""
        await self.db.run(f"UPDATE {table} SET {column} = ? WHERE guild_id = ? AND user_id = ?", (time.time() - 1, self.guild_id, self.target_id))
        kind = "mini_mute" if table == "mini_mutes" else "moderation.unmute"
        await self.db.run("UPDATE timers SET due_at = ? WHERE kind = ? AND key = ?", (time.time() - 1, kind, f"{self.guild_id}:{self.target_id}"))
        await self.bot.timers.run_due()
        await self.settle(0.05)

    async def cases(self) -> list[str]:
        rows = await self.db.all("SELECT action FROM cases WHERE guild_id = ? AND user_id = ? ORDER BY number", (self.guild_id, self.target_id))
        return [row["action"] for row in rows]

    async def mini_row(self) -> dict | None:
        return await self.db.one("SELECT * FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (self.guild_id, self.target_id))

    async def mute_row(self) -> dict | None:
        return await self.db.one("SELECT * FROM active_mutes WHERE guild_id = ? AND user_id = ?", (self.guild_id, self.target_id))

    async def mini_timer(self) -> dict | None:
        return await self.bot.timers.get_key("mini_mute", f"{self.guild_id}:{self.target_id}")

    async def leave_and_rejoin(self) -> None:
        """Discord strips roles from someone who leaves and comes back."""
        self.member_json[self.target_id]["roles"] = []
        state = self.bot._connection
        state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.target_id]["user"]})
        await self.settle(0.05)
        state.parse_guild_member_add({**self.member_json[self.target_id], "guild_id": str(self.guild_id)})
        await self.settle(0.3)


class SharedRoleTest(RoleCase):
    async def test_a_mini_mute_alone_makes_one_case_each_way(self):
        await self.mini_mute()
        self.assertTrue(self.has_role())
        self.assertIsNone(await self.mute_row(), "A mini mute isn't a regular mute")
        self.assertEqual(await self.cases(), ["minimute"])
        await self.time_passes("mini_mutes")
        self.assertFalse(self.has_role())
        self.assertIsNone(await self.mini_row())
        self.assertEqual(await self.cases(), ["minimute", "unminimute"])
        self.assert_no_errors()

    async def test_a_mini_mute_ending_leaves_a_regular_mute_in_place(self):
        await self.mini_mute()
        await self.mute(3600)
        await self.time_passes("mini_mutes")
        self.assertTrue(self.has_role(), "The moderator's mute still holds the role")
        self.assertIsNone(await self.mini_row())
        self.assertIsNotNone(await self.mute_row())
        self.assertEqual(await self.cases(), ["minimute", "mute", "unminimute"])
        await self.time_passes("active_mutes")
        self.assertFalse(self.has_role())
        self.assertEqual(await self.cases(), ["minimute", "mute", "unminimute", "unmute"])
        self.assert_no_errors()

    async def test_a_regular_mute_ending_leaves_a_longer_mini_mute_in_place(self):
        await self.mini_mute()
        await self.mute(60)
        await self.time_passes("active_mutes")
        self.assertTrue(self.has_role(), "The mini mute still holds the role")
        self.assertIsNone(await self.mute_row())
        self.assertIsNotNone(await self.mini_row())
        self.assertEqual(await self.cases(), ["minimute", "mute", "unmute"])
        await self.time_passes("mini_mutes")
        self.assertFalse(self.has_role())
        self.assertEqual(await self.cases(), ["minimute", "mute", "unmute", "unminimute"])
        self.assert_no_errors()

    async def test_a_permanent_mute_outlasts_a_mini_mute(self):
        await self.mini_mute()
        await self.mute(0)
        await self.time_passes("mini_mutes")
        self.assertTrue(self.has_role())
        self.assertIsNotNone(await self.mute_row())

    async def test_no_mini_mute_on_someone_already_muted(self):
        await self.mute(3600)
        actor = self.guild.get_member(self.owner_id)
        answer = await self.voice.request_mini_mute(actor, self.target, "Rude")
        self.assertIn("already muted", answer)
        self.assertIsNone(await self.mini_row())
        self.assertEqual(await self.cases(), ["mute"])

    async def test_unmute_ends_a_mini_mute_for_good(self):
        await self.mini_mute()
        await self.mods.punish(self.guild, self.target, "unmute", moderator=self.moderator, reason="Fine", dm=False)
        await self.settle(0.05)
        self.assertFalse(self.has_role())
        self.assertIsNone(await self.mini_row())
        self.assertIsNone(await self.mini_timer())
        self.assertEqual(await self.cases(), ["minimute", "unmute"])
        await self.leave_and_rejoin()
        self.assertFalse(self.has_role(), "A lifted mute must not come back")
        self.assert_no_errors()

    async def test_unmute_ends_both_kinds_at_once(self):
        await self.mini_mute()
        await self.mute(3600)
        await self.mods.punish(self.guild, self.target, "unmute", moderator=self.moderator, reason="Fine", dm=False)
        await self.settle(0.05)
        self.assertFalse(self.has_role())
        self.assertIsNone(await self.mini_row())
        self.assertIsNone(await self.mute_row())
        self.assertEqual(await self.cases(), ["minimute", "mute", "unmute"])

    async def test_taking_the_role_off_by_hand_ends_a_mini_mute(self):
        await self.mini_mute()
        self.set_roles(self.target_id, remove=self.muted)  # someone uses Discord's own menu
        await self.settle(0.1)
        self.assertIsNone(await self.mini_row())
        self.assertIsNone(await self.mini_timer())
        self.assertEqual(await self.cases(), ["minimute", "unmute"])
        await self.leave_and_rejoin()
        self.assertFalse(self.has_role())
        self.assert_no_errors()

    async def test_lifting_a_mini_mute_leaves_a_regular_mute_in_place(self):
        await self.mini_mute()
        await self.mute(3600)
        ctx = mock.Mock(author=self.guild.get_member(self.owner_id), guild=self.guild)
        ctx.defer = mock.AsyncMock()
        ctx.ok = mock.AsyncMock()
        ctx.fail = mock.AsyncMock()
        await self.voice.mini_mute_lift.callback(self.voice, ctx, self.target, reason="Enough")
        await self.settle(0.05)
        self.assertTrue(self.has_role(), "The moderator's mute still holds the role")
        self.assertIsNone(await self.mini_row())
        self.assertIsNotNone(await self.mute_row())
        self.assertIn("still", ctx.ok.await_args.args[0])
        self.assert_no_errors()

    async def test_lifting_a_mini_mute_from_the_site_leaves_a_regular_mute_in_place(self):
        await self.mini_mute()
        await self.mute(3600)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(self.owner_id, "owner", None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        response = await client.post(f"/api/g/{self.guild_id}/voice/mini-mutes/{self.target_id}/lift", json={})
        self.assertEqual(response.status, 200, await response.text())
        await self.settle(0.05)
        self.assertTrue(self.has_role())
        self.assertIsNone(await self.mini_row())
        self.assert_no_errors()

    async def test_lifting_a_mini_mute_alone_takes_the_role_off(self):
        await self.mini_mute()
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(self.owner_id, "owner", None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        response = await client.post(f"/api/g/{self.guild_id}/voice/mini-mutes/{self.target_id}/lift", json={})
        self.assertEqual(response.status, 200, await response.text())
        await self.settle(0.05)
        self.assertFalse(self.has_role())
        self.assertEqual(await self.cases(), ["minimute", "unminimute"], "Lifting it isn't a separate unmute")

    async def test_rejoining_puts_a_mini_mute_back_with_no_new_case(self):
        await self.mini_mute()
        await self.leave_and_rejoin()
        self.assertTrue(self.has_role())
        self.assertIsNone(await self.mute_row())
        self.assertEqual(await self.cases(), ["minimute"])
        self.assert_no_errors()

    async def test_rejoining_with_both_running_needs_one_role_change(self):
        await self.mini_mute()
        await self.mute(3600)
        before = len([c for c in self.net.calls if c[0] == "PUT" and "/roles/" in c[1]])
        await self.leave_and_rejoin()
        self.assertTrue(self.has_role())
        self.assertEqual(len([c for c in self.net.calls if c[0] == "PUT" and "/roles/" in c[1]]) - before, 1)
        self.assertEqual(await self.cases(), ["minimute", "mute"])
        self.assert_no_errors()

    async def test_an_outside_mute_is_still_recorded(self):
        self.set_roles(self.target_id, add=self.muted)  # someone uses Discord's own menu
        await self.settle(0.1)
        self.assertIsNotNone(await self.mute_row())
        self.assertEqual(await self.cases(), ["mute"])

    async def test_a_hardmute_ending_gives_roles_back_and_keeps_a_running_mini_mute(self):
        extra = int(self.role_mod["id"])
        self.set_roles(self.target_id, add=extra)
        await self.settle(0.05)
        await self.mini_mute()
        await self.mods.punish(self.guild, self.target, "hardmute", moderator=self.moderator, reason="Worse", seconds=60, dm=False)
        await self.settle(0.05)
        self.assertIsNone(self.target.get_role(extra))
        await self.time_passes("active_mutes")
        self.assertIsNotNone(self.target.get_role(extra), "The hardmute's roles come back")
        self.assertTrue(self.has_role(), "The mini mute still holds the role")
        self.assert_no_errors()


class SeparateRoleTest(RoleCase):
    separate_roles = True

    async def test_a_mini_mute_ending_only_takes_its_own_role(self):
        await self.mini_mute()
        await self.mute(3600)
        self.assertTrue(self.has_role(self.muted))
        await self.time_passes("mini_mutes")
        self.assertFalse(self.has_role(self.mini_role))
        self.assertTrue(self.has_role(self.muted))
        self.assertIsNotNone(await self.mute_row())
        self.assertEqual(await self.cases(), ["minimute", "mute", "unminimute"])
        self.assert_no_errors()

    async def test_a_regular_mute_ending_only_takes_its_own_role(self):
        await self.mini_mute()
        await self.mute(60)
        await self.time_passes("active_mutes")
        self.assertFalse(self.has_role(self.muted))
        self.assertTrue(self.has_role(self.mini_role))
        self.assertIsNotNone(await self.mini_row())
        self.assert_no_errors()

    async def test_unmute_leaves_a_mini_mute_on_its_own_role(self):
        await self.mini_mute()
        await self.mute(3600)
        await self.mods.punish(self.guild, self.target, "unmute", moderator=self.moderator, dm=False)
        await self.settle(0.05)
        self.assertFalse(self.has_role(self.muted))
        self.assertTrue(self.has_role(self.mini_role))
        self.assertIsNotNone(await self.mini_row())

    async def test_taking_the_mini_role_off_by_hand_ends_the_mini_mute(self):
        await self.mini_mute()
        self.set_roles(self.target_id, remove=self.mini_role)
        await self.settle(0.1)
        self.assertIsNone(await self.mini_row())
        self.assertIsNone(await self.mini_timer())
        self.assertEqual(await self.cases(), ["minimute", "unminimute"])
        await self.leave_and_rejoin()
        self.assertFalse(self.has_role(self.mini_role))
        self.assert_no_errors()


class VoiceAloneTest(RoleCase):
    modules = [Voice]

    async def asyncSetUp(self) -> None:
        GatewayTest_setup = super(RoleCase, self).asyncSetUp
        await GatewayTest_setup()
        self.bot.ops = DiscordOps(self.bot)
        self.muted = self.mini_role = int(self.role_muted["id"])
        self.target_id = self.member_id
        await self.bot.settings.save(self.guild_id, "voice", {"mini_mute_role": self.mini_role, "mini_mute_minutes": 10, "mini_mute_cases": True})
        self.mods = None
        self.voice = next(m for m in self.bot.modules if isinstance(m, Voice))
        self.clients = []
        real = self.net.http

        async def discord_answers(route: Any, **kwargs: Any) -> Any:
            answer = await real(route, **kwargs)
            self._echo_role_change(route, kwargs)
            return answer

        self.bot.http.request = discord_answers

    async def test_it_works_without_the_moderation_feature(self):
        await self.mini_mute()
        self.assertTrue(self.has_role())
        await self.time_passes("mini_mutes")
        self.assertFalse(self.has_role())
        self.assertIsNone(await self.mini_row())
        self.assert_no_errors()
