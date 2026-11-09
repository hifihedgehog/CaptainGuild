"""What each statistics card shows: the numbers, gathered under the server's settings, the period picked and what the viewer may see.

Nothing here draws or reaches Discord beyond reading the guild the bot already holds. cards.py draws what these return, and the commands
fetch the pictures a card names (the server icon, an avatar, emoji) before it's drawn.

A channel's name shows only where the person reading may see that channel. A reply only its asker sees checks the asker. A reply in a
channel checks everyone who can see that channel: the name shows when each of them can also see the channel it names. That's worked out
from roles and overwrites, never member by member, so it costs the same on a server of any size (see all_see). Members with an overwrite of
their own on either channel answer for themselves.

Someone who chose privacy is never named or ranked, from the moment they choose it: until their counts move to no one, the cards count them
as no one already.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Sequence

import discord

from .. import access
from . import emoji as emoji_text
from . import invites as invite_rules
from . import query, reads, store
from . import timeframe as tf
from . import words

VIEW_BIT = discord.Permissions(view_channel=True).value
ADMIN_BIT = discord.Permissions(administrator=True).value
EARLIEST = 1_420_070_400.0  # 2015-01-01 UTC, Discord's first moment: nothing is recorded before it, so no chart starts earlier
MINI_POINTS = 120  # a card's small chart has about this many points at most
TOP_SIZE = 10  # rows on a page of /top
CHART_LINES = 5  # activities or emoji drawn as lines on one chart
CHANNEL_KINDS = {
    discord.ChannelType.text.value: "text", discord.ChannelType.news.value: "text", discord.ChannelType.forum.value: "forum",
    discord.ChannelType.voice.value: "voice", discord.ChannelType.stage_voice.value: "stage", discord.ChannelType.category.value: "category",
    getattr(discord.ChannelType, "media", discord.ChannelType.forum).value: "forum", 999: "merged",
}
VOICE_LIKE = frozenset({"voice", "stage"})
VIEWS = ("server", "member", "channel", "messages", "voice", "activity", "emoji", "invites", "chart_messages", "chart_voice", "chart_members",
         "chart_status", "chart_activity", "chart_emoji", "chart_invites", "chart_member", "chart_channel", "top")
TOP_PAGES = ("overview", "members_messages", "members_voice", "channels_messages", "channels_voice", "emoji")
CHART_OF = {"server": "chart_messages", "messages": "chart_messages", "voice": "chart_voice", "member": "chart_member", "channel": "chart_channel",
            "activity": "chart_activity", "emoji": "chart_emoji", "invites": "chart_invites"}
OVERVIEW_OF = {chart: overview for overview, chart in CHART_OF.items() if overview != "server"}
OVERVIEW_OF.update({"chart_members": "server", "chart_status": "server"})


# ---- the server's filters ----

def filters_for(cfg: dict, guild: Any) -> query.Filters:
    """The server's view filters, with its role lists turned into the members holding those roles now. Someone who left holds no roles, so
    an allow list of roles leaves them out, as it did before."""
    members_in: set[int] | None = None
    members_out: set[int] = set()
    listed = set(cfg.get("members") or [])
    if cfg.get("member_mode") == "allow" and listed:
        members_in = set(listed)
    elif listed:
        members_out |= listed
    allowed = set(cfg.get("roles_allow") or [])
    blocked = set(cfg.get("roles_block") or [])
    if guild is not None and (allowed or blocked):
        holders_in: set[int] = set()
        for member in getattr(guild, "members", []):
            held = _role_ids(member)
            if allowed and held & allowed:
                holders_in.add(member.id)
            if blocked and held & blocked:
                members_out.add(member.id)
        if allowed:
            members_in = holders_in if members_in is None else members_in & holders_in
    states = frozenset(store.VOICE_STATES[name] for name in cfg.get("voice_states") or store.VOICE_STATES if name in store.VOICE_STATES)
    uses = frozenset(store.EMOJI_USES[name] for name in cfg.get("emoji_uses") or store.EMOJI_USES if name in store.EMOJI_USES)
    return query.Filters(
        include=cfg.get("include", "humans"), voice_states=states or query.ALL_STATES, channel_mode=cfg.get("channel_mode", "block"),
        channels=frozenset(cfg.get("channels") or []), voice_mode=cfg.get("voice_mode", "same"), voice_channels=frozenset(cfg.get("voice_channels") or []),
        members_in=None if members_in is None else frozenset(members_in), members_out=frozenset(members_out),
        activity_mode=cfg.get("activity_mode", "block"), activities=frozenset(cfg.get("activities") or []), emoji_mode=cfg.get("emoji_mode", "block"),
        emoji=frozenset(cfg.get("emoji") or []), emoji_uses=uses or frozenset(store.EMOJI_USES.values()),
    )


def _role_ids(member: Any) -> set[int]:
    raw = getattr(member, "_roles", None)
    if raw is not None:
        return set(raw)
    return {role.id for role in getattr(member, "roles", [])}


# ---- periods ----

def lookback_of(code: str, cfg: dict) -> dict:
    """A period code as the buttons carry it: "s" for the server's setting, "a" for all time, "d90" for 90 days, "y20250301" since a date."""
    if code == "a":
        return {"mode": "all", "days": tf.DEFAULT_LOOKBACK["days"], "since": None}
    if code.startswith("d") and code[1:].isdigit() and 1 <= int(code[1:]) <= tf.MAX_DAYS:
        return {"mode": "days", "days": int(code[1:]), "since": None}
    if code.startswith("y") and len(code) == 9 and code[1:].isdigit():
        try:
            day = dt.date(int(code[1:5]), int(code[5:7]), int(code[7:9]))
        except ValueError:
            day = None
        if day is not None:
            return {"mode": "since", "days": tf.DEFAULT_LOOKBACK["days"], "since": day.isoformat()}
    return dict(cfg.get("lookback") or tf.DEFAULT_LOOKBACK)


def code_of(lookback: dict) -> str:
    mode = lookback.get("mode")
    if mode == "all":
        return "a"
    if mode == "since" and lookback.get("since"):
        return "y" + lookback["since"].replace("-", "")
    return f"d{int(lookback.get('days') or tf.DEFAULT_LOOKBACK['days'])}"


