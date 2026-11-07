import asyncio
from unittest import mock

import discord

from captainguild.core import members
from captainguild.core.members import find_member, whole_member_list
from tests.fakes import BotTest, FakeMember, _FakeResponse


class FindMemberTest(BotTest):
    async def test_the_cache_answers_first(self):
        self.guild.fetch_member = mock.AsyncMock(side_effect=AssertionError("asked Discord"))
        self.assertIs(await find_member(self.guild, self.member.id), self.member)

    async def test_a_gap_in_the_cache_goes_to_discord(self):
        self.guild.members.remove(self.member)
        self.guild.fetch_member = mock.AsyncMock(return_value=self.member)
        self.assertIs(await find_member(self.guild, self.member.id), self.member)

    async def test_only_discord_saying_so_means_gone(self):
        self.assertIsNone(await find_member(self.guild, 42))

    async def test_anything_else_raises(self):
        self.guild.fetch_member = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(503), "down"))
        with self.assertRaises(discord.HTTPException):
            await find_member(self.guild, 42)


class WholeMemberListTest(BotTest):
    def short_by(self, missing: int, count: int) -> None:
        self.guild.count_said = count
        del self.guild.members[:missing]
        type(self.guild).member_count = property(lambda guild: getattr(guild, "count_said", len(guild.members)))

    async def asyncTearDown(self) -> None:
        type(self.guild).member_count = property(lambda guild: len(guild.members))
        await super().asyncTearDown()

    async def test_a_full_cache_asks_nothing(self):
        self.guild.chunk = mock.AsyncMock(side_effect=AssertionError("asked Discord"))
        self.assertTrue(await whole_member_list(self.guild))

    async def test_an_outage_isnt_whole(self):
        self.guild.unavailable = True
        self.guild.chunk = mock.AsyncMock(side_effect=AssertionError("asked Discord"))
        self.assertFalse(await whole_member_list(self.guild))

    async def test_a_short_cache_asks_for_the_rest(self):
        people = list(self.guild.members)
        self.short_by(2, len(people))

        async def chunk():
            self.guild.members[:] = people

        self.guild.chunk = chunk
        self.assertTrue(await whole_member_list(self.guild))

    async def test_a_count_a_little_off_is_still_whole(self):
        for number in range(200):
            FakeMember(self.guild, f"m{number}")
        self.short_by(1, len(self.guild.members))
        self.guild.chunk = mock.AsyncMock(side_effect=AssertionError("asked Discord"))
        self.assertTrue(await whole_member_list(self.guild))

    async def test_a_list_that_doesnt_arrive_isnt_whole(self):
        self.short_by(2, len(self.guild.members))

        async def never():
            await asyncio.sleep(10)

        self.guild.chunk = never
        with mock.patch.object(members, "CHUNK_WAIT", 0.01):
            self.assertFalse(await whole_member_list(self.guild))

    async def test_a_list_that_comes_back_short_isnt_whole(self):
        self.short_by(2, len(self.guild.members))
        self.guild.chunk = mock.AsyncMock()
        self.assertFalse(await whole_member_list(self.guild))
