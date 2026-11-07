"""The bot's command tree.

discord.py 2.7.1 refuses a sixth right-click menu of one type, but Discord has allowed 15 of each, globally and in each server, since
March 3, 2026. discord.py's own source raised its check to match in commit 026a882 ("Raise context menu command limit to 15 to match
Discord"), and no release carries it yet, so menus are added here through that same code path with the new number. Everything else is the
library's own, apart from the commands of a command pack, which are held apart from the shared list (see Bot.sync_server_commands).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from discord import AppCommandType
from discord.app_commands import CommandAlreadyRegistered, CommandLimitReached, CommandTree, ContextMenu
from discord.app_commands.tree import _retrieve_guild_ids
from discord.utils import MISSING

CONTEXT_MENUS_PER_TYPE = 15


class Tree(CommandTree):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A command pack's slash commands and menus, by pack, held apart from the list every server shares. They never count against its
        # limits, which a cog's commands reach as it loads, before the bot could take them back out. pack_of names a command's pack, or None.
        self.held: Dict[str, List[Any]] = {}
        self.pack_of: Callable[[Any], Optional[str]] = lambda command: None

    def add_command(self, command: Any, /, *, guild: Optional[Any] = MISSING, guilds: Sequence[Any] = MISSING, override: bool = False) -> None:
        pack = self.pack_of(command) if guild is MISSING and guilds is MISSING else None
        if pack is not None:
            kind = getattr(command, "type", AppCommandType.chat_input)
            held = self.held.setdefault(pack, [])
            same = [found for found in held if found.name == command.name and getattr(found, "type", AppCommandType.chat_input) == kind]
            if same and not override:
                raise CommandAlreadyRegistered(command.name, None)
            held[:] = [found for found in held if found not in same] + [command]
            return
        if not isinstance(command, ContextMenu):
            super().add_command(command, guild=guild, guilds=guilds, override=override)
            return
        # The context menu branch of discord/app_commands/tree.py CommandTree.add_command, as of commit 026a882.
        guild_ids = _retrieve_guild_ids(command, guild, guilds)
        kind = command.type.value

        def add(guild_id: Optional[int], data: Dict[Tuple[str, Optional[int], int], ContextMenu]) -> None:
            key = (command.name, guild_id, kind)
            found = key in self._context_menus
            if found and not override:
                raise CommandAlreadyRegistered(command.name, guild_id)
            to_add = not (override and found)
            total = sum(1 for _, g, t in self._context_menus if g == guild_id and t == kind)
            if total + to_add > CONTEXT_MENUS_PER_TYPE:
                raise CommandLimitReached(guild_id=guild_id, limit=CONTEXT_MENUS_PER_TYPE, type=AppCommandType(kind))
            data[key] = command

        if guild_ids is None:
            add(None, self._context_menus)
            return
        current: Dict[Tuple[str, Optional[int], int], ContextMenu] = {}
        for guild_id in guild_ids:
            add(guild_id, current)
        self._context_menus.update(current)  # all or nothing, as the library does it