# ---- who may see a channel's name ----

def _entries(channel: Any) -> list[tuple[int, bool, int, int]]:
    """(target, is a member, allowed, denied) for each of a channel's overwrites."""
    raw = getattr(channel, "_overwrites", None)
    if raw is not None:
        return [(entry.id, entry.type == 1, entry.allow, entry.deny) for entry in raw]
    out = []
    for target, overwrite in (getattr(channel, "overwrites", None) or {}).items():
        allow, deny = overwrite.pair()
        out.append((target.id, not hasattr(target, "permissions"), allow.value, deny.value))
    return out


def open_to_all(guild: Any, channel: Any) -> bool:
    """Whether every member can see the channel: @everyone can see channels, and nothing on this one takes that from anyone."""
    everyone = guild.default_role
    can_base = everyone.permissions.view_channel or everyone.permissions.administrator
    allowed_here = False
    for target, _member, allow, deny in _entries(channel):
        if deny & VIEW_BIT:
            return False
        if target == everyone.id and allow & VIEW_BIT:
            allowed_here = True
    return bool(can_base or allowed_here)


THREAD_TYPES = (discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread)


def _is_thread(channel: Any) -> bool:
    return isinstance(channel, discord.Thread) or getattr(channel, "type", None) in THREAD_TYPES


def _place(channel: Any) -> Any:
    """The channel whose permissions a thread follows."""
    if _is_thread(channel):
        return getattr(channel, "parent", None) or channel
    return channel


