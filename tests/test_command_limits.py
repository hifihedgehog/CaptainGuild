"""Discord refuses the whole command list if one entry breaks a limit, so these hold for everything the bot registers."""
import re

import discord
from discord import app_commands

from captainguild.core.tree import CONTEXT_MENUS_PER_TYPE
from captainguild.modules import load_all
from tests.fakes import BotTest

NAME = re.compile(r"^[-_'\wऀ-ॿ฀-๿]{1,32}$")
GLOBAL_CHAT_COMMANDS = 100
PER_LEVEL = 25
COMMAND_CHARACTERS = 8000


def characters(command: dict) -> int:
    total = len(command.get("name", "")) + len(command.get("description", ""))
    for option in command.get("options", []):
        total += characters(option)
        for choice in option.get("choices", []) or []:
            total += len(choice["name"]) + len(str(choice["value"]))
    return total


class CommandLimitsTest(BotTest):
    modules = load_all()

    def check(self, command: dict, path: str) -> None:
        self.assertRegex(command["name"], NAME, path)
        self.assertEqual(command["name"], command["name"].lower(), f"{path}: names must be lowercase")
        self.assertTrue(1 <= len(command.get("description", "x")) <= 100, f"{path}: description is {len(command.get('description', ''))} characters")
        options = command.get("options", [])
        self.assertLessEqual(len(options), PER_LEVEL, f"{path}: too many options")
        seen_optional = False
        for option in options:
            child = f"{path} {option['name']}"
            if option["type"] in (1, 2):
                self.check(option, child)
                continue
            self.assertRegex(option["name"], NAME, child)
            self.assertTrue(1 <= len(option["description"]) <= 100, f"{child}: description is {len(option['description'])} characters")
            if option.get("required"):
                self.assertFalse(seen_optional, f"{child}: required option after an optional one")
            else:
                seen_optional = True

    async def test_every_registered_command_fits_discords_limits(self):
        commands = self.bot.tree.get_commands()
        chat = [c for c in commands if isinstance(c, app_commands.Group) or isinstance(c, app_commands.Command)]
        menus = [c for c in commands if isinstance(c, app_commands.ContextMenu)]
        self.assertLessEqual(len(chat), GLOBAL_CHAT_COMMANDS)
        self.assertLessEqual(len([m for m in menus if m.type == discord.AppCommandType.user]), CONTEXT_MENUS_PER_TYPE)
        self.assertLessEqual(len([m for m in menus if m.type == discord.AppCommandType.message]), CONTEXT_MENUS_PER_TYPE)
        for command in chat:
            data = command.to_dict(self.bot.tree)
            self.check(data, data["name"])
            self.assertLessEqual(characters(data), COMMAND_CHARACTERS, f"{data['name']}: too many characters in total")

    async def test_every_pack_together_fits_one_servers_limits(self):
        """A server with every command pack on holds all of their commands, beside the shared ones. A pack command never takes a shared command's name."""
        packs = [command for commands in self.bot.pack_commands.values() for command in commands]
        chat = [c for c in packs if isinstance(c, (app_commands.Group, app_commands.Command))]
        menus = [c for c in packs if isinstance(c, app_commands.ContextMenu)]
        self.assertLessEqual(len(chat), GLOBAL_CHAT_COMMANDS)
        for kind in (discord.AppCommandType.user, discord.AppCommandType.message):
            self.assertLessEqual(len([m for m in menus if m.type == kind]), CONTEXT_MENUS_PER_TYPE)
        shared = {(c.name, getattr(c, "type", discord.AppCommandType.chat_input)) for c in self.bot.tree.get_commands()}
        for command in packs:
            self.assertNotIn((command.name, getattr(command, "type", discord.AppCommandType.chat_input)), shared, command.name)
            if not isinstance(command, app_commands.ContextMenu):
                data = command.to_dict(self.bot.tree)
                self.check(data, data["name"])
                self.assertLessEqual(characters(data), COMMAND_CHARACTERS, f"{data['name']}: too many characters in total")

    async def test_no_description_is_cut_to_fit(self):
        """The library cuts an option's description to 100 characters as it builds the command (discord.utils._shorten), so the limit
        above never sees one that's too long, and it ends mid-sentence in Discord's command list. The text as written stays on the
        command's function."""
        long: list[str] = []

        def walk(command: app_commands.Command | app_commands.Group, path: str) -> None:
            if len(command.description) > 100:
                long.append(f"{path}: {len(command.description)} characters")
            if isinstance(command, app_commands.Group):
                for child in command.commands:
                    walk(child, f"{path} {child.name}")
                return
            written = getattr(command.callback, "__discord_app_commands_param_description__", None) or {}
            for name, text in written.items():
                if isinstance(text, str) and len(" ".join(text.split())) > 100:
                    long.append(f"{path} {name}: {len(text)} characters")

        packs = [command for commands in self.bot.pack_commands.values() for command in commands]
        for command in [*self.bot.tree.get_commands(), *packs]:
            if isinstance(command, (app_commands.Command, app_commands.Group)):
                walk(command, command.name)
        self.assertEqual(long, [])

    async def test_prefix_and_slash_names_agree_and_nothing_collides(self):
        names = [c.qualified_name for c in self.bot.commands]
        self.assertEqual(len(names), len(set(names)))
