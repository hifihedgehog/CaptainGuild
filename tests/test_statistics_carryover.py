"""Statistics presets in Captain Guild's own format: what export writes, import reads back, and the routes that carry them as a file."""
import json
import unittest

from captainguild.core.stats import carryover
from captainguild.modules.statistics import Statistics
from tests.fakes import BotTest

TWO_MONTHS = {"name": "Two months", "kind": "filters", "query": {"period": "2mo", "interval": "week", "unit": "days", "include": "humans", "stat": "messages"},
              "picks": []}


class Format(unittest.TestCase):
    def test_a_preset_keeps_what_a_view_reads_and_drops_the_rest(self):
        cleaned = carryover.clean_preset({"name": "  Two   months ", "query": {**TWO_MONTHS["query"], "junk": "1", "include": "nobody"}, "picks": [5]})
        self.assertEqual(cleaned, {"name": "Two months", "kind": "filters", "query": {"period": "2mo", "interval": "week", "unit": "days", "stat": "messages"},
                                   "picks": []})
        drill = carryover.clean_preset({"name": "Helpers", "kind": "members", "query": {}, "picks": ["123456789012345678", 123456789012345679]})
        self.assertEqual(drill["picks"], [123456789012345678, 123456789012345679])
        for bad in ("not a preset", {"name": "x", "kind": "roles"}, {"name": "x", "kind": "members", "picks": ["bob"]}):
            with self.assertRaises(ValueError):
                carryover.clean_preset(bad)

    def test_names_stay_under_twenty_characters(self):
        self.assertEqual(carryover.preset_name("A very long preset name indeed"), "A very long preset")
        self.assertLess(len(carryover.preset_name("A very long preset name indeed")), 20)
        self.assertEqual(carryover.preset_name("  "), "Preset")


class Import(BotTest):
    modules = [Statistics]

    async def test_export_and_import_carry_presets_between_servers(self):
        done = await carryover.import_presets(self.db, self.guild.id, [TWO_MONTHS, {"name": "Helpers", "kind": "members", "query": {"period": "30d"},
                                                                                     "picks": ["123456789012345678"]}], actor_id=self.admin.id)
        self.assertEqual([preset["name"] for preset in done], ["Two months", "Helpers"])
        exported = await carryover.export_presets(self.db, self.guild.id)
        self.assertEqual(exported, [TWO_MONTHS, {"name": "Helpers", "kind": "members", "query": {"period": "30d"}, "picks": ["123456789012345678"]}])
        await carryover.import_presets(self.db, 1234, json.loads(json.dumps(exported)))
        self.assertEqual(await carryover.export_presets(self.db, 1234), exported)

    async def test_a_preset_of_the_same_name_is_replaced_in_any_case(self):
        await carryover.import_presets(self.db, self.guild.id, [TWO_MONTHS])
        done = await carryover.import_presets(self.db, self.guild.id, [{**TWO_MONTHS, "name": "TWO MONTHS", "query": {"period": "8w"}}])
        self.assertEqual(done[0]["query"], {"period": "8w"})
        rows = await self.db.all("SELECT name, query FROM stat_presets WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual([(row["name"], json.loads(row["query"])) for row in rows], [("TWO MONTHS", {"period": "8w"})])

    async def test_a_full_kind_takes_no_new_name_and_a_bad_preset_says_why(self):
        await carryover.import_presets(self.db, self.guild.id, [{"name": f"p{index}", "query": {}} for index in range(carryover.PRESETS_PER_KIND)])
        done = await carryover.import_presets(self.db, self.guild.id, [{"name": "one more", "query": {}}, {"name": "p3", "query": {"period": "7d"}}, "junk"])
        self.assertIn("error", done[0])
        self.assertEqual(done[1]["query"], {"period": "7d"}, "replacing one already there still works")
        self.assertIn("error", done[2])

    async def test_the_routes_carry_presets_as_a_file(self):
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        client = await self.client()
        sent = await client.post(self.path("/statistics/presets/import"), json={"presets": [TWO_MONTHS, {"name": "bad", "kind": "roles"}]})
        self.assertEqual(sent.status, 200, await sent.text())
        self.assertEqual([("error" in row) for row in (await sent.json())["presets"]], [False, True])
        exported = await client.get(self.path("/statistics/presets/export"))
        self.assertEqual((await exported.json())["presets"], [TWO_MONTHS])
        refused = await client.post(self.path("/statistics/presets/import"), json={"presets": "nope"})
        self.assertEqual(refused.status, 400)


if __name__ == "__main__":
    unittest.main()
