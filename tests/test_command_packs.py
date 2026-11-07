"""Command packs: a feature's slash commands and right-click menus go only into the servers that switch it on, and the right-click menus a
type can hold follow Discord's limit of 15 rather than discord.py 2.7.1's five."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from captainguild.core.module import Module
from captainguild.core.tree import CONTEXT_MENUS_PER_TYPE, Tree
from captainguild.core.validate import Field, Schema, boolean
from tests.gateway import GatewayTest


class Pack(Module):
    key = "packtest"
    category = "Misc"
    command_pack = True
    schema = Schema({"enabled": Field(False, boolean)})

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.menu = app_commands.ContextMenu(name="Pack Menu", callback=self.pack_menu)
        self.ran: list[str] = []

    async def on_load(self) -> None:
        self.bot.tree.add_command(self.menu)

    async def pack_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        self.ran.append("menu")
        await interaction.response.send_message("menu ran", ephemeral=True)

    @commands.hybrid_command(name="pack_hello", description="Says hello from the pack")
    async def pack_hello(self, ctx: commands.Context) -> None:
        self.ran.append("hello")
        await ctx.send("hello")


class TreeLimitTest(GatewayTest):
    modules: list = []

    def menu(self, name: str) -> app_commands.ContextMenu:
        async def callback(interaction: discord.Interaction, message: discord.Message) -> None:
            pass
        return app_commands.ContextMenu(name=name, callback=callback)

    async def test_the_tree_is_ours(self):
        self.assertIsInstance(self.bot.tree, Tree)

    async def test_fifteen_menus_of_a_type_fit_and_a_sixteenth_does_not(self):
        tree = self.bot.tree
        tree.clear_commands(guild=None, type=discord.AppCommandType.message)
        for number in range(CONTEXT_MENUS_PER_TYPE):
            tree.add_command(self.menu(f"Menu {number}"))
        with self.assertRaises(app_commands.CommandLimitReached) as caught:
            tree.add_command(self.menu("One more"))
        self.assertEqual(caught.exception.limit, 15)
        tree.add_command(self.menu("Menu 3"), override=True)  # replacing one doesn't count twice
        place = discord.Object(self.guild_id)
        for number in range(CONTEXT_MENUS_PER_TYPE):
            tree.add_command(self.menu(f"Server menu {number}"), guild=place)
        with self.assertRaises(app_commands.CommandLimitReached):
            tree.add_command(self.menu("One more"), guild=place)
        with self.assertRaises(app_commands.CommandAlreadyRegistered):
            tree.add_command(self.menu("Menu 4"))

    async def test_a_slash_command_still_goes_through_the_library(self):
        @app_commands.command(name="plain_one", description="x")
        async def plain(interaction: discord.Interaction) -> None:
            pass
        self.bot.tree.add_command(plain)
        self.assertIsNotNone(self.bot.tree.get_command("plain_one"))


class PackTest(GatewayTest):
    modules = [Pack]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.pack = self.bot.module("packtest")

    def registered(self) -> list[str]:
        return sorted(command["name"] for command in self.net.guild_commands.get(self.guild_id, []))

    async def switch(self, on: bool) -> None:
        await self.bot.settings.save(self.guild_id, "packtest", {"enabled": on})

    async def test_a_packs_commands_leave_the_shared_list(self):
        self.assertIsNone(self.bot.tree.get_command("pack_hello"))
        self.assertIsNone(self.bot.tree.get_command("Pack Menu", type=discord.AppCommandType.message))
        self.assertEqual(sorted(command.name for command in self.bot.pack_commands["packtest"]), ["Pack Menu", "pack_hello"])

    async def test_a_server_gets_the_commands_of_the_packs_it_has_on_and_only_when_they_changed(self):
        self.assertTrue(await self.bot.sync_server_commands(self.guild_id))
        self.assertEqual(self.registered(), [])
        self.assertFalse(await self.bot.sync_server_commands(self.guild_id))
        await self.switch(True)
        self.assertTrue(await self.bot.sync_server_commands(self.guild_id))
        self.assertEqual(self.registered(), ["Pack Menu", "pack_hello"])
        self.assertFalse(await self.bot.sync_server_commands(self.guild_id))
        await self.switch(False)
        self.assertTrue(await self.bot.sync_server_commands(self.guild_id))
        self.assertEqual(self.registered(), [])
        self.assert_no_errors()

    async def test_a_save_before_the_bot_is_connected_leaves_registration_to_the_ready(self):
        await self.switch(True)
        await self.settle()
        self.assertNotIn(self.guild_id, self.net.guild_commands)

    async def test_a_typed_command_answers_only_where_the_pack_is_on(self):
        self.assertFalse(self.pack.on_in(self.guild_id))
        self.assertFalse(self.pack.on_in(None))
        message = self.net.message_json(int(self.text["id"]), {"content": "!pack_hello"})
        message["author"] = self.member_json[self.member_id]["user"]
        message["member"] = {k: v for k, v in self.member_json[self.member_id].items() if k != "user"}
        message["guild_id"] = str(self.guild_id)
        self.bot._connection.parse_message_create(message)
        await self.settle()
        self.assertEqual(self.pack.ran, [])
        self.assertTrue(any("switched off in this server" in (body.get("content") or "") for _, _, body in self.net.calls))
        await self.switch(True)
        self.assertTrue(self.pack.on_in(self.guild_id))
        message = dict(message, id=str(int(message["id"]) + 1))
        self.bot._connection.parse_message_create(message)
        await self.settle()
        self.assertEqual(self.pack.ran, ["hello"])

    async def test_a_menu_in_a_server_reaches_the_pack(self):
        await self.switch(True)
        await self.bot.sync_server_commands(self.guild_id)
        target = self.net.message_json(int(self.text["id"]), {"content": "hi"})
        data = {"id": str(self.net.guild_commands[self.guild_id][0].get("id") or 1), "name": "Pack Menu", "type": 3, "target_id": target["id"],
                "resolved": {"messages": {target["id"]: target}}, "guild_id": str(self.guild_id)}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.member_id))
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.pack.ran, ["menu"])


class CrowdedTreeTest(GatewayTest):
    """The shared list is near Discord's 100 already. A pack the bot has named is held apart from it from the moment its feature loads, and its typed form stays with the bot."""
    modules: list = []

    async def test_a_packs_slash_form_never_enters_the_shared_list_and_its_typed_form_stays(self):
        tree = self.bot.tree
        for number in range(100 - len(tree.get_commands(type=discord.AppCommandType.chat_input))):
            @app_commands.command(name=f"filler_{number}", description="x")
            async def filler(interaction: discord.Interaction) -> None:
                pass
            tree.add_command(filler)
        self.bot._pack_keys[Pack.__module__] = Pack.key  # as prepare names each pack before its feature loads
        await self.bot.add_cog(Pack(self.bot))  # one more shared command would be past the limit
        self.assertIsNone(tree.get_command("pack_hello"))
        self.assertEqual([command.name for command in tree.held["packtest"]], ["pack_hello"])
        self.assertIsNotNone(self.bot.get_command("pack_hello"), "the typed form stays")
