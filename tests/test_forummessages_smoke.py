"""Runs every command, the menu, the button and the forms of the forum messages pack with it switched on, through the real library. Nothing may
log an error. The shared smoke test walks only the list every server shares, and these live in each server's own list."""
from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands

from captainguild.core.ops import DiscordOps
from captainguild.modules.forummessages import ForumMessages
from tests.gateway import iso, new_id
from tests.test_gateway_smoke import Walker

SOLVED = 1200000000000000001


class ForumSmokeTest(Walker):
    modules = [ForumMessages]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.net.keep_history = True
        self.bot.http._HTTPClient__session = object()  # set at login, and the network here is replaced below the session
        self.bot.module("forummessages").thread_gap = 0
        self.forum["available_tags"] = [{"id": str(SOLVED), "name": "Solved", "moderated": False, "emoji_id": None, "emoji_name": None}]
        self.bot._connection.parse_channel_update(self.forum)
        await self.bot.settings.save(self.guild_id, "forummessages", {"enabled": True, "log_channel": int(self.log_channel["id"]), "forums": [
            {"forum_id": int(self.forum["id"]), "message": "Hi {{OP.Mention}}", "inactivity_message": "", "solved_tag": SOLVED, "helper_roles": [int(self.role_member["id"])]}]})
        await self.bot.sync_server_commands(self.guild_id)
        self.place = discord.Object(self.guild_id)

    async def settle(self, seconds: float = 8.0) -> None:
        """The library's work, then the pack's own tasks, which the library's settle doesn't see."""
        for _ in range(50):
            await super().settle(seconds)
            cog = self.bot.module("forummessages") if getattr(self, "bot", None) is not None else None
            waiting = [task for task in (cog._tasks if cog is not None else ()) if not task.done()]
            if not waiting:
                return
            await asyncio.wait(waiting, timeout=seconds)

    def command_data(self, root: dict, path: tuple, leaf: dict, *, every_option: bool, target_id: int) -> dict:
        data = super().command_data(root, path, leaf, every_option=every_option, target_id=target_id)
        data["guild_id"] = str(self.guild_id)  # a server's own command names its server, and the library looks for it there
        return data

    def forum_post(self) -> dict:
        thread = {"id": str(new_id()), "guild_id": str(self.guild_id), "parent_id": self.forum["id"], "owner_id": str(self.member_id), "name": "Help", "type": 11,
                  "message_count": 1, "member_count": 1, "rate_limit_per_user": 0, "flags": 0, "applied_tags": [],
                  "thread_metadata": {"archived": False, "auto_archive_duration": 10080, "archive_timestamp": iso(), "locked": False}}
        self.net.channels[int(thread["id"])] = thread
        self.bot._connection.parse_thread_create({**thread, "newly_created": True})
        return thread

    async def test_every_pack_command(self):
        commands = [command for command in self.bot.tree.get_commands(guild=self.place) if not isinstance(command, app_commands.ContextMenu)]
        self.assertEqual(len(commands), 10)
        self.forum_post()
        for command in commands:
            root = command.to_dict(self.bot.tree)
            for path, leaf in self.leaves(root):
                with self.subTest(command=" ".join((root["name"], *path))):
                    self.errors.lines.clear()
                    await self.run_command(root, path, leaf, every_option=False, user_id=self.owner_id, target_id=self.member_id)
                    await self.run_command(root, path, leaf, every_option=True, user_id=self.owner_id, target_id=self.member_id)
                    await self.run_command(root, path, leaf, every_option=True, user_id=self.member_id, target_id=self.admin_id)

    async def test_the_menu_and_the_button_in_a_forum_post_and_elsewhere(self):
        thread = self.forum_post()
        starter = self.message_payload(user_id=self.member_id, channel=thread, content="It won't heat", id=thread["id"])
        self.net.messages[int(starter["id"])] = starter
        posted = len(self.net.posted)
        self.bot._connection.parse_message_create(starter)
        await self.settle()
        self.assert_no_errors("the welcome")
        reply = self.message_payload(user_id=self.mod_id, channel=thread, content="Dry the filament")
        self.net.messages[int(reply["id"])] = reply
        elsewhere = self.dispatch_message(user_id=self.member_id, content="not in a forum")
        [menu] = [command for command in self.bot.tree.get_commands(guild=self.place) if isinstance(command, app_commands.ContextMenu)]
        for target, where in ((reply, thread), (elsewhere, None)):
            for user_id in (self.member_id, self.mod_id, self.owner_id):
                with self.subTest(menu=menu.name, target=target["id"], user=user_id):
                    self.errors.lines.clear()
                    data: dict[str, Any] = {"id": str(new_id()), "name": menu.name, "type": 3, "target_id": target["id"],
                                            "resolved": {"messages": {target["id"]: target}}, "guild_id": str(self.guild_id)}
                    self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id, channel=where))
                    await self.settle()
                    self.assert_no_errors(f"menu {menu.name}")
        for user_id in (self.admin_id, self.member_id):
            self.errors.lines.clear()
            await self.press_everything(posted, user_id=user_id, label="the welcome's button")
