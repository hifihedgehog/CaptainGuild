"""Short text-command names run the real command under the server's rules."""
from __future__ import annotations

import time

from discord.ext import commands

from captainguild.core.ops import DiscordOps
from captainguild.core.perms import MODULE as COMMANDS_MODULE
from captainguild.core.shortcuts import SHORTCUTS
from captainguild.modules import load_all
from captainguild.modules.config import Config
from captainguild.modules.fun import Fun
from captainguild.modules.tags import Tags
from tests.gateway import GatewayTest
from tests.fakes import BotTest


class EveryTargetTest(BotTest):
    modules = load_all()

    async def test_each_short_name_leads_to_a_real_command(self):
        for name, target in SHORTCUTS.items():
            with self.subTest(name=name):
                words = target.split()
                # A value after the command, as in "cute cat", isn't part of the command's name.
                found = self.bot.get_command(" ".join(words)) or self.bot.get_command(words[0])
                self.assertIsNotNone(found, target)

    async def test_no_short_name_hides_a_real_command(self):
        for name in SHORTCUTS:
            found = self.bot.get_command(name)  # for several words, this finds the first word's command when the rest name nothing
            self.assertFalse(found is not None and found.qualified_name == name, f"{name} is a real command, so its shortcut never runs")


class TypedTest(GatewayTest):
    modules = [Config, Fun, Tags]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.net.keep_history = True

    def posted_text(self) -> str:
        return " ".join(m.get("content") or "" for m in self.net.posted)

    async def typed(self, content: str, user_id: int | None = None) -> None:
        self.dispatch_message(user_id=user_id or self.member_id, content=content)
        await self.settle(0.4)
        self.assert_no_errors(content)

    async def test_a_short_name_runs_the_command(self):
        await self.typed("!coinflip")
        self.assertRegex(self.posted_text().lower(), "heads|tails")

    async def test_the_rest_of_the_line_goes_along(self):
        await self.typed("!clap one two three", user_id=self.owner_id)
        self.assertIn("one", self.posted_text())
        self.assertIn("\N{CLAPPING HANDS SIGN}", self.posted_text())

    async def test_upper_case_works(self):
        await self.typed("!COINFLIP")
        self.assertRegex(self.posted_text().lower(), "heads|tails")

    async def test_a_name_nobody_knows_stays_silent(self):
        await self.typed("!notacommand")
        self.assertEqual(self.net.posted, [])

    async def test_a_trusted_tag_with_that_name_comes_first(self):
        await self.db.run(
            "INSERT INTO tags (guild_id, name, content, owner_id, uses, trusted, created_at, updated_at) VALUES (?, 'coinflip', 'My own coinflip', ?, 0, 1, ?, ?)",
            (self.guild_id, self.owner_id, time.time(), time.time()))
        await self.typed("!coinflip")
        self.assertIn("My own coinflip", self.posted_text())

    async def test_the_servers_rules_for_the_real_command_apply(self):
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"overwrites": {"fun": {"enabled": False}}})
        await self.typed("!coinflip")
        self.assertNotRegex(self.posted_text().lower(), "heads|tails")


class DiagnoseTest(GatewayTest):
    modules = [Config, Fun]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)

    async def test_it_says_when_a_command_would_run_and_why_not(self):
        cog = next(m for m in self.bot.modules if isinstance(m, Config))
        channel = self.guild.get_channel(int(self.text["id"]))
        command = self.bot.get_command("fun coinflip")
        self.assertTrue(self.bot.command_decision(command, self.guild.get_member(self.member_id), channel).allowed)
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"overwrites": {"fun": {"enabled": False}}})
        decision = self.bot.command_decision(command, self.guild.get_member(self.member_id), channel)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.reason)
        self.assertIsInstance(cog.config_diagnose, commands.HybridCommand)
