"""What the docs page and the Commands page say about who can run a command, against the real command tree."""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

from discord.ext import commands

from captainguild.core.module import required_permissions, staff_only_label
from captainguild.modules import load_all
from tests.fakes import BotTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build_docs import GUIDE, command_tables, needs_text  # noqa: E402

# A command whose body checks for these names limits who can run it, whatever its declared permissions say.
BODY_CHECK = re.compile(r"\bis_staff\(|\bis_moderator\(|\b_may_propose\(|guild_permissions\.manage_guild")


class CatalogTest(BotTest):
    modules = load_all()

    def rows(self) -> dict[str, dict]:
        return {row["name"]: row for group in self.bot.command_catalog() for row in group["commands"]}

    def child(self, group: str, name: str) -> dict:
        return next(child for child in self.rows()[group]["children"] if child["name"] == f"{group} {name}")

    async def test_each_subcommand_carries_its_own_permissions(self):
        self.assertEqual(self.child("suggestion", "suggest")["permissions"], [])
        self.assertTrue(self.child("suggestion", "approve")["permissions"] or self.child("suggestion", "approve")["staff"])

    async def test_a_group_whose_subcommands_differ_says_so(self):
        self.assertTrue(self.rows()["suggestion"]["varies"])

    async def test_a_group_whose_subcommands_agree_does_not(self):
        rows = self.rows()
        uniform = [row for row in rows.values() if row["children"] and not row["varies"]]
        self.assertTrue(uniform)
        for row in uniform:
            self.assertEqual(len({(tuple(child["permissions"]), child["staff"]) for child in row["children"]}), 1, row["name"])

    async def test_the_old_fields_still_hold(self):
        row = self.rows()["suggestion"]
        self.assertIn("suggestion approve", row["subcommands"])
        self.assertEqual(sorted(child["name"] for child in row["children"] if child["name"] in row["subcommands"]), row["subcommands"])
        self.assertTrue(row["permissions"], "the union stays for anything reading it")

    async def test_every_body_check_is_declared_for_the_page(self):
        missing = []
        for command in self.bot.walk_commands():
            if required_permissions(command) or staff_only_label(command):
                continue
            if isinstance(command, commands.Group) and not getattr(command, "fallback", None):
                continue
            if BODY_CHECK.search(inspect.getsource(command.callback)):
                missing.append(command.qualified_name)
        self.assertEqual(missing, [], "These check who's asking in their own body, so they need @staff_only, or the docs page says anyone can run them")

    async def test_staff_commands_are_labeled(self):
        rows = self.rows()
        self.assertEqual(rows["review"]["staff"], "Staff")
        self.assertEqual(rows["punishment_notice"]["staff"], "Staff")

    async def test_text_only_commands_are_flagged(self):
        rows = self.rows()
        self.assertTrue(rows["set"]["text_only"])
        self.assertFalse(rows["ban"]["text_only"])


class DocsPageTest(BotTest):
    modules = load_all()

    def page(self) -> str:
        return command_tables(self.bot.command_catalog())[0]

    def row_of(self, page: str, name: str, prefix: str = "/") -> str:
        return next(part for part in page.split("<tr>") if part.startswith(f"<td><code>{prefix}{name}</code>"))

    async def test_a_mixed_group_shows_varies_and_each_subcommand_shows_its_need(self):
        row = self.row_of(self.page(), "suggestion")
        self.assertIn(">Varies<", row)
        self.assertIn("<code>suggest</code>", row)
        self.assertRegex(row, r"<code>approve</code> \([^)]+\)")

    async def test_a_staff_command_names_who_can_run_it(self):
        self.assertIn(">Staff<", self.row_of(self.page(), "review"))

    async def test_a_command_anyone_can_run_says_none(self):
        self.assertIn(">None<", self.row_of(self.page(), "cute"))

    async def test_a_text_only_command_shows_with_the_prefix(self):
        page = self.page()
        self.assertIn("<code>!set</code>", page)
        self.assertNotIn("<code>/set</code>", page)
        self.assertIn("<code>/ban</code>", page)

    def test_the_guide_does_not_say_a_new_server_stores_nothing(self):
        self.assertNotIn("A feature does nothing until you set it up", GUIDE)
        self.assertIn("keeps recent message text", GUIDE)

    def test_the_guide_does_not_say_every_command_is_a_slash_command(self):
        self.assertNotIn("Every command works three ways", GUIDE)
        self.assertIn("Most commands work three ways", GUIDE)

    def test_needs_reads_labels_and_permissions(self):
        self.assertEqual(needs_text([], None), "None")
        self.assertEqual(needs_text(["manage_guild"], None), "Manage Server")
        self.assertEqual(needs_text(["manage_guild"], "Staff"), "Staff")
