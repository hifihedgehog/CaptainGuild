"""The outside term-matching module named at start: loading it, reading each server's lists, finding and censoring words."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from captainguild import __main__ as launcher
from captainguild.core.answers import screening
from tests.answer_fakes import write_term_lists

GUILD = 1100000000000000001
LISTS = {"blacklists": {"House list": ["badword", "worse"], "Phrases": ["print a weapon"]}, "whitelists": {"Fine": ["worse"]}, "replacement_string": "[gone]"}


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.holder.cleanup)
        self.folder = Path(self.holder.name)
        self.module, self.configs = write_term_lists(self.folder, {GUILD: LISTS})
        self.addCleanup(screening.use, None)

    def test_a_module_and_folder_load(self):
        lists = screening.load(self.module, self.configs)
        self.assertEqual(lists.config(GUILD)["replacement_string"], "[gone]")
        self.assertIsNone(lists.config(GUILD + 1), "a server with no file isn't screened by these lists")

    def test_what_cant_be_used_says_why(self):
        broken = self.folder / "broken.py"
        broken.write_text("raise RuntimeError('needs a package this machine lacks')\n", encoding="utf-8")
        partial = self.folder / "partial.py"
        partial.write_text("character_map = {}\nasync def find_blocked_terms(content, b, w):\n    return set()\n", encoding="utf-8")
        cases = [
            ((self.folder / "missing.py", self.configs), "isn't a file"),
            ((self.module, self.folder / "nowhere"), "isn't a folder"),
            ((broken, self.configs), "didn't load: RuntimeError: needs a package this machine lacks"),
            ((partial, self.configs), "has no censor_terms, replacement_string, load_character_map, pattern_cache"),
        ]
        for arguments, expected in cases:
            with self.assertRaises(screening.ScreeningError) as caught:
                screening.load(*arguments)
            self.assertIn(expected, str(caught.exception))

    def test_the_launcher_loads_it_once_or_says_why_not(self):
        launcher.load_term_matching(str(self.module), str(self.configs))
        self.assertIsNotNone(screening.active())
        self.assertIsNone(screening.problem())
        with self.assertLogs("captainguild", level="ERROR") as caught:
            launcher.load_term_matching(str(self.folder / "missing.py"), str(self.configs))
        self.assertIsNone(screening.active())
        self.assertIn("isn't a file", screening.problem())
        self.assertIn("automod word rule", caught.output[0])
        launcher.load_term_matching(None, None)
        self.assertIsNone(screening.active(), "no flags change nothing")

    def test_the_two_flags_go_together(self):
        root = Path(__file__).resolve().parent.parent
        run = subprocess.run([sys.executable, "-m", "captainguild", "--public-url", "https://x.example", "--term-matching", str(self.module)],
                             cwd=root, capture_output=True, text=True, timeout=120, env={**os.environ, "DISCORD_TOKEN": "", "MASTER_KEY": ""})
        self.assertEqual(run.returncode, 2)
        self.assertIn("--term-matching and --term-configs go together", run.stderr)


class ListsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.holder.name)
        module, self.configs = write_term_lists(self.folder, {GUILD: LISTS})
        self.lists = screening.load(module, self.configs)

    async def asyncTearDown(self):
        self.holder.cleanup()

    async def test_words_are_found_one_text_at_a_time(self):
        self.assertEqual(await self.lists.blocked(GUILD, ["a BADW0RD here", "worse is fine here"]), {"badword"})
        self.assertEqual(await self.lists.blocked(GUILD, ["how do I print", "a weapon mount"]), set(), "a match never spans two texts")
        self.assertEqual(await self.lists.blocked(GUILD, ["how do I print a weapon mount"]), {"print a weapon"})
        self.assertEqual(await self.lists.blocked(GUILD + 1, ["badword"]), set())
        self.assertEqual(await self.lists.blocked(None, ["badword"]), set())

    async def test_censoring_uses_the_servers_replacement(self):
        self.assertEqual(await self.lists.censor(GUILD, ["one badword two", "", "clean"]), ["one [gone] two", "", "clean"])
        self.assertEqual(await self.lists.censor(GUILD + 1, ["badword"]), ["badword"])

    async def test_a_changed_file_is_read_again_and_a_half_written_one_is_not(self):
        path = self.configs / f"{GUILD}.json"
        self.assertEqual(await self.lists.blocked(GUILD, ["newword"]), set())
        path.write_text(json.dumps({**LISTS, "blacklists": {"House list": ["newword"]}}), encoding="utf-8")
        later = time.time() + 5
        os.utime(path, (later, later))
        self.assertEqual(await self.lists.blocked(GUILD, ["newword"]), {"newword"})
        path.write_text('{"blacklists": {"House', encoding="utf-8")
        os.utime(path, (later + 5, later + 5))
        self.assertEqual(await self.lists.blocked(GUILD, ["newword"]), {"newword"}, "the last good copy stays")

    async def test_warming_compiles_each_servers_patterns(self):
        await self.lists.warm([GUILD])
        self.assertIn("badword", self.lists.module.pattern_cache)


if __name__ == "__main__":
    unittest.main()
