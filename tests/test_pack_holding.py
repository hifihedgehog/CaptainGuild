"""A command pack's commands are held apart from the list every server shares from the moment its feature loads, so they never count against
that list's limits of 100 slash commands and 15 right-click menus of each type."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from captainguild.core.module import Module
from captainguild.core.tree import CONTEXT_MENUS_PER_TYPE
from tests.gateway import GatewayTest
from tests.test_command_packs import Pack

SHARED_LIMIT = 100


class Filler(Module):
    """A feature that fills the shared list to Discord's limits before the pack loads."""
    key = "filler"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        async def slash(interaction: discord.Interaction) -> None:
            pass

        async def menu(interaction: discord.Interaction, message: discord.Message) -> None:
            pass
        for number in range(SHARED_LIMIT):
            bot.tree.add_command(app_commands.Command(name=f"filler_{number}", description="x", callback=slash))
        for number in range(CONTEXT_MENUS_PER_TYPE):
            bot.tree.add_command(app_commands.ContextMenu(name=f"Filler {number}", callback=menu))


class HeldFromTheStartTest(GatewayTest):
    modules = [Pack]

    async def test_a_packs_commands_never_enter_the_shared_list(self):
        self.assertEqual(sorted(command.name for command in self.bot.tree.held["packtest"]), ["Pack Menu", "pack_hello"])
        self.assertEqual(self.bot.tree.get_commands(), [])


class HeldTest(GatewayTest):
    modules = [Filler, Pack]

    async def test_a_pack_loads_beside_a_full_shared_list(self):
        shared = self.bot.tree.get_commands()
        self.assertEqual(len([c for c in shared if not isinstance(c, app_commands.ContextMenu)]), SHARED_LIMIT)
        self.assertEqual(len(self.bot.tree.get_commands(type=discord.AppCommandType.message)), CONTEXT_MENUS_PER_TYPE)
        self.assertEqual(sorted(command.name for command in self.bot.pack_commands["packtest"]), ["Pack Menu", "pack_hello"])
        self.assertIsNone(self.bot.tree.get_command("pack_hello"))
        await self.bot.settings.save(self.guild_id, "packtest", {"enabled": True})
        self.assertTrue(await self.bot.sync_server_commands(self.guild_id))
        self.assertEqual(sorted(command["name"] for command in self.net.guild_commands[self.guild_id]), ["Pack Menu", "pack_hello"])

    async def test_a_second_command_of_the_same_name_in_a_pack_is_refused(self):
        async def again(interaction: discord.Interaction, message: discord.Message) -> None:
            pass
        menu = app_commands.ContextMenu(name="Pack Menu", callback=again)
        menu.module = Pack.__module__
        with self.assertRaises(app_commands.CommandAlreadyRegistered):
            self.bot.tree.add_command(menu)
        self.bot.tree.add_command(menu, override=True)
        self.assertEqual([c for c in self.bot.tree.held["packtest"] if c.name == "Pack Menu"], [menu])
