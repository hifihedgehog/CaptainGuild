"""A join event for someone the member list already holds, through the real library with only the network replaced.

discord.py drops such an event (state.py, parse_guild_member_add): a member list that comes in before the join's own event, at startup or after
a reconnect, would hide the join from every feature. Each test counts what every feature that listens for joins hears.
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Any

from unittest import mock

from discord.state import ChunkRequest

from captainguild.core import members as members_module
from captainguild.modules.automod import Automod
from captainguild.modules.autoroles import Autoroles
from captainguild.modules.greetings import Greetings
from captainguild.modules.levels import Levels
from captainguild.modules.logs import Logs
from captainguild.modules.moderation import Moderation
from captainguild.modules.rolerules import RoleRules
from captainguild.modules.statistics import Statistics
from captainguild.modules.voice import Voice
from tests.gateway import GatewayTest, iso, new_id, user_json

LISTENERS = [Automod, Autoroles, Greetings, Levels, Logs, Moderation, RoleRules, Statistics, Voice]


class ListedJoinTest(GatewayTest):
    modules = LISTENERS

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.state = self.bot._connection
        self.heard: list[tuple[str, int]] = []
        listeners = self.bot.extra_events.get("on_member_join", [])
        self.assertEqual(sorted(type(listener.__self__).__name__ for listener in listeners), sorted(kind.__name__ for kind in LISTENERS))

        def counted(listener: Any) -> Any:
            async def heard(member: Any) -> None:
                self.heard.append((type(listener.__self__).__name__, member.id))
                await listener(member)
            return heard

        self.bot.extra_events["on_member_join"] = [counted(listener) for listener in listeners]
        await self.bot.settings.save(self.guild_id, "greetings", {"welcome_channel": int(self.text["id"]), "welcome": {"content": "Hi {mention}, member {server(members)}", "embeds": [], "buttons": []}})

    def newcomer(self, joined: datetime.datetime | None = None, user: dict | None = None) -> dict:
        user = user or user_json(new_id(), "newcomer")
        data = {"user": user, "roles": [], "joined_at": iso(joined), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "avatar": None, "premium_since": None, "communication_disabled_until": None}
        self.net.members[int(user["id"])] = data
        return data

    def chunk(self, *members: dict) -> None:
        """A member list request's answer. discord.py lists the members only for a request of its own, so one is made first."""
        request = ChunkRequest(self.guild_id, 0, asyncio.get_running_loop(), self.state._get_guild, cache=True)
        self.state._chunk_requests[request.nonce] = request
        self.state.parse_guild_members_chunk({"guild_id": str(self.guild_id), "nonce": request.nonce, "chunk_index": 0, "chunk_count": 1, "members": list(members)})

    def add(self, data: dict) -> None:
        self.state.parse_guild_member_add({"guild_id": str(self.guild_id), **data})

    def heard_once(self, member_id: int) -> list[tuple[str, int]]:
        return sorted((kind.__name__, member_id) for kind in LISTENERS)

    def welcomes(self, member_id: int) -> list[str]:
        return [post["content"] for post in self.net.posted if f"<@{member_id}>" in post["content"]]

    async def test_a_join_the_member_list_showed_first_reaches_every_feature_once(self):
        before = self.guild.member_count
        data = self.newcomer()
        member_id = int(data["user"]["id"])
        self.chunk(data)
        await self.settle()
        self.assertIsNotNone(self.guild.get_member(member_id), "the list holds the newcomer before the join's event")
        self.assertEqual(self.heard, [])
        self.add(data)
        await self.settle()
        self.assertEqual(sorted(self.heard), self.heard_once(member_id))
        self.assertEqual(self.guild.member_count, before + 1)
        self.assertEqual(self.welcomes(member_id), [f"Hi <@{member_id}>, member {before + 1}"])
        self.add(data)  # the same event again
        await self.settle()
        self.assertEqual(sorted(self.heard), self.heard_once(member_id))
        self.assertEqual(self.guild.member_count, before + 1)
        self.assertEqual(len(self.welcomes(member_id)), 1)
        self.assert_no_errors()

    async def test_a_second_event_for_a_join_the_library_passed_on_is_dropped(self):
        before = self.guild.member_count
        data = self.newcomer()
        member_id = int(data["user"]["id"])
        self.add(data)
        await self.settle()
        self.assertEqual(sorted(self.heard), self.heard_once(member_id))
        self.add(data)
        self.chunk(data)
        self.add(data)
        await self.settle()
        self.assertEqual(sorted(self.heard), self.heard_once(member_id))
        self.assertEqual(self.guild.member_count, before + 1)
        self.assertEqual(len(self.welcomes(member_id)), 1)
        self.assert_no_errors()

    async def test_a_rejoin_after_an_older_member_list_undid_the_leave_reaches_every_feature_once(self):
        # The leave comes in, then a member list built before it lists the member again with the old join, then the rejoin's event.
        before = self.guild.member_count
        old = self.member_json[self.member_id]
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": old["user"]})
        self.chunk(old)
        await self.settle()
        self.assertEqual(self.guild.get_member(self.member_id).joined_at, datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
        rejoined = self.newcomer(user=old["user"])
        self.add(rejoined)
        await self.settle()
        self.assertEqual(sorted(self.heard), self.heard_once(self.member_id))
        self.assertEqual(self.guild.member_count, before)
        self.assertEqual(self.guild.get_member(self.member_id).joined_at.isoformat(), rejoined["joined_at"])
        self.add(rejoined)
        self.add(old)  # an older join than the one listed
        await self.settle()
        self.assertEqual(sorted(self.heard), self.heard_once(self.member_id))
        self.assertEqual(self.guild.member_count, before)
        self.assert_no_errors()

    async def test_an_event_for_someone_already_in_the_server_when_the_bot_saw_it_is_dropped(self):
        # Discord's docs say this event can come for people who are already members. Their join came before the server's list did.
        before = self.guild.member_count
        self.add(self.member_json[self.member_id])
        await self.settle()
        self.assertEqual(self.heard, [])
        self.assertEqual(self.guild.member_count, before)
        self.assert_no_errors()

    async def test_an_event_for_an_older_join_than_the_listed_one_is_dropped(self):
        # The list already shows the newer join, as the member is now. An older join's data would put back old roles and an old join time.
        before = self.guild.member_count
        now = datetime.datetime.now(datetime.timezone.utc)
        newer = self.newcomer(now + datetime.timedelta(seconds=1))
        member_id = int(newer["user"]["id"])
        self.chunk(newer)
        older = self.newcomer(now, user=newer["user"])
        self.add(older)
        await self.settle()
        self.assertEqual(self.heard, [])
        self.assertEqual(self.guild.get_member(member_id).joined_at, now + datetime.timedelta(seconds=1))
        self.assertEqual(self.guild.member_count, before)
        self.assert_no_errors()

    async def test_the_joins_kept_are_the_newest(self):
        with mock.patch.object(members_module, "JOINS_KEPT", 2):
            joins = [self.newcomer() for _ in range(3)]
            for data in joins:
                self.add(data)
        kept = [member_id for _, member_id, _ in self.state.joins_passed_on]
        self.assertEqual(kept, [int(data["user"]["id"]) for data in joins[1:]])
        await self.settle()
        self.assert_no_errors()
