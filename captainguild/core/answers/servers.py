"""The servers where the bot's host lets Wiki answers spend its AI key, named when the bot starts:

    python -m captainguild ... --answers-servers 111111111111111111,222222222222222222

Every request is billed to the host's own account, so which servers may use it is the host's choice, not a server's. A server off the
list can't switch the pack on, and the pack does nothing there. With no list, no server can use it.
"""
from __future__ import annotations

import re
from typing import Iterable

_ID = re.compile(r"[0-9]{15,22}")
_servers: frozenset[int] = frozenset()


def parse(text: str) -> frozenset[int]:
    """Server IDs from the flag's value, separated by commas or spaces. Raises ValueError naming anything else."""
    found = set()
    for part in re.split(r"[\s,]+", text.strip()):
        if not part:
            continue
        if not _ID.fullmatch(part):
            raise ValueError(f"{part} isn't a server ID.")
        found.add(int(part))
    return frozenset(found)


def use(servers: Iterable[int]) -> None:
    global _servers
    _servers = frozenset(servers)


def listed() -> frozenset[int]:
    return _servers


def allowed(guild_id: int | None) -> bool:
    return guild_id is not None and guild_id in _servers
