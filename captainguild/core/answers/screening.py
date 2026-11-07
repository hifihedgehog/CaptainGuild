"""Screening AI text with an outside term-matching module, named when the bot starts.

A host that already runs a word filter can point the bot at that filter's own matching code and its folder of per-server lists:

    python -m captainguild ... --term-matching /path/to/term_matching.py --term-configs /path/to/server_configs

The module is loaded once, at start, and has to provide these coroutines, which read the lists they're given and touch nothing else:

    find_blocked_terms(text, blacklists, whitelists) -> set of the blacklist entries found
    censor_terms(text, blacklists, whitelists, replacement) -> the text with each one replaced
    replacement_string(config) -> what replaces a blocked term for this server

and two values: `character_map`, a dict filled by `load_character_map()` on first use, and `pattern_cache`. The folder holds one
`<server ID>.json` per server, with `blacklists` and `whitelists` objects of named lists and an optional `replacement_string`. A server
with no file there isn't screened by it. Every list applies to every text, whoever asked, since the bot's answer can be read anywhere.

Matching is slow work: compiling every pattern takes several seconds, and a check then takes about half a millisecond a character. It
runs in a worker thread with its own event loop, one text at a time, so no match spans two texts and the bot keeps answering events.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

NEEDED = ("find_blocked_terms", "censor_terms", "replacement_string", "load_character_map", "character_map", "pattern_cache")


class ScreeningError(Exception):
    """The module or its folder can't be used. The text says why."""


class TermLists:
    def __init__(self, module: Any, folder: Path) -> None:
        self.module = module
        self.folder = folder
        self._configs: dict[int, tuple[float, dict]] = {}  # server ID -> (the file's modified time, its contents)

    def config(self, guild_id: int | None) -> dict | None:
        """The server's lists, read again whenever the file changes."""
        if guild_id is None:
            return None
        path = self.folder / f"{int(guild_id)}.json"
        try:
            changed = os.stat(path).st_mtime
        except OSError:
            return None
        cached = self._configs.get(guild_id)
        if cached and cached[0] == changed:
            return cached[1]
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The filter rewrites its file in place, so a read can land halfway through a write.
            return cached[1] if cached else None
        if not isinstance(loaded, dict):
            return cached[1] if cached else None
        self._configs[guild_id] = (changed, loaded)
        return loaded

    def _in_thread(self, work: Callable, texts: list, config: dict) -> list:
        """work(text, blacklists, whitelists) over each text, on a private event loop."""
        module = self.module

        async def run() -> list:
            if not module.character_map:
                await module.load_character_map()
            blacklists, whitelists = config.get("blacklists") or {}, config.get("whitelists") or {}
            return [await work(text, blacklists, whitelists) if text and text.strip() else None for text in texts]
        return asyncio.run(run())

    async def blocked(self, guild_id: int | None, texts: list[str]) -> set[str]:
        """The blacklist entries found in any of the texts."""
        config = self.config(guild_id)
        if config is None:
            return set()
        results = await asyncio.to_thread(self._in_thread, self.module.find_blocked_terms, list(texts), config)
        return set().union(*(found for found in results if found))

    async def censor(self, guild_id: int | None, texts: list[str]) -> list[str]:
        """The texts with each blocked term replaced, the way the filter censors a member's message."""
        texts = list(texts)
        config = self.config(guild_id)
        if config is None:
            return texts
        module = self.module

        async def work(text: str, blacklists: dict, whitelists: dict) -> str:
            replacement = await module.replacement_string(config)
            return await module.censor_terms(text, blacklists, whitelists, replacement)

        results = await asyncio.to_thread(self._in_thread, work, texts, config)
        return [censored if censored is not None else text for text, censored in zip(texts, results)]

    async def warm(self, guild_ids: list[int]) -> None:
        """Compile every pattern ahead of the first real check."""
        for guild_id in guild_ids:
            await self.blocked(guild_id, ["warm up"])
        log.info("The term-matching module has %d patterns ready.", len(self.module.pattern_cache))


def load(module_path: str | Path, folder: str | Path) -> TermLists:
    """Load the module and check the folder. Raises ScreeningError with the reason."""
    path = Path(module_path)
    lists = Path(folder)
    if not path.is_file():
        raise ScreeningError(f"{path} isn't a file.")
    if not lists.is_dir():
        raise ScreeningError(f"{lists} isn't a folder.")
    try:
        spec = importlib.util.spec_from_file_location("term_matching_for_answers", path)
        if spec is None or spec.loader is None:
            raise ScreeningError(f"{path} can't be loaded as Python.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except ScreeningError:
        raise
    except Exception as error:  # the module is outside code, and anything it raises while loading means it can't be used
        raise ScreeningError(f"{path} didn't load: {type(error).__name__}: {error}") from error
    missing = [name for name in NEEDED if not hasattr(module, name)]
    if missing:
        raise ScreeningError(f"{path} has no {', '.join(missing)}.")
    return TermLists(module, lists)


# What the bot loaded at start, and why it didn't when it couldn't. Only __main__ sets these.
_active: TermLists | None = None
_problem: str | None = None


def use(found: TermLists | None, problem: str | None = None) -> None:
    global _active, _problem
    _active, _problem = found, problem


def active() -> TermLists | None:
    return _active


def problem() -> str | None:
    return _problem
