"""What a person reads: a refusal names the remedy for its own cause, one wording per message, and the published pages match the code."""
from __future__ import annotations

import html
import re
import sys
import types
from pathlib import Path
from unittest import mock

import discord

from captainguild.core.tagscript.runner import send_refusal
from captainguild.modules import load_all
from captainguild.modules.embeds import Embeds
from captainguild.modules.greetings import Greetings
from captainguild.modules.utilities import Utilities
from captainguild.modules.voice import Voice
from tests.fakes import BotTest, FakeMember

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.build_docs import GUIDE, command_tables, page_text  # noqa: E402

STATIC = ROOT / "captainguild" / "web" / "static"


def refused(status: int, text: str) -> discord.HTTPException:
    response = types.SimpleNamespace(status=status, reason="no")
    return discord.Forbidden(response, text) if status == 403 else discord.HTTPException(response, text)


def raising(error: Exception):
    async def send(*args, **kwargs):
        raise error
    return send


class RemedyFitsItsCauseTest(BotTest):
    modules = [Utilities, Embeds, Voice, Greetings]

    async def poll(self, error: Exception) -> list[str]:
        cog = self.cog(Utilities)
        ctx = self.context()
        with mock.patch.object(self.channel, "send", raising(error)):
            await cog.poll.callback(cog, ctx, question="Is this nice?", choices=None)
        return ctx.fails

    async def test_a_poll_discord_refused_for_its_text_names_no_permission(self):
        said = (await self.poll(refused(400, "Invalid Form Body")))[0]
        self.assertIn("Invalid Form Body", said)
        self.assertNotIn("I need", said)

    async def test_a_poll_with_no_permission_names_the_permissions(self):
        said = (await self.poll(refused(403, "Missing Permissions")))[0]
        self.assertIn("I need View Channel, Send Messages and Embed Links", said)

    async def test_a_poll_reaction_refused_for_a_bad_emoji_names_no_permission(self):
        cog = self.cog(Utilities)
        ctx = self.context()
        posted = await self.channel.send("x")
        posted.add_reaction = raising(refused(400, "Unknown Emoji"))

        async def send(*args, **kwargs):
            return posted
        with mock.patch.object(self.channel, "send", send):
            await cog.poll.callback(cog, ctx, question="Is this nice?", choices=None)
        self.assertIn("Unknown Emoji", ctx.fails[0])
        self.assertNotIn("I need", ctx.fails[0])

    async def giveaway(self, error: Exception) -> list[str]:
        cog = self.cog(Utilities)
        ctx = self.context(author=self.admin)
        with mock.patch.object(self.channel, "send", raising(error)):
            await cog.giveaway_create.callback(cog, ctx, 3600, 1, prize="A printer")
        return ctx.fails

    async def test_a_giveaway_discord_was_down_for_names_no_permission(self):
        said = (await self.giveaway(refused(503, "Service Unavailable")))[0]
        self.assertNotIn("I need", said)
        self.assertIn("Try again", said)

    async def test_a_giveaway_with_no_permission_names_the_permissions(self):
        self.assertIn("I need View Channel", (await self.giveaway(refused(403, "Missing Access")))[0])

    async def test_a_birthday_post_discord_refused_names_no_permission(self):
        cog = self.cog(Greetings)
        for error, expected, unexpected in (
            (refused(500, "broken"), "Discord refused", "Give it"),
            (refused(403, "Missing Access"), "Give it View Channel, Send Messages and Embed Links", "Discord refused"),
        ):
            cog._note_problem(self.guild.id, "birthday", None)
            with mock.patch.object(self.channel, "send", raising(error)):
                await cog._post_birthday(self.channel, self.guild, [FakeMember(self.guild, "someone")], {"content": "Happy birthday"})
            said = cog._problems[(self.guild.id, "birthday")]
            self.assertIn(expected, said)
            self.assertNotIn(unexpected, said)


    async def test_a_vc_report_discord_was_down_for_names_no_permission(self):
        cog = self.cog(Voice)
        await self.bot.settings.save(self.guild.id, "voice", {"report_channel": self.channel.id})
        for error, expected in ((refused(503, "down"), "Try again"), (refused(403, "Missing Access"), "Tell an admin")):
            with mock.patch.object(self.channel, "send", raising(error)):
                said = await cog.submit_report(self.admin, FakeMember(self.guild, "someone"), "shouting")
            self.assertIn(expected, said)

    async def test_the_voice_access_role_refusal_fits_its_cause(self):
        self.assertIn("Check Manage Roles", Voice._role_refusal(refused(403, "Missing Permissions")))
        said = Voice._role_refusal(refused(503, "down"))
        self.assertIn("Try again", said)
        self.assertNotIn("Manage Roles", said)

    async def test_a_template_post_refusal_fits_its_cause(self):
        self.assertIn("View Channel", send_refusal(refused(403, "Missing Access")))
        self.assertIn("Try again", send_refusal(refused(503, "down")))
        self.assertIn("links and embeds", send_refusal(refused(400, "Invalid Form Body")))


