"""The batch member route the dashboard uses to name a long list of people in a few calls."""
from __future__ import annotations

from captainguild.core.ops import DiscordOps
from captainguild.web.app import MEMBERS_PER_CALL
from tests.fakes import BotTest


class BatchLookup(BotTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)  # the real lookup, which reads the guild's members

    async def test_several_people_come_back_in_one_call(self) -> None:
        client = await self.client()
        response = await client.get(self.path("/members"), params={"ids": f"{self.admin.id},{self.mod.id},{self.member.id}"})
        self.assertEqual(response.status, 200)
        rows = (await response.json())["members"]
        self.assertEqual({row["name"] for row in rows}, {"admin", "mod", "member"})

    async def test_someone_who_left_and_a_junk_part_are_left_out(self) -> None:
        client = await self.client()
        response = await client.get(self.path("/members"), params={"ids": f"{self.admin.id},12345,not-a-number,,{self.admin.id}"})
        self.assertEqual(response.status, 200)
        names = [row["name"] for row in (await response.json())["members"]]
        self.assertEqual(names, ["admin", "admin"])

    async def test_one_call_takes_as_many_as_the_page_sends_and_no_more(self) -> None:
        client = await self.client()
        most = ",".join(str(self.admin.id) for _ in range(MEMBERS_PER_CALL))
        self.assertEqual((await client.get(self.path("/members"), params={"ids": most})).status, 200)
        refused = await client.get(self.path("/members"), params={"ids": most + f",{self.admin.id}"})
        self.assertEqual(refused.status, 400)
        self.assertIn(str(MEMBERS_PER_CALL), (await refused.json())["error"])

    async def test_an_empty_list_answers_with_no_people(self) -> None:
        client = await self.client()
        response = await client.get(self.path("/members"))
        self.assertEqual(await response.json(), {"members": []})

    async def test_only_staff_may_ask(self) -> None:
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/members"), params={"ids": str(self.admin.id)})).status, 200)
        nobody = await self.client(access=None)
        self.assertEqual((await nobody.get(self.path("/members"), params={"ids": str(self.admin.id)})).status, 403)
