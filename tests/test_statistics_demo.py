"""The demo's statistics pages each have something on them, its public page answers with no session, its people count only while they
were members, and its saved view is its first preset."""
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from captainguild.demo import GUILD, build_demo

SLOT = 900


class DemoStatistics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.bot, app, self.closers = await build_demo(Path(self.folder.name))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.bot.timers.stop()
        for close in self.closers:
            await close()
        self.folder.cleanup()

    async def get(self, tail, base=f"/api/g/{GUILD}"):
        response = await self.client.get(base + tail)
        self.assertEqual(response.status, 200, (tail, await response.text()))
        return await response.json()

    async def test_every_page_has_numbers(self):
        self.assertGreater((await self.get("/statistics/activity?kind=messages"))["total"], 0)
        self.assertGreater((await self.get("/statistics/activity?kind=voice"))["total"], 0)
        self.assertTrue((await self.get("/statistics/top?kind=messages&by=members"))["rows"])
        self.assertTrue((await self.get("/statistics/top?kind=messages&by=channels"))["rows"])
        self.assertTrue((await self.get("/statistics/overview"))["totals"]["members_now"])
        self.assertTrue((await self.get("/statistics/status"))["breakdown"])
        self.assertTrue((await self.get("/statistics/apps"))["rows"])
        self.assertTrue((await self.get("/statistics/invites"))["joins"])
        self.assertTrue([row for row in (await self.get("/statistics/emoji"))["rows"] if row["uses"]])
        self.assertTrue((await self.get("/statistics/analytics/members"))["rows"])
        self.assertGreater(sum((await self.get("/statistics/analytics"))["messages"]["count"]), 0)
        people = (await self.get("/statistics/top?kind=messages&by=members&period=all"))["rows"]
        self.assertTrue((await self.get(f"/statistics/drilldown/members?ids={people[0][1]}"))["members"][0]["messages"])
        values, start = [], 0
        while start is not None:
            answer = await self.get(f"/statistics/panels/values?from={start}")
            values += answer["values"]
            start = answer["next"]
        self.assertEqual(len(values), 5)
        self.assertTrue(all(value["number"] is not None for value in values))

    async def test_people_count_only_between_joining_and_leaving(self):
        db = self.bot.message_db
        people = {row["user_id"]: row for row in await db.all("SELECT user_id, joined_at, left_at FROM stat_people WHERE guild_id = ?", (GUILD,))}
        self.assertGreaterEqual(sum(1 for row in people.values() if row["left_at"]), 3, "some have left")
        checked = 0
        for table, column in (("stat_messages", "user_id"), ("stat_voice", "user_id"), ("stat_activities", "user_id"), ("stat_emoji", "user_id"),
                              ("stat_emoji", "target_id")):
            for row in await db.all(f"SELECT {column} AS who, MIN(slot) AS first, MAX(slot) AS last FROM {table} WHERE guild_id = ? GROUP BY {column}", (GUILD,)):
                person = people.get(row["who"])
                if person is None:
                    continue
                checked += 1
                if person["joined_at"]:
                    self.assertGreater(row["first"] * SLOT + SLOT, person["joined_at"], (table, column, row["who"], "before joining"))
                if person["left_at"]:
                    self.assertLess(row["last"] * SLOT, person["left_at"], (table, column, row["who"], "after leaving"))
        self.assertGreater(checked, 40)

    async def test_its_saved_view_is_a_preset(self):
        presets = await self.get("/statistics/presets")
        self.assertEqual([(row["name"], row["query"]) for row in presets["filters"]],
                         [("Two months, weekly", {"period": "2mo", "interval": "week", "unit": "days", "include": "humans", "stat": "messages"})])

    async def test_the_public_page_answers_with_no_session(self):
        page = await self.client.get(f"/stats/{GUILD}")
        self.assertEqual(page.status, 200)
        data = await self.get("/statistics/overview", base=f"/api/public/g/{GUILD}")
        self.assertGreater(data["totals"]["messages_now"], 0)
        text = json.dumps(await self.get("/statistics/top?kind=messages&by=channels", base=f"/api/public/g/{GUILD}"))
        self.assertIn("general", text)
        self.assertNotIn("printer_fan", text)


if __name__ == "__main__":
    unittest.main()