class OneWordingTest(BotTest):
    modules = []

    def shipping(self) -> list[Path]:
        roots = [ROOT / "captainguild"]
        return [path for root in roots for path in root.rglob("*") if path.suffix in {".py", ".js"} and "vendor" not in path.parts and path.name != "icons.js"]

    def test_anymore_is_one_word(self):
        found = [f"{path.name}: {line.strip()[:80]}" for path in self.shipping() for line in path.read_text(encoding="utf-8").splitlines() if re.search(r"(isn't|aren't|won't|doesn't|don't|on record|in the list|there) any more\b", line)]
        self.assertEqual(found, [])

    def test_a_button_for_someone_else_says_one_thing(self):
        wordings = set()
        for path in self.shipping():
            wordings |= set(re.findall(r'"((?:Only the person who ran|This (?:question|button) is for the person who ran)[^"]*)"', path.read_text(encoding="utf-8")))
        self.assertEqual(wordings, {"Only the person who ran this command can use this."})

    def test_a_channel_not_found_says_one_thing(self):
        found = [path.name for path in self.shipping() if "couldn't find a channel" in path.read_text(encoding="utf-8")]
        self.assertEqual(found, [])

    def test_the_elevated_setting_has_one_name(self):
        for path in self.shipping():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("elevated role assignment", text, path.name)
            self.assertNotIn("Commands, global settings", text, path.name)


class PublishedPagesTest(BotTest):
    modules = load_all()

    async def test_the_page_is_what_the_generator_writes(self):
        written = (STATIC / "docs.html").read_text(encoding="utf-8")
        self.assertEqual(written, page_text(self.bot.command_catalog()), "Run scripts/build_docs.py")

    async def test_each_command_lists_the_other_names_it_answers_to(self):
        tables = command_tables(self.bot.command_catalog())[0]
        for group in self.bot.command_catalog():
            for row in group["commands"]:
                for alias in row["aliases"]:
                    self.assertIn(f"<code>{html.escape(alias)}</code>", tables, row["name"])

    def test_the_settings_guide_names_each_setting_on_its_page(self):
        for label in ("Pause the sender", "Copied text", "Bad names", "Punish above", "Skip bots", "Joins in a minute before welcomes pause", "Panel opacity", "Moderator delete log channel", "Role log channel", "Name log channel", "Timeout log channel", "Private channels", "Record mini mutes as cases"):
            self.assertIn(label, GUIDE)

    def test_the_narrow_screen_rules_come_after_the_rules_they_change(self):
        css = (STATIC / "css" / "site.css").read_text(encoding="utf-8")
        narrow = css.index("@media (max-width: 720px)")
        self.assertGreater(narrow, css.index(".toc ul { margin: 0; columns: 2; }"))
        self.assertIn(".site-main table { display: block; overflow-x: auto; }", css[narrow:])

    def test_the_privacy_page_names_what_the_tables_hold(self):
        page = (STATIC / "privacy.html").read_text(encoding="utf-8")
        for phrase in ("raid mode", "saved messages", "Suggestions", "Starboard", "Giveaways", "mini mute", "Backup snapshots", "voice access", "could read a deleted channel"):
            self.assertIn(phrase, page)
        self.assertNotIn("nightly", page)