def _bits(permissions: Any) -> int:
    """A role's permissions as Discord's number."""
    value = getattr(permissions, "value", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    out = 0  # something that reads like Permissions by name, flag by flag
    for name, flag in discord.Permissions.VALID_FLAGS.items():
        if getattr(permissions, name, False) is True:
            out |= flag
    return out


def _for_people(role: Any) -> bool:
    """Whether people can hold a role. A bot's own role says nothing about who reads a channel. Boosters' and an integration's roles go to
    people, so they count."""
    if role.is_default():
        return False
    check = getattr(role, "is_bot_managed", None)
    return not (check() if check is not None else False)


@dataclass(frozen=True)
class _Gate:
    """What a channel's overwrites say about View Channel: @everyone's (True allows, False denies, None says nothing), the roles that allow
    it, the roles that deny it, and the members who have their own."""
    everyone: bool | None
    allow: frozenset
    deny: frozenset
    members_allow: frozenset
    members_deny: frozenset


def _gate(guild: Any, channel: Any) -> _Gate:
    everyone_id = guild.default_role.id
    everyone: bool | None = None
    allow: set[int] = set()
    deny: set[int] = set()
    members_allow: set[int] = set()
    members_deny: set[int] = set()
    for target, is_member, allowed, denied in _entries(channel):
        # Within one overwrite Discord takes away what it denies, then grants what it allows, so an allow wins.
        state = True if allowed & VIEW_BIT else False if denied & VIEW_BIT else None
        if state is None:
            continue
        if is_member:
            (members_allow if state else members_deny).add(target)
        elif target == everyone_id:
            everyone = state
        else:
            (allow if state else deny).add(target)
    return _Gate(everyone, frozenset(allow), frozenset(deny), frozenset(members_allow), frozenset(members_deny))


def _some_mix_loses_sight(here: _Gate, there: _Gate, free: set[int], viewers: set[int], base_view: bool) -> bool:
    """Whether some mix of the `free` roles sees `here` and not `there`. The free roles are the ones people can hold, minus Administrator
    and the roles that let anyone into `there` outright. `viewers` hold View Channel among their own permissions, and `base_view` says
    whether @everyone does.

    Discord works out a channel in this order: the server permissions of @everyone and the member's roles, then @everyone's overwrite, then
    every overwrite that denies on the member's roles, then every one that allows, so a single allowing role wins over any denial. Without
    an allowing role, a member sees a channel when @everyone's overwrite allows it, or when it says nothing and they may see channels at all,
    and none of their roles denies it."""
    opens_here = free & here.allow
    plain = (free - here.deny) & viewers  # roles that give View Channel and don't close `here`
    # A role that closes `there`, held with a way into `here`
    for role in free & there.deny:
        if opens_here:
            return True
        if role in here.deny:
            continue
        if here.everyone is True or (here.everyone is None and (base_view or role in viewers or bool(plain))):
            return True
    # @everyone's overwrite closes `there`: anyone who reads `here` without an allowing role there
    if there.everyone is False:
        if opens_here or here.everyone is True or (here.everyone is None and (base_view or bool(plain))):
            return True
    # Nothing on `there` either way, and @everyone may not see channels: someone with no role that may
    if there.everyone is None and not base_view:
        if (opens_here - viewers) or here.everyone is True:
            return True
    return False


def all_see(guild: Any, channel: Any, other: Any) -> bool:
    """Whether everyone who can see `channel` can also see `other`, so a reply in `channel` may name `other`.

    It reads the server's roles and the two channels' overwrites, and never the member list. Every mix of roles a person could hold counts,
    whether or not anyone holds it now, so the answer errs toward hiding a name. Someone with an overwrite of their own on either channel is
    checked as they are. Bots and the owner don't count: the owner sees everything, and a bot isn't a reader. A thread reads as its channel,
    whose readers include everyone in a private thread."""
    place, target = _place(channel), _place(other)
    if place is None or target is None or _is_thread(place) or _is_thread(target):
        return False  # a thread whose channel isn't known has readers nobody can tell
    if getattr(place, "id", None) == getattr(target, "id", None):
        return True
    everyone = guild.default_role
    base = _bits(everyone.permissions)
    if base & ADMIN_BIT:
        return True
    people = [role for role in guild.roles if _for_people(role)]
    admins = {role.id for role in people if _bits(role.permissions) & ADMIN_BIT}
    viewers = {role.id for role in people if _bits(role.permissions) & VIEW_BIT}
    here, there = _gate(guild, place), _gate(guild, target)
    free = {role.id for role in people} - admins - there.allow
    if _some_mix_loses_sight(here, there, free, viewers, bool(base & VIEW_BIT)):
        return False
    for user_id in here.members_allow | there.members_deny:
        member = guild.get_member(user_id)
        if member is None:
            if getattr(guild, "chunked", True):
                continue  # not in the server anymore
            return False
        if getattr(member, "bot", False) or member.id == guild.owner_id:
            continue
        if access.can_view(member, place) and not access.can_view(member, target):
            return False
    return True


@dataclass
class Visibility:
    """What one reply may name. `public` is a reply everyone in `channel` sees, and otherwise only `viewer` sees it."""
    guild: Any
    viewer: Any
    channel: Any
    public: bool
    _known: dict = field(default_factory=dict)

    def shows(self, channel: Any) -> bool:
        if channel is None:
            return False
        if channel.id in self._known:
            return self._known[channel.id]
        if self.public:
            answer = all_see(self.guild, self.channel, channel) if self.channel is not None else open_to_all(self.guild, channel)
        else:
            answer = self.viewer is not None and access.can_view(self.viewer, channel)
        self._known[channel.id] = answer
        return answer

    def shows_deleted(self) -> bool:
        """A channel that's gone can't be checked, so its name shows only to staff, in a reply only they see."""
        if self.public or self.viewer is None:
            return False
        permissions = getattr(self.viewer, "guild_permissions", None)
        return bool(permissions and (permissions.administrator or permissions.manage_guild)) or access.is_admin(self.viewer)

    def shows_invite_codes(self) -> bool:
        """Invite codes are what Discord shows to people who can manage the server, in a reply only they see."""
        return self.shows_deleted()


# ---- the setting a card is read in ----

@dataclass
class Scope:
    db: Any
    guild: Any
    cfg: dict
    lang: str
    now: float
    lookback: dict
    filters: query.Filters
    visibility: Visibility
    private: frozenset = frozenset()
    levels: Any = None  # the Levels feature, when it's on here

    def __post_init__(self) -> None:
        self.tz = tf.zone(self.cfg.get("timezone"))
        self.interval = self.cfg.get("interval", "day")
        self.unit = self.cfg.get("time_unit", "hours")
        # Charts read the period from the start of its first bucket, so their first point is whole. Totals over a number of days read
        # exactly that many days back, as the 1, 7 and 30 day rows do, so a 30-day period and the 30-day row agree.
        self.period = tf.period_for(self.lookback, self.now, self.interval, self.tz)
        self.days = int(self.lookback.get("days") or tf.DEFAULT_LOOKBACK["days"]) if self.lookback.get("mode") == "days" else None
        self.start = self.window(self.days) if self.days else self.period.first_slot()
        self.end = self.period.end_slot()

    @property
    def guild_id(self) -> int:
        return self.guild.id

    def window(self, days: int) -> int:
        return tf.slot_of(tf.rolling(self.now, days).start)

    def footer(self) -> dict:
        short = words.days_label(self.lang, self.days) if self.days else words.period_label(self.lang, self.lookback)
        return {"period": words.period_label(self.lang, self.lookback), "short": short, "zone": tf.zone_label(self.tz, self.now)}

    def windows_with_period(self, days: Sequence[int]) -> list[tuple[int | None, int | None]]:
        """(days, first slot) for each fixed window, then the period, unless the period is one of those windows already."""
        out: list[tuple[int | None, int | None]] = [(count, self.window(count)) for count in days]
        if self.days not in days:
            out.append((None, self.start))
        return out


def mini_interval(start: float, end: float) -> str:
    """The interval that gives a card's small chart at most about MINI_POINTS points."""
    for name in tf.INTERVALS:
        if (end - start) / tf.INTERVAL_SECONDS[name] <= MINI_POINTS:
            return name
    return "month"


async def chart_edges(scope: Scope, first: int | None, *, mini: bool = False) -> tuple[list[int], list[float], str]:
    """The slots a chart's buckets start at, their times, and the interval. All time starts at the first thing recorded."""
    if scope.period.start is not None:
        start = scope.period.start
    elif first is not None:
        start = tf.slot_time(first)
    else:
        start = scope.now - 86400
    start = min(max(start, EARLIEST), scope.now - 3600)
    interval = mini_interval(start, scope.now) if mini else tf.fitting_interval(start, scope.now, scope.interval)
    times = tf.bucket_edges(start, scope.now, interval, scope.tz)
    return query.slot_edges(times), times, interval


# ---- names ----

@lru_cache(maxsize=1)
def _emoji_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for group in json.loads(emoji_text.LIST_FILE.read_text(encoding="utf-8")):
        for entry in group["emojis"]:
            names.setdefault(entry[0].replace(emoji_text.SELECTOR, ""), entry[1])
    return names


def public(scope: Scope, totals: dict[int, int]) -> dict[int, int]:
    """Totals by person, with anyone who chose privacy counted as no one, user 0, as they will be once their counts have moved. A card
    never ranks user 0, so it never ranks them, and their counts still add up to the period's total."""
    hidden = scope.private & totals.keys() if scope.private else ()
    if not hidden:
        return totals
    out = dict(totals)
    for user_id in hidden:
        out[0] = out.get(0, 0) + out.pop(user_id)
    return out


async def people(scope: Scope, ids: Iterable[int]) -> dict[int, dict]:
    """Each person as a card names them: their name in the server now, else the last one recorded, with their username and global name for
    a card whose font can't draw the first. Someone who chose privacy has no name here."""
    ids = [user_id for user_id in dict.fromkeys(ids) if user_id]
    out: dict[int, dict] = {}
    missing = []
    for user_id in ids:
        if user_id in scope.private:
            out[user_id] = {"id": user_id, "name": None, "username": None, "global_name": None, "state": "private"}
            continue
        member = scope.guild.get_member(user_id)
        if member is not None:
            out[user_id] = {"id": user_id, "name": member.display_name, "username": getattr(member, "name", None),
                            "global_name": getattr(member, "global_name", None), "state": "member"}
        else:
            missing.append(user_id)
    if missing:
        rows = await query.people(scope.db, scope.guild_id, missing)
        for user_id in missing:
            row = rows.get(user_id)
            if row is not None:
                out[user_id] = {"id": user_id, "name": row.get("nick") or row.get("display_name") or row["name"], "username": row["name"],
                                "global_name": row.get("display_name"), "state": "former"}
            else:
                out[user_id] = {"id": user_id, "name": None, "username": None, "global_name": None, "state": "unknown"}
    return out


async def channels(scope: Scope, ids: Iterable[int]) -> dict[int, dict]:
    """Each channel as this reply may name it: shown, hidden, deleted or merged."""
    ids = list(dict.fromkeys(ids))
    stored = await query.channels(scope.db, scope.guild_id, ids) if ids else {}
    out: dict[int, dict] = {}
    for channel_id in ids:
        channel = scope.guild.get_channel(channel_id)
        row = stored.get(channel_id)
        if channel is not None:
            kind = CHANNEL_KINDS.get(getattr(channel.type, "value", 0), "text")
            out[channel_id] = {"id": channel_id, "name": channel.name if scope.visibility.shows(channel) else None, "kind": kind,
                               "state": "shown" if scope.visibility.shows(channel) else "hidden"}
        elif row is not None and row["type"] == 999:
            out[channel_id] = {"id": channel_id, "name": row["name"], "kind": "merged", "state": "shown"}
        else:
            kind = CHANNEL_KINDS.get(row["type"], "text") if row else "text"
            name = row["name"] if row and scope.visibility.shows_deleted() else None
            out[channel_id] = {"id": channel_id, "name": name, "kind": kind, "state": "deleted"}
    return out


async def emoji_entries(scope: Scope, keys: Sequence[str]) -> dict[str, dict]:
    info = await query.emoji_info(scope.db, scope.guild_id)
    names = _emoji_names()
    out = {}
    for key in keys:
        row = info.get(key)
        if key.startswith("u:"):
            text = key[2:]
            out[key] = {"key": key, "kind": "unicode", "text": emoji_text.display(text), "name": names.get(text, text), "id": None}
        else:
            number = int(key[2:]) if key[2:].isdigit() else 0
            out[key] = {"key": key, "kind": "sticker" if key.startswith("s:") else "custom", "text": None,
                        "name": row["name"] if row else str(number), "id": number, "animated": bool(row and row["animated"]),
                        "deleted": bool(row and row["deleted_at"]), "available": bool(row["available"]) if row else True}
    return out


def emoji_kept(cfg: dict, key: str, row: dict | None, *, stickers: bool) -> bool:
    """Whether the server's emoji type and state filters keep this emoji in a view."""
    if key.startswith("s:") and not stickers:
        return False
    states = set(cfg.get("emoji_states") or [])
    if row is not None:
        if row.get("deleted_at") and "deleted" not in states:
            return False
        if not row.get("available", 1) and "unavailable" not in states:
            return False
    if "used" not in states:
        return False
    picked = set(cfg.get("emoji_types") or [])
    if not picked:
        return True
    traits = {"server"} if not key.startswith("u:") else set()
    if row is not None:
        traits |= {name for name in ("animated", "managed", "restricted") if row.get(name)}
    hit = picked <= traits if cfg.get("emoji_type_match") == "all" else bool(picked & traits)
    return hit if cfg.get("emoji_type_mode") == "allow" else not hit


def _named_list(totals: list[tuple[Any, int]], names: dict) -> list[dict]:
    return [{**names.get(key, {"id": key, "name": None, "state": "unknown"}), "value": value} for key, value in totals]


# ---- the cards ----

async def server(scope: Scope) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    rows = []
    period_messages: dict[int, int] = {}
    period_voice: dict[int, int] = {}
    for days, start in scope.windows_with_period((1, 7)):
        messages = public(scope, await reads.by_user(db, "messages", gid, f, start, end))
        voice = public(scope, await reads.by_user(db, "voice", gid, f, start, end))
        rows.append({"days": days, "messages": sum(messages.values()), "voice": sum(voice.values()), "people": len(reads.merge_people(messages, voice))})
        if start == scope.start:
            period_messages, period_voice = messages, voice
    top_people = [reads.ranked(period_messages)[:1], reads.ranked(period_voice)[:1]]
    message_channels = reads.ranked(await reads.by_channel(db, "messages", gid, f, scope.start, end), skip_zero_key=False)[:1]
    voice_channels = reads.ranked(await reads.by_channel(db, "voice", gid, f, scope.start, end), skip_zero_key=False)[:1]
    named = await people(scope, [key for group in top_people for key, _ in group])
    places = await channels(scope, [key for key, _ in message_channels + voice_channels])
    began = await reads.first_slot(db, gid)
    edges, times, interval = await chart_edges(scope, began, mini=True)
    return {
        "view": "server", "title": scope.guild.name, "created": scope.guild.created_at.timestamp(), "began": tf.slot_time(began) if began is not None else None,
        "rows": rows, "top_member_messages": (_named_list(top_people[0], named) or [None])[0], "top_member_voice": (_named_list(top_people[1], named) or [None])[0],
        "top_channel_messages": (_named_list(message_channels, places) or [None])[0], "top_channel_voice": (_named_list(voice_channels, places) or [None])[0],
        "chart": {"times": times, "interval": interval, "messages": [total for total, _ in await reads.buckets(db, "messages", gid, f, edges, end)],
                  "voice": [total for total, _ in await reads.buckets(db, "voice", gid, f, edges, end)]},
    }


async def member(scope: Scope, target: Any) -> dict:
    """`target` is a member, or a user who left, whose statistics are still on record."""
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    user_id = target.id
    mine = f.only(user_id)
    all_messages = public(scope, await reads.by_user(db, "messages", gid, f, scope.start, end))
    all_voice = public(scope, await reads.by_user(db, "voice", gid, f, scope.start, end))
    message_rank, message_ranked = reads.rank_in(all_messages, user_id)
    voice_rank, voice_ranked = reads.rank_in(all_voice, user_id)
    fixed = [(days, start) for days, start in scope.windows_with_period((1, 7)) if days]
    message_windows = await reads.windows(db, "messages", gid, mine, [start for _, start in fixed], end, user_id=user_id, people=False)
    voice_windows = await reads.windows(db, "voice", gid, mine, [start for _, start in fixed], end, user_id=user_id, people=False)
    rows = [{"days": days, "messages": message_windows[index][0], "voice": voice_windows[index][0]} for index, (days, _) in enumerate(fixed)]
    if scope.days not in (1, 7):
        rows.append({"days": None, "messages": all_messages.get(user_id, 0), "voice": all_voice.get(user_id, 0)})
    text_channels = reads.ranked(await reads.by_channel(db, "messages", gid, mine, scope.start, end, user_id=user_id), skip_zero_key=False)[:3]
    voice_channels = reads.ranked(await reads.by_channel(db, "voice", gid, mine, scope.start, end, user_id=user_id), skip_zero_key=False)[:1]
    places = await channels(scope, [key for key, _ in text_channels + voice_channels])
    apps = await reads.by_activity(db, gid, mine, scope.start, end, user_id=user_id)
    app = None
    if apps:
        best = min(apps.items(), key=lambda item: (-item[1][0], item[0]))
        name_rows = await query.activity_names(db, gid, [best[0]])
        found = name_rows.get(best[0])
        app = {"name": found["name"] if found else None, "kind": found["kind"] if found else 0, "seconds": best[1][0]}
    used = await reads.by_emoji(db, gid, mine, scope.start, end, user_id=user_id)
    info = await query.emoji_info(db, gid)
    kept = [(key, value) for key, value in reads.ranked(used, skip_zero_key=False) if emoji_kept(scope.cfg, key, info.get(key), stickers=False)]
    emoji = None
    if kept:
        entry = (await emoji_entries(scope, [kept[0][0]]))[kept[0][0]]
        emoji = {**entry, "value": kept[0][1]}
    edges, times, interval = await chart_edges(scope, await reads.first_kind_slot(db, "messages", gid, user_id=user_id), mini=True)
    stored = (await query.people(db, gid, [user_id])).get(user_id) or {}
    joined_at = getattr(target, "joined_at", None)
    created_at = getattr(target, "created_at", None)
    level = await _level(scope, user_id)
    return {
        "view": "member", "id": user_id, "title": getattr(target, "display_name", None) or stored.get("nick") or stored.get("display_name") or stored.get("name") or "",
        "username": getattr(target, "name", None) or stored.get("name"), "global_name": getattr(target, "global_name", None) or stored.get("display_name"),
        "server": scope.guild.name, "bot": bool(getattr(target, "bot", False)),
        "created": created_at.timestamp() if created_at else stored.get("created_at"), "joined": joined_at.timestamp() if joined_at else stored.get("joined_at"),
        "in_server": scope.guild.get_member(user_id) is not None,
        "ranks": {"messages": (message_rank, message_ranked), "voice": (voice_rank, voice_ranked)}, "rows": rows,
        "text_channels": _named_list(text_channels, places), "voice_channel": (_named_list(voice_channels, places) or [None])[0], "app": app, "emoji": emoji,
        "level": level,
        "chart": {"times": times, "interval": interval,
                  "messages": [total for total, _ in await reads.buckets(db, "messages", gid, mine, edges, end, user_id=user_id)],
                  "voice": [total for total, _ in await reads.buckets(db, "voice", gid, mine, edges, end, user_id=user_id)]},
    }


async def _level(scope: Scope, user_id: int) -> dict | None:
    """The member's level, XP and level rank, when the Levels feature is on here."""
    levels = scope.levels
    if levels is None:
        return None
    try:
        from ...modules.levels import level_for_xp, xp_for_level, xp_to_next
        await levels.flush()
        entry = await levels._entry(scope.guild_id, user_id)
        level = level_for_xp(entry.xp)
        rank = await levels.rank_of(scope.guild_id, entry.xp) if entry.xp else None
        return {"level": level, "xp": entry.xp, "rank": rank, "into": entry.xp - xp_for_level(level), "size": xp_to_next(level)}
    except Exception:  # the card still draws without the level
        return None


async def channel_ids(scope: Scope, channel_id: int, kind: str) -> set[int]:
    """The channels a card about this one adds up: itself, or for a category every channel ever filed under it and the merged channels
    that belong to it, less the ones this reply may not name. A channel left out of the names is left out of the sums, so a category
    card can't show what a channel nobody here can see holds."""
    if kind != "category":
        return {channel_id}
    rows = await scope.db.all("SELECT channel_id, type FROM stat_channels WHERE guild_id = ? AND parent_id = ?", (scope.guild_id, channel_id))
    stored = {row["channel_id"]: row["type"] for row in rows}
    found = set(stored) | {item.id for item in getattr(scope.guild, "channels", []) if getattr(item, "category_id", None) == channel_id}
    kept = set()
    for child_id in found:
        child = scope.guild.get_channel(child_id)
        if child is not None:
            if scope.visibility.shows(child):
                kept.add(child_id)
        elif stored.get(child_id) == 999 or scope.visibility.shows_deleted():  # a merged channel is named to all, a deleted one to staff alone
            kept.add(child_id)
    return kept


async def channel(scope: Scope, target: Any) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    kind = CHANNEL_KINDS.get(getattr(target.type, "value", 0), "text")
    ids = await channel_ids(scope, target.id, kind)
    has_voice = kind in VOICE_LIKE or kind == "category"
    short = [scope.window(1), scope.window(7), scope.window(30)]
    blocks = {}
    top_members: list[tuple[int, int]] = []
    for name in ("messages", "voice") if has_voice else ("messages",):
        windows = await reads.windows(db, name, gid, f, short, end, channels=ids)
        period = public(scope, await reads.by_user(db, name, gid, f, scope.start, end, channels=ids))
        blocks[name] = [{"days": days, "total": total, "people": count} for days, (total, count) in zip((1, 7, 30), windows)]
        if scope.days not in (1, 7, 30):
            blocks[name].append({"days": None, "total": sum(period.values()), "people": len(reads.merge_people(period))})
        if name == "messages":
            top_members = reads.ranked(period)[:3]
    named = await people(scope, [key for key, _ in top_members])
    used = await reads.by_emoji(db, gid, f, scope.start, end, channels=ids)
    info = await query.emoji_info(db, gid)
    kept = [(key, value) for key, value in reads.ranked(used, skip_zero_key=False) if emoji_kept(scope.cfg, key, info.get(key), stickers=False)][:3]
    entries = await emoji_entries(scope, [key for key, _ in kept])
    edges, times, interval = await chart_edges(scope, await reads.first_kind_slot(db, "messages", gid, channels=ids), mini=True)
    chart = {"times": times, "interval": interval, "messages": [total for total, _ in await reads.buckets(db, "messages", gid, f, edges, end, channels=ids)]}
    if has_voice:
        chart["voice"] = [total for total, _ in await reads.buckets(db, "voice", gid, f, edges, end, channels=ids)]
    created = getattr(target, "created_at", None)
    return {"view": "channel", "title": target.name, "kind": kind, "server": scope.guild.name, "created": created.timestamp() if created else None,
            "blocks": blocks, "top_members": _named_list(top_members, named), "top_emoji": [{**entries[key], "value": value} for key, value in kept], "chart": chart}


async def totals(scope: Scope, kind: str) -> dict:
    """The messages card or the voice card: the period's total, the kind's last 1, 7 and 30 days with the other kind's beside them (voice on
    the messages card, messages on the voice card), and the top three members and channels."""
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    other = "voice" if kind == "messages" else "messages"
    starts = [scope.window(1), scope.window(7), scope.window(30)]
    windows = await reads.windows(db, kind, gid, f, starts, end, people=False)
    other_windows = await reads.windows(db, other, gid, f, starts, end, people=False)
    period = public(scope, await reads.by_user(db, kind, gid, f, scope.start, end))
    rows = [{"days": days, "total": total} for days, (total, _) in zip((1, 7, 30), windows)]
    rows.append({"days": None, "total": sum(period.values())})  # the headline, not a row
    top_people = reads.ranked(period)[:3]
    top_places = reads.ranked(await reads.by_channel(db, kind, gid, f, scope.start, end), skip_zero_key=False)[:3]
    named = await people(scope, [key for key, _ in top_people])
    places = await channels(scope, [key for key, _ in top_places])
    return {"view": kind, "title": scope.guild.name, "kind": kind, "rows": rows, "other_kind": other,
            "other_rows": [{"days": days, "total": total} for days, (total, _) in zip((1, 7, 30), other_windows)],
            "top_members": _named_list(top_people, named), "top_channels": _named_list(top_places, places)}


async def activity(scope: Scope) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    found = await reads.by_activity(db, gid, f, scope.start, end)
    ordered = sorted(found.items(), key=lambda item: (-item[1][0], item[0]))
    names = await query.activity_names(db, gid, [key for key, _ in ordered[:TOP_SIZE]])
    top = []
    for key, (seconds, count) in ordered[:TOP_SIZE]:
        row = names.get(key)
        top.append({"key": key, "name": row["name"] if row else str(key), "kind": row["kind"] if row else 0, "value": seconds, "people": count,
                    "verified": bool(row and row["verified"])})
    return {"view": "activity", "title": scope.guild.name, "top": top, "total": sum(seconds for seconds, _ in found.values()), "tracking": bool(scope.cfg.get("track_activities"))}


async def emoji(scope: Scope) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    used = await reads.by_emoji(db, gid, f, scope.start, end)
    info = await query.emoji_info(db, gid)
    kept = [(key, value) for key, value in reads.ranked(used, skip_zero_key=False) if emoji_kept(scope.cfg, key, info.get(key), stickers=False)]
    entries = await emoji_entries(scope, [key for key, _ in kept[:TOP_SIZE]])
    return {"view": "emoji", "title": scope.guild.name, "top": [{**entries[key], "value": value} for key, value in kept[:TOP_SIZE]],
            "total": sum(value for _, value in kept)}


def _invite_counts(scope: Scope, rows: list[dict]) -> tuple[list[dict], int]:
    """The joins that count under the server's invite and fake-reason filters, and how many were left out as fake."""
    cfg = scope.cfg
    listed = set(cfg.get("invites") or [])
    counted, fake = [], 0
    for row in rows:
        code = row.get("code")
        if listed and (code in listed) != (cfg.get("invite_mode") == "allow"):
            continue
        member = scope.guild.get_member(row["user_id"]) if row["user_id"] else None
        bits = invite_rules.reasons(row, cfg, scope.now, member)
        if invite_rules.passes(bits, cfg):
            counted.append(row)
        else:
            fake += 1
    return counted, fake


def invite_label(scope: Scope, code: str | None, invite: dict | None, named: dict) -> dict:
    """An invite as this reply may name it: its label, or the code to someone who may see codes, or whose invite it was."""
    labels = scope.cfg.get("invite_labels") or {}
    if code is None:
        return {"code": None, "label": None, "state": "unknown"}
    if code.startswith(invite_rules.VANITY_PREFIX):
        return {"code": code, "label": code[len(invite_rules.VANITY_PREFIX):], "state": "vanity"}
    if code in labels:
        return {"code": code, "label": labels[code], "state": "label"}
    if scope.visibility.shows_invite_codes():
        return {"code": code, "label": code, "state": "code"}
    inviter = (invite or {}).get("inviter_id")
    person = named.get(inviter) if inviter else None
    if person is not None and person.get("state") == "private":
        person = None
    return {"code": code, "label": person["name"] if person and person.get("name") else None, "state": "inviter", "person": person}


async def invites(scope: Scope) -> dict:
    db, gid = scope.db, scope.guild_id
    start_time = tf.slot_time(scope.start) if scope.start is not None else None  # the same start as the other cards' totals
    rows = await reads.joins(db, gid, start_time, scope.now + 1)
    counted, fake = _invite_counts(scope, rows)
    by_code = reads.folded((row["code"], 1) for row in counted if row["code"])
    by_inviter = public(scope, reads.folded((row["inviter_id"], 1) for row in counted if row["inviter_id"]))
    top_codes = reads.ranked(by_code, skip_zero_key=False)[:5]
    top_inviters = reads.ranked(by_inviter)[:5]
    stored = await reads.invites(db, gid)
    named = await people(scope, [key for key, _ in top_inviters] + [stored[code]["inviter_id"] for code, _ in top_codes if code in stored and stored[code]["inviter_id"]])
    began = min((row["at"] for row in rows), default=None)
    edges, times, interval = await chart_edges(scope, tf.slot_of(began) if began else None, mini=True)
    series = [0] * len(edges)
    for row in counted:
        index = query.fold(edges, tf.slot_of(row["at"]))
        if index is not None:
            series[index] += 1
    return {"view": "invites", "title": scope.guild.name, "joins": len(counted), "fake": fake, "unattributed": sum(1 for row in counted if not row["code"]),
            "top_invites": [{**invite_label(scope, code, stored.get(code), named), "value": value} for code, value in top_codes],
            "top_inviters": _named_list(top_inviters, named), "chart": {"times": times, "interval": interval, "joins": series},
            "can_read": bool(getattr(getattr(scope.guild.me, "guild_permissions", None), "manage_guild", False))}


# ---- charts ----

async def chart_kind(scope: Scope, kind: str) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    edges, times, interval = await chart_edges(scope, await reads.first_kind_slot(db, kind, gid))
    found = await reads.buckets(db, kind, gid, f, edges, end, people=True)
    return {"view": f"chart_{kind}", "title": scope.guild.name, "kind": kind, "times": times, "interval": interval,
            "totals": [total for total, _ in found], "people": [count for _, count in found]}


async def chart_members(scope: Scope) -> dict:
    db, gid = scope.db, scope.guild_id
    first = await db.scalar("SELECT MIN(slot) FROM stat_members WHERE guild_id = ?", (gid,))
    edges, times, interval = await chart_edges(scope, first)
    found = await reads.member_buckets(db, gid, edges, scope.end)
    now = getattr(scope.guild, "member_count", None)
    return {"view": "chart_members", "title": scope.guild.name, "times": times, "interval": interval, "members": [row["members"] for row in found],
            "joins": [row["joins"] for row in found], "leaves": [row["leaves"] for row in found], "now": now,
            "total_joins": sum(row["joins"] for row in found), "total_leaves": sum(row["leaves"] for row in found)}


async def chart_status(scope: Scope) -> dict:
    db, gid = scope.db, scope.guild_id
    first = await db.scalar("SELECT MIN(slot) FROM stat_status WHERE guild_id = ?", (gid,))
    edges, times, interval = await chart_edges(scope, first)
    found = await reads.status_buckets(db, gid, scope.filters.include, edges, scope.end)
    breakdown = any(row["online"] is not None for row in found)
    return {"view": "chart_status", "title": scope.guild.name, "times": times, "interval": interval, "breakdown": breakdown,
            "online": [row["online"] for row in found], "idle": [row["idle"] for row in found], "dnd": [row["dnd"] for row in found],
            "present": [row["present"] for row in found]}


async def chart_activity(scope: Scope) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    found = await reads.by_activity(db, gid, f, scope.start, end)
    top = [key for key, _ in sorted(found.items(), key=lambda item: (-item[1][0], item[0]))[:CHART_LINES]]
    names = await query.activity_names(db, gid, top)
    edges, times, interval = await chart_edges(scope, await reads.first_kind_slot(db, "activities", gid))
    series = await reads.bucket_series(db, "activities", gid, f, edges, end, "activity_id", top)
    return {"view": "chart_activity", "title": scope.guild.name, "times": times, "interval": interval,
            "lines": [{"key": key, "name": names[key]["name"] if key in names else str(key), "values": series[key], "total": found[key][0]} for key in top],
            "tracking": bool(scope.cfg.get("track_activities"))}


async def chart_emoji(scope: Scope) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    used = await reads.by_emoji(db, gid, f, scope.start, end)
    info = await query.emoji_info(db, gid)
    top = [key for key, _ in reads.ranked(used, skip_zero_key=False) if emoji_kept(scope.cfg, key, info.get(key), stickers=False)][:CHART_LINES]
    entries = await emoji_entries(scope, top)
    edges, times, interval = await chart_edges(scope, await reads.first_kind_slot(db, "emoji", gid))
    series = await reads.bucket_series(db, "emoji", gid, f, edges, end, "emoji", top)
    return {"view": "chart_emoji", "title": scope.guild.name, "times": times, "interval": interval,
            "lines": [{**entries[key], "values": series[key], "total": used[key]} for key in top]}


async def chart_invites(scope: Scope) -> dict:
    db, gid = scope.db, scope.guild_id
    rows = await reads.joins(db, gid, scope.period.start, scope.now + 1)
    counted, fake = _invite_counts(scope, rows)
    first = min((row["at"] for row in rows), default=None)
    edges, times, interval = await chart_edges(scope, tf.slot_of(first) if first else None)
    by_code = reads.folded((row["code"], 1) for row in counted if row["code"])
    top = [code for code, _ in reads.ranked(by_code, skip_zero_key=False)[:4]]
    stored = await reads.invites(db, gid)
    named = await people(scope, [stored[code]["inviter_id"] for code in top if code in stored and stored[code]["inviter_id"]])
    stacks = {code: [0] * len(edges) for code in top}
    other = [0] * len(edges)
    for row in counted:
        index = query.fold(edges, tf.slot_of(row["at"]))
        if index is None:
            continue
        if row["code"] in stacks:
            stacks[row["code"]][index] += 1
        else:
            other[index] += 1
    return {"view": "chart_invites", "title": scope.guild.name, "times": times, "interval": interval, "joins": len(counted), "fake": fake,
            "stacks": [{**invite_label(scope, code, stored.get(code), named), "values": stacks[code]} for code in top], "other": other}


async def chart_member(scope: Scope, target: Any) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    user_id = target.id
    mine = f.only(user_id)
    edges, times, interval = await chart_edges(scope, await reads.first_kind_slot(db, "messages", gid, user_id=user_id))
    messages = [total for total, _ in await reads.buckets(db, "messages", gid, mine, edges, end, user_id=user_id)]
    voice = [total for total, _ in await reads.buckets(db, "voice", gid, mine, edges, end, user_id=user_id)]
    stored = (await query.people(db, gid, [user_id])).get(user_id) or {}
    return {"view": "chart_member", "id": user_id, "title": getattr(target, "display_name", None) or stored.get("nick") or stored.get("display_name") or stored.get("name") or "",
            "username": getattr(target, "name", None) or stored.get("name"), "global_name": getattr(target, "global_name", None) or stored.get("display_name"),
            "server": scope.guild.name, "times": times, "interval": interval, "messages": messages, "voice": voice}


async def chart_channel(scope: Scope, target: Any) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    kind = CHANNEL_KINDS.get(getattr(target.type, "value", 0), "text")
    ids = await channel_ids(scope, target.id, kind)
    edges, times, interval = await chart_edges(scope, await reads.first_kind_slot(db, "messages", gid, channels=ids))
    found = await reads.buckets(db, "messages", gid, f, edges, end, channels=ids, people=True)
    out = {"view": "chart_channel", "title": target.name, "kind": kind, "server": scope.guild.name, "times": times, "interval": interval,
           "messages": [total for total, _ in found], "people": [count for _, count in found]}
    if kind in VOICE_LIKE or kind == "category":
        out["voice"] = [total for total, _ in await reads.buckets(db, "voice", gid, f, edges, end, channels=ids)]
    return out


# ---- /top ----

async def top(scope: Scope, page_kind: str, page: int) -> dict:
    db, gid, f, end = scope.db, scope.guild_id, scope.filters, scope.end
    if page_kind == "overview":
        message_people = reads.ranked(public(scope, await reads.by_user(db, "messages", gid, f, scope.start, end)))[:3]
        voice_people = reads.ranked(public(scope, await reads.by_user(db, "voice", gid, f, scope.start, end)))[:3]
        message_places = reads.ranked(await reads.by_channel(db, "messages", gid, f, scope.start, end), skip_zero_key=False)[:3]
        voice_places = reads.ranked(await reads.by_channel(db, "voice", gid, f, scope.start, end), skip_zero_key=False)[:3]
        used = await reads.by_emoji(db, gid, f, scope.start, end)
        info = await query.emoji_info(db, gid)
        top_emoji = [(key, value) for key, value in reads.ranked(used, skip_zero_key=False) if emoji_kept(scope.cfg, key, info.get(key), stickers=False)][:3]
        named = await people(scope, [key for key, _ in message_people + voice_people])
        places = await channels(scope, [key for key, _ in message_places + voice_places])
        entries = await emoji_entries(scope, [key for key, _ in top_emoji])
        return {"view": "top", "page_kind": "overview", "page": 0, "pages": 1, "title": scope.guild.name,
                "sections": {"members_messages": _named_list(message_people, named), "members_voice": _named_list(voice_people, named),
                             "channels_messages": _named_list(message_places, places), "channels_voice": _named_list(voice_places, places),
                             "emoji": [{**entries[key], "value": value} for key, value in top_emoji]}}
    if page_kind in ("members_messages", "members_voice"):
        ordered = reads.ranked(public(scope, await reads.by_user(db, page_kind.split("_")[1], gid, f, scope.start, end)))
    elif page_kind in ("channels_messages", "channels_voice"):
        ordered = reads.ranked(await reads.by_channel(db, page_kind.split("_")[1], gid, f, scope.start, end), skip_zero_key=False)
    else:
        used = await reads.by_emoji(db, gid, f, scope.start, end)
        info = await query.emoji_info(db, gid)
        ordered = [(key, value) for key, value in reads.ranked(used, skip_zero_key=False) if emoji_kept(scope.cfg, key, info.get(key), stickers=False)]
    pages = max(1, math.ceil(len(ordered) / TOP_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = ordered[page * TOP_SIZE:(page + 1) * TOP_SIZE]
    if page_kind.startswith("members"):
        rows = _named_list(chunk, await people(scope, [key for key, _ in chunk]))
    elif page_kind.startswith("channels"):
        rows = _named_list(chunk, await channels(scope, [key for key, _ in chunk]))
    else:
        entries = await emoji_entries(scope, [key for key, _ in chunk])
        rows = [{**entries[key], "value": value} for key, value in chunk]
    for index, row in enumerate(rows):
        row["rank"] = page * TOP_SIZE + index + 1
    return {"view": "top", "page_kind": page_kind, "page": page, "pages": pages, "count": len(ordered), "title": scope.guild.name, "rows": rows}


async def gather(scope: Scope, view: str, target: Any = None, *, page_kind: str = "overview", page: int = 0) -> dict:
    """The numbers for one card."""
    if view == "server":
        data = await server(scope)
    elif view == "member":
        data = await member(scope, target)
    elif view == "channel":
        data = await channel(scope, target)
    elif view in ("messages", "voice"):
        data = await totals(scope, view)
    elif view == "activity":
        data = await activity(scope)
    elif view == "emoji":
        data = await emoji(scope)
    elif view == "invites":
        data = await invites(scope)
    elif view in ("chart_messages", "chart_voice"):
        data = await chart_kind(scope, view.split("_")[1])
    elif view == "chart_members":
        data = await chart_members(scope)
    elif view == "chart_status":
        data = await chart_status(scope)
    elif view == "chart_activity":
        data = await chart_activity(scope)
    elif view == "chart_emoji":
        data = await chart_emoji(scope)
    elif view == "chart_invites":
        data = await chart_invites(scope)
    elif view == "chart_member":
        data = await chart_member(scope, target)
    elif view == "chart_channel":
        data = await chart_channel(scope, target)
    elif view == "top":
        data = await top(scope, page_kind, page)
    else:
        raise ValueError(f"no such view: {view}")
    data["footer"] = scope.footer()
    data["unit"] = scope.unit
    data["tz"] = scope.tz
    return data
