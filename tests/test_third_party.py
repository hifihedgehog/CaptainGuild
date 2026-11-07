"""What THIRD_PARTY.md and the requirements promise, against what the code and the bundled files really hold."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from captainguild.db import Database, _open_encrypted
from captainguild.dbtools import copy_database, decrypt
from tests.fakes import KEY

ROOT = Path(__file__).resolve().parent.parent
PROGRAM = ROOT / "captainguild"
LICENSES = PROGRAM / "web" / "static" / "vendor" / "licenses"
# Discord itself, sites only linked to in posts, and sample addresses in help text.
NOT_SERVICES = {"discord.com", "discord.gg", "cdn.discordapp.com", "media.discordapp.net", "twitch.tv", "www.twitch.tv", "www.youtube.com", "youtube.com", "youtu.be", "www.example.com", "example.com", "your.site", "www"}


class OutsideServicesTest(unittest.TestCase):
    def test_every_service_the_code_calls_is_listed(self):
        text = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
        hosts: set[str] = set()
        for path in PROGRAM.rglob("*.py"):
            hosts |= set(re.findall(r"https://([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)", path.read_text(encoding="utf-8")))
        wanted = {host.lower() for host in hosts} - NOT_SERVICES
        self.assertIn("api.urbandictionary.com", wanted)
        missing = sorted(host for host in wanted if host.removeprefix("www.").removeprefix("api.") not in text)
        self.assertEqual(missing, [], "THIRD_PARTY.md has to list what each of these receives")

    def test_links_a_member_pastes_are_listed(self):
        text = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
        self.assertIn("starb.in", text)
        self.assertIn("pastebin.com", text)

    def test_the_readme_says_a_person_using_a_feature_is_enough(self):
        self.assertIn("only when someone uses a feature that needs one", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_twitch_names_the_secret(self):
        self.assertRegex((ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8"), r"Twitch.*client ID and secret")


class BundledFilesTest(unittest.TestCase):
    def test_each_bundled_project_has_its_license_beside_it(self):
        expected = {"preact": "Jason Miller", "htm": "Google Inc.", "lucide": "Cole Bemis", "unicode-emoji-json": "Mu-An Chiou", "unicode": "Unicode, Inc.", "roboto": "Google Inc.", "poppins": "The Poppins Project Authors"}
        for name, holder in expected.items():
            text = (LICENSES / f"{name}-LICENSE.txt").read_text(encoding="utf-8")
            self.assertIn(holder, text, name)

    def test_the_guide_to_them_names_the_folder(self):
        self.assertIn("vendor/licenses/", (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8"))

    def test_the_bundles_say_where_their_notices_are(self):
        self.assertIn("licenses/", (PROGRAM / "web" / "static" / "vendor" / "htm-preact.js").read_text(encoding="utf-8")[:200])
        self.assertIn("licenses/", (PROGRAM / "web" / "static" / "js" / "icons.js").read_text(encoding="utf-8")[:200])
        self.assertIn("licenses/", (ROOT / "scripts" / "build_vendor.py").read_text(encoding="utf-8"), "the generator writes the same line")


class RequirementsTest(unittest.TestCase):
    def test_time_zones_come_with_the_packages(self):
        self.assertRegex((ROOT / "requirements.txt").read_text(encoding="utf-8"), r"(?m)^tzdata")
        self.assertRegex((ROOT / "requirements.lock").read_text(encoding="utf-8"), r"(?m)^tzdata==")

    def test_the_readme_says_which_python_the_lock_takes(self):
        self.assertIn("`requirements.lock` takes Python 3.13", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_the_test_job_has_time_to_finish(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(int(re.search(r"(?m)^  test:\n(?:.*\n)*?\s+timeout-minutes: (\d+)", workflow).group(1)), 30)
        # One process takes over an hour, so the job runs the suite in four shares, each several modules at once.
        self.assertIn("shard: [1, 2, 3, 4]", workflow)
        self.assertIn("python scripts/run_tests.py --shard ${{ matrix.shard }}/4", workflow)


class DatabaseToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.holder.cleanup)
        self.folder = Path(self.holder.name)
        self.source = self.folder / "main.db"
        db = Database(self.source, KEY)
        await db.open()
        await db.run("CREATE TABLE sample (value INTEGER)")
        await db.close()

    def test_a_copy_makes_the_folder_it_needs(self):
        target = self.folder / "backups" / "deeper" / "main.db"
        copy_database(self.source, target, KEY)
        reader = _open_encrypted(str(target), KEY)
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
        reader.close()

    def test_a_plain_copy_makes_the_folder_it_needs(self):
        target = self.folder / "readable" / "main.db"
        decrypt(self.source, target, KEY)
        self.assertTrue(target.is_file())
