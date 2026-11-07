"""The Discord things a template can read: {user}, {target}, {server}, {channel} and {role}.

A thing's plain name gives its usual text and a parameter gives a property, as in {user(id)}. Reading a property never raises: a deleted role or
a member who has left gives empty text, because the template's author can't guard against that. A property nobody has heard of stays as written.
"""
from __future__ import annotations

import random
from typing import Any, Callable

from .clock import stamp
from .text import plain, shield, words_of

# The permissions {role(has_elevated_perms)} looks for.
ELEVATED = frozenset({
    "administrator", "manage_guild", "manage_channels", "manage_roles", "manage_messages", "manage_expressions", "manage_webhooks",
    "manage_nicknames", "mention_everyone", "manage_threads", "manage_events", "kick_members", "ban_members", "moderate_members",
})


def _boolean(value: bool) -> str:
    return "true" if value else "false"


def _avatar(person: Any) -> str:
    return str(person.display_avatar.url)


def _color(person: Any) -> str:
    color = getattr(person, "color", None)
    if color is not None:
        return str(color)
    for role in sorted(getattr(person, "roles", []), key=lambda r: r.position, reverse=True):
        if role.color.value:
            return str(role.color)
    return "#000000"


def _role_ids(person: Any) -> str:
    return " ".join(str(role.id) for role in sorted(getattr(person, "roles", []), key=lambda r: r.position))


def _position(person: Any) -> str:
    top = getattr(person, "top_role", None)
    return str(top.position) if top is not None else "0"


_PERSON: dict[str, Callable[[Any], str]] = {
    "avatar": _avatar,
    "icon": _avatar,
    "id": lambda p: str(p.id),
    "mention": lambda p: p.mention,
    "created_at": lambda p: stamp(p.created_at),
    "joined_at": lambda p: stamp(getattr(p, "joined_at", None)),
    "color": _color,
    "name": lambda p: p.name,
    "nick": lambda p: p.display_name,
    "proper": str,
    "roleids": _role_ids,
    "position": _position,
}

_CHANNEL: dict[str, Callable[[Any], str]] = {
    "id": lambda c: str(c.id),
    "name": lambda c: c.name,
    "topic": lambda c: getattr(c, "topic", None) or "",
    "slowmode": lambda c: str(getattr(c, "slowmode_delay", 0) or 0),
    "position": lambda c: str(getattr(c, "position", 0) or 0),
    "mention": lambda c: c.mention,
}


class Thing:
    """Something a variable can hold besides text."""

    def __init__(self, source: Any, usual: Callable[[Any], str], properties: dict[str, Callable[[Any], str]]) -> None:
        self.source = source
        self.usual = usual
        self.properties = properties

    def _read(self, function: Callable[[Any], str]) -> str:
        try:
            return str(function(self.source))
        except Exception:  # a template can't guard against a half-gone Discord object, and it must not break the run
            return ""

    def read(self, parameter: str | None, payload: str | None) -> str | None:
        if parameter is None:
            return shield(self._read(self.usual))
        name = plain(parameter).strip()
        function = self.properties.get(name)
        if function is not None:
            return shield(self._read(function))
        if name.lstrip("+-").rstrip("+").isdigit():
            return words_of(shield(self._read(self.usual)), parameter, payload)
        return None


def plain_name(member: Any) -> str:
    """A person's name with one thing changed: `[text](link)` in a display name would show as a link in the bot's own post, with the server's trust behind it."""
    return member.display_name.replace("](", "]\\(")


def person(member: Any) -> Thing:
    return Thing(member, plain_name, _PERSON)


def channel(place: Any) -> Thing:
    return Thing(place, lambda c: c.name, _CHANNEL)


def role(item: Any, added: bool | None) -> Thing:
    def elevated(r: Any) -> str:
        return _boolean(any(on and name in ELEVATED for name, on in r.permissions))

    return Thing(item, lambda r: r.name, {
        "mention": lambda r: r.mention,
        "position": lambda r: str(r.position),
        "id": lambda r: str(r.id),
        "name": lambda r: r.name,
        "is_added": lambda r: _boolean(bool(added)),
        "has_elevated_perms": elevated,
    })


def server(guild: Any) -> Thing:
    """A server's member list is read once per run, so a template can't make the bot walk 43,000 members once per block."""
    groups: dict[str, list[Any]] = {}

    def everyone() -> list[Any]:
        if "all" not in groups:
            groups["all"] = list(getattr(guild, "members", []))
        return groups["all"]

    def is_online(member: Any) -> bool:
        return str(getattr(member, "status", "offline")) != "offline"

    def split_by_status() -> None:
        if "online" not in groups:
            groups["online"] = [m for m in everyone() if is_online(m)]
            groups["offline"] = [m for m in everyone() if not is_online(m)]

    def pick(group: str = "all") -> str:
        if group != "all":
            split_by_status()
        # Status is unknown without the presence intent. Then everyone looks offline, and any member stands in for one online.
        people = groups.get(group) or everyone()
        return str(random.choice(people)) if people else ""

    def bots() -> list[Any]:
        if "bots" not in groups:
            groups["bots"] = [m for m in everyone() if m.bot]
        return groups["bots"]

    def owner(g: Any) -> str:
        return str(g.owner) if g.owner else str(g.owner_id)

    def member_count(g: Any) -> str:
        return str(g.member_count if g.member_count is not None else len(everyone()))

    return Thing(guild, lambda g: g.name, {
        "icon": lambda g: str(g.icon.url) if g.icon else "",
        "id": lambda g: str(g.id),
        "name": lambda g: g.name,
        "owner": owner,
        "random": lambda g: pick(),
        "randomonline": lambda g: pick("online"),
        "randomoffline": lambda g: pick("offline"),
        "members": member_count,
        "bots": lambda g: str(len(bots())),
        "humans": lambda g: str(len(everyone()) - len(bots())),
        "roles": lambda g: str(len(g.roles)),
        "channels": lambda g: str(len(g.channels)),
        "created_at": lambda g: stamp(g.created_at),
    })
