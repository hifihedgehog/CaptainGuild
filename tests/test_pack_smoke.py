"""Runs every command, menu, button and form of the collaborative messages and message mover packs with both switched on, through the real library.
Nothing may log an error. The shared smoke test walks only the list every server shares, and these live in each server's own list."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from captainguild.core.ops import DiscordOps
from captainguild.modules.collaborative import Collaborative
from captainguild.modules.embeds import Embeds
from captainguild.modules.mover import Mover
from tests.gateway import new_id
from tests.test_gateway_smoke import Walker


class PackSmokeTest(Walker):
    modules = [Embeds, Collaborative, Mover]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.net.keep_history = True
        self.bot.http._HTTPClient__session = object()  # set at login, and the network here is replaced below the session
        for key in ("collaborative", "mover"):
            await self.bot.settings.save(self.guild_id, key, {"enabled": True, "log_channel": int(self.log_channel["id"])})
        await self.bot.settings.save(self.guild_id, "collaborative", {"log_enabled": True})
        await self.bot.sync_server_commands(self.guild_id)
        self.place = discord.Object(self.guild_id)

    def command_data(self, root: dict, path: tuple, leaf: dict, *, every_option: bool, target_id: int) -> dict:
        data = super().command_data(root, path, leaf, every_option=every_option, target_id=target_id)
        data["guild_id"] = str(self.guild_id)  # a server's own command names its server, and the library looks for it there
        return data

    async def test_every_pack_command(self):
        commands = [command for command in self.bot.tree.get_commands(guild=self.place) if not isinstance(command, app_commands.ContextMenu)]
        self.assertEqual(len(commands), 12)
        for command in commands:
            root = command.to_dict(self.bot.tree)
            for path, leaf in self.leaves(root):
                with self.subTest(command=" ".join((root["name"], *path))):
                    self.errors.lines.clear()
                    await self.run_command(root, path, leaf, every_option=False, user_id=self.owner_id, target_id=self.member_id)
                    await self.run_command(root, path, leaf, every_option=True, user_id=self.owner_id, target_id=self.member_id)
                    await self.run_command(root, path, leaf, every_option=True, user_id=self.member_id, target_id=self.admin_id)

    async def test_every_pack_menu_on_a_collaborative_message_and_on_a_members_message(self):
        create = next(command for command in self.bot.tree.get_commands(guild=self.place) if command.name == "create_collaborative_message").to_dict(self.bot.tree)
        await self.run_command(create, (), create, every_option=True, user_id=self.owner_id, target_id=self.mod_id)
        made = await self.db.scalar("SELECT p.message_id FROM collab_posts c JOIN posted_messages p ON p.id = c.posted_id ORDER BY c.id DESC LIMIT 1")
        collaborative = self.net.messages[made]
        theirs = self.dispatch_message(user_id=self.member_id, content="something to move")
        self.net.messages[int(theirs["id"])] = theirs
        await self.settle()
        menus = [command for command in self.bot.tree.get_commands(guild=self.place) if isinstance(command, app_commands.ContextMenu)]
        self.assertEqual(len(menus), 9)
        for target in (collaborative, theirs):
            for command in menus:
                for user_id in (self.mod_id, self.owner_id, self.member_id):
                    with self.subTest(menu=command.name, target=target["id"], user=user_id):
                        self.errors.lines.clear()
                        data: dict[str, Any] = {"id": str(new_id()), "name": command.name, "type": 3, "target_id": target["id"],
                                                "resolved": {"messages": {target["id"]: target}}, "guild_id": str(self.guild_id)}
                        posted = len(self.net.posted)
                        modal_mark = len(self.net.modals)
                        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id))
                        await self.settle()
                        self.assert_no_errors(f"menu {command.name}")
                        await self.fill_forms(modal_mark, user_id=user_id, label=f"menu {command.name}")
                        await self.press_everything(posted, user_id=user_id, label=f"menu {command.name}")
